"""Append-only JSONL effect log, instance-neutral.

Ship records every write *before* applying it, then marks the row
applied / failed / superseded with timing. The next tick resumes unfinished
effects before deciding again, because once a write has landed the item may
no longer be in the state that produced it — re-deciding alone can never
finish the job ("Partial ship").

Nothing here is specific to one deployment: the log directory is the
object's constructor parameter, every call names its role explicitly (there
is no default role), and the module imports only the standard library.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time

# Status vocab. A reader folds rows by (item, tick_id, seq): the latest status
# row wins; only an `applied` finish counts as done.
APPLIED = "applied"
FAILED = "failed"
SUPERSEDED = "superseded"
PENDING = "pending"
DONE_STATUSES = frozenset({APPLIED, SUPERSEDED})


def fold(rows: list[dict]) -> dict[tuple, str]:
    """Latest status per (item, tick_id, seq) key."""
    latest: dict[tuple, str] = {}
    for r in rows:
        key = (r.get("item"), r.get("tick_id"), r.get("seq"))
        latest[key] = r.get("status")
    return latest


class EffectLog:
    """The append-only JSONL effect log for one directory: one file per role.

    ``role`` is required on every call that does not take it from a row —
    no default — so an omitted role is a TypeError, never a write into some
    default role's file. A row that carries no ``role`` of its own is a
    ValueError; ``apply`` raises it *before* running the effect so no
    not-logged effect ever runs.
    """

    def __init__(self, log_dir: str):
        self.log_dir = log_dir

    def path(self, role: str) -> str:
        """The JSONL file for *role*'s effects: ``<log dir>/<role>.jsonl``."""
        return os.path.join(self.log_dir, f"{role}.jsonl")

    def _append(self, row: dict) -> dict:
        path = self.path(row["role"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
        return row

    def _read(self, role: str) -> list[dict]:
        """All rows for *role*. A missing or unreadable log reads as empty."""
        try:
            with open(self.path(role), encoding="utf-8") as f:
                lines = f.readlines()
        except (FileNotFoundError, OSError):
            return []
        rows: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue  # a corrupt line never blinds the reader to the rest
            if isinstance(data, dict):
                rows.append(data)
        return rows

    def _status_row(self, row, status, error=None, *, started=None, finished=None, ms=None):
        role = row.get("role")
        if not role:
            raise ValueError(
                f"row for item {row.get('item')!r} has no 'role': a status row names its "
                "role explicitly, never a default role's file"
            )
        return {
            "role": role,
            "item": row["item"],
            "tick_id": row["tick_id"],
            "seq": row["seq"],
            "effect": row["effect"],
            "payload": row.get("payload"),
            "head_sha": row.get("head_sha"),
            "status": status,
            "started": started,
            "finished": finished,
            "ms": ms,
            "error": error,
        }

    def plan(self, item, tick_id, effects, head_sha=None, *, role):
        """Log one ``pending`` row per effect BEFORE any of them is applied, then
        return the rows in order. ``effects`` is a list of ``(effect_name,
        payload)`` pairs; ``seq`` numbers them within this plan (0-based).
        ``item`` is ``<repo>#<n>``; ``head_sha`` pins resume to the exact commit
        the rows were decided for, and every row of one plan carries the same
        ``tick_id`` for the reader to fold by. Each append is best-effort: a
        failed log write is skipped so a logging outage never blocks the ship
        (that row simply never makes it into the log; the pending in-memory row
        is still returned so the caller can apply its effect).
        """
        rows = [
            {
                "role": role,
                "item": item,
                "tick_id": tick_id,
                "seq": seq,
                "effect": effect,
                "payload": payload,
                "head_sha": head_sha,
                "status": PENDING,
                "started": None,
                "finished": None,
                "ms": None,
                "error": None,
            }
            for seq, (effect, payload) in enumerate(effects)
        ]
        for row in rows:
            try:
                self._append(row)
            except OSError as e:
                print(f"[effect_log] plan append failed for {item}: {e}", file=sys.stderr)
                continue  # a failed log write never blocks the ship
        return rows

    def mark(self, row, status, error=None, *, started=None, finished=None, ms=None):
        """Append a status row for *row* (a ``plan()`` row). Statuses are
        ``applied``, ``failed`` and ``superseded``; ``superseded`` marks a row
        void because the item's head has moved on (the resume path marks each
        remaining row itself).

        Timing is supplied by ``apply()`` (``started``/``finished`` wall-clock
        + an ``ms`` duration measured around the write): ``mark()`` alone records
        the *state change* — ``started``/``finished`` are the moment the status
        landed (defaulting to now), and ``ms`` stays None because no write was
        measured. A bare ``mark()`` never claims an ``ms`` the code never took.
        """
        if started is None:
            started = time.time()
        if finished is None:
            finished = started
        return self._append(self._status_row(row, status, error, started=started, finished=finished, ms=ms))

    def apply(self, row, fn):
        """Run ``fn`` and mark *row* ``applied`` or ``failed`` with timing around
        it. Returns ``fn()``'s result; the exception (if any) propagates so the
        caller keeps today's control flow — the log only records what happened.

        The status append is best-effort here too: an OSError landing on the
        log write is reported as a logging failure, never as the effect having
        failed. A row with no ``role`` is a ValueError raised *before* ``fn``
        runs — the effect never runs for a row that can't be logged."""
        if not row.get("role"):
            raise ValueError(
                f"row for item {row.get('item')!r} has no 'role': refusing to run an "
                "effect that cannot be logged to its role's file"
            )
        started = time.time()
        pc_start = time.perf_counter()
        wall_finished = None
        ms = None
        try:
            result = fn()
        except Exception as e:
            ms = round((time.perf_counter() - pc_start) * 1000, 3)
            wall_finished = time.time()
            try:
                self.mark(row, FAILED, str(e), started=started, finished=wall_finished, ms=ms)
            except OSError as le:
                print(f"[effect_log] apply mark (failed) append error: {le}", file=sys.stderr)
            raise
        wall_finished = time.time()
        ms = round((time.perf_counter() - pc_start) * 1000, 3)
        try:
            self.mark(row, APPLIED, None, started=started, finished=wall_finished, ms=ms)
        except OSError as le:
            print(f"[effect_log] apply mark (applied) append error: {le}", file=sys.stderr)
        return result

    def latest_plans(self, rows: list[dict]) -> dict:
        """The latest plan (by log order) per item, as ``{item: [pending rows]}``
        with each row annotated with its folded status. A plan is keyed by its
        ``tick_id`` — pending rows of an *earlier* plan for the same item never
        leak into the latest one's row list, so the returned rows are always one
        plan's, in that plan's seq order."""
        plans: dict[str, list[dict]] = {}
        latest_tick: dict[str, str] = {}
        status_by_key = fold(rows)
        pending_rows = [r for r in rows if r.get("status") == PENDING]
        # The item's latest plan is the LAST pending row in log order — a re-plan
        # replaces any earlier draft, whose rows never resume.
        for r in pending_rows:
            latest_tick[r.get("item")] = r.get("tick_id")
        for r in pending_rows:
            item = r.get("item")
            if r.get("tick_id") != latest_tick.get(item):
                continue  # an older draft of this item — a re-plan replaced it
            key = (item, r.get("tick_id"), r.get("seq"))
            plans.setdefault(item, []).append({**r, "status": status_by_key.get(key, PENDING)})
        return plans

    def unfinished(self, role: str):
        """Items whose latest plan still has rows that are neither ``applied`` nor
        ``superseded``: ``{item: [pending rows in seq order]}``. A missing or
        empty log reads as no unfinished items. Never raises."""
        pending = self.latest_plans(self._read(role))
        out: dict[str, list[dict]] = {}
        for item, rows in pending.items():
            live = [r for r in rows if r["status"] not in DONE_STATUSES]
            if live:
                out[item] = sorted(live, key=lambda r: r.get("seq", 0))
        return out


def _percentile(values: list, p: int):
    """Nearest-rank percentile of *values* — ``idx = ceil(p/100 * n) - 1``, no
    interpolation, stdlib only. Empty input is ``None``, never ``0``."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(p / 100 * len(ordered)) - 1]


def _empty_stats() -> dict:
    return {
        "applied": 0,
        "failed": 0,
        "superseded": 0,
        "pending": 0,
        "p50": None,
        "p90": None,
        "finished_later": 0,
    }


def summarize(rows: list[dict]) -> dict:
    """A JSON-able, read-only summary of effect-log *rows*.

    Rows are folded by ``(item, tick_id, seq)`` — the latest status row wins —
    and counted per ``(role, effect)`` as ``applied`` / ``failed`` /
    ``superseded`` / ``pending``; a folded ``pending`` key is its item's still
    unfinished work. ``p50``/``p90`` are nearest-rank percentiles over ``ms``
    on folded-``applied`` keys only (``mark()`` records a state change without
    measuring a write and leaves ``ms`` None; only ``apply()`` measures it),
    and stay ``None`` when no applied ``ms`` exists. ``finished_later`` counts
    fold keys — one each — whose status sequence holds a ``failed`` row
    followed, later in log order, by an ``applied`` row for the same key: a
    resume finished it. ``unfinished`` lists the folded-pending rows in log
    order. The command never writes to the log directory.
    """
    folded_rows: dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("item"), r.get("tick_id"), r.get("seq"))
        folded_rows[key] = r  # the last row in log order wins, like fold()

    groups: dict = {}
    ms_by_group: dict[tuple, list] = {}
    for _key, r in folded_rows.items():
        gkey = (r.get("role"), r.get("effect"))
        stats = groups.setdefault(gkey[0], {}).setdefault(gkey[1], _empty_stats())
        status = r.get("status")
        if status == APPLIED:
            stats["applied"] += 1
            ms = r.get("ms")
            if ms is not None:
                ms_by_group.setdefault(gkey, []).append(ms)
        elif status == FAILED:
            stats["failed"] += 1
        elif status == SUPERSEDED:
            stats["superseded"] += 1
        else:
            stats["pending"] += 1

    for gkey, ms_values in ms_by_group.items():
        stats = groups[gkey[0]][gkey[1]]
        stats["p50"] = _percentile(ms_values, 50)
        stats["p90"] = _percentile(ms_values, 90)

    finished_later = 0
    seen_failed: set[tuple] = set()
    for r in rows:  # log order: a `failed` row finished by a later `applied` row
        key = (r.get("item"), r.get("tick_id"), r.get("seq"))
        if r.get("status") == FAILED:
            seen_failed.add(key)
        elif r.get("status") == APPLIED and key in seen_failed:
            groups.setdefault(r.get("role"), {}).setdefault(r.get("effect"), _empty_stats())[
                "finished_later"
            ] += 1
            finished_later += 1
            seen_failed.discard(key)  # count each fold key once

    unfinished = [
        {
            "role": r.get("role"),
            "item": r.get("item"),
            "tick_id": r.get("tick_id"),
            "seq": r.get("seq"),
            "effect": r.get("effect"),
        }
        for r in folded_rows.values()
        if r.get("status") not in (APPLIED, FAILED, SUPERSEDED)
    ]
    return {"effects": groups, "finished_later": finished_later, "unfinished": unfinished}
