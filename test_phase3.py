"""Test Phase 3 (Claude Agent) with existing extracted files."""

import asyncio
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

async def test_phase3():
    """Test the Claude Agent phase with existing extracted text files."""
    # Use the session with extracted files
    session_id = "luis_velez_20251008_225017"
    base_dir = Path(__file__).parent
    extracted_dir = base_dir / "data" / "extracted" / session_id
    output_dir = base_dir / "data" / "output" / session_id

    # Check if extracted files exist
    extracted_files = list(extracted_dir.glob("*.txt"))
    if not extracted_files:
        print(f"❌ No extracted files found in {extracted_dir}")
        return

    print(f"✅ Found {len(extracted_files)} extracted text files")
    for f in extracted_files:
        print(f"   - {f.name}")

    # Import Claude Agent SDK
    from claude_agent_sdk import query, ClaudeAgentOptions

    # Set API key
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        print("❌ ANTHROPIC_API_KEY not set in .env")
        return

    os.environ['ANTHROPIC_API_KEY'] = api_key

    # Build directive
    file_list = "\n".join([f"  - {f.name}" for f in extracted_files])
    directive = f"""You are tasked with creating a medical chronology from OCR-extracted medical records.

**Input Details:**
- Number of files: {len(extracted_files)}
- Input directory: {extracted_dir}
- Output directory: {output_dir}

**Files to process:**
{file_list}

**Your Task:**
Follow ALL rules in .claude/CLAUDE.md to generate:
1. chronology.md - The formatted medical chronology
2. chronology.json - Structured data version
3. summary.md - Executive summary
4. gaps.md - Document gaps and OCR issues

**Important:**
- Read each .txt file in the input directory
- Extract information following the strict formatting rules
- Check for OCR errors and note them in gaps.md
- Write all outputs to: {output_dir}

Begin by scanning the input directory and reading all files."""

    # Create options
    options = ClaudeAgentOptions(
        system_prompt="You are a medical chronology expert. Follow the rules in .claude/CLAUDE.md precisely.",
        cwd=str(base_dir),
        permission_mode='acceptEdits'
    )

    print("\n🚀 Starting Claude Agent processing...")
    print(f"📂 Input: {extracted_dir}")
    print(f"📂 Output: {output_dir}\n")

    # Run query
    message_count = 0
    async for message in query(prompt=directive, options=options):
        message_count += 1
        # Print progress
        if hasattr(message, 'content'):
            for block in message.content:
                if hasattr(block, 'text') and block.text:
                    preview = block.text[:150].replace('\n', ' ')
                    print(f"[{message_count}] {preview}...")

    print(f"\n✅ Agent processing complete ({message_count} messages)")

    # Check outputs
    print("\n📋 Checking outputs:")
    required_files = ['chronology.md', 'chronology.json', 'summary.md', 'gaps.md']
    for filename in required_files:
        file_path = output_dir / filename
        if file_path.exists():
            size = file_path.stat().st_size
            print(f"   ✅ {filename} ({size:,} bytes)")
        else:
            print(f"   ❌ {filename} (missing)")

if __name__ == "__main__":
    asyncio.run(test_phase3())
