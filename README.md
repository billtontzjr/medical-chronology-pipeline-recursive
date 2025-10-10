# Medical Chronology Pipeline

An automated pipeline for generating professionally formatted medical chronologies from medical records stored in Dropbox using Google Vision OCR and Claude Agent SDK.

## Overview

This pipeline automates the process of:
1. 📥 Downloading medical PDF files from Dropbox shared links
2. 📄 Extracting text using Google Cloud Vision OCR
3. 🤖 Generating formatted medical chronologies using Claude Agent SDK
4. ✅ Validating output formatting and quality

The generated chronologies follow strict medical-legal formatting standards suitable for spinal surgeons, life care planners, attorneys, and insurance companies.

## Architecture

```
┌─────────────────┐
│  Dropbox Link   │
└────────┬────────┘
         │
         v
┌─────────────────┐
│ Download PDFs   │  (Dropbox API)
└────────┬────────┘
         │
         v
┌─────────────────┐
│  OCR Extract    │  (Google Vision API)
└────────┬────────┘
         │
         v
┌─────────────────┐
│ Claude Agent    │  (Agent SDK)
│ Generate Chron  │
└────────┬────────┘
         │
         v
┌─────────────────┐
│ Output Files:   │
│ - chronology.md │
│ - chronology.json
│ - summary.md    │
│ - gaps.md       │
└─────────────────┘
```

## Prerequisites

- **Python 3.10+**
- **API Keys:**
  - Dropbox Access Token
  - Google Cloud Vision API Key
  - Anthropic API Key

## Installation

### 1. Clone or Create Repository

```bash
cd /Users/billtontz/medical-chronology-pipeline
```

### 2. Create Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment

Copy the example environment file and add your API keys:

```bash
cp .env.example .env
```

Edit `.env` and add your credentials:

```bash
# Dropbox Configuration
DROPBOX_ACCESS_TOKEN=your_dropbox_access_token_here

# Google Cloud Vision API
GOOGLE_CLOUD_API_KEY=your_google_vision_api_key_here

# Anthropic API
ANTHROPIC_API_KEY=your_anthropic_api_key_here
```

## Getting API Keys

### Dropbox Access Token

1. Go to [Dropbox App Console](https://www.dropbox.com/developers/apps)
2. Click **"Create App"**
3. Choose: **Scoped access** → **Full Dropbox**
4. Name your app (e.g., `medical-chronology-pipeline`)
5. In app settings, scroll to **"OAuth 2"** section
6. Click **"Generate"** under "Generated access token"
7. Copy the token to your `.env` file

### Google Cloud Vision API Key

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project or select existing
3. Enable **Cloud Vision API**
4. Go to **APIs & Services → Credentials**
5. Click **"Create Credentials" → "API Key"**
6. Copy the API key to your `.env` file

### Anthropic API Key

1. Go to [Anthropic Console](https://console.anthropic.com)
2. Sign in or create account
3. Go to **API Keys** section
4. Click **"Create Key"**
5. Copy the key to your `.env` file

## Usage

### Basic Usage

Run the pipeline with a Dropbox shared link:

```bash
python run_pipeline.py --dropbox-link "https://www.dropbox.com/..."
```

### With Patient ID

Include a patient identifier for organized output:

```bash
python run_pipeline.py \
  --dropbox-link "https://www.dropbox.com/..." \
  --patient-id "john_doe"
```

### Non-Interactive Mode

Skip confirmation prompts:

```bash
python run_pipeline.py \
  --dropbox-link "https://www.dropbox.com/..." \
  --no-interactive
```

### Interactive Mode (Default)

The script will prompt you for inputs:

```bash
python run_pipeline.py
```

## Output Files

The pipeline generates four files in the output directory:

### 1. `chronology.md`
The complete medical chronology in markdown format following strict formatting rules:
- Proper header with patient info
- Chronological entries
- Direct, factual tone
- No bold, bullets, or citations

### 2. `chronology.json`
Structured JSON data version with:
- Patient metadata
- Array of chronological entries
- Facility, provider, and visit information

### 3. `summary.md`
Executive summary with:
- Key findings
- Treatment timeline
- Diagnostic results
- Outcome information

### 4. `gaps.md`
Documentation of:
- Missing records or timeline gaps
- OCR quality issues
- Unclear or garbled text
- Recommendations for manual review

## Directory Structure

```
medical-chronology-pipeline/
├── .claude/
│   ├── CLAUDE.md                    # Main formatting prompt
│   ├── agents/                      # Subagent definitions
│   │   ├── document-analyzer.md
│   │   ├── event-extractor.md
│   │   ├── quality-checker.md
│   │   └── ocr-validator.md
│   └── settings.json
├── src/
│   ├── __init__.py
│   ├── pipeline.py                  # Main pipeline orchestrator
│   ├── ocr_client.py               # Google Vision OCR client
│   ├── hooks/
│   │   ├── __init__.py
│   │   └── formatting_guard.py     # Format validation hook
│   └── tools/
│       ├── __init__.py
│       └── dropbox_tool.py         # Dropbox integration
├── data/
│   ├── test/                        # Test sample files
│   ├── input/                       # Downloaded PDFs (gitignored)
│   ├── extracted/                   # OCR text files (gitignored)
│   └── output/                      # Generated chronologies (gitignored)
├── tests/
│   └── test_pipeline.py
├── run_pipeline.py                  # CLI entry point
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## Formatting Rules

The pipeline enforces strict medical chronology formatting:

### Header Format
```
MEDICAL RECORDS SUMMARY
[PATIENT'S FULL NAME]
Date of Birth: [Month Day, YYYY]
Date of Injury: [Month Day, YYYY]
```

### Entry Format
```
[MM/DD/YYYY]. [Facility]. [Provider Name], [Credentials]. [Visit Type].
[Summary paragraph with in-paragraph headings]
```

### Prohibited Elements
- ❌ Bold text (`**text**`)
- ❌ Bullet points or lists
- ❌ All-caps sections
- ❌ Citations or page numbers
- ❌ Narrative phrasing ("patient was seen for...")

### Required Elements
- ✅ Direct, factual tone
- ✅ In-paragraph headings (Chief Complaint:, Physical Examination:, etc.)
- ✅ Orthopedic/neurological focus
- ✅ Impression-only for imaging reports
- ✅ Consolidated therapy visits

## Testing

### Run with Test Data

Test the pipeline with sample files:

```bash
# Copy sample files to a test directory
cp data/test/*.txt data/extracted/test_session/

# The pipeline will process these as if they were OCR output
```

### Run Unit Tests

```bash
pytest tests/
```

## Troubleshooting

### "Missing required environment variables"
- Ensure `.env` file exists and contains all three API keys
- Check that keys are not empty or placeholder values

### "Dropbox download failed"
- Verify Dropbox access token is valid (tokens expire after 4 hours for new apps)
- Ensure shared link is accessible and contains PDF files
- Check Dropbox app permissions include `files.content.read`

### "OCR failed"
- Verify Google Cloud Vision API key is valid
- Ensure Cloud Vision API is enabled in your Google Cloud project
- Check that PDFs are not password-protected or corrupted

### "No text could be extracted"
- PDFs may be purely image-based (OCR should handle this)
- Check OCR confidence scores in logs
- Verify PDF files are not corrupted

### "Format validation failed"
- Review generated `chronology.md` for violations
- Check logs for specific formatting issues
- The formatting guard hook will block writes with clear error messages

## Development

### Adding Custom Formatting Rules

Edit `.claude/CLAUDE.md` to modify chronology formatting requirements.

### Modifying Validation Rules

Edit `src/hooks/formatting_guard.py` to add or change format validation checks.

### Extending OCR Capabilities

Edit `src/ocr_client.py` to customize OCR processing or add support for other document types.

## Output Example

```markdown
MEDICAL RECORDS SUMMARY
JANE DOE
Date of Birth: May 12, 1978
Date of Injury: September 20, 2023

09/20/2023. HCA Florida Trinity Hospital. Sarah Johnson, MD. Emergency Department Visit.
Chief Complaint: Motor vehicle collision. History of Present Illness: Patient was the restrained driver in a rear-end collision, complaining of lower back pain and a slight headache. Physical Examination: Patient is nontoxic in appearance and in no acute distress. Tenderness is present in the lumbar spine. Patient is ambulatory with a steady gait. Diagnostics: CT of the brain reveals no acute process. CT of the lumbar spine reveals no acute bony abnormality but shows facet hypertrophic changes. Assessment: Back pain due to MVC. Plan: Discharged to home with instructions for outpatient follow-up.

10/15/2023. Diagnostic Imaging Center. Robert Martinez, MD. MRI Lumbar Spine without Contrast.
Impression: 3mm right foraminal herniation with annular tear/fissure at L5-S1 causing moderate right foraminal stenosis and contact of the right L5 nerve root. 2mm left foraminal herniation at L3-4 causing mild left foraminal stenosis with possible contact of the left L3 nerve root. Annular bulging at L4-5 with facet arthropathy causing mild to moderate bilateral foraminal stenosis and contact of bilateral L4 nerve roots. Facet effusions at L4-5 and L5-S1.
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests for new functionality
5. Submit a pull request

## License

This project is for internal use. All rights reserved.

## Support

For issues or questions:
- Check the troubleshooting section above
- Review logs in the console output
- Examine generated `gaps.md` for processing issues

## Data Privacy

- All processing happens locally or via secure APIs
- Medical records are never permanently stored
- Output files are in gitignored directories
- Follow HIPAA guidelines for handling protected health information
