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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from pydantic import ValidationError

from src.anthropic_client import AnthropicClient
from src.prompts.extraction import build_extraction_prompt
from src.schemas import ChunkExtraction, ExtractedFact
from src.session_state import SessionStore


CHUNK_SIZE = 8000
CHUNK_OVERLAP = 800

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


def _stamp_facts(
    parsed: ChunkExtraction,
    source_file_stem: str,
    chunk_id: str,
    full_text: str,
    chunk_start: int,
    chunk_end: int,
) -> ChunkExtraction:
    """Stamp identity + source_page on each fact, overriding model mistakes.

    The model is told the source_file and chunk_id explicitly but
    sometimes echoes them imperfectly. We re-stamp them, and we compute
    ``source_page`` from the chunk start offset since the model cannot
    see the page marker reliably outside its chunk.
    """
    page = page_for_offset(full_text, chunk_start)
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


def _atomic_write_json(path: Path, payload: Dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def run_extraction(
    session_store: SessionStore,
    session_id: str,
    anthropic_client: AnthropicClient,
    *,
    model: Optional[str] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> Dict:
    """Run extraction over every OCR text file in the session.

    Returns a summary dict::

        {
            "files_processed": int,
            "chunks_total": int,
            "chunks_extracted": int,   # this run only
            "chunks_skipped": int,     # already-on-disk (resume)
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
        facts_emitted=0,
    )

    if progress_callback:
        progress_callback(f"Extraction: {len(text_files)} OCR file(s) found")

    for text_path in text_files:
        stem = text_path.stem  # e.g. "record_001"
        with open(text_path, "r", encoding="utf-8") as f:
            full_text = f.read()
        pieces = chunk_text(full_text, chunk_size=chunk_size, overlap=overlap, stem=stem)
        summary["files_processed"] += 1
        summary["chunks_total"] += len(pieces)

        for piece in pieces:
            out_path = facts_dir / f"{piece.chunk_id}.json"
            if out_path.exists():
                summary["chunks_skipped"] += 1
                if progress_callback:
                    progress_callback(
                        f"Extraction: skip {piece.chunk_id} (already present)"
                    )
                # Count facts in the existing file
                try:
                    with open(out_path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                    summary["facts_emitted"] += len(existing.get("facts", []))
                except Exception:
                    pass
                continue

            if progress_callback:
                progress_callback(
                    f"Extraction: {piece.chunk_id} (chars {piece.start_char}-{piece.end_char})"
                )

            prompt = build_extraction_prompt(
                chunk_text=piece.text,
                source_file=stem,
                chunk_id=piece.chunk_id,
                chunk_start_char=piece.start_char,
                chunk_end_char=piece.end_char,
            )
            raw = anthropic_client.complete(prompt, model=model, max_tokens=8000)
            cleaned = _strip_md_fence(raw)

            try:
                parsed = ChunkExtraction.model_validate_json(cleaned)
            except ValidationError as exc:
                log.error("Validation failed for %s: %s", piece.chunk_id, exc)
                # Persist an empty-facts record so we don't retry forever.
                empty = ChunkExtraction(
                    source_file=stem,
                    chunk_id=piece.chunk_id,
                    chunk_start_char=piece.start_char,
                    chunk_end_char=piece.end_char,
                    facts=[],
                    extraction_notes=f"validation_error: {exc.errors()[:3]}",
                )
                _atomic_write_json(out_path, empty.model_dump(mode="json"))
                summary["chunks_extracted"] += 1
                continue

            stamped = _stamp_facts(
                parsed,
                source_file_stem=stem,
                chunk_id=piece.chunk_id,
                full_text=full_text,
                chunk_start=piece.start_char,
                chunk_end=piece.end_char,
            )
            _atomic_write_json(out_path, stamped.model_dump(mode="json"))
            summary["chunks_extracted"] += 1
            summary["facts_emitted"] += len(stamped.facts)

    if progress_callback:
        progress_callback(
            f"Extraction complete: {summary['facts_emitted']} facts from "
            f"{summary['chunks_extracted']} chunk(s) "
            f"(+{summary['chunks_skipped']} resumed)"
        )

    return summary


__all__ = [
    "Chunk",
    "CHUNK_SIZE",
    "CHUNK_OVERLAP",
    "chunk_text",
    "page_for_offset",
    "run_extraction",
]
