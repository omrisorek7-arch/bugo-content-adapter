import os
import subprocess
import tempfile

import whisper


_model = None


def _get_model():
    global _model
    if _model is None:
        print("  Loading Whisper large-v2 model (first run may download ~3GB)...")
        _model = whisper.load_model("large-v2")
    return _model


def _extract_audio(video_path: str) -> str:
    """Extract audio from video to a temporary WAV file."""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", video_path,
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            tmp.name,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    return tmp.name


def transcribe(video_path: str) -> dict:
    """
    Transcribe a video file using Whisper.

    Returns dict with keys:
        - text: full transcription string
        - language: detected language code (e.g. "he", "en")
    """
    audio_path = _extract_audio(video_path)
    try:
        model = _get_model()
        result = model.transcribe(audio_path, task="transcribe")
        return {
            "text": result["text"].strip(),
            "language": result["language"],
        }
    finally:
        os.unlink(audio_path)
