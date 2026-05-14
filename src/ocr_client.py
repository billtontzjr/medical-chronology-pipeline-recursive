"""Google Vision OCR client for extracting text from PDFs.

Ported from the predecessor. The one functional change for
precision-chronology: the saved .txt file includes a literal
``=== PAGE N ===`` marker line between every pair of pages. The page
number is the Vision API's reported page index plus one, matching the
spec in Phase 2. This marker is what the extractor uses to populate
``source_page`` on every ExtractedFact.
"""

from __future__ import annotations

import base64
import gc
import io
import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional

import httpx
from pdf2image import convert_from_path
from PIL import Image


PAGE_MARKER = "=== PAGE {n} ==="


class OCRClient:
    """OCR PDFs one page at a time using Google Cloud Vision."""

    def __init__(self, api_key: str) -> None:
        self.api_key = (api_key or "").strip()
        if not self.api_key:
            raise ValueError("GOOGLE_VISION_API_KEY is not set")
        self.api_url = (
            f"https://vision.googleapis.com/v1/images:annotate?key={self.api_key}"
        )
        self._logger = logging.getLogger(__name__)

    def _image_to_base64(self, image: Image.Image) -> str:
        if image.mode in ("RGBA", "LA", "P"):
            rgb = Image.new("RGB", image.size, (255, 255, 255))
            if image.mode == "P":
                image = image.convert("RGBA")
            rgb.paste(image, mask=image.split()[-1] if image.mode in ("RGBA", "LA") else None)
            image = rgb

        for quality in (95, 85, 75, 65):
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) < 15_000_000:
                return base64.b64encode(data).decode("utf-8")

        for scale in (0.5, 0.4, 0.3, 0.25):
            new_size = (int(image.width * scale), int(image.height * scale))
            resized = image.resize(new_size, Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=85, optimize=True)
            data = buf.getvalue()
            if len(data) < 15_000_000:
                return base64.b64encode(data).decode("utf-8")

        tiny = image.resize((int(image.width * 0.2), int(image.height * 0.2)), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        tiny.save(buf, format="JPEG", quality=60, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("utf-8")

    def _vision_call(self, image_b64: str, timeout: int = 60) -> Dict:
        body = {
            "requests": [
                {
                    "image": {"content": image_b64},
                    "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                }
            ]
        }
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(self.api_url, json=body)
            if resp.status_code != 200:
                return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text}", "text": ""}
            data = resp.json()
            if "responses" not in data or not data["responses"]:
                return {"success": False, "error": "Invalid API response", "text": ""}
            page = data["responses"][0]
            if "error" in page:
                msg = page["error"].get("message", "Unknown error")
                return {"success": False, "error": msg, "text": ""}
            text = ""
            if "fullTextAnnotation" in page:
                text = page["fullTextAnnotation"].get("text", "")
            elif "textAnnotations" in page and page["textAnnotations"]:
                text = page["textAnnotations"][0].get("description", "")
            return {"success": True, "text": text, "error": ""}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc), "text": ""}

    def extract_text(
        self,
        file_path: str,
        timeout: int = 300,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict:
        """OCR a single PDF and return per-page text plus a joined output.

        The joined ``text`` uses ``=== PAGE N ===`` separators so downstream
        code can recover page boundaries by literal string match.
        """
        file_name = Path(file_path).name

        try:
            from pdf2image.pdf2image import pdfinfo_from_path

            info = pdfinfo_from_path(file_path)
            total_pages = int(info.get("Pages", 0))
        except Exception:
            total_pages = 100  # upper bound; loop breaks when pdf2image errors out

        if progress_callback:
            progress_callback(f"Processing {file_name} ({total_pages} pages)")

        per_page_text: List[str] = []
        successful_pages = 0
        actual_pages = 0

        for page_num in range(1, total_pages + 1):
            if progress_callback:
                progress_callback(f"{file_name}: Page {page_num}/{total_pages}")

            try:
                images = convert_from_path(
                    file_path,
                    dpi=150,
                    first_page=page_num,
                    last_page=page_num,
                    fmt="png",
                )
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                if any(token in msg for token in ("out of range", "exceed", "invalid")):
                    break
                if progress_callback:
                    progress_callback(f"Page {page_num} failed: {str(exc)[:100]}")
                continue

            if not images:
                break

            image = images[0]
            try:
                b64 = self._image_to_base64(image)
                result = self._vision_call(b64, timeout=60)
                page_text = result["text"] if result.get("success") else ""
                if page_text.strip():
                    successful_pages += 1
                per_page_text.append(page_text)
                actual_pages = page_num
            finally:
                del images
                del image
                gc.collect()

        if actual_pages == 0:
            return {
                "success": False,
                "file_name": file_name,
                "error": "No text extracted",
                "text": "",
                "confidence": 0.0,
                "page_count": 0,
                "pages": [],
            }

        joined = self._join_pages_with_markers(per_page_text)
        confidence = successful_pages / actual_pages if actual_pages else 0.0

        if progress_callback:
            progress_callback(
                f"{file_name}: Extracted {successful_pages}/{actual_pages} pages"
            )

        return {
            "success": True,
            "file_name": file_name,
            "text": joined,
            "confidence": confidence,
            "page_count": actual_pages,
            "pages": per_page_text,
        }

    @staticmethod
    def _join_pages_with_markers(pages: List[str]) -> str:
        """Join page texts with ``=== PAGE N ===`` separators.

        The marker for page N appears immediately before page N's text.
        Page 1's marker is included, so downstream chunkers can determine
        the page of any chunk by scanning backward to the nearest marker.
        """
        parts: List[str] = []
        for idx, text in enumerate(pages, start=1):
            parts.append(PAGE_MARKER.format(n=idx))
            parts.append(text)
        return "\n".join(parts)

    def save_extracted_text(self, result: Dict, output_dir: str) -> str:
        if not result.get("success"):
            raise RuntimeError(f"Cannot save failed extraction: {result.get('error')}")
        out_name = Path(result["file_name"]).stem + ".txt"
        out_path = Path(output_dir) / out_name
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(result["text"])
        return str(out_path)

    def batch_extract(
        self,
        file_paths: List[str],
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> List[Dict]:
        """Process files sequentially. Simpler than threading; OCR is I/O bound."""
        results: List[Dict] = []
        total = len(file_paths)
        for i, path in enumerate(file_paths):
            if progress_callback:
                progress_callback(f"File {i + 1}/{total}: {Path(path).name}")
            results.append(self.extract_text(path, timeout=300, progress_callback=progress_callback))
            gc.collect()
        return results
