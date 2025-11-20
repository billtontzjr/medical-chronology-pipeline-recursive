# Dropbox Upload Feature

## Overview
The medical chronology pipeline now automatically uploads all generated outputs to your Dropbox Team Folder after processing is complete.

## Upload Location
All chronology outputs are uploaded to:
```
/Tontz Team Folder/Precision Life Care Planning/Confidential/Medical chronology pipeline outputs/{session_id}/
```

Where `{session_id}` is a timestamp-based identifier (e.g., `20231120_143022` or `PatientID_20231120_143022` if a patient ID is provided).

## Files Uploaded
The following files are automatically uploaded:
- `chronology.md` - The formatted medical chronology
- `chronology.json` - Structured JSON data
- `summary.md` - Summary of the medical records
- `gaps.md` - Identified gaps in the chronology

## How It Works
1. The pipeline processes medical records as usual
2. After generating the chronology (Phase 4), it automatically uploads files (Phase 5)
3. If upload succeeds, you'll see: `✅ Uploaded X files to Dropbox`
4. If upload fails, the pipeline still completes successfully, but shows a warning

## Code Changes
### 1. DropboxTool (`src/tools/dropbox_tool.py`)
Added two new methods:
- `upload_file(local_path, dropbox_path)` - Upload a single file
- `upload_folder(local_dir, dropbox_folder, extensions)` - Upload all files from a directory

### 2. Pipeline (`src/pipeline.py`)
- Added Phase 5: Upload to Dropbox after validation
- Returns `dropbox_upload` and `dropbox_path` in results

### 3. CLI Runner (`run_pipeline.py`)
- Displays Dropbox upload status and location after completion

### 4. Web App (`app.py`)
- Shows Dropbox location in success message

## Configuration
The Dropbox destination path is configured in `src/pipeline.py`:
```python
dropbox_base_path = "/Tontz Team Folder/Precision Life Care Planning/Confidential/Medical chronology pipeline outputs"
```

To change the upload location, edit this variable in the `run_pipeline` method.

## Authentication
Uses the existing Dropbox OAuth credentials from your `.env` file:
- `DROPBOX_APP_KEY`
- `DROPBOX_APP_SECRET`
- `DROPBOX_REFRESH_TOKEN`

No additional setup required!

## Error Handling
- If upload fails, the pipeline still completes successfully
- Local files remain in `data/output/{session_id}/`
- Error messages are logged and displayed to the user
- Failed uploads can be manually retried by running the upload separately

## Example Output
```
Phase 5: Uploading to Dropbox...
✓ Uploaded 4 files to Dropbox

☁️  Dropbox upload:
   ✅ Uploaded 4 files
   📁 Location: /Tontz Team Folder/Precision Life Care Planning/Confidential/Medical chronology pipeline outputs/20231120_143022
```

## Accessing Uploaded Files
1. Open Dropbox
2. Navigate to: **Tontz Team Folder** → **Precision Life Care Planning** → **Confidential** → **Medical chronology pipeline outputs**
3. Find your session folder by timestamp or patient ID
4. All output files will be available there

## Testing
To test the upload feature without running the full pipeline:
```python
from src.tools.dropbox_tool import DropboxTool
from pathlib import Path

# Initialize tool
dropbox_tool = DropboxTool(use_oauth=True)

# Test upload
result = dropbox_tool.upload_folder(
    local_dir='data/output/test_session',
    dropbox_folder='/Tontz Team Folder/Precision Life Care Planning/Confidential/Medical chronology pipeline outputs/test'
)

print(result)
```
