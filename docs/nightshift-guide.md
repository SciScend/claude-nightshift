# claude-nightshift — usage guide

Run a Claude Code task autonomously for hours. Each time the 5-hour session
limit is hit, nightshift waits for the window to reset and resumes — so an
unattended stretch uses the session gaps that would otherwise be lost — while
keeping a single task from eating your whole weekly budget.

For Pro and Max plans. Nothing here spends tokens on bookkeeping.

---

## Quick start

```bash
claude-nightshift "fix all the failing tests"
```

That runs the task, waits out every 5-hour limit, and stops when weekly usage
reaches 70%.

Leaving it for a long unattended stretch — the case it is built for:

```bash
claude-nightshift -w auto --horizon 20h "migrate the API layer to v2"
```

Now it also refuses to spend more of the week than a 20-hour stretch is
entitled to.

---

## The two limits, and which one bites

| Window | What it is | What nightshift does |
|---|---|---|
| **5-hour** | Resets every 5 hours, all day | Waits it out and resumes. This is the loop. |
| **7-day** | Your weekly pool | Decides whether there *should* be another session at all. |

Within one stretch the 5-hour window decides how *long* you work; the weekly
pool decides how many *stretches* you get this week. They need different
controls, which is why there are two.

---

## Controlling weekly spend

### `-t, --threshold` — the wall

```bash
claude-nightshift -t 85 "long refactor"
```

Never start a session at or above this weekly percentage. Default 70. This is an
absolute ceiling: nothing below can raise it, and it is the right tool for
"always keep 30% in reserve for my interactive work".

On its own it answers *"am I out of weekly budget?"* — but not *"should this one
task be allowed to spend what's left?"*

### `-w, --max-windows N` — the boring, predictable cap

```bash
claude-nightshift -w 2 "big refactor"
```

At most two 5-hour windows, then stop, even if the threshold is nowhere near.
Use it when you know what a task is worth.

### `-w auto` — spread what's left over the time that's left

```bash
claude-nightshift -w auto --horizon 20h "big refactor"
```

This run is entitled to the share of the remaining pool that its horizon is of
the time before the weekly window resets:

```
allowance = weekly_remaining × (horizon / time_to_reset)
N windows = allowance / measured_cost_of_one_window
```

**Worked example.** 22% used, so 78% left; the weekly window resets in 3d 18h
(90h); you are away for 20h:

```
allowance = 78% × 20/90 = 17.3%     →  run ceiling 22 + 17.3 = 39.3%
N         = 17.3% / ~8% per window  →  2 windows
```

A 20-hour stretch could hold four 5-hour windows. `auto` deliberately takes two
and leaves the rest of the week alone. **That trade is the whole point** — if
you would rather have all four tonight, say `-w 4` or drop `-w` entirely.

### `--horizon` — the one input only you know

How long you are actually leaving it unattended: `20h`, `90m`, `2d`, or a bare
number meaning hours. Default `12h`. Only `-w auto` reads it.

The denominator is wall-clock time to the reset, not time you are *available*.
If you have 90h before the reset but only two 20h stretches in it, `auto`
under-allocates. That errs on the safe side — the slack stays available for your
daytime interactive use — but it is why `auto` can feel stingy. Raise
`--horizon`, or use an explicit `-w N`, when you want it to lean the other way.

### `--no-overshoot` — don't start what you can't finish

Weekly usage moves in lumps of roughly one window at a time. A plain threshold
can only notice you crossed it *after* a session already did. `--no-overshoot`
checks before starting: if `current + one window` lands past the ceiling, it
stops instead.

`-w` turns this on for you. Use the flag alone to get it without a window cap:

```bash
claude-nightshift -t 70 --no-overshoot "long task"
```

### Two things worth knowing about `auto`

**It never quite empties the pool.** Each run takes a *share* of what remains,
so the remainder decays but never reaches zero. Slight systematic under-use, and
the price of never being caught out.

**The last day is generous.** As the reset approaches, `horizon / time_to_reset`
approaches 1, so the allowance approaches everything left. Use-it-or-lose-it,
automatically. Expect the first day of a weekly window to feel much tighter than
the last.

---

## Window cost is measured, not guessed

The budget maths needs one number: what a full 5-hour window costs in weekly
percent. Nightshift reads the weekly figure before and after every window that
ends on a limit — the difference is the real cost, and it is free, because the
figures are already in Claude's output stream.

The median of the last 20 samples lives in `~/.claude/nightshift-state.json`.
After your first real run the arithmetic is calibrated to your actual workload
instead of an assumption.

Until then a conservative estimate (8%) is used, and the log says so:

```
Window cost   : ~8.0% of weekly (estimated, no samples yet)
...
[budget] window 1 cost 6.4% of the weekly pool (estimate now 6.4% from 1 sample(s))
```

Samples that can't be real are discarded: a negative delta (the weekly window
reset mid-run) or an absurd one. Delete the state file to start over.

---

## Reading the startup banner

```
Starting claude-nightshift
  Command       : claude --model opus --output-format stream-json --verbose -p ...
  Weekly gate   : start a session only below 70.0%
  Window budget : auto, over a 20h horizon
  Window cost   : ~6.4% of weekly (measured)
  Max retries   : 10
  Compress after: 3 resumes
  Auto budget   : 17.3% of the weekly pool (78.0% left, 20h horizon of 90h to reset)
  Window budget : 2 × 5h window(s) at ~6.4%/window (measured)
  Run ceiling   : stop before 7-day usage reaches 39.3%
Gate open: 7-day usage 22.0%, run ceiling 39.3%, window 1/2
```

The first `Window budget` line is what you asked for; the second is what that
worked out to once real usage was read. `Run ceiling` is the number actually
enforced — always the *lower* of your threshold and the auto allowance.

### Why it stopped

| Message | Meaning |
|---|---|
| `window budget spent — 2 of 2 five-hour window(s) used` | `-w` count exhausted. |
| `7-day usage X% is at or above the run ceiling Y%` | The `auto` allowance for this run is gone. The week isn't — a new run gets a fresh share. |
| `7-day usage X% is at or above the threshold Y%` | The real wall. |
| `another window would reach ~X%, past the ceiling Y%` | `--no-overshoot` declined to start a window that wouldn't fit. |
| `weekly usage could not be determined` | Couldn't read usage; stopping rather than running blind. `--allow-unknown-usage` flips this. |

Exit code is `0` only when the task actually completed.

---

## Everything else

### Resuming and context

On each limit hit nightshift resumes automatically. The first `--compress-after`
resumes (default 3) continue the real conversation by session id, carrying full
history. After that it starts fresh sessions seeded with the original task plus
a progress summary Claude writes to `.nightshift-status.md` before each session
ends — which keeps context from growing without bound across a long night.

```bash
claude-nightshift --compress-after 2 "very large refactor"
```

Commit `.nightshift-status.md` or add it to `.gitignore`; it is the handoff
between sessions and is rewritten constantly.

### Model and effort

```bash
claude-nightshift -m opus -e medium "long refactor"
```

`-e/--effort` is `low|medium|high|xhigh|max`. Lower effort spends fewer thinking
tokens, so more work fits inside one 5-hour window — often a better trade than a
bigger budget.

### Bounded sessions

```bash
claude-nightshift --max-turns 50 "fix all lint errors"
```

Hitting the turn cap ends the session and nightshift resumes; it is not an
error. Turn-capped sessions do **not** count against `-w`, because they don't
consume a whole 5-hour window.

### Logging

```bash
claude-nightshift -l "task"              # nightshift_YYYYMMDD_HHMMSS.log
claude-nightshift --log-file run.log "task"
```

### Full control

```bash
claude-nightshift -w auto -- claude -p "task" --model opus --allowedTools Edit,Write
```

Everything after `--` is passed to `claude` verbatim; nightshift only adds
`--dangerously-skip-permissions` and the stream format.

### Other flags

| Flag | Default | Purpose |
|---|---|---|
| `-r, --max-retries` | 10 | Resume attempts across limits and turn caps. |
| `--usage-max-age` | 120 | How long a usage reading from the stream stays fresh before re-querying. |
| `--allow-unknown-usage` | off | Run even when usage can't be read. |
| `--raw-output` | off | Plain text instead of stream-json; falls back to matching limit messages as English. |
| `--dry-run` | off | Print the command that would run, then exit. |
| `--hard-stop` | off | Kill a *running* session at the threshold. Destroys in-progress work — see below. |
| `-i, --interval` | 300 | Poll interval, `--hard-stop` only. |

### `--hard-stop`

By default the threshold is a **gate before a session starts**; a session
already running always finishes. `--hard-stop` restores the old behaviour of
polling and killing mid-session. It loses unsaved work and prevents the status
file from being written, so the compressed-resume handoff breaks. Off for a
reason.

---

## Where the numbers come from

In priority order, none of which cost tokens:

1. **`rate_limit_event` in Claude's own output stream** — free, already there,
   carries both windows and the exact reset time.
2. **`https://api.anthropic.com/api/oauth/usage`** — one ~0.5s request, using the
   OAuth token Claude Code keeps in `~/.claude/.credentials.json`. Same endpoint
   `/usage` uses.
3. **`cclimits --claude --json`** — fallback only, if both above fail.

A typical overnight run makes one request at startup and one after each 5-hour
wait. Everything else is read out of the stream.

---

## Recipes

```bash
# Overnight, unattended, don't blow the week
claude-nightshift -w auto --horizon 10h "task"

# Long weekend stretch, generous
claude-nightshift -w auto --horizon 20h -t 85 "task"

# Exactly two windows, no arithmetic
claude-nightshift -w 2 "task"

# Old behaviour, plus don't overshoot the wall
claude-nightshift -t 70 --no-overshoot "task"

# Everything you've got before the weekly reset
claude-nightshift -t 95 "task"

# Cheap and bounded, for something exploratory
claude-nightshift -m haiku -e low --max-turns 30 -w 1 "task"
```

---

## Design notes

Fuller rationale, including why the threshold became a gate rather than an
interrupt and the limit-detection bugs this replaced, is in
[`reports/2026-09-04-nightshift-weekly-gate.md`](reports/2026-09-04-nightshift-weekly-gate.md).
