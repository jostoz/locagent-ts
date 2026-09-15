# Cloud reasoning-control gate (2026-09-15)

Status: **archived**. Cloud-provider validation is complete for the current
stage; the active evaluation track is now tgrep vs LocAgent vs hybrid routing.

Executor: `qwen/qwen3.8-27b` through Cline's native `openrouter` provider. All
cases ran without LocAgent MCP against the repository pinned at `da9acfe`.
Raw transcripts remain under the ignored `eval/results/` tree.

| Case | Correct | Time | Native calls | Input tokens | Output tokens | Reasoning chars | Cost |
|---|---:|---:|---:|---:|---:|---:|---:|
| q1 / none | yes | 20.7 s | 5 | 55,845 | 434 | 0 | $0.00938 |
| q1 / low | yes | 23.4 s | 6 | 59,685 | 839 | 2,368 | $0.01315 |
| qC1 / none | yes | 66.1 s | 11 | 308,398 | 2,284 | 0 | $0.04999 |
| qC1 / low | no (0.75) | 55.6 s | 7 | 129,151 | 1,894 | 4,840 | $0.02302 |
| qC1 / medium | yes | 47.8 s | 6 | 139,701 | 2,349 | 7,324 | $0.02873 |

The native provider fixed the control problem seen with the generic
`openai-compatible` adapter: `none` emitted no reasoning events, while `low`
and `medium` emitted observable reasoning. Quality was non-monotonic. On qC1,
`none` found the answer by spending substantially more search/context, `low`
missed requested file coverage, and `medium` was the cheapest passing setting.

The edit gate `e6` also passed with `medium` and no LocAgent. It changed only
`src/board/useStickyNotes.ts` and `src/board/Board.tsx`; `npx tsc -p
tsconfig.app.json --noEmit` and both helper-presence checks passed. The run took
49.9 seconds, made 13 native calls, used 113,317 input and 2,069 output tokens,
and cost $0.02092.

The local Unsloth deployment uses a Jinja chat template to serialize roles,
tools, and thinking controls. OpenRouter owns the server-side template, so this
experiment establishes behavioral compatibility rather than byte-for-byte
template parity. The relevant contract is successful tool use, structured
output, edit acceptance, reasoning-control compliance, and bounded cost.

## Next gates

1. Repeat `none`, `low`, and `medium` at least three times on a stratified set:
   literal localization, convention recovery, multi-hop localization, plan JSON,
   and cross-file edits.
2. Use `medium` as the provisional cloud default for complex planning/editing;
   retain `none` for simple lookup only when its search budget is capped.
3. Compare cloud `medium` with the local Unsloth executor on the same seeds,
   prompts, native-tool condition, and acceptance checks. Report success rate,
   wall time, calls, tokens, cost, and malformed/empty-output rate.
4. Add a structured-plan gate before claiming Jinja-template compatibility:
   validate the exact planner schema and record `finish_reason` separately from
   schema errors.
5. Run the tgrep/LocAgent router experiment in `TGREP_HYBRID_PLAN.md`; compare
   lexical-only, graph-only, and hybrid routing under fixed call budgets.
