"""Loader and validator for a workflow states file.

A states file maps each workflow state to a predicate (``when``), a board
bucket (``board``) and an owner (``owner``: a role name or ``human``). This
module knows nothing about any instance: the caller supplies the path and the
set of allowed owner names. It names no role and has no default path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

HUMAN = "human"

# The predicate vocabulary a `when` block may use, at any nesting depth.
PREDICATE_KEYS = frozenset(
    {
        "issue.state",
        "issue.assignee",
        "issue.dependencies",
        "pr",
        "pr.state",
        "pr.assignee",
        "pr.requested_reviewers",
        "pr.reviewed_on_head",
        "pr.approved_on_head",
        "any",
        "not",
    }
)

VALID_BOARDS = frozenset(
    {
        "queued",
        "in_progress",
        "waiting_on_human",
        "blocked",
        "done",
        "failed",
        "intake",
    }
)

_STATE_FIELDS = frozenset({"when", "board", "owner", "override", "terminal"})


def load(path: str | Path, roles: set[str]) -> dict[str, dict[str, Any]]:
    """Read and validate the states file at *path*.

    *roles* is the set of allowed role-name owners; ``human`` is always allowed.

    Returns ``{state name: {when, board, owner, override, terminal}}`` in file
    order (``override``/``terminal`` default to ``False``; a ``terminal`` state
    may omit ``owner``).

    Raises ``ValueError`` naming the state and the offending field on any
    violation.
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("states"), dict):
        raise ValueError(f"{path}: top level must be a mapping with a `states` block")
    allowed_owners = set(roles) | {HUMAN}
    states: dict[str, dict[str, Any]] = {}
    for name, spec in data["states"].items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"states: state name {name!r} must be a non-empty string")
        if not isinstance(spec, dict):
            raise ValueError(f"{name}: state must be a mapping, got {type(spec).__name__}")
        unknown = sorted(set(spec) - _STATE_FIELDS)
        if unknown:
            raise ValueError(f"{name}: unknown field(s) {unknown}")
        when = spec.get("when")
        if not isinstance(when, dict) or not when:
            raise ValueError(f"{name}: `when` must be a non-empty mapping")
        _check_predicates(name, when)
        board = spec.get("board")
        if board not in VALID_BOARDS:
            raise ValueError(f"{name}: `board` {board!r} not in {sorted(VALID_BOARDS)}")
        for field in ("override", "terminal"):
            value = spec.get(field, False)
            if not isinstance(value, bool):
                raise ValueError(f"{name}: `{field}` must be a boolean, got {value!r}")
        owner = spec.get("owner")
        if owner is None:
            if not spec.get("terminal", False):
                raise ValueError(f"{name}: missing `owner`")
        elif owner not in allowed_owners:
            raise ValueError(f"{name}: `owner` {owner!r} not in {sorted(allowed_owners)}")
        states[name] = {
            "when": when,
            "board": board,
            "owner": owner,
            "override": spec.get("override", False),
            "terminal": spec.get("terminal", False),
        }
    return states


def _check_predicates(state: str, pred: dict[str, Any]) -> None:
    """Validate predicate keys at any depth of a `when` block."""
    for key, value in pred.items():
        if key not in PREDICATE_KEYS:
            raise ValueError(f"{state}: `when` has unknown predicate key {key!r}")
        if key == "any":
            if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
                raise ValueError(f"{state}: `when.any` must be a list of mappings")
            for sub in value:
                _check_predicates(state, sub)
        elif key == "not":
            if not isinstance(value, dict):
                raise ValueError(f"{state}: `when.not` must be a mapping")
            _check_predicates(state, value)


# ─── predicate evaluation ─────────────────────────────────────────────────────

# Predicate leaf → the field of the item dict it reads. Assignee predicates
# name a ROLE, or the value `none` (`issue.assignee` only) meaning an empty
# assignees list; role names resolve through the caller-supplied `logins`.
_ITEM_KEYS = {
    "assignee": "assignees",
    "requested_reviewers": "requested_reviewers",
    "state": "state",
    "dependencies": "open_dependencies",
    "reviewed_on_head": "reviewed_on_head",
    "approved_on_head": "approved_on_head",
}


def evaluate(states: dict[str, dict[str, Any]], item: dict[str, Any], logins: dict[str, str]) -> list[str]:
    """Return the names of all states whose `when` predicate matches *item*.

    Override states are tested first, in file order, and the first match wins
    alone. Otherwise the result is every matching non-override state.

    *item* is a plain dict built from adapter data: ``repo``, ``issue``
    (``number``/``state``/``assignees``/``open_dependencies``) and ``pr``
    (``None``, or ``number``/``state``/``assignees``/``requested_reviewers``/
    ``reviewed_on_head``/``approved_on_head``). Role names in predicates
    resolve to logins through *logins*; this module never looks logins up
    itself. Well-formed items resolve to exactly one state; zero or 2+ is an
    anomaly that :func:`check` reports.
    """
    for name, spec in states.items():
        if spec["override"] and _match(spec["when"], item, logins):
            return [name]
    return [
        name for name, spec in states.items() if not spec["override"] and _match(spec["when"], item, logins)
    ]


def check(
    states: dict[str, dict[str, Any]],
    items: list[dict[str, Any]],
    logins: dict[str, str],
) -> list[dict]:
    """Report every *item* that matches zero or 2+ states.

    Returns one anomaly ``{repo, issue, pr, matched: [...]}`` per mis-mapped
    item, in items order: an item matched by nothing is owned by nobody, one
    matched twice is contested between owners.
    """
    anomalies: list[dict] = []
    for item in items:
        matched = evaluate(states, item, logins)
        if len(matched) != 1:
            anomalies.append(
                {
                    "repo": item.get("repo"),
                    "issue": (item.get("issue") or {}).get("number"),
                    "pr": (item.get("pr") or {}).get("number"),
                    "matched": matched,
                }
            )
    return anomalies


def _match(pred: dict[str, Any], item: dict[str, Any], logins: dict[str, str]) -> bool:
    return all(_leaf(key, value, item, logins) for key, value in pred.items())


def _leaf(key: str, expected: Any, item: dict[str, Any], logins: dict[str, str]) -> bool:
    if key == "any":
        return any(_match(sub, item, logins) for sub in expected)
    if key == "not":
        return not _match(expected, item, logins)
    section, _, attr = key.partition(".")
    if not attr:
        # A bare section name (`pr`) tests presence: the only use is `pr: none`.
        has = item.get("pr") is not None
        return has if expected != "none" else not has
    obj = item.get(section) or {}
    if attr == "assignee" or attr == "requested_reviewers":
        if not isinstance(expected, str):
            return False
        if expected == "none":
            # `issue.assignee: none` tests an empty assignees list, in the
            # way `pr: none` tests PR absence. A role name is never `none`.
            if attr != "assignee":
                return False
            return not (obj.get(_ITEM_KEYS[attr]) or [])
        return _role_login(expected, logins, key) in (obj.get(_ITEM_KEYS[attr]) or [])
    if attr == "state":
        return obj.get("state") == expected
    if attr == "dependencies":
        truth = bool(obj.get("open_dependencies"))
        return truth if expected == "open" else not truth
    if attr in ("reviewed_on_head", "approved_on_head"):
        return bool(obj.get(_ITEM_KEYS[attr])) == _as_bool(expected)
    return False  # validated files cannot reach an unknown leaf


def _role_login(role: str, logins: dict[str, str], key: str) -> str:
    try:
        return logins[role]
    except KeyError:
        raise ValueError(f"`{key}`: role {role!r} not in the caller-supplied `logins`") from None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes"}
