"""LM Studio / Windows model-loading machinery, ported from the scratchpad
``eval_*.sh`` (``ensure_loaded`` / ``free_gb`` / flake-retry).

Hard-won facts baked in here:

- ``lms server start --port 11434`` -- port 1234 gives EACCES on Windows.
- Load explicitly every time: ``lms unload --all`` then
  ``lms load <model> -c 65536 --gpu max --parallel 1 -y``.
    - ``--parallel 1`` is mandatory: the default 4 splits the KV cache.
    - JIT auto-load resets context to 8192, so never rely on it -- pass ``-c``.
- Keep the LM Studio GUI closed; it can unload the CLI model mid-session.
- ``free_gb`` guard: pause when free RAM < 3.5 GB so a load does not thrash.

Nothing here imports torch or the graph; it only shells out to ``lms``.
"""

from __future__ import annotations

import subprocess
import time
from typing import Optional

PORT = 11434
DEFAULT_CTX = 65536
MODEL_9B = 'qwen/qwen3.5-9b'
MODEL_35B = 'qwen/qwen3.5-35b-a3b'
_FREE_RAM_FLOOR_GB = 3.5


def _run(args, timeout=180) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def server_start(port: int = PORT) -> None:
    try:
        _run(['lms', 'server', 'start', '--port', str(port)], timeout=60)
    except Exception:  # noqa: BLE001 -- best effort; the API call will fail loudly
        pass


def _ps_text() -> str:
    try:
        return _run(['lms', 'ps'], timeout=30).stdout
    except Exception:  # noqa: BLE001
        return ''


def is_loaded(model: str) -> bool:
    return model in _ps_text()


def loaded_context(model: str) -> Optional[int]:
    """Parse the CONTEXT column of ``lms ps`` for *model*, or None."""
    for line in _ps_text().splitlines():
        if model in line:
            for tok in line.split():
                if tok.isdigit() and int(tok) >= 4096:
                    return int(tok)
    return None


def unload_all() -> None:
    try:
        _run(['lms', 'unload', '--all'], timeout=60)
    except Exception:  # noqa: BLE001
        pass


def free_gb() -> float:
    """Available system RAM in GiB (psutil if present, else a PowerShell probe)."""
    try:
        import psutil  # type: ignore
        return psutil.virtual_memory().available / (1024 ** 3)
    except Exception:  # noqa: BLE001
        pass
    try:
        out = _run(['powershell', '-NoProfile', '-Command',
                    '(Get-CIMInstance Win32_OperatingSystem).FreePhysicalMemory'],
                   timeout=20).stdout.strip()
        return int(out) / (1024 ** 2)   # FreePhysicalMemory is in KiB
    except Exception:  # noqa: BLE001
        return 999.0


def wait_for_ram(floor_gb: float = _FREE_RAM_FLOOR_GB, tries: int = 6) -> None:
    for _ in range(tries):
        if free_gb() >= floor_gb:
            return
        time.sleep(20)


def load(model: str, ctx: int = DEFAULT_CTX) -> None:
    wait_for_ram()
    unload_all()
    time.sleep(1)
    _run(['lms', 'load', model, '-c', str(ctx), '--gpu', 'max',
          '--parallel', '1', '-y'], timeout=600)
    time.sleep(2)


def ensure_loaded(model: str = MODEL_9B, ctx: int = DEFAULT_CTX) -> None:
    """Idempotent: load *model* at *ctx* unless it is already up at that context.
    Catches the JIT footgun where a prior auto-load pinned ctx to 8192."""
    server_start()
    if is_loaded(model):
        got = loaded_context(model)
        if got is not None and got >= ctx:
            return
    load(model, ctx)


def assert_reasoning_disabled(model: str, base_url: str = f'http://127.0.0.1:{PORT}/v1') -> None:
    """Hard preflight gate: fail loudly before a corpus run instead of
    discovering mid-run that LM Studio's GUI-level ``Enable Thinking``
    toggle (or a stale ``lms load`` that ignored the saved preset) left
    hidden reasoning on. ``lms load`` does not apply a saved GUI preset
    (confirmed empirically, 2026-11-11): a model loaded via the CLI comes
    up with reasoning enabled by default regardless of what preset was
    last saved in the GUI, and the only way to turn it off is the GUI
    toggle itself -- see ``skill://lmstudio-reasoning-control`` step 3.

    A deterministic smoke request (temperature=0, trivial arithmetic) is
    the only reliable signal: the model-list endpoint's loaded config
    never exposes the Enable Thinking state, only context/batch/offload
    settings. Raises RuntimeError naming the exact GUI fix instead of
    silently burning a 20+ minute corpus run on a reasoning-polluted model.
    """
    import json
    import urllib.request

    payload = json.dumps({
        'model': model, 'temperature': 0,
        'messages': [{'role': 'user', 'content': 'What is 11*13? Answer with just the number.'}],
    }).encode('utf-8')
    req = urllib.request.Request(
        f'{base_url}/chat/completions', data=payload,
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f'lmstudio preflight: could not reach {base_url} for model {model!r} '
            f'-- is LM Studio running and the model loaded? ({exc})') from exc
    reasoning_tokens = (data.get('usage', {})
                        .get('completion_tokens_details', {})
                        .get('reasoning_tokens', 0))
    if reasoning_tokens:
        raise RuntimeError(
            f'lmstudio preflight FAILED: model {model!r} answered a trivial '
            f'prompt with {reasoning_tokens} reasoning tokens even at '
            f'temperature=0. Enable Thinking is ON in the LM Studio GUI (or '
            f'the model was loaded via `lms load`, which ignores any saved '
            f'preset). Fix: open LM Studio -> Local Server -> the model -> '
            f'Inference tab -> Reasoning -> turn Enable Thinking OFF, then '
            f'reload the model -- see skill://lmstudio-reasoning-control.')


def swap_model(to_model: str, ctx: int = DEFAULT_CTX) -> None:
    """Unload whatever is up and load *to_model* -- for the local strong-planner
    fallback (9b <-> 35b-a3b) since 24 GB VRAM can't hold both."""
    if is_loaded(to_model) and (loaded_context(to_model) or 0) >= ctx:
        return
    load(to_model, ctx)


if __name__ == '__main__':
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--ensure', metavar='MODEL', nargs='?', const=MODEL_9B)
    ap.add_argument('--ctx', type=int, default=DEFAULT_CTX)
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--unload', action='store_true')
    args = ap.parse_args()

    if args.unload:
        unload_all()
        print('unloaded all')
    if args.ensure:
        ensure_loaded(args.ensure, args.ctx)
    if args.status or not (args.unload or args.ensure):
        print(json.dumps({
            'free_gb': round(free_gb(), 2),
            '9b_loaded': is_loaded(MODEL_9B),
            '9b_ctx': loaded_context(MODEL_9B),
            '35b_loaded': is_loaded(MODEL_35B),
            'ps': _ps_text().strip(),
        }, indent=2))
    sys.exit(0)
