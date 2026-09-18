"""Post-hoc executor-discipline analyzer.

Cline 3.0.61's file-based hooks do not dispatch in the standalone CLI (every run
logs ``hook dispatch failed: session.hook requires a valid hook event payload``),
so the plan's blocking ``PreToolUse`` guard is not possible. Instead this module
inspects a finished run -- the parsed ``--json`` transcript plus a git snapshot
of the repo -- and returns the same violation set as ``failure_flags`` for the
Stage-1 CSV. Stage 2's detectors reuse this logic at checkpoint boundaries.

    rep = analyze(transcript, repo="C:/.../miro-clone",
                  target_file="src/board/useStickyNotes.ts", expect_edit=True,
                  baseline_ref="HEAD")
    rep.flags   -> ['wrong_shell', 'repeated_command_failure', ...]
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from eval.transcript import (
    Transcript,
    _DELEGATION_RE,
    _SHELL_WRITE_RE,
    _TMP_FILE_RE,
    _WRONG_SHELL_RE,
    FILE_EDIT_TOOLS,
)

CTX_WINDOW = 65536
CTX_THRESHOLD = 45000
BIG_FILE_LINES = 500
BIG_READ_SPAN = 400

_GAVE_UP_RE = re.compile(
    r"(?i)\b(i (can'?t|cannot|am unable to|was unable to|couldn'?t)"
    r"|unable to (complete|finish|proceed|create|write|edit)"
    r"|giving up|i'?ll stop here|please (do this|take over|continue)"
    r"|this exceeds what i can|beyond my current)")
_NEW_FILE_RE = re.compile(r'(?i)\.(tmp|bak|orig|rej)$|~$|_(part\d*|new|copy|old)\.[a-z]+$')


@dataclass
class GuardReport:
    flags: List[str] = field(default_factory=list)
    details: Dict[str, object] = field(default_factory=dict)

    def add(self, flag: str, detail: object = True) -> None:
        if flag not in self.flags:
            self.flags.append(flag)
        self.details[flag] = detail

    def as_dict(self) -> dict:
        return {'flags': self.flags, 'details': self.details}


# ---------------------------------------------------------------- git helpers
def _git(repo: str, *args: str, timeout: int = 30) -> str:
    try:
        return subprocess.run(['git', '-C', repo, *args],
                              capture_output=True, text=True, timeout=timeout).stdout
    except Exception:  # noqa: BLE001
        return ''


def untracked_and_modified(repo: str) -> List[str]:
    out = _git(repo, 'status', '--porcelain')
    paths = []
    for ln in out.splitlines():
        if len(ln) > 3:
            paths.append(ln[3:].strip().strip('"'))
    return paths


def diff_numstat(repo: str, baseline_ref: str, path: Optional[str] = None) -> Dict[str, int]:
    args = ['diff', '--numstat', baseline_ref]
    if path:
        args += ['--', path]
    changed: Dict[str, int] = {}
    for ln in _git(repo, *args).splitlines():
        parts = ln.split('\t')
        if len(parts) == 3:
            add, dele, p = parts
            try:
                changed[p] = int(add) + int(dele)
            except ValueError:
                changed[p] = 0
    return changed


def file_line_count(repo: str, path: str) -> Optional[int]:
    txt = _git(repo, 'show', f'HEAD:{path}') or ''
    if txt:
        return txt.count('\n') + 1
    try:
        with open(f'{repo}/{path}', encoding='utf-8', errors='replace') as fh:
            return sum(1 for _ in fh)
    except OSError:
        return None


# ------------------------------------------------------------------- analysis
def _normalize_cmd(c: str) -> str:
    c = re.sub(r'\s+', ' ', c.strip().lower())
    return c[:120]


def analyze(
    tr: Transcript,
    *,
    repo: Optional[str] = None,
    target_file: Optional[str] = None,
    expect_edit: bool = False,
    baseline_ref: Optional[str] = None,
    step_done_marker: str = 'STEP DONE',
    shell: str = 'windows',
) -> GuardReport:
    rep = GuardReport()

    # --- context pressure -------------------------------------------------
    if tr.ctx_peak > CTX_THRESHOLD:
        rep.add('context_threshold', tr.ctx_peak)
    if tr.ctx_peak >= CTX_WINDOW - 1500:
        rep.add('compaction', tr.ctx_peak)

    # --- shell misuse ---------------------------------------------------
    cmds = tr.all_shell_commands()
    joined_cmds = '\n'.join(cmds)
    if shell == 'windows' and (_WRONG_SHELL_RE.search(joined_cmds) or any(
            _WRONG_SHELL_RE.search(o.get('result') or '')
            for tc in tr.tool_calls for o in tc.outputs)):
        rep.add('wrong_shell', _WRONG_SHELL_RE.search(joined_cmds).group(0)
                if _WRONG_SHELL_RE.search(joined_cmds) else 'in tool output')
    if _SHELL_WRITE_RE.search(joined_cmds):
        rep.add('shell_write', _SHELL_WRITE_RE.search(joined_cmds).group(0))

    # --- repeated command failure ------------------------------------
    fail_norm = Counter()
    for tc in tr.tool_calls:
        for o in tc.outputs:
            if o.get('success') is False or o.get('error'):
                fail_norm[_normalize_cmd(o.get('query') or tc.name)] += 1
    repeated = {k: v for k, v in fail_norm.items() if v >= 2}
    if repeated:
        rep.add('repeated_command_failure', repeated)

    # --- delegation / give-up -----------------------------------------
    if _DELEGATION_RE.search(tr.final_text):
        rep.add('paste_this', _DELEGATION_RE.search(tr.final_text).group(0))
    completed_step = step_done_marker.lower() in tr.final_text.lower()
    if ((_GAVE_UP_RE.search(tr.final_text) and not completed_step)
            or tr.finish_reason not in ('completed', '')):
        rep.add('gave_up', tr.finish_reason or _GAVE_UP_RE.search(tr.final_text).group(0))

    # --- big-file reads ---------------------------------------------
    for tc in tr.tool_calls:
        if tc.name not in ('read_file', 'read_files', 'view_file'):
            continue
        span = None
        if isinstance(tc.input, dict):
            for a, b in (('start_line', 'end_line'), ('offset', 'limit'), ('from', 'to')):
                if a in tc.input and b in tc.input:
                    try:
                        span = abs(int(tc.input[b]) - int(tc.input[a]))
                    except (TypeError, ValueError):
                        span = None
        result_lines = sum((o.get('result') or '').count('\n') for o in tc.outputs)
        if (span is not None and span > BIG_READ_SPAN) or result_lines > BIG_READ_SPAN:
            rep.add('big_file_read', {'tool': tc.name, 'span': span, 'result_lines': result_lines})
            break

    # --- git-derived checks ---------------------------------------
    if repo:
        new_paths = untracked_and_modified(repo)
        tmp_new = [p for p in new_paths if _NEW_FILE_RE.search(p) or _TMP_FILE_RE.search(p)]
        if tmp_new:
            rep.add('tmp_file', tmp_new)

        if baseline_ref:
            changed = diff_numstat(repo, baseline_ref)
            rep.details['diff_files'] = changed
            if expect_edit:
                touched = sum(changed.values())
                said_done = step_done_marker.lower() in tr.final_text.lower()
                if touched == 0 and (said_done or tr.finish_reason == 'completed'):
                    rep.add('no_progress', 'edit expected, 0 lines changed')
                if target_file:
                    stray = [p for p in changed if p != target_file
                             and not p.endswith(('.snap', '.lock'))]
                    if stray:
                        rep.add('off_target_edit', stray)
            # whole-file rewrite of a large file via the editor tool
            for p, n in changed.items():
                lc = file_line_count(repo, p)
                if lc and lc > BIG_FILE_LINES and n > lc * 0.6:
                    rep.add('whole_file_rewrite', {'file': p, 'changed': n, 'of': lc})

    return rep


if __name__ == '__main__':
    import argparse
    import json
    import sys

    from eval.transcript import load

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('run_jsonl')
    ap.add_argument('--repo')
    ap.add_argument('--target-file')
    ap.add_argument('--baseline-ref', default='HEAD')
    ap.add_argument('--expect-edit', action='store_true')
    args = ap.parse_args()

    tr = load(args.run_jsonl)
    rep = analyze(tr, repo=args.repo, target_file=args.target_file,
                  expect_edit=args.expect_edit, baseline_ref=args.baseline_ref)
    print(json.dumps(rep.as_dict(), indent=2, default=str))
    sys.exit(1 if rep.flags else 0)
