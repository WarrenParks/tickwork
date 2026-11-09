"""A stdlib-only :class:`~tickwork.replay.Provider` for OpenAI-compatible APIs.

Speaks any ``/chat/completions`` endpoint: the base URL and the name of the
API-key environment variable are constructor parameters, so nothing about a
deployment is baked in. The key is read from the environment per call and
used only in the request's ``Authorization`` header — it is never logged,
printed, or included in an error string.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any

from tickwork.replay import Reply


class OpenAICompatProvider:
    """Provider for any OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(self, base_url: str, key_env: str = "OPENAI_API_KEY", timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.key_env = key_env
        self.timeout = timeout

    def generate(self, model: str, messages: list[dict]) -> Reply:
        """Send *messages* to *model*; failures come back as a Reply."""
        start = time.monotonic()
        response, usage, finish_reason, error = self._chat(model, messages)
        return {
            "response": response,
            "ms": int((time.monotonic() - start) * 1000),
            "prompt_tokens": _usage_int(usage, "prompt_tokens"),
            "completion_tokens": _usage_int(usage, "completion_tokens"),
            "reasoning_tokens": _usage_int(usage.get("completion_tokens_details") or {}, "reasoning_tokens"),
            "cache_hit_tokens": _usage_int(usage.get("prompt_tokens_details") or {}, "cached_tokens"),
            "finish_reason": finish_reason,
            "error": error,
        }

    def _chat(self, model: str, messages: list[dict]) -> tuple[str | None, Any, str | None, str | None]:
        """One POST; returns (response text, usage, finish_reason, error).

        The key is only ever attached as the Authorization header of the
        request object, which is never str()'d or logged — so no failure
        path can carry it into the returned error string.
        """
        key = os.environ.get(self.key_env)
        if not key:
            return None, {}, None, f"environment variable {self.key_env} is not set"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps({"model": model, "messages": messages}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # one bad call is one errored reply, not a crash
            return None, {}, None, f"{type(exc).__name__}: {exc}"
        if not isinstance(data, dict):
            return None, {}, None, f"malformed response: top level is {type(data).__name__}"
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return None, {}, None, "malformed response: no choices[0] object"
        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        response_text = content if isinstance(content, str) else None
        usage = data.get("usage") or {}
        usage = usage if isinstance(usage, dict) else {}
        return response_text, usage, choice.get("finish_reason"), None


def _usage_int(usage: dict, key: str) -> int | None:
    """The int value of one usage counter, or None when absent/malformed."""
    val = usage.get(key)
    return int(val) if isinstance(val, (int, float)) else None
