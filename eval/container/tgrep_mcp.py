"""Read-only MCP adapter for the pinned tgrep binary inside the executor."""
import json
import os
import subprocess
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

REPO = Path(os.environ.get("LOCAGENT_REPO", "/workspace")).resolve()
TGREP = "/usr/local/bin/tgrep"
mcp = FastMCP("tgrep")


@mcp.tool()
def tgrep_search(pattern: str, fixed: bool = False, max_results: int = 100,
                 no_index: bool = False) -> str:
    """Search repository text with tgrep; returns bounded JSON matches."""
    if not pattern or len(pattern) > 2000:
        return json.dumps({"error": "pattern must be 1..2000 characters"})
    if max_results < 1 or max_results > 500:
        return json.dumps({"error": "max_results must be 1..500"})
    started = time.perf_counter()
    cmd = [TGREP, "--json"]
    if no_index:
        cmd.append("--no-index")
    if fixed:
        cmd.append("--fixed-strings")
    cmd += ["--max-count", str(max_results), pattern, str(REPO)]
    try:
        proc = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace",
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=30)
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "tgrep timeout", "fallback": True})
    if proc.returncode not in (0, 1):
        return json.dumps({"error": proc.stderr[-500:], "fallback": True})
    matches = []
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "match":
            data = event["data"]
            path = Path(data["path"]["text"])
            try:
                rel = path.resolve().relative_to(REPO).as_posix()
            except ValueError:
                continue
            matches.append({"path": rel, "line": data["line_number"],
                            "text": data["lines"]["text"].rstrip("\r\n")})
            if len(matches) >= max_results:
                break
    return json.dumps({"matches": matches, "count": len(matches),
                       "exit_code": proc.returncode,
                       "indexed": not no_index,
                       "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)},
                      ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
