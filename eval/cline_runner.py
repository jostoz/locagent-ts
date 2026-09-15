"""Run one Cline CLI invocation and return a structured result.

Wraps the proven scratchpad pattern:

    timeout $T cline -P <prov> -m <model> -c <repo> --thinking none \\
        --auto-approve true --compaction <mode> --json [-s <sys>] "<prompt>" \\
        </dev/null 2>&1 | tee <run>.jsonl

plus flake taxonomy + reload-retry from ``eval_matrix4.sh``:

    conn      -- "ConnectionRefused" / "Cannot connect to API"  -> reload, retry
    timeout   -- the process hit the wall-clock limit            -> reload, retry
    mcp       -- locagent MCP tools never appeared and an MCP error was logged
    no-tools  -- 0 tool calls and a suspiciously short run
    ok        -- a usable transcript

The locagent MCP server config (incl. LOCAGENT_REPO / LOCAGENT_CACHE_DIR) lives
in ``~/.cline/data/settings/cline_mcp_settings.json`` -- nothing to pass here.
File-based Cline hooks do not dispatch in this CLI build, so there is no
``--hooks-dir``; violations are found post-hoc by ``eval.guard``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from eval import lmstudio
from eval.transcript import Transcript, parse

DEFAULT_TIMEOUT = 300
LMSTUDIO_PROVIDER = 'lmstudio'


def _resolve_cline() -> str:
    """``subprocess.run(['cline', ...])`` fails with WinError 2 on Windows --
    the npm-global install is a ``cline.cmd`` shim and CreateProcess (unlike a
    shell's PATH search) does not do PATHEXT resolution on a bare name.
    ``shutil.which`` does, and is a no-op cost the rest of the time."""
    return shutil.which('cline') or 'cline'


CLINE = _resolve_cline()


@dataclass
class RunResult:
    task_id: str
    condition: str
    step: Optional[int]
    cmd: List[str]
    run_path: str                 # raw --json stream on disk
    transcript: Transcript
    exit_code: int
    wall_s: float
    flake: str = 'ok'             # ok | conn | timeout | mcp | no-tools | container | process
    attempts: int = 1
    stderr_tail: str = ''

    @property
    def timed_out(self) -> bool:
        return self.flake == 'timeout'


def _classify_flake(tr: Transcript, exit_code: int, killed: bool,
                    expect_mcp: bool) -> str:
    if killed:
        return 'timeout'
    if exit_code == 125:
        return 'container'
    if exit_code != 0:
        return 'process'
    if tr.connection_flake:
        return 'conn'
    if expect_mcp and tr.n_graph == 0 and any(
            'mcp' in e.lower() or 'tool' in e.lower() for e in tr.errors):
        return 'mcp'
    if not tr.tool_calls and tr.iterations <= 1 and len(tr.final_text) < 40 \
            and tr.finish_reason not in ('completed',):
        return 'no-tools'
    return 'ok'


def build_cmd(
    *,
    provider: str,
    model: str,
    repo: str,
    prompt: str,
    system: Optional[str] = None,
    compaction: str = 'agentic',
    thinking: str = 'none',
    timeout_s: int = DEFAULT_TIMEOUT,
    data_dir: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    max_tokens: int = 8192,
    sandbox_image: Optional[str] = None,
    disable_locagent: bool = False,
    enable_tgrep: bool = False,
) -> List[str]:
    executor_repo = '/workspace' if sandbox_image else repo
    # CLINE resolves to a Windows npm shim on the host. The container has its
    # own Linux installation, so never forward that host-only executable path.
    cmd = ['cline' if sandbox_image else CLINE]
    if data_dir:
        cmd += ['--data-dir', data_dir]
    cmd += [
        '--json', prompt, '-P', provider, '-m', model, '-c', executor_repo,
        '--thinking', thinking, '--auto-approve', 'true',
        '--compaction', compaction,
    ]
    if api_key:
        cmd += ['-k', api_key]
    if timeout_s:
        cmd += ['-t', str(timeout_s)]
    if system:
        cmd += ['-s', system]
    if sandbox_image:
        # Docker is the filesystem boundary. Only the disposable worktree and
        # read-only LocAgent code are mounted; no host home, source checkout,
        # credentials, Docker socket, or evaluator output directory is visible.
        cmd = [
            'docker', 'run', '--rm', '--read-only',
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=1g',
            '--tmpfs', '/state:rw,noexec,nosuid,size=64m',
            '--mount', f'type=bind,source={Path(repo).resolve()},target=/workspace',
            '--mount', r'type=bind,source=C:\Users\joz\orca\projects\locagent-ts,target=/opt/locagent,readonly',
            '--add-host', 'host.docker.internal:host-gateway',
            '-e', f'CLINE_MODEL={model}',
            '-e', f'CLINE_PROVIDER={provider}',
            '-e', 'CLINE_API_KEY',
            '-e', 'CLINE_BASE_URL',
            '-e', 'CLINE_MAX_TOKENS',
            '-e', f'DISABLE_LOCAGENT_MCP={1 if disable_locagent else 0}',
            '-e', f'ENABLE_TGREP_MCP={1 if enable_tgrep else 0}',
            sandbox_image,
        # The image entrypoint already executes its own `cline` binary after
        # installing ephemeral config and MCP state.
        ] + cmd[1:]
    return cmd


def run_once(
    *,
    task_id: str,
    condition: str,
    prompt: str,
    repo: str,
    run_path: str,
    provider: str = LMSTUDIO_PROVIDER,
    model: str = lmstudio.MODEL_9B,
    system: Optional[str] = None,
    compaction: str = 'agentic',
    thinking: str = 'none',
    step: Optional[int] = None,
    timeout_s: int = DEFAULT_TIMEOUT,
    expect_mcp: bool = True,
    data_dir: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    max_tokens: int = 8192,
    sandbox_image: Optional[str] = None,
    disable_locagent: bool = False,
    enable_tgrep: bool = False,
) -> RunResult:
    cmd = build_cmd(provider=provider, model=model, repo=repo, prompt=prompt,
                    system=system, compaction=compaction, thinking=thinking,
                    timeout_s=timeout_s,
                    data_dir=data_dir, api_key=api_key, base_url=base_url,
                    max_tokens=max_tokens,
                    sandbox_image=sandbox_image,
                    disable_locagent=disable_locagent,
                    enable_tgrep=enable_tgrep)
    Path(run_path).parent.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env['CLINE_API_KEY'] = api_key or 'lm-studio-local'
    env['CLINE_BASE_URL'] = base_url or 'http://host.docker.internal:11434/v1'
    env['CLINE_MAX_TOKENS'] = str(max_tokens)
    env.setdefault('PATH', '')
    for extra in (r'C:\Users\joz\AppData\Roaming\npm', r'C:\Users\joz\.lmstudio\bin'):
        if extra.lower() not in env['PATH'].lower():
            env['PATH'] = env['PATH'] + os.pathsep + extra

    t0 = time.time()
    killed = False
    stderr_tail = ''
    # a hard ceiling above cline's own -t, so a wedged process still returns
    hard = (timeout_s or DEFAULT_TIMEOUT) + 90
    try:
        with open(run_path, 'w', encoding='utf-8') as out:
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, stdout=out,
                stderr=subprocess.PIPE, encoding='utf-8', errors='replace',
                timeout=hard, env=env)
        exit_code = proc.returncode
        stderr_tail = (proc.stderr or '')[-2000:]
    except subprocess.TimeoutExpired as e:
        killed = True
        exit_code = -9
        stderr_tail = (e.stderr or b'')[-2000:].decode('utf-8', 'replace') \
            if isinstance(e.stderr, (bytes, bytearray)) else (e.stderr or '')[-2000:]
    wall_s = round(time.time() - t0, 1)

    text = Path(run_path).read_text(encoding='utf-8', errors='replace')
    tr = parse(text)
    flake = _classify_flake(tr, exit_code, killed, expect_mcp)

    return RunResult(
        task_id=task_id, condition=condition, step=step, cmd=cmd,
        run_path=run_path, transcript=tr, exit_code=exit_code,
        wall_s=wall_s, flake=flake, stderr_tail=stderr_tail,
    )


def run_with_retry(
    *,
    max_attempts: int = 3,
    model: str = lmstudio.MODEL_9B,
    ctx: int = lmstudio.DEFAULT_CTX,
    reload_on_flake: bool = True,
    **kw,
) -> RunResult:
    """``run_once`` + reload-and-retry on conn/timeout/mcp flakes. ``no-tools`` and
    ``ok`` are returned as-is (retrying a model that just refused to act rarely
    helps)."""
    last: Optional[RunResult] = None
    for attempt in range(1, max_attempts + 1):
        if kw.get('provider', LMSTUDIO_PROVIDER) == LMSTUDIO_PROVIDER:
            lmstudio.ensure_loaded(model, ctx)
        res = run_once(model=model, **kw)
        res.attempts = attempt
        last = res
        if res.flake in ('ok', 'no-tools'):
            return res
        if not reload_on_flake or attempt == max_attempts:
            return res
        time.sleep(3)
        if kw.get('provider', LMSTUDIO_PROVIDER) == LMSTUDIO_PROVIDER:
            lmstudio.unload_all()
            time.sleep(2)
    assert last is not None
    return last


if __name__ == '__main__':
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo', required=True)
    ap.add_argument('--prompt', required=True)
    ap.add_argument('--model', default=lmstudio.MODEL_9B)
    ap.add_argument('--provider', default=LMSTUDIO_PROVIDER)
    ap.add_argument('--system')
    ap.add_argument('--compaction', default='agentic')
    ap.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument('--run-path', default='eval/results/_adhoc.jsonl')
    ap.add_argument('--data-dir', help='isolated Cline data directory')
    ap.add_argument('--api-key', help='per-run provider key (for local servers, any marker)')
    ap.add_argument('--retry', action='store_true')
    args = ap.parse_args()

    sys_prompt = None
    if args.system:
        sys_prompt = Path(args.system).read_text(encoding='utf-8') \
            if Path(args.system).exists() else args.system

    kw = dict(task_id='_adhoc', condition='_adhoc', prompt=args.prompt,
              repo=args.repo, run_path=args.run_path, provider=args.provider,
              system=sys_prompt, compaction=args.compaction, timeout_s=args.timeout)
    if args.data_dir:
        kw['data_dir'] = args.data_dir
    if args.api_key:
        kw['api_key'] = args.api_key
    r = run_with_retry(model=args.model, **kw) if args.retry else run_once(model=args.model, **kw)
    tr = r.transcript
    print(json.dumps({
        'exit': r.exit_code, 'wall_s': r.wall_s, 'flake': r.flake,
        'attempts': r.attempts, 'finish': tr.finish_reason,
        'iterations': tr.iterations, 'ctx_peak': tr.ctx_peak,
        'n_graph': tr.n_graph, 'n_native': tr.n_native, 'n_shell': tr.n_shell,
        'tool_names': tr._names(), 'errors': tr.errors,
        'run_path': r.run_path, 'final_head': tr.final_text[:240],
    }, indent=2))
    sys.exit(0 if r.flake == 'ok' else 1)
