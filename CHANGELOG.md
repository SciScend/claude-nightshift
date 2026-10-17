# Changelog

## Unreleased

- Distinct exit codes, so a wrapper can tell why a run stopped: `0` completed,
  `1` failed, `2` usage error, `3` weekly limit (`--threshold`) reached, `4` run
  budget (`-w N` / `-w auto`) spent with the task unfinished, `130`
  interrupted. Previously every stop other than completion or Ctrl-C exited `1`.
- `-f/--task-file FILE` reads the task prompt from a file, or from stdin with
  `-f -`.

## 1.0.0 - unreleased

First public release.

- Runs a Claude Code task across 5-hour limits: waits for the reset and resumes.
- Weekly budget: `--threshold`, `--max-windows N` and `--max-windows auto --horizon`.
- Measures what one 5-hour window costs in weekly percent and keeps the median
  of recent runs in `~/.claude/nightshift-state.json`.
- Reads usage from Claude's `stream-json` output, the OAuth usage endpoint, or
  `cclimits`, without spending tokens.
- 5-hour gate (`--min-window-left`, default 5%): waits for the reset instead of
  starting a session in an almost spent window, and a session that hits the
  limit within 15 minutes is not counted as a window or a cost sample.
- Long runs switch to fresh sessions seeded with a progress summary after
  `--compress-after` resumes.
