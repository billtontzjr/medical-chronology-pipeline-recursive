"""
Simple OCR test script to diagnose Google Vision API issues.
Run this on Render to see exactly what's happening.
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from src.ocr_client import OCRClient

# Load environment
load_dotenv()

def test_ocr():
    """Test OCR with detailed diagnostics."""

    # Check API key
    api_key = os.getenv('GOOGLE_CLOUD_API_KEY')
    if not api_key:
        print("❌ GOOGLE_CLOUD_API_KEY not found in environment")
        return

    print(f"✅ API key found: {api_key[:10]}...{api_key[-4:]}")

    # Find a test PDF
    test_pdfs = list(Path('data/input').rglob('*.pdf'))
    if not test_pdfs:
        print("❌ No PDFs found in data/input")
        return

    test_file = test_pdfs[0]
    print(f"\n📄 Testing with: {test_file.name}")
    print(f"   File size: {test_file.stat().st_size / 1_000_000:.2f}MB")

    # Initialize OCR client
    client = OCRClient(api_key)

    # Try to extract text from first page only
    print("\n🔍 Extracting text from first page...")

    def progress(msg):
        print(f"   {msg}")

    result = client.extract_text(str(test_file), progress_callback=progress)

    print("\n" + "="*60)
    if result['success']:
        print("✅ SUCCESS!")
        print(f"   Extracted {len(result['text'])} characters")
        print(f"   Confidence: {result.get('confidence', 0):.2%}")
        print(f"\n   First 200 chars:\n   {result['text'][:200]}")
    else:
        print("❌ FAILED!")
        print(f"   Error: {result['error']}")
        print(f"\n   This is the exact error Google Vision returned.")
    print("="*60)

if __name__ == "__main__":
    test_ocr()
