"""Token-budget helpers.

Claude's 1M-token context window is a hard ceiling. The actual prompt
budget we aim for is 800k tokens so there's room for the response and
for our own rough char-to-token approximation to be off without
breaking the call. These helpers let modules cap payloads BEFORE
sending and surface a clear log signal when they truncate.
"""

from __future__ import annotations

import logging
from typing import List, Tuple, TypeVar


# English text averages about 3.5-4 chars per token under Claude's
# tokenizer. We use 3.5 to err on the conservative (high-token) side.
CHARS_PER_TOKEN = 3.5

# Total context window for Claude models we support.
HARD_TOKEN_LIMIT = 1_000_000

# What we actually aim for as the prompt budget. Leaves headroom for
# response tokens and tokenizer-estimate slop.
SAFE_PROMPT_TOKENS = 800_000
SAFE_PROMPT_CHARS = int(SAFE_PROMPT_TOKENS * CHARS_PER_TOKEN)


log = logging.getLogger(__name__)


def chars_to_tokens(n_chars: int) -> int:
    return int(n_chars / CHARS_PER_TOKEN)


def truncate_to_chars(s: str, max_chars: int = SAFE_PROMPT_CHARS, *, label: str = "") -> Tuple[str, bool]:
    """Trim ``s`` to ``max_chars`` if needed.

    Returns ``(text, was_truncated)``. Logs a warning if truncation
    actually happened, so the operator sees it in Render logs.
    """
    if len(s) <= max_chars:
        return s, False
    if label:
        log.warning(
            "truncate_to_chars: %s payload %d chars > budget %d chars; trimming",
            label,
            len(s),
            max_chars,
        )
    else:
        log.warning(
            "truncate_to_chars: payload %d chars > budget %d chars; trimming",
            len(s),
            max_chars,
        )
    return s[:max_chars], True


T = TypeVar("T")


def cap_items_by_chars(
    items: List[T],
    measure: callable,
    max_chars: int = SAFE_PROMPT_CHARS,
    *,
    label: str = "",
) -> Tuple[List[T], bool]:
    """Take the longest prefix of ``items`` whose combined ``measure(item)``
    chars stays under ``max_chars``. Returns ``(kept, was_capped)``.

    ``measure`` should return the approximate char length the item will
    contribute to the final prompt. Used when serializing fact lists
    where each fact's char weight is independent.
    """
    total = 0
    kept: List[T] = []
    for item in items:
        size = measure(item)
        if total + size > max_chars:
            if label:
                log.warning(
                    "cap_items_by_chars: %s capped at %d/%d items (~%d/%d chars)",
                    label,
                    len(kept),
                    len(items),
                    total,
                    max_chars,
                )
            return kept, True
        kept.append(item)
        total += size
    return kept, False


__all__ = [
    "CHARS_PER_TOKEN",
    "HARD_TOKEN_LIMIT",
    "SAFE_PROMPT_TOKENS",
    "SAFE_PROMPT_CHARS",
    "chars_to_tokens",
    "truncate_to_chars",
    "cap_items_by_chars",
]
