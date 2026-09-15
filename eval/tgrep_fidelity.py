"""Compare pinned tgrep search semantics with ripgrep before agent evaluation."""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
from pathlib import Path


QUERIES = [
    {"id": "handler", "pattern": "handleAiAction", "fixed": True},
    {"id": "action_prop", "pattern": "onAction={handleAiAction}", "fixed": True},
    {"id": "textbox_convention", "pattern": "note.color === undefined", "fixed": True},
    {"id": "font_sizing", "pattern": "fontSizeForText", "fixed": True},
    {"id": "image_operation", "pattern": "create_ai_image", "fixed": True},
    {"id": "note_shape_decl", "pattern": r"function\s+NoteShape", "fixed": False},
]


def run(command: list[str], cwd: Path) -> tuple[int, float, str, str]:
    start = time.perf_counter()
    proc = subprocess.run(command, cwd=cwd, text=True, encoding="utf-8",
                          errors="replace", stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    elapsed_ms = (time.perf_counter() - start) * 1000
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"command failed ({proc.returncode}): {proc.stderr[-500:]}")
    return proc.returncode, elapsed_ms, proc.stdout, proc.stderr


def matches(payload: str, repo: Path) -> list[tuple[str, int, str]]:
    found = []
    for line in payload.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event["data"]
        path = Path(data["path"]["text"])
        if not path.is_absolute():
            path = repo / path
        found.append((path.resolve().relative_to(repo).as_posix(),
                      data["line_number"], data["lines"]["text"]))
    return sorted(set(found))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--tgrep", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    repo, binary = args.repo.resolve(), args.tgrep.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    built = subprocess.run([str(binary), "index", "--force", str(repo)],
                           text=True, encoding="utf-8", errors="replace",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if built.returncode:
        raise RuntimeError(built.stderr[-1000:])
    build_ms = (time.perf_counter() - started) * 1000
    index_dir = repo / ".tgrep"
    index_bytes = sum(p.stat().st_size for p in index_dir.rglob("*") if p.is_file())

    rows = []
    for query in QUERIES:
        common = ["-F"] if query["fixed"] else []
        rg_cmd = ["rg", "--json", *common, query["pattern"], "."]
        tg_cmd = [str(binary), "--json", *common, query["pattern"], str(repo)]
        direct_cmd = [str(binary), "--json", "--no-index", *common,
                      query["pattern"], str(repo)]
        rg_runs, tg_runs, direct_runs = [], [], []
        rg_set = tg_set = direct_set = []
        for _ in range(args.repeats):
            _, elapsed, output, _ = run(rg_cmd, repo)
            rg_runs.append(elapsed); rg_set = matches(output, repo)
            _, elapsed, output, _ = run(tg_cmd, repo)
            tg_runs.append(elapsed); tg_set = matches(output, repo)
            _, elapsed, output, _ = run(direct_cmd, repo)
            direct_runs.append(elapsed); direct_set = matches(output, repo)
        rows.append({
            **query,
            "matches": len(rg_set),
            "indexed_equal": tg_set == rg_set,
            "direct_equal": direct_set == rg_set,
            "rg_median_ms": round(statistics.median(rg_runs), 3),
            "tgrep_indexed_median_ms": round(statistics.median(tg_runs), 3),
            "tgrep_direct_median_ms": round(statistics.median(direct_runs), 3),
            "rg": rg_set, "tgrep_indexed": tg_set, "tgrep_direct": direct_set,
        })
    report = {
        "tgrep_version": subprocess.check_output([str(binary), "--version"], text=True).strip(),
        "repo": str(repo), "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
        "build_ms": round(build_ms, 3), "index_bytes": index_bytes,
        "repeats": args.repeats, "queries": rows,
        "all_equal": all(r["indexed_equal"] and r["direct_equal"] for r in rows),
    }
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "queries"}))
    for row in rows:
        print(json.dumps({k: row[k] for k in ("id", "matches", "indexed_equal",
              "direct_equal", "rg_median_ms", "tgrep_indexed_median_ms",
              "tgrep_direct_median_ms")}))
    return 0 if report["all_equal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
