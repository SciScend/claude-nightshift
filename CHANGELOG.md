# Changelog

## 1.0.0 - unreleased

First public release.

- Runs a Claude Code task across 5-hour limits: waits for the reset and resumes.
- Weekly budget: `--threshold`, `--max-windows N` and `--max-windows auto --horizon`.
- Measures what one 5-hour window costs in weekly percent and keeps the median
  of recent runs in `~/.claude/nightshift-state.json`.
- Reads usage from Claude's `stream-json` output, the OAuth usage endpoint, or
  `cclimits`, without spending tokens.
- Long runs switch to fresh sessions seeded with a progress summary after
  `--compress-after` resumes.
