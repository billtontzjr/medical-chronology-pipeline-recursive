# Testing the Recursive Dropbox Feature

## Quick Test

Run this command from the `medical-chronology-pipeline-recursive` directory:

```bash
cd medical-chronology-pipeline-recursive
python3 test_recursive.py
```

When prompted, paste your Dropbox shared link (use "Copy link" from Dropbox web).

The test will download PDFs to `/tmp/test_recursive_download` and show you what was found.

## What to Test

1. **Test with a subfolder link** (like "City of Vista CA"):
   - Should work exactly like the original app
   - Downloads only files from that one folder

2. **Test with the parent folder link** (like "Esther Franck"):
   - **NEW!** Should recursively search ALL subfolders
   - Should find PDFs in City of Vista CA, CMG, Kaiser, etc.
   - Should show "Entering subfolder: [name]" messages

## If OAuth is not configured

If you get an OAuth error, you need to set up the Dropbox credentials:

```bash
cd medical-chronology-pipeline-recursive
python3 setup_dropbox_oauth.py
```

Then run the test again.

## Expected Output

```
🔧 Initializing Dropbox client...
✅ Connected to Dropbox

Please paste your Dropbox shared link:
> [paste your link here]

📂 Downloading PDFs to: /tmp/test_recursive_download
🔄 Searching recursively through all subfolders...

============================================================
✅ SUCCESS!
   Downloaded: X PDFs
   Skipped: Y non-PDF files

📄 Downloaded files:
   - file1.pdf
   - file2.pdf
   ...
============================================================

📁 Check output at: /tmp/test_recursive_download
```
