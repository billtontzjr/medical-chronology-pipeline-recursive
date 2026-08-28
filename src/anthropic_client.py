"""Shared Anthropic API wrapper.

Ported from the predecessor's ChronologyAgent._call_api_with_retry. The
client enforces two invariants that are load-bearing for the rest of the
pipeline:

* Deterministic-leaning output: temperature=0 is sent to models that
  still support sampling parameters; newer models (Opus 4.7+, Claude 5
  family) reject temperature and use reasoning mode instead.
* Per-call model selection. The extraction stage and the assembly stage
  may use different models. Default for both is claude-opus-5; the
  ANTHROPIC_MODEL env var overrides the default.

Exponential backoff with jitter handles overload (HTTP 500 with
"overloaded" message) and rate limit (HTTP 429) errors. Other API errors
propagate immediately.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

try:
    from anthropic import Anthropic, APIError, APIStatusError
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError(
        "anthropic is required. Run: pip install -r requirements.txt"
    ) from exc


DEFAULT_MODEL = "claude-opus-5"

# Models that still accept sampling parameters (temperature). Opus 4.7+ and
# the Claude 5 family reject temperature with a 400 error; reasoning mode
# replaces it as the consistency mechanism on those models.
_TEMPERATURE_SUPPORTED_PREFIXES = (
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-5",
    "claude-opus-4-6",
    "claude-haiku",
    "claude-3",
)


class AnthropicClient:
    """Thin wrapper around the Anthropic SDK with retry and model selection."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
        connect_timeout: float = 60.0,
        read_timeout: float = 300.0,
    ) -> None:
        api_key = (api_key or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")

        self.default_model = (
            default_model or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
        )

        # A plain float timeout is accepted by every SDK generation. Passing
        # a custom httpx.Client broke when the 1.x SDK moved to httpx2
        # ("Expected an instance of httpx2.Client"), so we let the SDK own
        # its HTTP transport.
        self._client = Anthropic(
            api_key=api_key,
            timeout=read_timeout,
            max_retries=5,
        )
        self._logger = logging.getLogger(__name__)

    def complete(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        max_tokens: int = 8000,
        max_retries: int = 5,
        base_delay: float = 2.0,
        system: Optional[str] = None,
    ) -> str:
        """Call Claude with a single user prompt and return its text.

        Args:
            prompt: User-role content sent to the model.
            model: Override the default model for this call.
            max_tokens: Maximum tokens in the completion.
            max_retries: How many times to retry on overload/rate limit.
            base_delay: Initial backoff in seconds; doubles on each retry.
            system: Optional system prompt.

        Returns:
            The model's text response, stripped.

        Raises:
            RuntimeError: If all retries are exhausted.
            anthropic.APIError: For non-retryable API errors.
        """
        chosen_model = model or self.default_model

        for attempt in range(max_retries):
            try:
                kwargs = dict(
                    model=chosen_model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                # temperature=0 only on models that still support sampling
                # params; newer models (Opus 4.7+, Claude 5 family) return
                # a 400 if temperature is present.
                if chosen_model.startswith(_TEMPERATURE_SUPPORTED_PREFIXES):
                    kwargs["temperature"] = 0
                if system is not None:
                    kwargs["system"] = system
                response = self._client.messages.create(**kwargs)
                # Newer models may include thinking blocks in content;
                # extract only the text blocks.
                text_parts = [
                    block.text
                    for block in response.content
                    if getattr(block, "type", None) == "text"
                ]
                return "\n".join(text_parts).strip()

            except (APIError, APIStatusError) as exc:
                msg = str(exc).lower()
                is_overload = "overload" in msg or (
                    "500" in msg and "api_error" in msg
                )
                is_rate_limit = "429" in msg or "rate" in msg
                if not (is_overload or is_rate_limit):
                    self._logger.error("Non-retryable Anthropic error: %s", exc)
                    raise

                if attempt >= max_retries - 1:
                    self._logger.error(
                        "Anthropic call failed after %d retries: %s",
                        max_retries,
                        exc,
                    )
                    raise RuntimeError(
                        f"Anthropic call failed after {max_retries} retries: {exc}"
                    ) from exc

                delay = base_delay * (2 ** attempt) + (time.time() % 1)
                kind = "overload" if is_overload else "rate-limit"
                self._logger.warning(
                    "Anthropic %s on attempt %d/%d; sleeping %.1fs",
                    kind,
                    attempt + 1,
                    max_retries,
                    delay,
                )
                time.sleep(delay)

        raise RuntimeError(f"Anthropic call failed after {max_retries} retries")
