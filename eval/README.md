# `eval/` — staged multi-agent orchestrator experiment

Full design: `docs/TYPESCRIPT_PORT_PLAN.md` → *Fase 4b*, and
`~/.claude/plans/cuddly-squishing-stroustrup.md`.

The question this harness answers, in order:

1. **Stage 0** (here now) — pin a small localize+edit corpus against miro-clone's
   live HEAD and score it with a torch-free metric.
2. **Stage 1** — does handing the local `qwen/qwen3.5-9b` a *structured plan*
   (from a strong planner) materially beat letting it run solo? Go/no-go gate
   for building the orchestrator.
3. **Stage 2** — LangGraph orchestrator sketch (design + unit-tested detectors),
   in the sibling `orchestrator/` package. Gated on Stage 1.

`locagent_mcp.py` and the graph builder are the system under test, not part of
this package — the only import back into the tree is `import locagent_mcp` for
ground-truth resolution.

## Corpus — `miro_clone_localization.jsonl`

18 hand-written records over `C:/Users/joz/Documents/miro-clone`, one JSON object
per line. Two shapes:

| field | localize (`q*`) | edit (`e*`) |
|---|---|---|
| `prompt` | the question | the change to make |
| `gt_files` | files a correct answer must point at | files a correct patch touches |
| `gt_entities` | graph node ids (`path:Dotted.name`) the answer must name | entities the patch changes (pre-edit ids; a *new* file appears only in `gt_files`) |
| `gt_lines` | `{file: [entity start lines]}` — drives drift detection | same |
| `answer_tokens` | `;;`-separated regex bundle, all must hit the answer text | `null` |
| `acceptance` | `null` | list of shell commands, all must exit 0 in the repo after the edit |
| `pinned_at` | miro-clone short sha the lines were verified against | same |
| `isolated_file_ok` | may a single-file read answer it without the graph? | may the edit be done file-local? |

### Relevance criterion (what counts as a ground-truth hit)

An entity is **relevant** to a record iff a competent engineer, having answered
the prompt, would necessarily have opened that entity and would cite it in the
answer — it is load-bearing for the prompt, not merely nearby. Concretely: the
function/class that *defines* the thing asked about; every function that
*directly* calls or renders it when the prompt asks "who calls/renders X";
the specific file(s) a correct patch must edit. Not relevant: a parent
container entity that merely *contains* the real target (e.g. the 6300-line
`Board.tsx:Board` when the answer is one method inside it), test files, types
that are only passed through, and "you might also look at" context. When the
convention lives only in comments (`qC1`: `note.color === undefined` ⇒ text
box), the relevant entities are the ones whose *comments or code* actually
encode the check, plus the file that consumes it.

`gt_entities` is deliberately tight so `acc@k` means something at small k.

## Metric — `metrics.py`

Torch-free reimplementation of `evaluation/eval_metric.py` (which needs
`torch`+`datasets`+`pandas`). Set arithmetic on de-duplicated ranked lists:

- `acc@k` — `1.0` iff the top-k picks contain `min(|gt|, k)` gt items (i.e. the
  whole gt set was surfaced within k, when `|gt| ≤ k`). Mean over records.
- `recall@k` — `|top-k ∩ gt| / |gt|`, mean over records. With a perfect
  prediction, `recall@k < 1` for `k < |gt|` on the broad-enumerate records
  (`q3` has 7 gt entities) — that is correct, not a bug. `recall@10 == 1.0`.
- `precision@k` — `|top-k ∩ gt| / k`.

Scored at two levels: `file` (entity ids projected to their path) and `entity`.

```
python -m eval.metrics --selftest
python -m eval.metrics --gt eval/miro_clone_localization.jsonl --pred eval/results/<run>.pred.jsonl
```

Prediction file: one line per task,
`{"id": "...", "ranked_entities": [...], "ranked_files": [...]}`. Ranking =
first-seen order the run referenced each entity (search hit → `graph_get`
target → final-answer mention). If `ranked_files` is omitted it is derived from
`ranked_entities`.

## Scorer / validator — `score.py`

```
python -m eval.score --validate-corpus          # schema gate (categories, keys, regex, edit acceptance)
python -m eval.score --answer transcript.txt --tokens "handleAiAction ;; 6091"
```

`score_answer(text, bundle)` → `(fraction_matched, all_matched)`; a record is
"correct" only when every `;;` fragment matches (case-insensitive `re.search`).

## Re-pin — `fixtures/pin_ground_truth.py`

miro-clone is a live repo; Sonnet edits it, so recorded line numbers drift and
the same script scores differently on different days. Before trusting any
Stage-1 number, re-pin:

```
python -m eval.fixtures.pin_ground_truth          # human-readable report; exit 1 if anything is stale
python -m eval.fixtures.pin_ground_truth --json   # machine-readable
```

It resolves every `gt_entities` id through `locagent_mcp`, prints old→current
line diffs, checks each `answer_tokens` *content* fragment against the gt entity
source (bare line-number / filename fragments are "locators" — verified against
the paths/lines/notes, never failed for not being in source), and prints HEAD.
It does **not** rewrite the jsonl — read the report, edit the file, bump
`pinned_at` on every touched record. `run_matrix.py` (Stage 1) refuses to
publish metrics when `pinned_at` ≠ live HEAD.

Current pin: **`da9acfe`**, 0 unresolved / 0 drift / 0 token miss (18/18).

## LM Studio / Windows machinery (Stage 1+, preserved from the scratchpad `eval_*.sh`)

- `lms server start --port 11434` — port 1234 → `EACCES` on Windows.
- Load explicitly, every time: `lms unload --all; lms load qwen/qwen3.5-9b -c 65536 --gpu max --parallel 1 -y`.
  - `--parallel 1` is mandatory — the default 4 splits the KV cache.
  - JIT auto-load resets context to 8192 → always pass `-c 65536`.
- Keep the **LM Studio GUI closed** during a run — it can unload the
  CLI-loaded model mid-session.
- Qwen3.5 always reasons: executor uses Cline `--thinking none`; a local
  strong-planner fallback uses `reasoning_effort="low"`.
- `free_gb()` guard: pause ~20 s when free RAM < 3500 MB.
- `resource module not available on Windows` on every `locagent_mcp` import is a
  harmless pre-existing stderr warning.

## What's tracked vs ignored

Tracked: the corpus, `metrics.py`/`score.py`, `fixtures/`, Stage-1 planner code,
`plans/*.json` (cached planner outputs), prompt/template files.
Ignored (`.gitignore`): `eval/results/` (run transcripts + prediction + CSV),
`eval/.env` (`DEEPSEEK_API_KEY` — read from the environment, never copied out of
`~/.cline/data/settings/providers.json`).
