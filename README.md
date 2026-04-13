# Bugo Content Adapter

Transcribes competitor ad videos (Hebrew/English) with Whisper, adapts them to Bugo's brand voice via the Claude API, lets you review and edit, then uploads to Google Drive.

## Setup

1. **Clone the repo:**
   ```bash
   git clone <repo-url>
   cd bugo-content-adapter
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Create a `.env` file** with the following variables:
   ```
   ANTHROPIC_API_KEY=your-claude-api-key
   GOOGLE_DOC_ID=id-of-google-doc-for-scripts
   GOOGLE_DRIVE_VIDEO_FOLDER_ID=id-of-drive-folder-for-videos
   ```

4. **Add Google credentials:**
   - Place your `credentials.json` (Google OAuth client secret) in the project root.
   - On first run you'll be prompted to authorize; this creates `token.json`.

5. **Add input videos:**
   - Place video files in the `input_videos/` directory.

## Usage

### Web UI (recommended)
```bash
python3 app.py
# Open http://localhost:5000
```

### CLI (auto-uploads without review)
```bash
python3 process.py
```

## Key Files

- `app.py` -- Flask web UI: drag videos, review/edit scripts, approve upload
- `process.py` -- CLI entry point, auto-processes input_videos/
- `transcribe.py` -- Whisper large-v2 transcription with auto language detection
- `adapt.py` -- Claude API adaptation to Bugo's brand voice
- `drive_upload.py` -- Appends scripts to a Google Doc and uploads videos to Google Drive
- `brand_book.md` -- Bugo brand guidelines used for adaptation
