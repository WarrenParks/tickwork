"""Generic replay of recorded LLM calls.

The instance-neutral core of the offline replay: read the final transcript
records (the ``final: true`` JSONL records carrying the
complete ``messages`` array), send each ``messages`` array to a model
through a caller-supplied :class:`Provider`, deep-merge a variant overlay,
and score every reply into one metrics dict per (record, model, repeat).
The module imports nothing beyond the standard library and names no
deployment: providers and scorers are parameters, never imports.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, TypedDict


class Reply(TypedDict):
    """One model reply plus the per-call facts a metrics line is built from."""

    response: str | None
    ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    reasoning_tokens: int | None
    cache_hit_tokens: int | None
    finish_reason: str | None
    error: str | None


class Provider(Protocol):
    """Anything that can send a recorded ``messages`` array to a model.

    Implementations surface call failures as the ``error`` field of a
    :class:`Reply` (never raised), so one bad call skips into the metrics
    instead of aborting the run.
    """

    def generate(self, model: str, messages: list[dict]) -> Reply: ...


def read_records(paths: Iterable[str | Path]) -> Iterator[dict]:
    """Yield the final transcript records of *paths* (JSONL files).

    A final record (the transcript writer marks it ``final: true``) carries
    the call's complete ``messages`` array; every other line is skipped:
    blank lines, lines JSON cannot parse, non-dict
    lines, intermediate (non-final) records, and final records without a
    usable ``messages`` array. Skipping, not failing: a transcript
    legitimately mixes records, and only the finals replay.
    """
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(rec, dict)
                    and rec.get("final") is True
                    and isinstance(rec.get("messages"), list)
                    and rec.get("messages")
                ):
                    yield rec


def deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge ``overlay`` into ``base`` (returns a new dict).

    Nested mappings merge key by key; every other value (scalars, lists)
    is replaced wholesale by the overlay's value.
    """
    out = dict(base)
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def replay(
    records: Iterable[dict],
    provider: Provider,
    models: Sequence[str],
    scorers: Mapping[tuple[str, str], Any],
    repeats: int = 1,
    skips: dict[str, int] | None = None,
) -> Iterator[dict]:
    """Replay every (record, model, repeat) and yield one metrics dict each.

    ``scorers`` maps a ``(role, stage)`` pair — or the wildcard
    ``("*", "*")`` fallback — to a ``(record, reply) -> dict`` function whose
    result is merged into that record's metrics line. A record with no
    matching scorer is skipped and counted in ``skips`` under a readable
    reason when the caller passes a mapping. A provider failure arrives as
    the reply's ``error`` field and flows into the metrics line; replay
    itself never raises for one bad call.
    """
    for rec in records:
        role, stage = rec.get("role"), rec.get("stage")
        scorer = scorers.get((role, stage), scorers.get(("*", "*")))
        if scorer is None:
            if skips is not None:
                reason = f"no scorer for {role}/{stage}"
                skips[reason] = skips.get(reason, 0) + 1
            continue
        for model in models:
            for repeat in range(max(repeats, 0)):
                reply = provider.generate(model, rec["messages"])
                metrics = {
                    "role": role,
                    "stage": stage,
                    "repo": rec.get("repo"),
                    "issue": rec.get("issue"),
                    "model": model,
                    "repeat": repeat,
                    "ms": reply.get("ms"),
                    "response": reply.get("response"),
                    "prompt_tokens": reply.get("prompt_tokens"),
                    "completion_tokens": reply.get("completion_tokens"),
                    "reasoning_tokens": reply.get("reasoning_tokens"),
                    "cache_hit_tokens": reply.get("cache_hit_tokens"),
                    "finish_reason": reply.get("finish_reason"),
                    "error": reply.get("error"),
                }
                metrics.update(scorer(rec, reply))
                yield metrics
