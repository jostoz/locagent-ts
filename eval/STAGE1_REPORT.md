# Stage 1: local executor versus DeepSeek planner

The corpus is fixed at `da9acfe` and contains 12 localization cases plus 6 isolated edit cases. Each edit ran in a disposable Docker worktree. Cline received only the local `qwen3.8-27b` executor; the DeepSeek credential was used by the planner/cache step and was never passed into the executor container.

The localization matrix is `eval/results/stage1-qwen38-deepseek-isolated-20260914/`. The corrected edit rerun is split across `eval/results/stage1-qwen38-deepseek-isolated-edits-r2-20260914/` (solo and planned e1-e5) and `eval/results/stage1-qwen38-deepseek-isolated-edits-r2-planned-tail-20260914/` (planned e5-e6 after the junction cleanup fix). GPU watchdog logs are under `eval/results/_logs/`; both completed with `intervention=False` and maintained roughly 3.2–3.7 GiB free VRAM.

Localization results (12 cases):

| condition | file recall@10 | entity recall@10 | answer correct |
|---|---:|---:|---:|
| qwen3.8-27b solo | 0.6310 | 0.4643 | 12/12 |
| DeepSeek plan + qwen3.8-27b executor | 0.8512 | 0.7143 | 12/12 |

The planner materially improves retrieval coverage, especially at larger cutoffs, while the final answer correctness ceiling is already reached by the local model. Corrected edit acceptance is 5/6 solo and 4/6 planned. Solo e5 timed out while trying to manage dependencies. Planned e3 was rejected by the progress guard because the executor claimed the create step completed but produced zero diff; planned e5 timed out. e1, e2, e4, and e6 planned edits passed acceptance.

Additional control: Qwen planner + Qwen executor (`eval/results/stage1-qwen38-localplanner-localexecutor-20260914/`) also achieved 12/12 localization answers and 5/6 edit acceptance. Its file/entity recall@10 was 0.7500/0.5833, compared with 0.8512/0.7143 for DeepSeek + Qwen. This isolates a real DeepSeek planning advantage: both planners reach the same final-answer accuracy, but DeepSeek produces better ranked repository coverage. The local planner is nevertheless a viable fallback and slightly outperformed the corrected DeepSeek run on edit acceptance in this sample.

The first full run had a harness defect: inspect-only plan steps were incorrectly treated as no-progress failures, and POSIX checks were executed through Windows shell semantics. Those results are retained for audit but are not used for the corrected edit comparison. The runner now treats no-progress as fatal only for create/edit steps, evaluates `test -f` and quoted grep checks directly, and cleans partial `node_modules` state before the evaluator junction. The source checkout still has no tracked diff after all runs; its pre-existing untracked files are preserved.

DeepSeek plan files under `eval/plans/` identify `deepseek-reasoner`. A later attempt to refresh a cached plan experienced API latency and was stopped; the benchmark therefore uses the already validated cached DeepSeek plans and records that limitation rather than silently substituting a local planner.

MCP ablation smoke (`eval/results/mcp-ablation-q1-r2-20260914/`) used q1 with the same Qwen executor and local plan, but disabled LocAgent in the container. Solo and planned both remained correct; the planned run used three steps, took 81.9 s total, and made zero `graph_*` calls. The equivalent MCP-enabled local-plan smoke also remained correct, took 81.2 s, and made four graph calls. This single case does not establish a quality difference, but it confirms the control is actually MCP-free and provides the baseline for a larger ablation.

A harder q3 MCP-free sample (`eval/results/mcp-ablation-q3-20260914/`) also remained correct: four planned steps, zero `graph_*` calls, 137 s. The equivalent MCP-enabled local-plan run was correct in four steps with nine graph calls and 154.4 s. This is still one task, so it is evidence about feasibility rather than a general performance claim.
# Local planner with LocAgent MCP (2026-09-15)

The 12 localization cases completed with a Qwen `qwen3.8-27b` planner that could
query LocAgent MCP and the same local Qwen executor. Results are in
`eval/results/mcp-planner-executor-localization-r2-20260915/`; planner artifacts
are in `eval/results/mcp-planner-localization-r3-20260915/`.

- Answer correctness: 12/12.
- File recall@10: 0.9583; entity recall@10: 0.9583.
- Executor cost: 26 steps, 50 graph calls, 747.1 seconds, zero flakes.
- Planner cost for the successful 12-plan batch, including three parse retries:
  69 graph calls and 635.8 seconds. End-to-end: 119 graph calls and 1,382.9
  seconds. Earlier failed qC1 diagnostics are excluded from this reproducible
  batch cost.
- GPU watchdog: no intervention; observed free VRAM stayed roughly 2.9–3.3 GiB.

This is the strongest localization result in the experiment. For comparison,
the static local planner reached file/entity recall@10 of 0.7500/0.5833, and the
DeepSeek planner reached 0.8512/0.7143. The result supports giving the planner
read-only graph access, but it also exposed a control failure: qC1 initially
spent 19 and then 13 graph calls without returning a plan. An explicit four-call
exploration budget produced a valid plan in four calls. This budget is part of
the successful condition and should be retained in any replication.
