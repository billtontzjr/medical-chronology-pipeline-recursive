"""Phase 3 extractor: chunk OCR text and ask Claude for atomic facts.

This module is the entry point for the anti-hallucination pipeline. It
splits each OCR text file into overlapping chunks, sends each chunk to
Claude with the extraction prompt, validates the returned JSON against
``ChunkExtraction``, and persists one JSON file per chunk under
``data/sessions/<session_id>/extracted_facts/``.

Chunk-level resume: if a chunk's output file already exists on disk we
skip re-calling Claude for it. The pipeline can therefore be interrupted
mid-extraction and resumed without losing prior work.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from pydantic import ValidationError

from src.anthropic_client import AnthropicClient, ModelRefused, ResponseTruncated
from src.prompts.extraction import build_extraction_prompt
from src.schemas import ChunkExtraction, ExtractedFact
from src.session_state import PHASE_EXTRACTION, SessionStore


def _report_progress(session_store, state, phase, current, total, item="") -> None:
    """Persist sub-phase progress to state.json if a state object was passed.

    Best-effort: a progress-write failure must never interrupt a phase.
    Called only from the main thread (single writer to state.json).
    """
    if state is None or session_store is None:
        return
    try:
        session_store.update_phase_progress(state, phase, current, total, item)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("progress write failed", exc_info=True)


CHUNK_SIZE = 8000
CHUNK_OVERLAP = 800

# Chunks are independent and each persists its own JSON file, so the Claude
# calls can run concurrently. The Anthropic client uses a 10-connection pool
# with built-in 429/overload backoff, so 8 workers stays comfortably under it.
# Override with the EXTRACTION_WORKERS env var.
EXTRACTION_WORKERS = max(1, int(os.environ.get("EXTRACTION_WORKERS", "8")))

PAGE_MARK_RE = re.compile(r"^=== PAGE (\d+) ===$", re.MULTILINE)
MD_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class Chunk:
    chunk_id: str
    index: int
    start_char: int
    end_char: int
    text: str


def page_for_offset(text: str, offset: int) -> int:
    """Return the page number whose ``=== PAGE N ===`` marker most recently
    preceded ``offset``. If no marker precedes the offset, returns 1.
    """
    last = 1
    for m in PAGE_MARK_RE.finditer(text):
        if m.start() > offset:
            break
        last = int(m.group(1))
    return last


def chunk_text(
    text: str,
    *,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
    stem: str = "doc",
) -> List[Chunk]:
    """Split text into overlapping chunks, preferring newline boundaries.

    The output chunk IDs are ``{stem}_chunk_{nn}`` zero-padded to two
    digits (room for up to 100 chunks per document). Chunk start/end are
    character offsets into ``text``.
    """
    chunks: List[Chunk] = []
    n = len(text)
    if n == 0:
        return chunks

    pos = 0
    idx = 0
    while pos < n:
        end = min(pos + chunk_size, n)
        # Try to back up to a recent newline so we don't slice mid-sentence.
        if end < n:
            window_start = max(end - 400, pos + 1)
            nl = text.rfind("\n", window_start, end)
            if nl > pos + (chunk_size // 2):
                end = nl
        piece = text[pos:end]
        chunks.append(
            Chunk(
                chunk_id=f"{stem}_chunk_{idx:02d}",
                index=idx,
                start_char=pos,
                end_char=end,
                text=piece,
            )
        )
        if end >= n:
            break
        pos = max(end - overlap, pos + 1)
        idx += 1
    return chunks


def _strip_md_fence(s: str) -> str:
    """Defensive: strip a leading/trailing ``` fence if the model added one."""
    s = s.strip()
    if s.startswith("```"):
        s = MD_FENCE_RE.sub("", s)
    return s.strip()


def _isolate_json_object(s: str) -> str:
    """Return the outermost {...} span of ``s`` if it is wrapped in prose.

    Models occasionally prefix the JSON with a sentence or trail it with a
    remark. Rather than discarding the whole chunk, isolate the object.
    Returns the input unchanged when no braces are found.
    """
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end > start:
        return s[start : end + 1]
    return s


# Extraction JSON budget. Doubled once on truncation before giving up.
EXTRACTION_MAX_TOKENS = 16000
EXTRACTION_MAX_TOKENS_CEILING = 32000


def _stamp_facts(
    parsed: ChunkExtraction,
    source_file_stem: str,
    chunk_id: str,
    page: int,
    chunk_start: int,
    chunk_end: int,
) -> ChunkExtraction:
    """Stamp identity + source_page on each fact, overriding model mistakes.

    The model is told the source_file and chunk_id explicitly but
    sometimes echoes them imperfectly. We re-stamp them, and we set
    ``source_page`` from ``page`` (precomputed by the caller via
    :func:`page_for_offset` while it still had the full text) since the
    model cannot see the page marker reliably outside its chunk.
    """
    fixed: List[ExtractedFact] = []
    for fact in parsed.facts:
        fixed_data = fact.model_dump()
        fixed_data["source_file"] = source_file_stem
        fixed_data["chunk_id"] = chunk_id
        fixed_data["source_page"] = page
        fixed.append(ExtractedFact(**fixed_data))
    return ChunkExtraction(
        source_file=source_file_stem,
        chunk_id=chunk_id,
        chunk_start_char=chunk_start,
        chunk_end_char=chunk_end,
        facts=fixed,
        extraction_notes=parsed.extraction_notes,
    )


def _atomic_write_json(path: Path, payload: Dict, *, max_attempts: int = 3) -> None:
    """Write ``payload`` to ``path`` atomically.

    Production hardening: in ephemeral container environments (e.g. Render
    without a persistent disk) we have seen ``os.replace`` raise
    ``FileNotFoundError`` because the ``.tmp`` disappears between the
    write and the rename. We retry up to ``max_attempts`` times after
    re-ensuring the parent directory exists, then fall back to a direct
    in-place write so a single chunk's persist never kills the whole
    phase. The final fsync makes the write durable before rename.
    """
    log = logging.getLogger(__name__)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, indent=2)

    for attempt in range(1, max_attempts + 1):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass  # not all filesystems support fsync
            os.replace(tmp, path)
            return
        except FileNotFoundError as exc:
            log.warning(
                "Atomic write race on %s (attempt %d/%d): %s",
                path.name,
                attempt,
                max_attempts,
                exc,
            )
            if attempt >= max_attempts:
                # Final fallback: write directly to the target. We lose the
                # atomicity guarantee but we keep the chunk's data instead
                # of dropping it.
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(data)
                return
            time.sleep(0.1 * attempt)


def run_extraction(
    session_store: SessionStore,
    session_id: str,
    anthropic_client: AnthropicClient,
    *,
    state=None,
    model: Optional[str] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> Dict:
    """Run extraction over every OCR text file in the session.

    All pending chunks across every file are fanned out into a single
    thread pool (``EXTRACTION_WORKERS`` wide), so a long tail of small
    one-chunk files no longer runs near-serial. Already-extracted chunks
    are counted and skipped without an API call (resume). When ``state``
    is provided, sub-phase progress is persisted so the UI shows a live
    ``done/total`` counter and a fresh timestamp.

    Returns a summary dict::

        {
            "files_processed": int,
            "chunks_total": int,
            "chunks_extracted": int,   # this run only
            "chunks_skipped": int,     # already-on-disk (resume)
            "chunks_invalid": int,     # unparseable twice; empty facts persisted
            "facts_emitted": int,
        }
    """
    log = logging.getLogger(__name__)
    extracted_dir = session_store.extracted_dir(session_id)
    facts_dir = session_store.extracted_facts_dir(session_id)

    text_files = sorted(p for p in extracted_dir.glob("*.txt") if p.is_file())
    summary = dict(
        files_processed=0,
        chunks_total=0,
        chunks_extracted=0,
        chunks_skipped=0,
        chunks_failed_transient=0,
        chunks_invalid=0,
        facts_emitted=0,
    )

    if progress_callback:
        progress_callback(f"Extraction: scanning {len(text_files)} OCR file(s)…")

    # Build a single global work list of pending chunks across ALL files.
    # source_page is precomputed here (while we still hold the file's text)
    # so workers never need full_text — memory stays bounded to the pending
    # chunk texts rather than every file's full text at once.
    pending: List[Dict] = []  # {"stem", "piece", "page"}
    for text_path in text_files:
        stem = text_path.stem
        with open(text_path, "r", encoding="utf-8") as f:
            full_text = f.read()
        pieces = chunk_text(full_text, chunk_size=chunk_size, overlap=overlap, stem=stem)
        summary["files_processed"] += 1
        summary["chunks_total"] += len(pieces)
        for piece in pieces:
            out_path = facts_dir / f"{piece.chunk_id}.json"
            if out_path.exists():
                summary["chunks_skipped"] += 1
                try:
                    with open(out_path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                    summary["facts_emitted"] += len(existing.get("facts", []))
                except Exception:
                    pass
                continue
            pending.append(
                {"stem": stem, "piece": piece, "page": page_for_offset(full_text, piece.start_char)}
            )

    total_pending = len(pending)

    def _extract_one_chunk(item: Dict) -> Dict[str, int]:
        """Call Claude for one chunk and persist it. Returns count deltas.

        Per-chunk isolation: one bad chunk (transient OS error, network
        blip, malformed model output) must NOT kill the whole phase. The
        chunk's JSON is simply not written, so the next Resume picks it up
        via the skip-if-present check.
        """
        piece: Chunk = item["piece"]
        stem: str = item["stem"]
        out_path = facts_dir / f"{piece.chunk_id}.json"

        def _call_and_parse(prompt: str, max_tokens: int) -> ChunkExtraction:
            """One model call + parse. Doubles the token budget once if the
            output was truncated (a truncated JSON array would otherwise
            silently lose every fact after the cut)."""
            try:
                raw = anthropic_client.complete(prompt, model=model, max_tokens=max_tokens)
            except ResponseTruncated:
                if max_tokens >= EXTRACTION_MAX_TOKENS_CEILING:
                    raise
                log.warning(
                    "Chunk %s truncated at max_tokens=%d; retrying with %d",
                    piece.chunk_id, max_tokens, max_tokens * 2,
                )
                raw = anthropic_client.complete(
                    prompt, model=model, max_tokens=max_tokens * 2
                )
            cleaned = _isolate_json_object(_strip_md_fence(raw))
            return ChunkExtraction.model_validate_json(cleaned)

        try:
            prompt = build_extraction_prompt(
                chunk_text=piece.text,
                source_file=stem,
                chunk_id=piece.chunk_id,
                chunk_start_char=piece.start_char,
                chunk_end_char=piece.end_char,
            )

            parsed: Optional[ChunkExtraction] = None
            last_validation_error: Optional[ValidationError] = None
            # Two attempts: malformed JSON from the model is usually a
            # one-off, and a retry recovers the chunk instead of dropping
            # every fact in it.
            for parse_attempt in range(2):
                try:
                    parsed = _call_and_parse(prompt, EXTRACTION_MAX_TOKENS)
                    break
                except ValidationError as exc:
                    last_validation_error = exc
                    log.warning(
                        "Validation failed for %s (attempt %d/2): %s",
                        piece.chunk_id, parse_attempt + 1, str(exc)[:300],
                    )

            if parsed is None:
                log.error(
                    "Chunk %s produced unparseable output twice; persisting "
                    "empty facts (flagged for manual review)",
                    piece.chunk_id,
                )
                # Persist an empty-facts record so we don't retry forever,
                # but count it so gaps.md can tell the reviewer which source
                # pages were not captured.
                errs = last_validation_error.errors()[:3] if last_validation_error else []
                empty = ChunkExtraction(
                    source_file=stem,
                    chunk_id=piece.chunk_id,
                    chunk_start_char=piece.start_char,
                    chunk_end_char=piece.end_char,
                    facts=[],
                    extraction_notes=f"validation_error: {errs}",
                )
                _atomic_write_json(out_path, empty.model_dump(mode="json"))
                return {"chunks_extracted": 1, "chunks_invalid": 1}

            stamped = _stamp_facts(
                parsed,
                source_file_stem=stem,
                chunk_id=piece.chunk_id,
                page=item["page"],
                chunk_start=piece.start_char,
                chunk_end=piece.end_char,
            )
            _atomic_write_json(out_path, stamped.model_dump(mode="json"))
            return {"chunks_extracted": 1, "facts_emitted": len(stamped.facts)}
        except ModelRefused as exc:
            # Both the chosen model and its fallback declined this chunk.
            # Retrying on resume would just refuse again, so record it
            # (surfaced in gaps.md) rather than deferring forever.
            log.error("Chunk %s refused by model and fallback: %s", piece.chunk_id, exc)
            refused = ChunkExtraction(
                source_file=stem,
                chunk_id=piece.chunk_id,
                chunk_start_char=piece.start_char,
                chunk_end_char=piece.end_char,
                facts=[],
                extraction_notes=f"validation_error: model_refused: {exc}",
            )
            _atomic_write_json(out_path, refused.model_dump(mode="json"))
            return {"chunks_extracted": 1, "chunks_invalid": 1}
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "Transient failure on chunk %s; will retry on next resume",
                piece.chunk_id,
            )
            return {"chunks_failed_transient": 1}

    if progress_callback:
        progress_callback(
            f"Extraction: {total_pending} chunk(s) to extract across "
            f"{summary['files_processed']} file(s) "
            f"({summary['chunks_skipped']} already done) — parallel x{EXTRACTION_WORKERS}"
        )
    _report_progress(session_store, state, PHASE_EXTRACTION, 0, total_pending, "starting")

    # One shared pool over every pending chunk keeps all workers saturated
    # regardless of how the chunks are distributed across files. Progress is
    # reported from this (single) main thread, so state.json has one writer.
    if total_pending:
        completed = 0
        with ThreadPoolExecutor(max_workers=EXTRACTION_WORKERS) as pool:
            futures = [pool.submit(_extract_one_chunk, item) for item in pending]
            for fut in as_completed(futures):
                counts = fut.result()  # worker swallows its own errors
                for key, val in counts.items():
                    summary[key] += val
                completed += 1
                if completed % 10 == 0 or completed == total_pending:
                    if progress_callback:
                        progress_callback(
                            f"Extraction: {completed}/{total_pending} chunks "
                            f"({summary['facts_emitted']} facts so far)"
                        )
                    _report_progress(
                        session_store,
                        state,
                        PHASE_EXTRACTION,
                        completed,
                        total_pending,
                        f"{summary['facts_emitted']} facts",
                    )

    if progress_callback:
        msg = (
            f"Extraction complete: {summary['facts_emitted']} facts from "
            f"{summary['chunks_extracted']} chunk(s) "
            f"(+{summary['chunks_skipped']} resumed)"
        )
        if summary["chunks_failed_transient"]:
            msg += (
                f" — {summary['chunks_failed_transient']} chunk(s) deferred to next resume"
            )
        if summary["chunks_invalid"]:
            msg += (
                f" — {summary['chunks_invalid']} chunk(s) unparseable after retry "
                "(listed in gaps.md for manual review)"
            )
        progress_callback(msg)

    return summary


__all__ = [
    "Chunk",
    "CHUNK_SIZE",
    "CHUNK_OVERLAP",
    "chunk_text",
    "page_for_offset",
    "run_extraction",
]
