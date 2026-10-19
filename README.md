# claude-nightshift

**Run a Claude Code task autonomously for hours — across every 5-hour limit — without blowing your weekly budget.**

[![CI](https://github.com/SciScend/claude-nightshift/actions/workflows/ci.yml/badge.svg)](https://github.com/SciScend/claude-nightshift/actions/workflows/ci.yml)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Claude Code on a Pro or Max plan has two limits: a **5-hour session window** that
resets all day, and a **7-day pool** that decides how many of those windows you
get. Start a long task in the evening and the 5-hour limit stops it an hour
later — the remaining windows overnight go unused.

`claude-nightshift` uses them. It runs your task, and each time the 5-hour limit
lands it waits for the reset and resumes — while a weekly budget you control
decides whether there should be another session at all.

```bash
claude-nightshift -w auto --horizon 20h "migrate the API layer to v2"
```

No tokens are spent on bookkeeping: usage figures are read out of Claude's own
output stream, or from the same OAuth endpoint `/usage` uses.

---

## Install

```bash
uv tool install git+https://github.com/SciScend/claude-nightshift   # recommended
pipx install git+https://github.com/SciScend/claude-nightshift
```

Requires Python 3.9+ and the [Claude Code](https://claude.com/claude-code) CLI on
your `PATH`, already authenticated on a Pro or Max plan.

From a checkout:

```bash
git clone https://github.com/SciScend/claude-nightshift
cd claude-nightshift
uv tool install .
```

---

## Quick start

```bash
# Run until done, stop when weekly usage reaches 70%
claude-nightshift "fix all the failing tests"

# Overnight, unattended, don't eat the whole week
claude-nightshift -w auto --horizon 10h "refactor the auth module"

# Exactly two 5-hour windows, no arithmetic
claude-nightshift -w 2 "big refactor"

# See what would run, change nothing
claude-nightshift --dry-run "some task"

# Long prompt from a file (or `-f -` for stdin)
claude-nightshift -w auto --horizon 10h -f task.md

# Current usage as JSON, for scripts (costs no tokens)
claude-nightshift --usage-json
```

Exit code is `0` only when the task actually completed. The other codes tell a
script (a queue, a cron job) why the run stopped:

| Exit code | Meaning | What a wrapper should do |
|---|---|---|
| `0` | The task completed. | Mark it done. |
| `1` | Failed: Claude exited with an error, or `--max-retries` ran out. | Look at the log. |
| `2` | Usage error: bad arguments (argparse), unreadable or empty `--task-file`. | Fix the command. |
| `3` | Weekly limit: `--threshold` reached (also by `--hard-stop`). No budget left this week. | Stop; nothing else should start either. |
| `4` | Run budget spent: the `-w N` window count or the `-w auto` run ceiling, task not finished. | Run it again later; it resumes from the status file. |
| `5` | Weekly usage could not be read, so no session was started (see `--allow-unknown-usage`). | Stop; it is an environment problem (login, network), not the task. |
| `6` | The week ended: the weekly limit reset after `--week-of`. | Stop; start again when you want to spend the new week. |
| `130` | Interrupted (Ctrl-C). | Stop. |

> **Heads up:** every session runs with `--dangerously-skip-permissions`, because
> nobody is there to approve tool calls. Run it on work you are willing to leave
> unsupervised — a checkout you can `git reset`, ideally in a container.

---

## The three controls

| Flag | Answers | Default |
|---|---|---|
| `-t, --threshold` | *Am I out of weekly budget?* Never start a session at or above this weekly percentage. | `70` |
| `-w, --max-windows N` | *What is this one task worth?* Stop after N five-hour windows. | no cap |
| `-w auto --horizon 20h` | *What share of the week does an unattended 20h stretch deserve?* | — |

`auto` gives this run an even spread of what is left over the time that is left:

```
allowance = weekly_remaining × (horizon / time_to_reset)
N windows = allowance / measured_cost_of_one_window
```

With 78% left, 3d18h until the weekly reset and a 20h horizon, this run is
entitled to `78 × 20/90 = 17.3%` of the pool — about two windows. The other two
that a 20-hour stretch could have held are left for the rest of the week.

**What a window costs is measured, not guessed.** The weekly figure is read
before and after every window that ends on a limit; the difference is that
window's real cost, and the median of recent samples lives in
`~/.claude/nightshift-state.json`. After your first run the arithmetic is
calibrated to your actual workload.

### Several runs at once

All runs on one account share the same 5-hour window and the same weekly pool,
and the usage figures only say what the account as a whole has spent. To give
each of several parallel runs its own budget, name the runs and cap them:

```bash
claude-nightshift --run-id docs --spend 5 -f docs-task.md
claude-nightshift --run-id api  --spend auto --spend-share 2 -f api-task.md
```

`--spend PCT` stops the run once its own share of the weekly pool reaches PCT.
`--spend auto` gives it an even share of what is left below `--threshold`,
split `--spend-share` ways. Unlike the threshold, the spend budget is also
checked during a session (every `--spend-interval` seconds, default 120), and a
session that runs past it is stopped, with exit code `4`.

The runs keep a shared ledger in `~/.claude/nightshift-ledger.json`. Each time
the weekly figure rises, the rise is split between the runs by the tokens each
used since the last rise, priced per model (replaced by Claude's exact cost
when a session ends). It is an estimate: the figures come in whole percent,
and your own use of Claude during a session is charged to the runs that are in
one. `--usage-json` lists the runs with what each spent and its budget, and
`--runs-json` lists only the runs, without reading usage.

### Staying in one week

`--threshold` compares against the account's weekly usage, which drops to
about zero when the weekly limit resets. A run that is still going then would
carry on into the new week. `--week-of EPOCH|now` ties the run to the week
that was current at that moment: once the weekly limit has reset after it, no
session starts and the run exits `6`. A wait for a 5-hour reset that falls
after the weekly reset ends the run at once instead of sleeping. A session
that is running at the weekly reset is not cut short. A wrapper that starts
several runs passes all of them the same moment, so a run it starts after the
reset stops at once too.

---

## Long runs without unbounded context

On each limit hit nightshift resumes. The first `--compress-after` resumes
(default 3) continue the real conversation by session id, carrying full history.
After that it starts fresh sessions seeded with the original task plus a progress
summary Claude writes to `.nightshift-status.md` before each session ends — so an
all-night run does not drag a growing transcript behind it.

Add `.nightshift-status.md` to your `.gitignore`, or commit it; it is the handoff
between sessions and is rewritten constantly. `--status-file PATH` puts it
somewhere else, for example outside the repository, or one file per task when
several tasks share a folder.

---

## Where the numbers come from

In priority order, none of which cost tokens:

1. **`rate_limit_event` in Claude's own `stream-json` output** — free, already
   there, carries both windows and the exact reset time.
2. **`https://api.anthropic.com/api/oauth/usage`** — one ~0.5s request using the
   OAuth token Claude Code keeps in `~/.claude/.credentials.json`. The same
   endpoint `/usage` uses. The token is read locally and sent only to Anthropic.
3. **`cclimits --claude --json`** — fallback, only if both above fail.

A typical overnight run makes one request at startup and one after each 5-hour
wait. Everything else is read out of the stream.

> **Note:** neither the `rate_limit_event` stream fields nor the
> `/api/oauth/usage` endpoint are documented by Anthropic, and either may change
> without notice. If every source fails, nightshift refuses to start a session
> rather than guess (pass `--allow-unknown-usage` to run anyway), and the 5-hour
> wait falls back to the reset time in Claude's limit message.

---

## Documentation

- **[Usage guide](docs/nightshift-guide.md)** — every flag, the budget maths,
  worked examples, and how to read the startup banner.
- **[Contributing](CONTRIBUTING.md)** — dev setup, tests, and the release flow.

---

## Contributing

Issues and pull requests are welcome. `uv sync --extra dev && uv run pytest`
gets you a working checkout; see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE) © Iva Popova
