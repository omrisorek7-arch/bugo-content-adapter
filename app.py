import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request

load_dotenv(Path(__file__).parent / ".env", override=True)

from adapt import adapt_script
from adapt_outdoor import adapt_script_outdoor
from drive_upload import upload_to_drive
from transcribe import transcribe

app = Flask(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}

# Store job data: job_id -> {"dir": temp_dir_path, "files": [filename, ...]}
_jobs: dict = {}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    """Accept uploaded video files, save to a temp directory, return a job_id."""
    files = request.files.getlist("videos")
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    job_id = uuid.uuid4().hex[:12]
    tmp_dir = tempfile.mkdtemp(prefix=f"bugo_{job_id}_")
    saved = []

    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in VIDEO_EXTENSIONS:
            continue
        safe_name = f.filename
        dest = os.path.join(tmp_dir, safe_name)
        f.save(dest)
        saved.append(safe_name)

    if not saved:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": "No valid video files"}), 400

    _jobs[job_id] = {"dir": tmp_dir, "files": saved}
    return jsonify({"job_id": job_id, "files": saved})


@app.route("/process/<job_id>")
def process(job_id):
    """SSE endpoint: transcribe + adapt only (NO Drive upload)."""
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    def generate():
        tmp_dir = job["dir"]
        files = job["files"]

        for filename in files:
            video_path = os.path.join(tmp_dir, filename)

            def send_status(status):
                return f"event: status\ndata: {json.dumps({'video': filename, 'status': status})}\n\n"

            yield send_status("transcribing")

            # Step 1: Transcribe
            try:
                result = transcribe(video_path)
                transcript_text = result["text"]
                language = result["language"]
            except Exception as e:
                yield send_status("error")
                yield f"event: result\ndata: {json.dumps({'video': filename, 'error': f'Transcription failed: {e}'})}\n\n"
                continue

            # Step 2: Adapt (no Drive upload here — user approves manually)
            yield send_status("adapting")
            try:
                script_data = adapt_script(transcript_text, language)
            except Exception as e:
                yield send_status("error")
                yield f"event: result\ndata: {json.dumps({'video': filename, 'error': f'Adaptation failed: {e}'})}\n\n"
                continue

            # Done — send scripts to UI for review/editing
            yield send_status("done")
            result_data = {
                "video": filename,
                "hebrew": script_data.get("hebrew", ""),
                "hooks_he": script_data.get("hooks_he", ""),
                "english": script_data.get("english", ""),
                "hooks_en": script_data.get("hooks_en", ""),
                "job_id": job_id,
            }
            yield f"event: result\ndata: {json.dumps(result_data, ensure_ascii=False)}\n\n"

        yield "event: done\ndata: {}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/retranslate", methods=["POST"])
def retranslate():
    """Take edited Hebrew script + hooks and re-translate both to English."""
    data = request.get_json() or {}
    hebrew_script = data.get("hebrew", "").strip()
    hebrew_hooks = data.get("hooks_he", "").strip()
    print("hooks_he received:", hebrew_hooks[:100] if hebrew_hooks else "(empty)")
    if not hebrew_script:
        return jsonify({"error": "No Hebrew text provided"}), 400

    import anthropic
    try:
        client = anthropic.Anthropic()

        prompt_parts = [
            "Here is a Hebrew ad script that has been edited:\n\n",
            hebrew_script,
        ]
        if hebrew_hooks:
            prompt_parts.append(f"\n\nHebrew hooks:\n{hebrew_hooks}")

        prompt_parts.append(
            "\n\nTranslate and adapt this into natural, fluent US English for the same brand (Bugo). "
            "Keep the same structure, tone and emotional beats.\n\n"
        )

        if hebrew_hooks:
            prompt_parts.append(
                "Return the English script first, then on a new line write ---HOOKS--- "
                "and then the English hooks (numbered).\n"
                "Translate ALL hooks exactly - same number as in the Hebrew, no more no less.\n"
                "No explanations, just the script and hooks."
            )
        else:
            prompt_parts.append("Return only the English script, no explanations.")

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system="You are a copywriter for Bugo, an ultrasonic pest repeller brand.",
            messages=[{"role": "user", "content": "".join(prompt_parts)}],
        )

        raw = message.content[0].text.strip()

        if hebrew_hooks and "---HOOKS---" in raw:
            parts = raw.split("---HOOKS---", 1)
            return jsonify({
                "english": parts[0].strip(),
                "hooks_en": parts[1].strip(),
            })
        else:
            return jsonify({"english": raw, "hooks_en": ""})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/approve-upload", methods=["POST"])
def approve_upload():
    """Manual upload: user clicks approve -> upload video + append script to Drive."""
    data = request.get_json() or {}

    job_id = data.get("job_id", "")
    filename = data.get("video", "")
    script_data = {
        "hebrew": data.get("hebrew", ""),
        "hooks_he": data.get("hooks_he", ""),
        "english": data.get("english", ""),
        "hooks_en": data.get("hooks_en", ""),
    }

    doc_id = os.environ.get("GOOGLE_DOC_ID", "")
    video_folder_id = os.environ.get("GOOGLE_DRIVE_VIDEO_FOLDER_ID", "")

    if not doc_id:
        return jsonify({"error": "GOOGLE_DOC_ID not configured in .env"}), 500
    if not video_folder_id:
        return jsonify({"error": "GOOGLE_DRIVE_VIDEO_FOLDER_ID not configured in .env"}), 500

    # Find the video file in the job's temp dir
    job = _jobs.get(job_id)
    video_path = None
    if job:
        candidate = os.path.join(job["dir"], filename)
        if os.path.exists(candidate):
            video_path = candidate

    if not video_path:
        return jsonify({"error": f"Video file not found: {filename}"}), 404

    try:
        result = upload_to_drive(video_path, script_data, doc_id, video_folder_id)
        return jsonify({
            "success": True,
            "doc_id": result["doc_id"],
            "video_file_id": result["video_file_id"],
            "video_folder_id": video_folder_id,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/process-outdoor/<job_id>")
def process_outdoor(job_id):
    """SSE endpoint for the OUTDOOR pipeline: transcribe + adapt with the outdoor brand."""
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    def generate():
        tmp_dir = job["dir"]
        files = job["files"]

        for filename in files:
            video_path = os.path.join(tmp_dir, filename)

            def send_status(status):
                return f"event: status\ndata: {json.dumps({'video': filename, 'status': status})}\n\n"

            yield send_status("transcribing")

            try:
                result = transcribe(video_path)
                transcript_text = result["text"]
                language = result["language"]
            except Exception as e:
                yield send_status("error")
                yield f"event: result\ndata: {json.dumps({'video': filename, 'error': f'Transcription failed: {e}'})}\n\n"
                continue

            yield send_status("adapting")
            try:
                script_data = adapt_script_outdoor(transcript_text, language)
            except Exception as e:
                yield send_status("error")
                yield f"event: result\ndata: {json.dumps({'video': filename, 'error': f'Adaptation failed: {e}'})}\n\n"
                continue

            yield send_status("done")
            result_data = {
                "video": filename,
                "hebrew": script_data.get("hebrew", ""),
                "hooks_he": script_data.get("hooks_he", ""),
                "english": script_data.get("english", ""),
                "hooks_en": script_data.get("hooks_en", ""),
                "job_id": job_id,
            }
            yield f"event: result\ndata: {json.dumps(result_data, ensure_ascii=False)}\n\n"

        yield "event: done\ndata: {}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/retranslate-outdoor", methods=["POST"])
def retranslate_outdoor():
    """Take edited Hebrew script + hooks (outdoor) and re-translate both to English."""
    data = request.get_json() or {}
    hebrew_script = data.get("hebrew", "").strip()
    hebrew_hooks = data.get("hooks_he", "").strip()
    if not hebrew_script:
        return jsonify({"error": "No Hebrew text provided"}), 400

    import anthropic
    try:
        client = anthropic.Anthropic()

        prompt_parts = [
            "Here is a Hebrew ad script that has been edited:\n\n",
            hebrew_script,
        ]
        if hebrew_hooks:
            prompt_parts.append(f"\n\nHebrew hooks:\n{hebrew_hooks}")

        prompt_parts.append(
            "\n\nTranslate this into natural, fluent US English for the Bugo Outdoor brand. "
            "Stay as close to the Hebrew as possible — minimal adaptation, just an accurate "
            "idiomatic translation. Keep the same structure, tone, and emotional beats. "
            "Do not rewrite or embellish.\n\n"
        )

        if hebrew_hooks:
            prompt_parts.append(
                "Return the English script first, then on a new line write ---HOOKS--- "
                "and then the English hooks (numbered).\n"
                "Translate ALL hooks exactly - same number as in the Hebrew, no more no less.\n"
                "No explanations, just the script and hooks."
            )
        else:
            prompt_parts.append("Return only the English script, no explanations.")

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system="You are a copywriter for Bugo Outdoor, a solar-powered ground-vibration pest repeller for snakes, mice, rats and outdoor pests.",
            messages=[{"role": "user", "content": "".join(prompt_parts)}],
        )

        raw = message.content[0].text.strip()

        if hebrew_hooks and "---HOOKS---" in raw:
            parts = raw.split("---HOOKS---", 1)
            return jsonify({
                "english": parts[0].strip(),
                "hooks_en": parts[1].strip(),
            })
        else:
            return jsonify({"english": raw, "hooks_en": ""})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("=" * 50)
    print("  Bugo Content Adapter")
    print("  http://localhost:5000")
    print("=" * 50)
    app.run(debug=False, port=5000, threaded=True)
