"""Test the recursive shared link feature locally."""

import os

import pytest
from src.tools.dropbox_tool import DropboxTool

# This interactive integration helper remains executable directly, but ordinary
# test discovery must never prompt for a real case or download patient records.
pytestmark = pytest.mark.skip(reason="Manual Dropbox integration helper; run python test_recursive.py explicitly.")

def test_recursive_download():
    """Test downloading from a shared link with subfolders."""
    
    print("🔧 Initializing Dropbox client...")
    try:
        dropbox_tool = DropboxTool()
        print("✅ Connected to Dropbox\n")
    except Exception as e:
        print(f"❌ Failed to connect: {e}")
        return
    
    # Get shared link from user
    print("Please paste your Dropbox shared link:")
    print("(Use the 'Copy link' button from a folder in Dropbox)")
    shared_link = input("> ").strip()
    
    if not shared_link:
        print("❌ No link provided")
        return
    
    # Create output directory
    output_dir = "/tmp/test_recursive_download"
    os.makedirs(output_dir, exist_ok=True)
    
    print(f"\n📂 Downloading PDFs to: {output_dir}")
    print("🔄 Searching recursively through all subfolders...\n")
    
    # Download files
    result = dropbox_tool.get_shared_link_files(
        shared_link, 
        output_dir, 
        extensions=['.pdf']
    )
    
    # Print results
    print("\n" + "="*60)
    if result['success']:
        print(f"✅ SUCCESS!")
        print(f"   Downloaded: {len(result['downloaded'])} PDFs")
        print(f"   Skipped: {len(result['skipped'])} non-PDF files")
        if result['failed']:
            print(f"   ⚠️  Failed: {len(result['failed'])} files")
        
        if result['downloaded']:
            print("\n📄 Downloaded files:")
            for file in result['downloaded']:
                print(f"   - {file['name']}")
    else:
        print(f"❌ FAILED: {result.get('error', 'Unknown error')}")
    
    print("="*60)
    print(f"\n📁 Check output at: {output_dir}")

if __name__ == "__main__":
    test_recursive_download()
