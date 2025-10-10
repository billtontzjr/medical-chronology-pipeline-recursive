# Quick Start Guide

Get the Medical Chronology Pipeline running in 5 minutes.

## Step 1: Run Setup Script

```bash
cd /Users/billtontz/medical-chronology-pipeline
./setup.sh
```

This will:
- Create Python virtual environment
- Install all dependencies
- Create `.env` file from template

## Step 2: Configure API Keys

Edit the `.env` file and add your API keys:

```bash
nano .env
# or
open .env
```

Add your keys:
```
DROPBOX_ACCESS_TOKEN=sl.xxxxxxxxxxxxx
GOOGLE_CLOUD_API_KEY=AIzaxxxxxxxxxxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxx
```

**How to get these keys?** See [README.md](README.md#getting-api-keys)

## Step 3: Test with Sample Data (Optional)

Before using real medical records, test with sample files:

```bash
# The pipeline includes test sample files in data/test/
# These simulate OCR-extracted medical records
ls data/test/
```

## Step 4: Run the Pipeline

### Option A: Interactive Mode

```bash
source venv/bin/activate
python run_pipeline.py
```

It will prompt you for:
- Dropbox shared link
- Optional patient ID
- Confirmation to start

### Option B: Command Line

```bash
source venv/bin/activate
python run_pipeline.py \
  --dropbox-link "https://www.dropbox.com/scl/fo/..." \
  --patient-id "john_doe"
```

## What Happens Next?

The pipeline will:

1. **Download PDFs** from Dropbox
   ```
   ✓ Downloaded: medical_record_1.pdf
   ✓ Downloaded: medical_record_2.pdf
   ```

2. **Extract Text** with Google Vision OCR
   ```
   ✓ Extracted: medical_record_1.txt (confidence: 94%)
   ✓ Extracted: medical_record_2.txt (confidence: 91%)
   ```

3. **Generate Chronology** with Claude Agent
   ```
   ✓ Agent processing medical records...
   ✓ Generated: chronology.md
   ✓ Generated: chronology.json
   ✓ Generated: summary.md
   ✓ Generated: gaps.md
   ```

4. **Show Results**
   ```
   Session ID: john_doe_20250108_143022
   Files processed: 15
   Output directory: data/output/john_doe_20250108_143022
   ```

## View Output

```bash
# Open the chronology
open data/output/[session_id]/chronology.md

# View JSON data
cat data/output/[session_id]/chronology.json | jq .

# Check for gaps
cat data/output/[session_id]/gaps.md
```

## Common Issues

### "Missing required environment variables"
→ Make sure `.env` file has all three API keys

### "Dropbox download failed"
→ Check that:
- Dropbox token is valid (regenerate if expired)
- Shared link is accessible
- Link contains PDF files

### "OCR failed"
→ Verify:
- Google Cloud Vision API is enabled
- API key is correct
- PDFs are not password-protected

## Next Steps

- Read the full [README.md](README.md) for detailed documentation
- Review formatting rules in `.claude/CLAUDE.md`
- Customize validation in `src/hooks/formatting_guard.py`
- Run tests: `pytest tests/`

## Support

- Check `gaps.md` in output for processing issues
- Review console logs for errors
- See [Troubleshooting](README.md#troubleshooting) in README

---

**Ready to go?** Run `./setup.sh` to get started!
