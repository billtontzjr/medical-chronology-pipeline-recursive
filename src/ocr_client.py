"""Google Vision OCR client for extracting text from PDFs."""

import base64
import io
import gc
import os
import tempfile
import subprocess
import re
from pathlib import Path
from typing import Dict, List, Callable, Optional
import httpx
from pdf2image import convert_from_path
from PIL import Image
from .blank_pages import assess_blank


class OCRClient:
    """Handle OCR processing using Google Cloud Vision API."""

    protocol = "vision-layout-300-v1"

    def inspect_blank_page(self, file_path, page):
        """Inspect only a saved no-text page; no OCR call or source mutation."""
        images = []
        try:
            images = convert_from_path(file_path, dpi=300, first_page=page, last_page=page, fmt='png')
            return assess_blank(images[0]) if len(images) == 1 else {'blank': False}
        except Exception:
            return {'blank': False, 'error': 'Original page could not be assessed'}
        finally:
            for image in images:
                image.close()

    def __init__(self, api_key: str):
        """
        Initialize OCR client.

        Args:
            api_key: Google Cloud Vision API key
        """
        self.api_key = api_key
        self.api_url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"

    def _image_to_base64(self, image: Image.Image) -> str:
        """Prefer lossless pages and respect Vision's JSON request size limit.

        Never silently shrink a hard-to-read page. Oversize pages fail explicitly
        so the team can inspect them instead of receiving degraded recognition.
        """
        if image.mode not in ("L", "RGB"):
            image = image.convert("RGB")
        for fmt, options in (("PNG", {"optimize": True}), ("JPEG", {"quality": 95, "optimize": True})):
            buffered = io.BytesIO()
            image.save(buffered, format=fmt, **options)
            encoded = base64.b64encode(buffered.getvalue()).decode("ascii")
            if len(encoded) < 9_000_000:
                return encoded
        raise ValueError("Page exceeds OCR request limit at preserved resolution; inspect or split the page.")

    def _extract_text_from_image(self, image_base64: str, timeout: int = 60) -> Dict:
        """
        Extract text from a single image using Google Vision API.

        Args:
            image_base64: Base64 encoded image
            timeout: Request timeout

        Returns:
            Dictionary with text and error info
        """
        request_body = {
            "requests": [
                {
                    "image": {
                        "content": image_base64
                    },
                    "features": [
                        {
                            "type": "DOCUMENT_TEXT_DETECTION"
                        }
                    ]
                }
            ]
        }

        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(self.api_url, json=request_body)

            if response.status_code != 200:
                return {
                    'success': False,
                    'error': f"HTTP {response.status_code}: {response.text}",
                    'text': ''
                }

            data = response.json()

            if "responses" not in data or not data["responses"]:
                return {
                    'success': False,
                    'error': "Invalid API response",
                    'text': ''
                }

            response_data = data["responses"][0]

            if "error" in response_data:
                import logging
                logger = logging.getLogger(__name__)

                error_msg = response_data["error"].get("message", "Unknown error")
                error_code = response_data["error"].get("code", "no_code")

                # Log full error details
                logger.error(f"Vision API error: code={error_code}, message={error_msg}")
                logger.error(f"Full error object: {response_data['error']}")

                # Add context for common errors
                if "Bad image data" in error_msg or "image" in error_msg.lower():
                    error_msg = f"Vision API error: {error_msg} (image may be too large or invalid format)"

                return {
                    'success': False,
                    'error': error_msg,
                    'text': ''
                }

            text = ""
            if "fullTextAnnotation" in response_data:
                text = response_data["fullTextAnnotation"].get("text", "")
            elif "textAnnotations" in response_data and response_data["textAnnotations"]:
                text = response_data["textAnnotations"][0].get("description", "")

            annotation = response_data.get('fullTextAnnotation', {})
            words = [w for p in annotation.get('pages', []) for b in p.get('blocks', []) for para in b.get('paragraphs', []) for w in para.get('words', [])]
            confidences = [w['confidence'] for w in words if isinstance(w.get('confidence'), (float, int))]
            return {
                'success': True,
                'text': text,
                'layout': annotation.get('pages', []),
                'word_confidence': sum(confidences) / len(confidences) if confidences else None,
                'error': ''
            }

        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'text': ''
            }

    def extract_text(self, file_path: str, timeout: int = 300,
                    progress_callback: Optional[Callable[[str], None]] = None) -> Dict:
        """
        Extract text from a PDF file using Google Vision OCR.
        Memory-optimized: processes one page at a time.

        Args:
            file_path: Path to the PDF file
            timeout: Timeout in seconds (default 5 minutes)
            progress_callback: Optional callback for progress updates

        Returns:
            Dictionary with extraction results
        """
        try:
            file_name = Path(file_path).name

            # Get page count first (without loading all pages)
            try:
                from pdf2image.pdf2image import pdfinfo_from_path
                info = pdfinfo_from_path(file_path)
                total_pages = info.get('Pages', 0)
            except Exception as e:
                # Fallback: convert first page to get count
                try:
                    test_images = convert_from_path(file_path, dpi=72, first_page=1, last_page=1)
                    # Try to get all pages with low DPI just to count
                    all_images = convert_from_path(file_path, dpi=72)
                    total_pages = len(all_images)
                    del all_images
                    del test_images
                    gc.collect()
                except Exception as exc:
                    raise RuntimeError("Could not establish the PDF page count; extraction needs review") from exc
            if not isinstance(total_pages, int) or total_pages < 1:
                raise RuntimeError("PDF page count is missing or invalid")

            if progress_callback:
                progress_callback(f"📄 Processing {file_name} ({total_pages} pages)")

            # Process each page individually (memory-efficient)
            all_text = []
            successful_pages = 0
            page_results = []

            for page_num in range(1, total_pages + 1):
                if progress_callback:
                    progress_callback(f"📄 {file_name}: Page {page_num}/{total_pages}")

                try:
                    # Convert only ONE page at a time
                    # Preserve fine print and handwriting at a 300 DPI baseline
                    images = convert_from_path(
                        file_path,
                        dpi=300,
                        first_page=page_num,
                        last_page=page_num,
                        fmt='png'
                    )

                    if not images:
                        raise RuntimeError("No image returned for an expected PDF page")

                    image = images[0]

                    # Convert image to base64
                    image_base64 = self._image_to_base64(image)

                    # Extract text from this page
                    result = self._extract_text_from_image(image_base64, timeout=60)

                    attempts = [{"dpi": 300, "word_confidence": result.get("word_confidence"), "success": result["success"]}]
                    # Retry only genuinely difficult pages, never whole packets.
                    weak = result["success"] and (not result["text"].strip() or
                            (result.get("word_confidence") is not None and result["word_confidence"] < .80))
                    histogram = image.convert("L").histogram()
                    blank = sum(histogram[:245]) == 0
                    if weak and not blank:
                        sharper = None
                        try:
                            sharper = convert_from_path(file_path, dpi=400, first_page=page_num, last_page=page_num, fmt="png")
                            retry = self._extract_text_from_image(self._image_to_base64(sharper[0]), timeout=60)
                            attempts.append({"dpi": 400, "word_confidence": retry.get("word_confidence"), "success": retry["success"]})
                            if retry["success"] and retry["text"].strip() and (not result["text"].strip() or
                                    (retry.get("word_confidence") or 0) > (result.get("word_confidence") or 0)):
                                result = retry
                        except Exception:
                            attempts.append({"dpi": 400, "success": False, "error": "Sharper-page retry failed; first extraction retained"})
                        finally:
                            del sharper
                    detail = {"page": page_num, "attempts": attempts, "word_confidence": result.get("word_confidence"),
                              "layout": result.get("layout", []), "method": "google_vision"}
                    if result['success'] and result['text'].strip():
                        # Native text preserves column association only when its words
                        # agree with independent image recognition. Keep both versions.
                        native = native_page_text(file_path, page_num)
                        detail["native_text"] = native
                        detail["vision_text"] = result["text"]
                        layout_text = spatial_text(result.get("layout", []))
                        chosen = layout_text or result["text"]
                        if native and native_agrees(native, result["text"]):
                            chosen = native
                            detail["method"] = "native_text_verified_by_vision"
                        all_text.append(f"=== SOURCE PDF PAGE {page_num} ===\n{chosen}")
                        successful_pages += 1
                        page_results.append({**detail, 'status': 'text'})
                    elif result['success'] and blank:
                        all_text.append(f"=== SOURCE PDF PAGE {page_num} ===\n[Blank page: no visible marks detected]")
                        page_results.append({**detail, 'status': 'blank', 'blank_method': 'all_pixels_at_least_245'})
                    elif result['success']:
                        page_results.append({**detail, 'status': 'no_text'})
                    else:
                        page_results.append({**detail, 'status': 'error', 'error': 'OCR service did not return a successful result'})

                    # Clear memory immediately after processing this page
                    del images
                    del image
                    del image_base64
                    gc.collect()

                except Exception as e:
                    page_results.append({'page': page_num, 'status': 'error',
                                         'error': 'Page conversion or extraction failed'})
                    if progress_callback:
                        progress_callback(f"⚠️ Page {page_num} could not be extracted; recorded for review")
                    continue

            # Combine all pages
            full_text = "\n\n".join(all_text)

            # Compatibility field is page coverage, not recognition accuracy.
            confidence = successful_pages / total_pages if total_pages > 0 else 0.0

            if not full_text.strip():
                return {
                    'success': False,
                    'file_name': file_name,
                    'error': f"No text extracted from {total_pages} pages",
                    'text': '',
                    'confidence': 0.0,
                    'source_path': str(Path(file_path)),
                    'page_count': total_pages,
                    'page_results': page_results
                }

            if progress_callback:
                icon = "✅" if successful_pages == total_pages else "⚠️"
                progress_callback(f"{icon} {file_name}: Text extracted from {successful_pages}/{total_pages} pages"
                                  + (" — remaining pages need review" if successful_pages < total_pages else ""))

            return {
                'success': True,
                'file_name': file_name,
                'source_path': str(Path(file_path)),
                'text': full_text,
                'confidence': confidence,
                'confidence_kind': 'page_coverage',
                'ocr_protocol': 'vision-layout-300-v1',
                'page_count': total_pages,
                'page_results': page_results
            }

        except Exception as e:
            return {
                'success': False,
                'file_name': Path(file_path).name,
                'source_path': str(Path(file_path)),
                'error': str(e),
                'text': '',
                'confidence': 0.0
            }

    async def batch_extract(self, file_paths: List[str],
                           max_concurrent: int = 1,
                           progress_callback: Optional[Callable[[str], None]] = None) -> List[Dict]:
        """
        Extract text from multiple PDF files SYNCHRONOUSLY.
        Simplified to avoid thread pool issues.

        Args:
            file_paths: List of paths to PDF files
            max_concurrent: Ignored (kept for compatibility)
            progress_callback: Optional callback for progress updates

        Returns:
            List of extraction results
        """
        results = []
        total_files = len(file_paths)

        # Process files one by one synchronously
        for i, file_path in enumerate(file_paths):
            if progress_callback:
                progress_callback(f"📁 Processing file {i+1}/{total_files}: {Path(file_path).name}")

            # Run OCR synchronously (no threads, no async complexity)
            result = self.extract_text(file_path, timeout=300, progress_callback=progress_callback)
            results.append(result)

            # Force garbage collection after each file
            gc.collect()

        return results

    def save_extracted_text(
        self,
        result: Dict,
        output_dir: str,
        base_input_dir: Optional[str] = None,
    ) -> str:
        """
        Save extracted text to a file.

        Args:
            result: OCR extraction result dictionary
            output_dir: Directory to save the text file

        Returns:
            Path to saved text file
        """
        if not result['success']:
            raise Exception(f"Cannot save failed extraction: {result.get('error')}")

        source_path = Path(result.get('source_path') or result['file_name'])
        if base_input_dir:
            try:
                rel_path = source_path.resolve().relative_to(Path(base_input_dir).resolve())
            except ValueError:
                rel_path = Path(result['file_name'])
        else:
            rel_path = Path(result['file_name'])

        output_path = Path(output_dir) / rel_path.with_suffix('.txt')

        # Ensure output directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomically publish text only after the complete file has been written.
        fd, name = tempfile.mkstemp(prefix=output_path.name+'.', suffix='.tmp', dir=output_path.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(result['text'])
            temp.replace(output_path)
        finally:
            temp.unlink(missing_ok=True)

        return str(output_path)


def native_page_text(path, page):
    try:
        result = subprocess.run(["pdftotext", "-f", str(page), "-l", str(page), "-layout", str(path), "-"],
                                capture_output=True, text=True, timeout=30, check=True)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def native_agrees(native, vision):
    from collections import Counter
    a, b = Counter(re.findall(r"\w+", native.casefold())), Counter(re.findall(r"\w+", vision.casefold()))
    overlap = sum((a & b).values())
    return min(sum(a.values()), sum(b.values())) >= 40 and overlap / max(sum(a.values()), sum(b.values()), 1) >= .97


def spatial_text(pages):
    """Recover header rows across Vision blocks, preserve body column blocks.

    Geometry is retained alongside both raw and chosen text. Wide column gaps
    become line breaks, so a right-column date field cannot attach to a left-
    column patient field. Only the top quarter is joined across blocks.
    """
    def render(words):
        rows = []
        for word in words:
            vertices = word.get("boundingBox", {}).get("vertices", [])
            if len(vertices) != 4:
                return None
            x = min(v.get("x", 0) for v in vertices)
            end = max(v.get("x", 0) for v in vertices)
            y = sum(v.get("y", 0) for v in vertices) / 4
            height = max(v.get("y", 0) for v in vertices) - min(v.get("y", 0) for v in vertices)
            text = "".join(s.get("text", "") for s in word.get("symbols", []))
            row = next((r for r in rows if abs(r[0] - y) <= max(2, height * .4)), None)
            if row is None:
                row = [y, []]
                rows.append(row)
            row[1].append((x, end, height, text))
        output = []
        for _, row in sorted(rows):
            line, previous = "", None
            for x, end, height, text in sorted(row):
                if previous is not None and x - previous > max(30, height * 3):
                    output.append(line)
                    line = ""
                line += (" " if line else "") + text
                previous = end
            output.append(line)
        return output
    output = []
    for page in pages:
        header, body = [], []
        threshold = page.get("height", 0) * .25
        for block in page.get("blocks", []):
            rest = []
            for paragraph in block.get("paragraphs", []):
                for word in paragraph.get("words", []):
                    vertices = word.get("boundingBox", {}).get("vertices", [])
                    if len(vertices) != 4:
                        return ""
                    (header if max(v.get("y", 0) for v in vertices) <= threshold else rest).append(word)
            if rest:
                body.append(rest)
        for words in [header, *body]:
            rendered = render(words)
            if rendered is None:
                return ""
            output.extend(rendered)
    return "\n".join(output)
