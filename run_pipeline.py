#!/usr/bin/env python3
"""CLI script to run the medical chronology pipeline."""

import os
import sys
import asyncio
from pathlib import Path
from dotenv import load_dotenv
import click

from src.pipeline import MedicalChronologyPipeline


def validate_environment():
    """Validate that all required environment variables are set."""
    required_vars = [
        'DROPBOX_ACCESS_TOKEN',
        'GOOGLE_CLOUD_API_KEY',
        'ANTHROPIC_API_KEY'
    ]

    missing = []
    for var in required_vars:
        if not os.getenv(var):
            missing.append(var)

    if missing:
        print("❌ Missing required environment variables:")
        for var in missing:
            print(f"   - {var}")
        print("\nPlease set these in your .env file or environment.")
        sys.exit(1)


def validate_output(result: dict):
    """Validate and display output file results."""
    if not result.get('success'):
        print(f"\n❌ Pipeline failed: {result.get('error')}")
        return False

    print("\n✅ Pipeline completed successfully!")
    print(f"\nSession ID: {result['session_id']}")
    print(f"Files processed: {result['files_processed']}")

    print("\n📁 Output files:")
    output_files = result.get('output_files', {})

    if output_files:
        for filename, filepath in output_files.items():
            file_size = Path(filepath).stat().st_size
            print(f"   ✓ {filename} ({file_size:,} bytes)")
    else:
        print("   ⚠️  No output files generated")

    missing = result.get('missing_files', [])
    if missing:
        print("\n⚠️  Missing expected files:")
        for filename in missing:
            print(f"   ✗ {filename}")

    print(f"\n📂 Output directory: {result['output_dir']}")

    # Basic format validation for chronology.md
    chronology_path = output_files.get('chronology.md')
    if chronology_path:
        print("\n🔍 Validating chronology format...")
        with open(chronology_path, 'r') as f:
            content = f.read()

        issues = []

        # Check for proper header
        if not content.startswith('MEDICAL RECORDS SUMMARY'):
            issues.append("Missing proper header")

        # Check for violations
        if '**' in content:
            issues.append("Contains bold formatting (**)")

        if any(pattern in content for pattern in ['* ', '- ', '• ']):
            issues.append("Contains bullet points")

        # Check for date format
        import re
        if not re.search(r'\d{2}/\d{2}/\d{4}\.', content):
            issues.append("Missing expected date format (MM/DD/YYYY.)")

        if issues:
            print("   ⚠️  Format issues detected:")
            for issue in issues:
                print(f"      - {issue}")
        else:
            print("   ✓ Format validation passed")

    return True


@click.command()
@click.option(
    '--dropbox-link',
    prompt='Dropbox shared link',
    help='Dropbox shared link to medical records folder'
)
@click.option(
    '--patient-id',
    help='Optional patient identifier for organizing output'
)
@click.option(
    '--interactive/--no-interactive',
    default=True,
    help='Run in interactive mode with prompts'
)
def main(dropbox_link: str, patient_id: str, interactive: bool):
    """
    Run the Medical Chronology Pipeline.

    This pipeline will:
    1. Download PDF files from Dropbox
    2. Extract text using Google Vision OCR
    3. Generate formatted medical chronology with Claude Agent SDK
    """
    # Load environment variables
    load_dotenv()

    # Display banner
    print("=" * 60)
    print("  MEDICAL CHRONOLOGY PIPELINE")
    print("=" * 60)

    # Validate environment
    validate_environment()

    # Display configuration
    print(f"\n📋 Configuration:")
    print(f"   Dropbox Link: {dropbox_link[:50]}...")
    print(f"   Patient ID: {patient_id or 'Auto-generated'}")

    if interactive:
        confirm = input("\n▶️  Start pipeline? (y/n): ")
        if confirm.lower() != 'y':
            print("Pipeline cancelled.")
            sys.exit(0)

    # Initialize pipeline
    print("\n🔧 Initializing pipeline...")
    pipeline = MedicalChronologyPipeline(
        dropbox_token=os.getenv('DROPBOX_ACCESS_TOKEN'),
        google_api_key=os.getenv('GOOGLE_CLOUD_API_KEY'),
        anthropic_api_key=os.getenv('ANTHROPIC_API_KEY')
    )

    # Run pipeline
    print("\n🚀 Running pipeline...\n")
    result = asyncio.run(pipeline.run_pipeline(dropbox_link, patient_id))

    # Validate and display results
    success = validate_output(result)

    if success:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == '__main__':
    main()
