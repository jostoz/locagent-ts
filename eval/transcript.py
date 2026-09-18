"""Parse a Cline CLI ``--json`` event stream into a structured ``Transcript``.

The stream is newline-delimited JSON. Shapes seen in Cline 3.0.61 (lmstudio
provider):

  {"type":"agent_event","event":{"type":"content_start","contentType":"tool",
     "toolCallId":"...","toolName":"run_commands","input":{"commands":[...]}}}
  {"type":"agent_event","event":{"type":"content_end","contentType":"tool",
     "toolName":"run_commands","output":[{"query":"...","result":"...","success":false,"error":"..."}]}}
  {"type":"agent_event","event":{"type":"usage","inputTokens":37838,"outputTokens":585,
     "totalInputTokens":...,"totalOutputTokens":...}}
  {"type":"agent_event","event":{"type":"iteration_end","iteration":3,"hadToolCalls":true,"toolCallCount":1}}
  {"type":"agent_event","event":{"type":"done","reason":"completed","text":"..."}}
  {"type":"hook_event","hookEventName":"tool_call",...}         # telemetry only
  {"type":"run_result","finishReason":"completed","iterations":5,"durationMs":9639,
     "usage":{...},"aggregateUsage":{...},"text":"...","model":{...}}
  {"type":"error","message":"Cannot connect to API: ... (ConnectionRefused)"}

``inputTokens`` on each ``usage`` event is the context size of that API call, so
``ctx_peak`` = max over the run. ``hook_event`` lines are ignored (file-based
hooks do not dispatch in the standalone CLI - a known bug).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# provider tool names (lmstudio / cline builtins). The Anthropic-style
# write_to_file / replace_in_file names do NOT appear on this provider.
FILE_EDIT_TOOLS = {'editor', 'write_to_file', 'replace_in_file', 'apply_diff',
                   'insert_content', 'edit_file', 'create_file'}
SHELL_TOOLS = {'run_commands', 'run_command', 'execute_command'}
READ_TOOLS = {'read_file', 'read_files', 'view_file'}
GRAPH_TOOL_RE = re.compile(r'graph_(search|get|traverse|map)')

# a source edit smuggled through the shell instead of the editor tool
_SHELL_WRITE_RE = re.compile(
    r'(?i)(set-content|add-content|out-file|new-item\b[^\n]*-itemtype\s+file'
    r'|\btee\b|>>?\s*[\'"]?[\w./\\-]+\.(?:ts|tsx|js|jsx|json|css)\b'
    r'|@[\'"]|<<\s*[\'"]?\w+|echo\s+.*\|\s*set-content)')
# heredoc / bash-ism on a PowerShell shell
_WRONG_SHELL_RE = re.compile(
    r'(?i)(command not found|is not recognized as (the name of |an? )'
    r'|<<\s*[\'"]?EOF|/dev/null|\bcat\s+<<|\bnano\b|\bvi\b\s)')
# the model handing the job back to the human
_DELEGATION_RE = re.compile(
    r'(?i)(paste (this|the following|it)|open (it |this )?in (your|the) editor'
    r'|copy the (following|code|snippet)|you (should|can|could|need to) (manually|now)'
    r'|in your ide|apply (this|these) (change|edit)s? (yourself|manually)'
    r'|add the following (lines|code) to)')
_TMP_FILE_RE = re.compile(
    r'(?i)[\w./\\-]+(\.tmp|\.bak|~|_part\d*|_new|_copy|\.orig)(\.[a-z]+)?\b')


@dataclass
class ToolCall:
    iteration: int
    name: str
    input: Any
    commands: List[str] = field(default_factory=list)   # run_commands only
    outputs: List[Dict[str, Any]] = field(default_factory=list)
    # per-output: {query, result, error, success}

    @property
    def any_failed(self) -> bool:
        return any(o.get('success') is False or o.get('error') for o in self.outputs)

    @property
    def all_ok(self) -> bool:
        return bool(self.outputs) and all(
            o.get('success') is not False and not o.get('error') for o in self.outputs)


@dataclass
class Transcript:
    tool_calls: List[ToolCall] = field(default_factory=list)
    ctx_peak: int = 0
    iterations: int = 0
    finish_reason: str = ''
    final_text: str = ''
    errors: List[str] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    n_events: int = 0

    # ---- derived tool economy ----
    def _names(self) -> List[str]:
        return [tc.name for tc in self.tool_calls]

    @property
    def n_graph(self) -> int:
        return sum(1 for n in self._names() if GRAPH_TOOL_RE.search(n))

    @property
    def n_shell(self) -> int:
        return sum(1 for n in self._names() if n in SHELL_TOOLS)

    @property
    def n_file_edit(self) -> int:
        return sum(1 for n in self._names() if n in FILE_EDIT_TOOLS)

    @property
    def n_read(self) -> int:
        return sum(1 for n in self._names() if n in READ_TOOLS)

    @property
    def n_native(self) -> int:
        """Non-graph tool calls -- shell + edit + read + everything else."""
        return sum(1 for n in self._names() if not GRAPH_TOOL_RE.search(n))

    def all_shell_commands(self) -> List[str]:
        out: List[str] = []
        for tc in self.tool_calls:
            out.extend(tc.commands)
        return out

    @property
    def connection_flake(self) -> bool:
        return any(re.search(r'(?i)connection ?refused|cannot connect to api|econnrefused', e)
                   for e in self.errors)


def _get_event(obj: dict) -> Optional[dict]:
    ev = obj.get('event')
    return ev if isinstance(ev, dict) else None


def parse(text: str) -> Transcript:
    t = Transcript()
    cur_iter = 0
    open_tools: Dict[str, ToolCall] = {}

    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] != '{':
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        t.n_events += 1
        otype = obj.get('type')

        if otype == 'error':
            msg = obj.get('message') or ''
            # the file-hook dispatch bug fires every run and is harmless
            if 'hook dispatch failed' not in msg:
                t.errors.append(msg)
            continue

        if otype == 'run_result':
            t.finish_reason = obj.get('finishReason') or t.finish_reason
            t.iterations = obj.get('iterations') or t.iterations
            t.duration_ms = obj.get('durationMs') or t.duration_ms
            t.usage = obj.get('aggregateUsage') or obj.get('usage') or t.usage
            if obj.get('text'):
                t.final_text = obj['text']
            continue

        if otype != 'agent_event':
            continue
        ev = _get_event(obj)
        if not ev:
            continue
        et = ev.get('type')

        if et == 'iteration_start':
            cur_iter = ev.get('iteration', cur_iter + 1)
        elif et == 'iteration_end':
            t.iterations = max(t.iterations, ev.get('iteration', 0))
        elif et == 'usage':
            it = ev.get('inputTokens') or 0
            if it > t.ctx_peak:
                t.ctx_peak = it
        elif et == 'done':
            if ev.get('text'):
                t.final_text = ev['text']
            t.finish_reason = t.finish_reason or ev.get('reason', '')
        elif et == 'content_start' and ev.get('contentType') == 'tool':
            tc = ToolCall(
                iteration=cur_iter,
                name=ev.get('toolName') or '',
                input=ev.get('input'),
            )
            inp = ev.get('input')
            if isinstance(inp, dict) and isinstance(inp.get('commands'), list):
                tc.commands = [str(c) for c in inp['commands']]
            cid = ev.get('toolCallId') or f'_{len(t.tool_calls)}'
            open_tools[cid] = tc
            t.tool_calls.append(tc)
        elif et == 'content_end' and ev.get('contentType') == 'tool':
            cid = ev.get('toolCallId')
            tc = open_tools.get(cid)
            if tc is None:
                # unmatched end -- attach to the last tool of the same name
                for prev in reversed(t.tool_calls):
                    if prev.name == ev.get('toolName'):
                        tc = prev
                        break
            if tc is not None:
                out = ev.get('output')
                if isinstance(out, list):
                    for o in out:
                        if isinstance(o, dict):
                            tc.outputs.append({
                                'query': o.get('query'),
                                'result': (o.get('result') or '')[:4000],
                                'error': o.get('error'),
                                'success': o.get('success'),
                            })
                elif isinstance(out, dict):
                    # MCP tools in Cline 3 return {content:[{type,text}],
                    # structuredContent:{...}} rather than the builtin-tool
                    # list shape above.
                    texts = []
                    for block in out.get('content') or []:
                        if isinstance(block, dict) and isinstance(block.get('text'), str):
                            texts.append(block['text'])
                    structured = out.get('structuredContent')
                    if isinstance(structured, dict):
                        texts.extend(str(v) for v in structured.values() if isinstance(v, str))
                    tc.outputs.append({
                        'query': None,
                        'result': '\n'.join(texts)[:4000],
                        'error': out.get('error'),
                        'success': not out.get('isError', False),
                    })

    if not t.iterations:
        t.iterations = cur_iter
    return t


# ---- convenience: load a run file -------------------------------------------
def load(path: str) -> Transcript:
    with open(path, encoding='utf-8', errors='replace') as fh:
        return parse(fh.read())


if __name__ == '__main__':
    import argparse
    import sys

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('run_jsonl')
    args = ap.parse_args()
    tr = load(args.run_jsonl)
    print(json.dumps({
        'finish_reason': tr.finish_reason,
        'iterations': tr.iterations,
        'ctx_peak': tr.ctx_peak,
        'duration_ms': tr.duration_ms,
        'n_tool_calls': len(tr.tool_calls),
        'n_graph': tr.n_graph, 'n_native': tr.n_native,
        'n_shell': tr.n_shell, 'n_file_edit': tr.n_file_edit, 'n_read': tr.n_read,
        'tool_names': tr._names(),
        'errors': tr.errors,
        'connection_flake': tr.connection_flake,
        'final_text_head': tr.final_text[:280],
    }, indent=2))
    sys.exit(0)
