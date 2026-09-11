# Executor constraints (Stage-1 experiment)

This file is copied into `<repo>/.clinerules/executor-constraints.md` by
`eval/run_matrix.py` for the `9b-plan` / `9b-plan-oneshot` conditions and
removed on teardown. It is appended to Cline's system prompt as a rules file
(the safe mechanism — it does **not** replace the built-in tool protocol).

## One step at a time

- You are executing **one step** of a plan that was written for you. Do that
  step and nothing else. Do not start the next step, do not "also fix" things
  you notice.
- When the step's acceptance check would pass, stop and output the single line
  `STEP DONE` — then end your turn. Do not summarize, do not ask what's next.

## Writing and editing source — never through the shell

- You are on **Windows PowerShell**. There is no `bash`, no `sh`, no heredoc
  (`<<EOF`), no `cat`, no `nano`/`vi`, no `/dev/null`.
- **Never** create or modify a source file with `run_commands`. Not with
  `Set-Content`, `Add-Content`, `Out-File`, `New-Item`, `echo … >`, `… >>`,
  `Write-Output … |`, here-strings (`@'…'@`). There is no PowerShell cmdlet
  called `Write-Content`.
- All file creation and editing goes through the **`editor`** tool. For an
  existing file, edit the specific region; do not rewrite the whole file.
- If a write looks hard, that means you have the wrong tool — switch to
  `editor`, do not try another shell incantation.

## Reading — never whole large files

- Before opening any file, use `graph_search` / `graph_get` / `graph_traverse`
  to find the exact entity. `graph_get "<id>"` returns a skeleton; that is
  usually enough.
- If you must `read_file`, pass an explicit line range of **≤ 120 lines**.
  Never read a file whole when it is over ~400 lines — `Board.tsx` is 7600
  lines and reading it will destroy your context.

## Never hand the work back

- Never tell the user to "paste this", "open it in your editor", "apply this
  change manually", or "add the following lines". You make the edit with the
  `editor` tool. If you cannot, output `STEP BLOCKED: <one line why>` and stop.

## Scratch files

- Never create `*.tmp`, `*_part*`, `*.bak`, `*_new.*`, `*_copy.*`. Edit the
  real file in place.

## Value-level conventions

- Some rules live only in comments (e.g. "a note with `color === undefined`
  **is** a text box"). No graph edge carries them. Using `grep` / a targeted
  read to confirm such a convention is expected — that is not a failure.

## Shell commands that ARE fine

`npx tsc …`, `npm test` / `vitest …`, `git diff|status|rev-parse`, `grep`/
`Select-String`, `Get-Content` with `-TotalCount`/`-Tail` for a peek,
`Get-ChildItem`. Read-only inspection only.
