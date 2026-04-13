# Bugo Content Adapter

Transcribes competitor ad videos (Hebrew/English) with Whisper, adapts to Bugo's brand voice via Claude API, lets user edit + approve, then uploads to Google Drive.

## Running

```bash
# Web UI (recommended):
python3 app.py
# Then open: http://localhost:5000

# CLI (auto-uploads without review):
python process.py
```

## Key files

- `app.py` – Flask web UI: drag videos, review/edit scripts, approve upload
- `process.py` – CLI entry point, auto-processes input_videos/
- `transcribe.py` – Whisper large-v2 transcription with auto language detection
- `adapt.py` – Claude API (claude-sonnet-4-20250514): returns dict {hebrew, hooks_he, english, hooks_en}
- `drive_upload.py` – Appends to fixed Google Doc (GOOGLE_DOC_ID), uploads videos to GOOGLE_DRIVE_VIDEO_FOLDER_ID

## .env variables

- `ANTHROPIC_API_KEY` – Claude API key
- `GOOGLE_DOC_ID` – ID of the single Google Doc to append all scripts to
- `GOOGLE_DRIVE_VIDEO_FOLDER_ID` – Folder ID where reference videos are uploaded

## Dependencies

- ffmpeg: `brew install ffmpeg`
- Python: `pip install -r requirements.txt`
- Google OAuth2 credentials in `credentials.json`
- Brand book content in `brand_book.md`

## Drive output structure

- Videos upload to GOOGLE_DRIVE_VIDEO_FOLDER_ID (flat, no subfolders)
- Scripts append to GOOGLE_DOC_ID as numbered sections (Vid 1, Vid 2...)
- Each section has Hebrew script + hooks, then English script + hooks

## Web UI flow

1. Drag videos -> transcribe + adapt (no auto-upload)
2. User edits Hebrew script + hooks directly in the browser
3. Click "תרגם לאנגלית" to re-translate edited Hebrew to English
4. Click "אשר והעלה לדרייב" to upload video + append script to doc
