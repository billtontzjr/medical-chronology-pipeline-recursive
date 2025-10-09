# Setup Instructions for Clare

## Quick Start (Same Network)
1. Open browser and go to: http://10.1.2.166:8501
2. The app is ready to use with Dr. Tontz's Dropbox access

## Using the Pipeline

### Input Format:
**Dropbox Path:** `/folder/patient-name`
- Use the folder path from Dropbox, not a shared link
- Example: `/2025 expert files/flores, ernesto - dr. tontz - expert`

**Patient ID:** `Patient Name`
- Example: `Ernesto Flores`

### Important Notes:
- Only works with folders in Dr. Tontz's personal Dropbox
- Team folders may not work (OAuth limitation)
- For large files (100MB+), processing takes 20-40 minutes

## Output Location:
Files are saved to:
```
/Users/billtontz/medical-chronology-pipeline/data/output/[Patient]_[Date]/
```

Contains:
- chronology.md - Full medical chronology
- chronology.json - Structured data
- summary.md - Executive summary
- gaps.md - Document gaps and OCR issues

## Troubleshooting:
- If "Pipeline failed" appears, check the folder path is correct
- Folder must exist in Dr. Tontz's personal Dropbox
- Use direct paths, not shared links (those have permission issues)
