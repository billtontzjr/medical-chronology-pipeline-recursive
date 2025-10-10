# 🏥 How to Run the Medical Chronology Web App

## Quick Start

1. **Activate virtual environment:**
   ```bash
   source venv/bin/activate
   ```

2. **Run the Streamlit app:**
   ```bash
   streamlit run app.py
   ```

3. **Open in browser:**
   - The app will automatically open in your browser
   - Or go to: http://localhost:8501

## Using the App

1. **Check API Keys** (sidebar):
   - Should auto-load from `.env` file
   - Green checkmarks = ready to go

2. **Enter Information:**
   - Paste Dropbox shared link
   - Enter patient ID (e.g., "john_doe")

3. **Generate:**
   - Click "Generate Chronology" button
   - Watch the progress in real-time

4. **Download Results:**
   - View generated files in tabs
   - Download individual files
   - All 4 files: chronology.md, chronology.json, summary.md, gaps.md

## Features

- ✅ Real-time progress tracking
- ✅ Preview all generated files
- ✅ Download individual files
- ✅ Clean, professional interface
- ✅ Works on any device with a browser

## Deployment Options

### Option 1: Local (Current)
- Run on your computer
- Private and secure
- Free

### Option 2: Streamlit Cloud (Share with team)
1. Push code to GitHub
2. Go to https://streamlit.io/cloud
3. Connect repository
4. Deploy (free tier available)
5. Share URL with team

### Option 3: Docker (Production)
```bash
# Build
docker build -t medical-chronology .

# Run
docker run -p 8501:8501 medical-chronology
```

## Troubleshooting

**App won't start:**
```bash
pip install streamlit
```

**API keys not loading:**
- Check `.env` file exists
- Verify keys are correct

**Port already in use:**
```bash
streamlit run app.py --server.port 8502
```
