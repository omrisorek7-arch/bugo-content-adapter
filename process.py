import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from transcribe import transcribe
from adapt import adapt_script
from drive_upload import upload_to_drive

INPUT_DIR = Path(__file__).parent / "input_videos"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}


def _format_script_data(data: dict) -> str:
    """Format script data dict into readable text for fallback saving."""
    parts = []
    parts.append("🇮🇱 סקריפט עברית")
    parts.append(data.get("hebrew", ""))
    parts.append("\nהוקים חלופיים בעברית:")
    parts.append(data.get("hooks_he", ""))
    parts.append("\n─────────────────────────")
    parts.append("\n🇺🇸 English Script")
    parts.append(data.get("english", ""))
    parts.append("\nAlternative Hooks – English:")
    parts.append(data.get("hooks_en", ""))
    return "\n".join(parts)


def _save_fallback(video_name: str, script_data: dict) -> str:
    """Save script as local .txt file when Drive upload fails."""
    fallback_dir = Path(__file__).parent / "output_fallback"
    fallback_dir.mkdir(exist_ok=True)
    today = date.today().isoformat()
    fallback_path = fallback_dir / f"Bugo_Script_{video_name}_{today}.txt"
    fallback_path.write_text(_format_script_data(script_data), encoding="utf-8")
    return str(fallback_path)


def process_videos():
    doc_id = os.environ.get("GOOGLE_DOC_ID", "")
    video_folder_id = os.environ.get("GOOGLE_DRIVE_VIDEO_FOLDER_ID", "")
    if not doc_id:
        print("ERROR: Set GOOGLE_DOC_ID in .env before running.")
        sys.exit(1)
    if not video_folder_id:
        print("ERROR: Set GOOGLE_DRIVE_VIDEO_FOLDER_ID in .env before running.")
        sys.exit(1)

    videos = [
        f for f in INPUT_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
    ]

    if not videos:
        print("No video files found in input_videos/. Drop .mp4/.mov/.avi/.mkv files there and re-run.")
        return

    print(f"Found {len(videos)} video(s) to process.\n")

    results = []

    for i, video_path in enumerate(videos, 1):
        video_name = video_path.stem
        print(f"[{i}/{len(videos)}] Processing: {video_path.name}")

        # Step 1: Transcribe
        try:
            print("  Transcribing with Whisper...")
            result = transcribe(str(video_path))
            transcript_text = result["text"]
            language = result["language"]
            print(f"  ✓ Transcribed (language: {language}, {len(transcript_text)} chars)")
        except Exception as e:
            print(f"  ✗ Transcription failed: {e}")
            results.append({"video": video_path.name, "status": "failed", "step": "transcription"})
            continue

        # Step 2: Adapt with Claude
        try:
            print("  Adapting script with Claude...")
            script_data = adapt_script(transcript_text, language)
            he_len = len(script_data.get("hebrew", ""))
            en_len = len(script_data.get("english", ""))
            print(f"  ✓ Adapted (HE: {he_len} chars, EN: {en_len} chars)")
        except Exception as e:
            print(f"  ✗ Adaptation failed: {e}")
            results.append({"video": video_path.name, "status": "failed", "step": "adaptation"})
            continue

        # Step 3: Upload to Drive
        try:
            upload_result = upload_to_drive(str(video_path), script_data, doc_id, video_folder_id)
            print(f"  ✓ Uploaded to Drive (doc: {upload_result['doc_id']})")
            results.append({"video": video_path.name, "status": "success"})
        except Exception as e:
            print(f"  ✗ Drive upload failed: {e}")
            fallback_path = _save_fallback(video_name, script_data)
            print(f"  → Script saved locally: {fallback_path}")
            results.append({"video": video_path.name, "status": "partial", "fallback": fallback_path})

        print()

    # Summary
    print("=" * 50)
    print("SUMMARY")
    print("=" * 50)
    success = sum(1 for r in results if r["status"] == "success")
    partial = sum(1 for r in results if r["status"] == "partial")
    failed = sum(1 for r in results if r["status"] == "failed")
    print(f"  ✓ Success: {success}")
    if partial:
        print(f"  ~ Partial (script saved locally): {partial}")
    if failed:
        print(f"  ✗ Failed: {failed}")

    for r in results:
        status_icon = {"success": "✓", "partial": "~", "failed": "✗"}[r["status"]]
        line = f"  {status_icon} {r['video']}"
        if r.get("step"):
            line += f" (failed at {r['step']})"
        if r.get("fallback"):
            line += f" → {r['fallback']}"
        print(line)


if __name__ == "__main__":
    process_videos()
