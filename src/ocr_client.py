"""Google Vision OCR client for extracting text from PDFs."""

import base64
import io
import gc
from pathlib import Path
from typing import Dict, List, Callable, Optional
import asyncio
from concurrent.futures import ThreadPoolExecutor
import httpx
from pdf2image import convert_from_path
from PIL import Image


class OCRClient:
    """Handle OCR processing using Google Cloud Vision API."""

    def __init__(self, api_key: str):
        """
        Initialize OCR client.

        Args:
            api_key: Google Cloud Vision API key
        """
        self.api_key = api_key
        self.api_url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"

    def _image_to_base64(self, image: Image.Image) -> str:
        """
        Convert PIL Image to base64 string with size optimization.

        Uses JPEG format with quality control to stay under Google Vision's 20MB limit.
        """
        import logging
        logger = logging.getLogger(__name__)

        logger.info(f"Image to encode: {image.size[0]}x{image.size[1]} pixels, mode={image.mode}")

        # Convert RGBA to RGB if needed (JPEG doesn't support transparency)
        if image.mode in ('RGBA', 'LA', 'P'):
            rgb_image = Image.new('RGB', image.size, (255, 255, 255))
            if image.mode == 'P':
                image = image.convert('RGBA')
            rgb_image.paste(image, mask=image.split()[-1] if image.mode in ('RGBA', 'LA') else None)
            image = rgb_image
            logger.info(f"Converted to RGB mode")

        # Try high quality first, reduce if too large
        for quality in [95, 85, 75, 65]:
            buffered = io.BytesIO()
            image.save(buffered, format="JPEG", quality=quality, optimize=True)
            image_bytes = buffered.getvalue()
            size_mb = len(image_bytes) / 1_000_000

            logger.info(f"Quality {quality}: {size_mb:.2f}MB")

            # Google Vision API limit is 20MB for base64-encoded data
            # Base64 encoding increases size by ~33%, so we check raw bytes against 15MB
            # 15MB raw → ~20MB base64
            if len(image_bytes) < 15_000_000:
                logger.info(f"Using quality {quality} ({size_mb:.2f}MB)")
                return base64.b64encode(image_bytes).decode('utf-8')

        # If still too large after quality reduction, aggressively resize
        # Start with 50% size reduction, then try smaller if needed
        logger.warning(f"Image still too large after quality reduction, resizing...")
        for scale in [0.5, 0.4, 0.3, 0.25]:
            new_size = (int(image.width * scale), int(image.height * scale))
            resized = image.resize(new_size, Image.Resampling.LANCZOS)

            buffered = io.BytesIO()
            resized.save(buffered, format="JPEG", quality=85, optimize=True)
            image_bytes = buffered.getvalue()
            size_mb = len(image_bytes) / 1_000_000

            logger.info(f"Resize {scale*100}% ({new_size[0]}x{new_size[1]}): {size_mb:.2f}MB")

            if len(image_bytes) < 15_000_000:
                logger.info(f"Using {scale*100}% resize ({size_mb:.2f}MB)")
                return base64.b64encode(image_bytes).decode('utf-8')

        # Last resort: return heavily compressed version
        # This should always work
        logger.warning(f"Using last resort: 20% size, quality 60")
        buffered = io.BytesIO()
        tiny_size = (int(image.width * 0.2), int(image.height * 0.2))
        tiny = image.resize(tiny_size, Image.Resampling.LANCZOS)
        tiny.save(buffered, format="JPEG", quality=60, optimize=True)
        final_bytes = buffered.getvalue()
        logger.info(f"Final size: {len(final_bytes)/1_000_000:.2f}MB")
        return base64.b64encode(final_bytes).decode('utf-8')

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

            return {
                'success': True,
                'text': text,
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
                except:
                    # Last resort: assume 10 pages and try
                    total_pages = 100  # Try up to 100 pages

            if progress_callback:
                progress_callback(f"📄 Processing {file_name} ({total_pages} pages)")

            # Process each page individually (memory-efficient)
            all_text = []
            successful_pages = 0

            for page_num in range(1, total_pages + 1):
                if progress_callback:
                    progress_callback(f"📄 {file_name}: Page {page_num}/{total_pages}")

                try:
                    # Convert only ONE page at a time
                    # Using 150 DPI balances quality and file size for OCR
                    images = convert_from_path(
                        file_path,
                        dpi=150,
                        first_page=page_num,
                        last_page=page_num,
                        fmt='png'
                    )

                    if not images:
                        # No more pages, stop processing
                        break

                    image = images[0]

                    # Convert image to base64
                    image_base64 = self._image_to_base64(image)

                    # Extract text from this page
                    result = self._extract_text_from_image(image_base64, timeout=60)

                    if result['success'] and result['text'].strip():
                        all_text.append(result['text'])
                        successful_pages += 1

                    # Clear memory immediately after processing this page
                    del images
                    del image
                    del image_base64
                    gc.collect()

                except Exception as e:
                    error_msg = str(e).lower()
                    # Stop if we've gone past the last page
                    if 'page' in error_msg and ('out of range' in error_msg or 'invalid' in error_msg or 'exceed' in error_msg):
                        if progress_callback:
                            progress_callback(f"✅ Reached end of document at page {page_num-1}")
                        break
                    # For other errors, log and continue
                    if progress_callback:
                        progress_callback(f"⚠️ Page {page_num} failed: {str(e)[:100]}")
                    continue

            # Combine all pages
            full_text = "\n\n".join(all_text)

            # Calculate confidence based on success rate
            confidence = successful_pages / total_pages if total_pages > 0 else 0.0

            if not full_text.strip():
                return {
                    'success': False,
                    'file_name': file_name,
                    'error': f"No text extracted from {total_pages} pages",
                    'text': '',
                    'confidence': 0.0
                }

            if progress_callback:
                progress_callback(f"✅ {file_name}: Extracted {successful_pages}/{total_pages} pages")

            return {
                'success': True,
                'file_name': file_name,
                'text': full_text,
                'confidence': confidence,
                'page_count': total_pages
            }

        except Exception as e:
            return {
                'success': False,
                'file_name': Path(file_path).name,
                'error': str(e),
                'text': '',
                'confidence': 0.0
            }

    async def batch_extract(self, file_paths: List[str],
                           max_concurrent: int = 1,
                           progress_callback: Optional[Callable[[str], None]] = None) -> List[Dict]:
        """
        Extract text from multiple PDF files with concurrency control.
        Memory-optimized: processes ONE file at a time by default.

        Args:
            file_paths: List of paths to PDF files
            max_concurrent: Maximum concurrent files (default 1 for memory safety)
            progress_callback: Optional callback for progress updates

        Returns:
            List of extraction results
        """
        results = []
        semaphore = asyncio.Semaphore(max_concurrent)

        async def process_file(file_path: str, file_num: int, total_files: int) -> Dict:
            async with semaphore:
                if progress_callback:
                    progress_callback(f"📁 Processing file {file_num}/{total_files}: {Path(file_path).name}")

                # Run OCR in thread pool to avoid blocking
                loop = asyncio.get_event_loop()
                with ThreadPoolExecutor() as pool:
                    result = await loop.run_in_executor(
                        pool,
                        self.extract_text,
                        file_path,
                        300,  # timeout
                        progress_callback  # pass callback through
                    )

                # Force garbage collection after each file
                gc.collect()
                return result

        # Process all files
        total_files = len(file_paths)
        tasks = [process_file(fp, i+1, total_files) for i, fp in enumerate(file_paths)]
        results = await asyncio.gather(*tasks)

        return results

    def save_extracted_text(self, result: Dict, output_dir: str) -> str:
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

        # Create output filename (replace .pdf with .txt)
        file_name = Path(result['file_name']).stem + '.txt'
        output_path = Path(output_dir) / file_name

        # Ensure output directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Save text
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(result['text'])

        return str(output_path)
