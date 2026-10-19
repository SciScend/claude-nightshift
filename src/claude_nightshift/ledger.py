"""Who spent what: the weekly pool split between runs that share it.

Several nightshift processes can run at the same time. They all draw on the
same 5-hour window and the same weekly pool, and the usage figures only ever
say how much the account as a whole has spent. To give each run its own
budget (``--spend``), every run with a ``--run-id`` keeps two numbers in a
shared ledger file:

- its *weight*: the tokens it has used, priced per model in approximate
  US dollars (replaced by Claude's exact ``total_cost_usd`` when a session
  ends), and
- its *spent* share of the weekly pool, in weekly percent.

Whenever a run sees the weekly figure rise, the rise is split between the
runs by how much weight each gained since the last split. When no run
gained any weight, the rise goes in equal parts to the runs that are in a
session; with none in a session it is nobody's (the user's own use).

All of this is an estimate: usage readings come in whole percent, and use
outside nightshift during a session is charged to the runs in it.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - not on POSIX
    fcntl = None

from claude_nightshift.logsetup import get_logger

__all__ = [
    "Ledger",
    "LEDGER_PATH_ENV",
    "ledger_path",
    "read_runs",
    "message_cost",
]

logger = get_logger(__name__)

DEFAULT_LEDGER_PATH = "~/.claude/nightshift-ledger.json"
LEDGER_PATH_ENV = "NIGHTSHIFT_LEDGER_PATH"

# Finished runs stay in the ledger this long, so a wrapper can still read
# what they spent.
KEEP_FINISHED_SECONDS = 7 * 24 * 3600

# A weekly figure lower than the last one by more than this means the weekly
# window has reset; a smaller drop is two sources disagreeing and is ignored.
RESET_DROP = 5.0

# Weight is written to the ledger at most this often while tokens stream in.
FLUSH_SECONDS = 20

# US dollars per million tokens (input, output) by model family. Only the
# ratios between runs matter; exact prices come from each session's result.
MODEL_PRICES = {
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}
DEFAULT_PRICE = MODEL_PRICES["sonnet"]
CACHE_WRITE_FACTOR = 1.25
CACHE_READ_FACTOR = 0.1


def ledger_path() -> str:
    """Where the shared ledger lives (``$NIGHTSHIFT_LEDGER_PATH`` overrides it)."""
    return os.path.expanduser(os.environ.get(LEDGER_PATH_ENV) or DEFAULT_LEDGER_PATH)


def message_cost(model, usage) -> float:
    """Approximate cost in US dollars of one assistant message's ``usage``."""
    name = (model or "").lower()
    price_in, price_out = next(
        (p for family, p in MODEL_PRICES.items() if family in name), DEFAULT_PRICE
    )

    def tokens(key):
        try:
            return float((usage or {}).get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    cost_in = (
        tokens("input_tokens")
        + tokens("cache_creation_input_tokens") * CACHE_WRITE_FACTOR
        + tokens("cache_read_input_tokens") * CACHE_READ_FACTOR
    ) * price_in
    return (cost_in + tokens("output_tokens") * price_out) / 1e6


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


@contextlib.contextmanager
def _locked(path):
    """Hold an exclusive lock on the ledger, across processes."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".lock", "a") as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _load(path) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"runs": {}, "last": None}
    if not isinstance(data, dict) or not isinstance(data.get("runs"), dict):
        return {"runs": {}, "last": None}
    data.setdefault("last", None)
    return data


def _save(path, data) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _attribute(data, pct, resets, now) -> None:
    """Split the rise in weekly usage since the last reading between the runs."""
    last = data.get("last")
    if last and pct < last["weekly_pct"] and last["weekly_pct"] - pct <= RESET_DROP:
        # Two sources disagreeing by a point; keep the higher reading.
        return
    reset = not last or pct < last["weekly_pct"]
    if (not reset and resets and last.get("weekly_resets")
            and abs(resets - last["weekly_resets"]) > 3600):
        reset = True
    data["last"] = {"weekly_pct": pct, "weekly_resets": resets, "at": now}
    if reset:
        for run in data["runs"].values():
            run["attributed_weight"] = run.get("weight", 0.0)
        return

    delta = pct - last["weekly_pct"]
    if delta <= 0:
        return
    runs = data["runs"]
    gains = {rid: max(0.0, r.get("weight", 0.0) - r.get("attributed_weight", 0.0))
             for rid, r in runs.items() if not r.get("finished")}
    total = sum(gains.values())
    if total > 0:
        shares = {rid: gain / total for rid, gain in gains.items() if gain > 0}
    else:
        active = [rid for rid, r in runs.items()
                  if r.get("active") and not r.get("finished") and _pid_alive(r.get("pid"))]
        shares = {rid: 1.0 / len(active) for rid in active}
    for rid, share in shares.items():
        runs[rid]["spent"] = runs[rid].get("spent", 0.0) + delta * share
    for rid in gains:
        runs[rid]["attributed_weight"] = runs[rid].get("weight", 0.0)


def _prune(data, now) -> None:
    for rid, run in list(data["runs"].items()):
        done = run.get("finished") or not _pid_alive(run.get("pid"))
        if done and now - run.get("updated", 0) > KEEP_FINISHED_SECONDS:
            del data["runs"][rid]


def read_runs(path=None) -> dict:
    """The runs in the ledger: ``{run_id: {spent, cap, active, finished}}``."""
    data = _load(path or ledger_path())
    out = {}
    for rid, run in data["runs"].items():
        alive = not run.get("finished") and _pid_alive(run.get("pid"))
        out[rid] = {
            "spent": round(run.get("spent", 0.0), 2),
            "cap": run.get("cap"),
            "active": bool(run.get("active")) and alive,
            "finished": not alive,
        }
    return out


class Ledger:
    """This run's entry in the shared ledger."""

    def __init__(self, run_id, path=None, clock=time.time):
        self.run_id = run_id
        self.path = path or ledger_path()
        self.clock = clock
        self._lock = threading.Lock()
        self._base = 0.0  # weight of the sessions that have ended
        self._messages = {}  # message id -> cost, for the session running now
        self._flushed_at = 0.0
        self.cap = None
        self.spent = 0.0

    @property
    def weight(self) -> float:
        return self._base + sum(self._messages.values())

    def _update(self, change) -> None:
        """Apply ``change(data, run)`` to this run's entry under the file lock."""
        with self._lock, _locked(self.path):
            data = _load(self.path)
            now = self.clock()
            run = data["runs"].setdefault(self.run_id, {"spent": 0.0, "weight": 0.0,
                                                        "attributed_weight": 0.0})
            run["pid"] = os.getpid()
            run["updated"] = now
            run["weight"] = self.weight
            run["cap"] = self.cap
            change(data, run)
            _prune(data, now)
            _save(self.path, data)
            self.spent = run.get("spent", 0.0)

    def _safe_update(self, change) -> None:
        try:
            self._update(change)
        except OSError as exc:
            logger.warning(f"[ledger] could not update {self.path}: {exc}")

    def start(self, cap=None) -> None:
        """Register the run. ``cap`` is its spend budget in weekly percent."""
        self.cap = cap

        def change(data, run):
            run.pop("finished", None)
            run["active"] = False

        self._safe_update(change)

    def set_cap(self, cap) -> None:
        self.cap = cap
        self._safe_update(lambda data, run: None)

    def session(self, active) -> None:
        """Mark the start or the end of a Claude session."""
        if not active:
            # The exact cost, when known, has replaced the estimates already.
            self._base += sum(self._messages.values())
            self._messages = {}

        def change(data, run):
            run["active"] = active

        self._safe_update(change)

    def message(self, message) -> None:
        """Count one stream-json assistant message (repeats of an id replace it)."""
        usage = message.get("usage")
        if not usage:
            return
        key = message.get("id") or f"anon-{len(self._messages)}"
        cost = message_cost(message.get("model"), usage)
        self._messages[key] = max(cost, self._messages.get(key, 0.0))
        if self.clock() - self._flushed_at >= FLUSH_SECONDS:
            self._flushed_at = self.clock()
            self._safe_update(lambda data, run: None)

    def result(self, result) -> None:
        """A session's result: its exact cost replaces the estimates."""
        try:
            cost = float(result.get("total_cost_usd"))
        except (TypeError, ValueError):
            return
        if cost > 0:
            self._messages = {"result": cost}

    def observe(self, usage) -> float:
        """Take a usage reading into account. Returns this run's spend so far."""
        if usage is None or usage.weekly_pct is None:
            return self.spent
        self._safe_update(lambda data, run: _attribute(
            data, usage.weekly_pct, usage.weekly_resets, self.clock()))
        return self.spent

    def refresh(self) -> float:
        """Re-read this run's spend, which other runs' readings may have changed."""
        run = _load(self.path)["runs"].get(self.run_id) or {}
        self.spent = run.get("spent", self.spent)
        return self.spent

    def last_reading_age(self):
        """Seconds since any run last recorded a usage reading, or None."""
        last = _load(self.path).get("last")
        return self.clock() - last["at"] if last else None

    def finish(self) -> None:
        def change(data, run):
            run["active"] = False
            run["finished"] = True

        self._safe_update(change)
