"""Build an isolated Cline ``--data-dir`` for the Stage-1 executor.

Two things verified live (see docs/TYPESCRIPT_PORT_PLAN.md Fase 4b and the
commit history on this branch) that a blocking hook could not give us:

1. Cline's file-based hooks never dispatch in the standalone CLI ("hook
   dispatch failed: session.hook requires a valid hook event payload" on
   every run) -- so there is no way to *block* a tool call live.
2. ``global-settings.json`` supports ``"disabledTools": [...]`` and it DOES
   remove a tool from what the model is offered -- confirmed empirically: a
   9b given a task that would normally reach for ``run_commands``, with
   ``run_commands`` disabled, tried every other tool available (including
   several unrelated Orca MCP tools) across 17 iterations and never called
   it once. This is a hard removal, not a request the model can ignore.

So instead of a live guard, the executor runs under an isolated
``--data-dir`` whose ``global-settings.json`` disables ``run_commands``
outright, and whose MCP config carries **only** the ``locagent`` server --
the user's real ``~/.cline/data/settings/cline_mcp_settings.json`` also
registers a stack of unrelated Orca tools (``team_run_task``, ``skills``,
``computer-use``, ``spawn_agent``, ...) that otherwise bleed into every
executor run as pure noise/distraction (confirmed: they showed up in a
stress-test transcript). The real Cline data dir (interactive sessions) is
never touched.

Source of truth for what to copy: the real
``~/.cline/data/settings/{cline_mcp_settings.json,providers.json}`` -- never
duplicate secrets beyond what's needed (the executor only needs the
`lmstudio` provider, which carries no real API key).

    from eval.isolated_env import build_executor_data_dir
    data_dir = build_executor_data_dir()   # eval/.cline-data-executor/, cached
    # then: cline --data-dir <data_dir> -P lmstudio -m ...
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional

_HERE = Path(__file__).resolve().parent
DEFAULT_DEST = _HERE / '.cline-data-executor'
SOURCE_SETTINGS_DIR = Path(os.environ.get('CLINE_DATA_DIR', str(Path.home() / '.cline' / 'data'))) / 'settings'

DEFAULT_MCP_SERVERS = ('locagent',)
DEFAULT_PROVIDERS = ('lmstudio',)
DISABLED_TOOLS = ['run_commands']


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding='utf-8')


def _build_mcp_settings(dest: Path, servers: Iterable[str]) -> None:
    src = SOURCE_SETTINGS_DIR / 'cline_mcp_settings.json'
    all_servers = _read_json(src).get('mcpServers', {}) if src.exists() else {}
    picked = {name: all_servers[name] for name in servers if name in all_servers}
    missing = [name for name in servers if name not in all_servers]
    if missing:
        raise KeyError(f'MCP server(s) not found in {src}: {missing}')
    _write_json(dest / 'cline_mcp_settings.json', {'mcpServers': picked})


def _build_providers(dest: Path, providers: Iterable[str]) -> None:
    src = SOURCE_SETTINGS_DIR / 'providers.json'
    raw = _read_json(src) if src.exists() else {'version': 1, 'providers': {}}
    all_providers = raw.get('providers', {})
    picked = {name: all_providers[name] for name in providers if name in all_providers}
    missing = [name for name in providers if name not in all_providers]
    if missing:
        raise KeyError(f'provider(s) not found in {src}: {missing}')
    out = {
        'version': raw.get('version', 1),
        'lastUsedProvider': next(iter(providers), raw.get('lastUsedProvider')),
        'modes': {},
        'providers': picked,
    }
    _write_json(dest / 'providers.json', out)


def _build_global_settings(dest: Path, disabled_tools: Iterable[str]) -> None:
    _write_json(dest / 'global-settings.json', {'disabledTools': list(disabled_tools)})


def build_executor_data_dir(
    dest: Optional[Path] = None,
    *,
    mcp_servers: Iterable[str] = DEFAULT_MCP_SERVERS,
    providers: Iterable[str] = DEFAULT_PROVIDERS,
    disabled_tools: Iterable[str] = DISABLED_TOOLS,
    force: bool = False,
) -> Path:
    """Build (or reuse) the isolated data dir; returns its path for
    ``cline --data-dir <path>``. Rebuilt whenever *force* or the source
    settings are newer than the cached copy."""
    dest = Path(dest) if dest else DEFAULT_DEST
    settings_dir = dest / 'settings'
    marker = settings_dir / 'providers.json'

    if not force and marker.exists():
        src_mcp = SOURCE_SETTINGS_DIR / 'cline_mcp_settings.json'
        src_prov = SOURCE_SETTINGS_DIR / 'providers.json'
        stale = any(
            p.exists() and p.stat().st_mtime > marker.stat().st_mtime
            for p in (src_mcp, src_prov)
        )
        if not stale:
            return dest

    _build_mcp_settings(settings_dir, mcp_servers)
    _build_providers(settings_dir, providers)
    _build_global_settings(settings_dir, disabled_tools)
    return dest


if __name__ == '__main__':
    import argparse
    import sys

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dest', default=str(DEFAULT_DEST))
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--mcp-servers', default=','.join(DEFAULT_MCP_SERVERS))
    ap.add_argument('--providers', default=','.join(DEFAULT_PROVIDERS))
    args = ap.parse_args()

    path = build_executor_data_dir(
        Path(args.dest), force=args.force,
        mcp_servers=[s.strip() for s in args.mcp_servers.split(',') if s.strip()],
        providers=[p.strip() for p in args.providers.split(',') if p.strip()],
    )
    print(f'executor data-dir ready: {path}')
    for f in sorted((path / 'settings').glob('*.json')):
        print(f'  {f.name}: {f.read_text(encoding="utf-8")[:200]}')
    sys.exit(0)
