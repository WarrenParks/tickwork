"""The ``tickwork`` command line: ``check`` (the exactly-one-state check),
``replay`` (re-send recorded LLM calls to a model and score the replies) and
``effects`` (summarize an effect log).

Runs :func:`~tickwork.workflow_states.check` over a states file, a JSONL
items file and a YAML logins map supplied on the command line, so a
standalone tickwork installation can run the exactly-one-state check
without any instance-specific fetcher:

.. code-block:: text

    tickwork check STATES --items ITEMS --logins LOGINS

* STATES is loaded with :func:`~tickwork.workflow_states.load`; the allowed
  owner roles are the keys of LOGINS.
* ITEMS is a JSONL file with one item per line in the shape
  :func:`~tickwork.workflow_states.evaluate` describes.
* LOGINS is a YAML mapping of role name to login.

One line is printed per anomaly (repo, issue, pr, matched states) followed
by a summary line. Exit codes: ``1`` when any anomaly was found, ``0``
when clean, ``2`` when a file cannot be read, parsed or validated (e.g. a
states file naming a role that is not a key of LOGINS).

``tickwork effects LOG_DIR [--role ROLE] [--since ISO] [--json]`` summarizes
an effect log with :func:`~tickwork.effect_log.summarize`. It is read-only:
it never creates or writes LOG_DIR, and a missing or empty log prints an
empty summary and exits 0.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from tickwork import replay, workflow_states
from tickwork.effect_log import PENDING, EffectLog, summarize
from tickwork.providers import OpenAICompatProvider

EMPTY_SUMMARY = {"effects": {}, "finished_later": 0, "unfinished": []}


def _read_logins(path: str | Path) -> dict[str, str]:
    """Read a YAML mapping of role name → login from *path*."""
    logins = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(logins, dict) or not all(
        isinstance(role, str) and isinstance(login, str) for role, login in logins.items()
    ):
        raise ValueError(f"{path}: logins must be a mapping of role name to login")
    return logins


def _read_items(path: str | Path) -> list[dict[str, Any]]:
    """Read JSONL items, one dict per line, from *path* (blank lines skipped)."""
    items: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError(f"{path}:{lineno}: item must be a JSON object")
            items.append(item)
    return items


def _format_anomaly(anomaly: dict[str, Any]) -> str:
    return (
        f"{anomaly['repo']}: issue {anomaly['issue']} pr {anomaly['pr']} "
        f"matched {', '.join(anomaly['matched']) or 'nothing'}"
    )


# The one CLI provider name, resolving to a stdlib OpenAI-compatible adapter.
_PROVIDER = "openai-compat"


def _length_exact_scorer(record: dict[str, Any], reply: dict[str, Any]) -> dict[str, Any]:
    """The built-in scorer: response length and whether the reply is an
    exact match of the recorded response (null when there is nothing to
    compare against)."""
    recorded = record.get("response")
    response = reply.get("response")
    return {
        "response_len": len(response) if response is not None else None,
        "exact_match": None if recorded is None or response is None else response == recorded,
    }


def _run_replay(args: argparse.Namespace) -> int:
    """Run the generic replay over the given JSONL record paths.

    Only the ``openai-compat`` provider is wired in: importing
    :mod:`tickwork.providers` in the CLI (not in :mod:`tickwork.replay`)
    keeps the replay core free of transport code.
    """
    if args.provider != _PROVIDER:
        print(f"tickwork: unknown provider {args.provider!r}", file=sys.stderr)
        return 2
    if args.repeats < 1:
        print(f"tickwork: --repeats must be >= 1 (got {args.repeats})", file=sys.stderr)
        return 2
    skips: dict[str, int] = {}
    lines = 0
    try:
        records = replay.read_records(args.records)
        for metrics in replay.replay(
            records,
            OpenAICompatProvider(base_url=args.base_url, key_env=args.key_env),
            [args.model],
            {("*", "*"): _length_exact_scorer},
            repeats=args.repeats,
            skips=skips,
        ):
            print(json.dumps(metrics))
            lines += 1
    except (OSError, ValueError) as exc:
        print(f"tickwork: {exc}", file=sys.stderr)
        return 2
    for reason, count in sorted(skips.items()):
        print(f"skipped {count} record(s): {reason}", file=sys.stderr)
    return 0 if lines or skips else 1


def _roles_in(log_dir: str) -> list[str]:
    """Role names with a ``<role>.jsonl`` in *log_dir*, sorted. Read-only."""
    try:
        names = sorted(os.listdir(log_dir))
    except (FileNotFoundError, OSError):
        return []
    return [name[: -len(".jsonl")] for name in names if name.endswith(".jsonl")]


def _rows_for(log_dir: str, role: str) -> list[dict]:
    """All of *role*'s rows. Read-only: a missing log reads as no rows and the
    log directory is never created or written."""
    return EffectLog(log_dir)._read(role)


def print_summary(summary: dict, out=None) -> None:
    """Render ``summarize()``'s object as text. Pure: writes only to *out*."""
    out = out if out is not None else sys.stdout
    effects = summary.get("effects") or {}
    for role in sorted(effects):
        for effect in sorted(effects[role]):
            s = effects[role][effect]
            p50 = "-" if s["p50"] is None else f"{s['p50']}ms"
            p90 = "-" if s["p90"] is None else f"{s['p90']}ms"
            line = (
                f"{role}/{effect}: applied={s['applied']} failed={s['failed']} "
                f"superseded={s['superseded']} pending={s['pending']} "
                f"finished_later={s['finished_later']} p50={p50} p90={p90}"
            )
            print(line, file=out)
            for u in summary.get("unfinished") or []:
                if u["role"] == role and u["effect"] == effect:
                    print(
                        f"  unfinished: {u['item']} {u['tick_id']}/{u['seq']}",
                        file=out,
                    )
    if not effects and not summary.get("unfinished"):
        print("(empty log)", file=out)


def _rows_since(rows: list[dict], cutoff: float) -> list[dict]:
    """Drop whole fold keys whose final status row finished before *cutoff*.

    ``--since`` filters fold keys, not rows: a plan row is written with
    ``finished: None`` and never rewritten, so dropping only old status rows
    would leave the plan row to fold to pending and report long-finished
    work as unfinished. A key ``(item, tick_id, seq)`` — including every
    status row that resumed it — is dropped when its last row in log order
    has a status other than ``pending`` and a numeric ``finished`` earlier
    than the cutoff; keys still pending survive so they stay resumable.
    """
    last: dict[tuple, dict] = {}
    for r in rows:
        last[(r.get("item"), r.get("tick_id"), r.get("seq"))] = r
    stale = {
        key
        for key, r in last.items()
        if r.get("status") != PENDING
        and isinstance(r.get("finished"), (int, float))
        and r["finished"] < cutoff
    }
    return [r for r in rows if (r.get("item"), r.get("tick_id"), r.get("seq")) not in stale]


def _effects(args: argparse.Namespace) -> int:
    """``tickwork effects``: print the effect-log summary. Read-only."""
    roles = args.role
    if not roles:
        roles = _roles_in(args.log_dir)
    cutoff = _since_epoch(args.since) if args.since else None
    all_rows: list[dict] = []
    for role in roles:
        rows = _rows_for(args.log_dir, role)
        if cutoff is not None:
            rows = _rows_since(rows, cutoff)
        all_rows.extend(rows)
    summary = summarize(all_rows) if all_rows else EMPTY_SUMMARY
    if args.json:
        print(json.dumps(summary, sort_keys=True), file=sys.stdout)
    else:
        print_summary(summary)
    return 0


def _since_epoch(iso_date: str) -> float:
    """Epoch seconds for an ISO date (a date-only string is UTC midnight)."""
    date = datetime.date.fromisoformat(iso_date)
    return datetime.datetime(date.year, date.month, date.day, tzinfo=datetime.UTC).timestamp()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tickwork", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check_parser = sub.add_parser("check", help="report items matching zero or 2+ states")
    check_parser.add_argument("states", help="the workflow states file")
    check_parser.add_argument("--items", required=True, help="a JSONL file of items, one per line")
    check_parser.add_argument("--logins", required=True, help="a YAML map of role name to login")
    replay_parser = sub.add_parser(
        "replay",
        help="re-send recorded final transcript records to a model and score the replies",
    )
    replay_parser.add_argument("records", nargs="+", help="JSONL transcript files (finals)")
    replay_parser.add_argument("--model", required=True, help="the model to replay each call against")
    replay_parser.add_argument(
        "--provider", default=_PROVIDER, help=f"the provider adapter (default {_PROVIDER})"
    )
    replay_parser.add_argument("--base-url", required=True, help="the API base URL of the endpoint")
    replay_parser.add_argument(
        "--key-env",
        default="OPENAI_API_KEY",
        help="the env var carrying the API key (the key itself is never logged)",
    )
    replay_parser.add_argument("--repeats", type=int, default=1, help="repeats per record and model")
    effects = sub.add_parser("effects", help="summarize an effect log (read-only)")
    effects.add_argument("log_dir", help="the effects log directory (read-only)")
    effects.add_argument(
        "--role",
        action="append",
        default=None,
        help="role to summarize (repeatable); default: every <role>.jsonl in LOG_DIR",
    )
    effects.add_argument(
        "--since",
        default=None,
        help="ISO date; drop fold keys whose final status row finished before it",
    )
    effects.add_argument("--json", action="store_true", help="print the summary as one JSON object")
    args = parser.parse_args(argv)
    if args.command == "effects":
        return _effects(args)

    if args.command == "replay":
        return _run_replay(args)

    # The evaluation stays inside the guard: workflow_states.load() validates
    # only the `owner` field, so a predicate naming a role absent from `logins`
    # raises ValueError here, and that must exit 2 like the other file errors,
    # not surface as a traceback the shell cannot distinguish from anomalies.
    try:
        logins = _read_logins(args.logins)
        states = workflow_states.load(args.states, set(logins))
        items = _read_items(args.items)
        anomalies = workflow_states.check(states, items, logins)
    except (OSError, ValueError, yaml.YAMLError, json.JSONDecodeError) as exc:
        print(f"tickwork: {exc}", file=sys.stderr)
        return 2

    for anomaly in anomalies:
        print(_format_anomaly(anomaly))
    print(f"{len(items)} items checked, {len(anomalies)} anomalies")
    return 1 if anomalies else 0


if __name__ == "__main__":
    sys.exit(main())
