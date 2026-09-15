# tgrep / LocAgent integration sample (2026-09-15)

Four localization tasks (`q1`, `q3`, `q4`, `qC1`) were run once per condition
against the same `da9acfe` worktrees, with the same local Unsloth Qwen executor,
Docker sandbox, and no cloud provider.

| Condition | Correct | Mean wall time | Input tokens | Native calls | tgrep calls | Graph calls |
|---|---:|---:|---:|---:|---:|---:|
| rg/native search | 4/4 | 43.3 s | 362,147 | 32 | 0 | 0 |
| tgrep | 4/4 | 42.5 s | 330,285 | 29 | 17 | 0 |
| LocAgent | 4/4 | 40.0 s | 407,205 | 25 | 0 | 7 |
| hybrid prompt | 4/4 | 42.3 s | 459,509 | 31 | 0 | 5 |

The standalone tgrep condition reduced input context by 8.8% and native calls by
9.4% relative to the rg/native baseline, while mean end-to-end time changed by
less than one second. LocAgent was fastest and used fewer calls, but consumed
12.4% more input context than the baseline on this sample.

The hybrid condition did not invoke tgrep at all; the model selected LocAgent for
every task. Its larger context than both baselines means this run does not test a
real hybrid router. The next experiment must expose tgrep as an explicit routed
tool (or enforce a deterministic lexical pre-router) and record the route before
the executor starts. A prompt recommendation alone is insufficient evidence of
integration.

This is a feasibility sample, not an adoption gate: one repetition over four
tasks cannot establish a statistically stable winner. Repeat the four conditions
over the full localization corpus, then include edit cases and index freshness.
