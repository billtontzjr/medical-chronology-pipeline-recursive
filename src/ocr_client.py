"""Google Vision OCR client for extracting text from PDFs."""

import base64
import io
from pathlib import Path
from typing import Dict, List
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
        """Convert PIL Image to base64 string."""
        buffered = io.BytesIO()
        image.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode('utf-8')

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
                return {
                    'success': False,
                    'error': response_data["error"].get("message", "Unknown error"),
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

    def extract_text(self, file_path: str, timeout: int = 300) -> Dict:
        """
        Extract text from a PDF file using Google Vision OCR.

        Args:
            file_path: Path to the PDF file
            timeout: Timeout in seconds (default 5 minutes)

        Returns:
            Dictionary with extraction results
        """
        try:
            file_name = Path(file_path).name

            # Convert PDF to images (one image per page)
            try:
                images = convert_from_path(
                    file_path,
                    dpi=200,  # Good balance between quality and size
                    fmt='png'
                )
            except Exception as e:
                return {
                    'success': False,
                    'file_name': file_name,
                    'error': f"Failed to convert PDF to images: {str(e)}",
                    'text': '',
                    'confidence': 0.0
                }

            if not images:
                return {
                    'success': False,
                    'file_name': file_name,
                    'error': "No pages found in PDF",
                    'text': '',
                    'confidence': 0.0
                }

            # Process each page
            all_text = []
            successful_pages = 0

            for page_num, image in enumerate(images, 1):
                # Convert image to base64
                image_base64 = self._image_to_base64(image)

                # Extract text from this page
                result = self._extract_text_from_image(image_base64, timeout=60)

                if result['success'] and result['text'].strip():
                    all_text.append(result['text'])
                    successful_pages += 1

            # Combine all pages
            full_text = "\n\n".join(all_text)

            # Calculate confidence based on success rate
            confidence = successful_pages / len(images) if images else 0.0

            if not full_text.strip():
                return {
                    'success': False,
                    'file_name': file_name,
                    'error': f"No text extracted from {len(images)} pages",
                    'text': '',
                    'confidence': 0.0
                }

            return {
                'success': True,
                'file_name': file_name,
                'text': full_text,
                'confidence': confidence,
                'page_count': len(images)
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
                           max_concurrent: int = 3) -> List[Dict]:
        """
        Extract text from multiple PDF files with concurrency control.

        Args:
            file_paths: List of paths to PDF files
            max_concurrent: Maximum number of concurrent OCR requests

        Returns:
            List of extraction results
        """
        results = []
        semaphore = asyncio.Semaphore(max_concurrent)

        async def process_file(file_path: str) -> Dict:
            async with semaphore:
                # Run OCR in thread pool to avoid blocking
                loop = asyncio.get_event_loop()
                with ThreadPoolExecutor() as pool:
                    result = await loop.run_in_executor(
                        pool,
                        self.extract_text,
                        file_path
                    )
                return result

        # Process all files
        tasks = [process_file(fp) for fp in file_paths]
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
