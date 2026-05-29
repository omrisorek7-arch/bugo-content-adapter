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
from replicator import (
    BRAND_OVERRIDES,
    build_hooks_block,
    generate_extra_hooks,
    get_profiles,
    overrides_instructions_en,
    split_hook_and_body,
    translate_literal_to_hebrew,
)
from replicator_pets import (
    overrides_instructions_en_pets,
    translate_literal_to_hebrew_pets,
)

app = Flask(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}

# Store job data: job_id -> {"dir": temp_dir_path, "files": [filename, ...]}
_jobs: dict = {}


@app.route("/")
def index():
    # Pass the live profile list so the picker reflects whichever optional
    # profiles (PROFILE3_*, PROFILE4_*) are configured in .env.
    return render_template("index.html", profiles=get_profiles())


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


# =========================================================================
# REPLICATOR ROUTES — isolated from Indoor/Outdoor flows
# =========================================================================

@app.route("/process-replicator/<job_id>")
def process_replicator(job_id):
    """SSE endpoint: transcribe → literal Hebrew translation → 4 fact-grounded hooks."""
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

            yield send_status("translating")
            try:
                hebrew = translate_literal_to_hebrew(transcript_text, language)
            except Exception as e:
                yield send_status("error")
                yield f"event: result\ndata: {json.dumps({'video': filename, 'error': f'Translation failed: {e}'})}\n\n"
                continue

            yield send_status("generating-hooks")
            try:
                original_hook, body = split_hook_and_body(hebrew)
                extras = generate_extra_hooks(original_hook, body)
                hooks_he = build_hooks_block(original_hook, extras)
            except Exception as e:
                # Hook generation failure is non-fatal — keep the Hebrew, hooks empty.
                print(f"  Hook generation failed for {filename}: {e}")
                hooks_he = ""

            yield send_status("done")
            result_data = {
                "video": filename,
                "hebrew": hebrew,
                "hooks_he": hooks_he,
                "english": "",
                "hooks_en": "",
                "job_id": job_id,
            }
            yield f"event: result\ndata: {json.dumps(result_data, ensure_ascii=False)}\n\n"

        yield "event: done\ndata: {}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/regenerate-extra-hooks/<job_id>", methods=["POST"])
def regenerate_extra_hooks(job_id):
    """Re-roll the 4 alternative hooks against the user's currently-edited Hebrew."""
    data = request.get_json() or {}
    hebrew = data.get("hebrew", "").strip()
    if not hebrew:
        return jsonify({"error": "No Hebrew text provided"}), 400

    try:
        original_hook, body = split_hook_and_body(hebrew)
        extras = generate_extra_hooks(original_hook, body)
        hooks_he = build_hooks_block(original_hook, extras)
        return jsonify({"hooks_he": hooks_he})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/retranslate-replicator", methods=["POST"])
def retranslate_replicator():
    """Take edited Hebrew script + hooks and re-translate both to English (literal)."""
    data = request.get_json() or {}
    hebrew_script = data.get("hebrew", "").strip()
    hebrew_hooks = data.get("hooks_he", "").strip()
    if not hebrew_script:
        return jsonify({"error": "No Hebrew text provided"}), 400

    import anthropic
    try:
        client = anthropic.Anthropic()

        overrides_en = overrides_instructions_en()

        prompt_parts = [
            "Here is a Hebrew video script to translate:\n\n",
            hebrew_script,
        ]
        if hebrew_hooks:
            prompt_parts.append(f"\n\nHebrew hooks:\n{hebrew_hooks}")

        prompt_parts.append(
            "\n\nTranslate this into natural, fluent US English. "
            "Stay as close to the Hebrew as possible — minimal adaptation, just an accurate "
            "idiomatic translation. Keep the same structure, tone, and emotional beats. "
            "Do not rewrite or embellish.\n\n"
            f"Apply these brand substitutions exactly:\n{overrides_en}\n\n"
        )

        if hebrew_hooks:
            prompt_parts.append(
                "Return the English script first, then on a new line write ---HOOKS--- "
                "and then the English hooks (numbered).\n"
                "Translate ALL hooks exactly — same number as in the Hebrew, no more no less.\n"
                "No explanations, just the script and hooks."
            )
        else:
            prompt_parts.append("Return only the English script, no explanations.")

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=(
                "You are a translator. Translate Hebrew video scripts into natural, "
                "fluent US English. No brand voice — render exactly what the Hebrew says."
            ),
            messages=[{"role": "user", "content": "".join(prompt_parts)}],
        )

        raw = message.content[0].text.strip()
        if hebrew_hooks and "---HOOKS---" in raw:
            parts = raw.split("---HOOKS---", 1)
            return jsonify({"english": parts[0].strip(), "hooks_en": parts[1].strip()})
        return jsonify({"english": raw, "hooks_en": ""})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/approve-upload-with-profile", methods=["POST"])
def approve_upload_with_profile():
    """Shared profile-aware upload for Indoor and Outdoor tabs.
    Logic mirrors /approve-upload-replicator. Existing /approve-upload route
    is left untouched for backward compatibility."""
    data = request.get_json() or {}

    job_id = data.get("job_id", "")
    filename = data.get("video", "")
    profile_key = (data.get("profile") or "").strip().lower()
    custom_name = (data.get("custom_name") or "").strip()
    include_english = bool(data.get("include_english", False))

    profiles = get_profiles()
    if profile_key not in profiles:
        return jsonify({"error": "יש לבחור פרופיל לפני ההעלאה"}), 400

    profile = profiles[profile_key]
    profile_label = profile["label_he"]
    doc_id = profile["doc_id"]
    folder_id = profile["folder_id"]

    if not doc_id:
        return jsonify({"error": f"{profile_label} לא הוגדר ב-.env — חסר {profile_key.upper()}_GOOGLE_DOC_ID"}), 500
    if not folder_id:
        return jsonify({"error": f"{profile_label} לא הוגדר ב-.env — חסר {profile_key.upper()}_GOOGLE_DRIVE_VIDEO_FOLDER_ID"}), 500

    script_data = {
        "hebrew": data.get("hebrew", ""),
        "hooks_he": data.get("hooks_he", ""),
        "english": data.get("english", ""),
        "hooks_en": data.get("hooks_en", ""),
    }

    job = _jobs.get(job_id)
    video_path = None
    if job:
        candidate = os.path.join(job["dir"], filename)
        if os.path.exists(candidate):
            video_path = candidate
    if not video_path:
        return jsonify({"error": f"Video file not found: {filename}"}), 404

    try:
        result = upload_to_drive(video_path, script_data, doc_id, folder_id, custom_name, include_english)
        return jsonify({
            "success": True,
            "profile": profile_key,
            "profile_label": profile_label,
            "doc_id": result["doc_id"],
            "video_file_id": result["video_file_id"],
            "video_folder_id": folder_id,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/approve-upload-replicator", methods=["POST"])
def approve_upload_replicator():
    """Profile-aware upload for the PestLab Replicator tab."""
    data = request.get_json() or {}

    job_id = data.get("job_id", "")
    filename = data.get("video", "")
    profile_key = (data.get("profile") or "").strip().lower()
    custom_name = (data.get("custom_name") or "").strip()
    include_english = bool(data.get("include_english", False))

    profiles = get_profiles()
    if profile_key not in profiles:
        return jsonify({"error": "יש לבחור פרופיל לפני ההעלאה"}), 400

    profile = profiles[profile_key]
    profile_label = profile["label_he"]
    doc_id = profile["doc_id"]
    folder_id = profile["folder_id"]

    if not doc_id:
        return jsonify({"error": f"{profile_label} לא הוגדר ב-.env — חסר {profile_key.upper()}_GOOGLE_DOC_ID"}), 500
    if not folder_id:
        return jsonify({"error": f"{profile_label} לא הוגדר ב-.env — חסר {profile_key.upper()}_GOOGLE_DRIVE_VIDEO_FOLDER_ID"}), 500

    script_data = {
        "hebrew": data.get("hebrew", ""),
        "hooks_he": data.get("hooks_he", ""),
        "english": data.get("english", ""),
        "hooks_en": data.get("hooks_en", ""),
    }

    job = _jobs.get(job_id)
    video_path = None
    if job:
        candidate = os.path.join(job["dir"], filename)
        if os.path.exists(candidate):
            video_path = candidate
    if not video_path:
        return jsonify({"error": f"Video file not found: {filename}"}), 404

    try:
        result = upload_to_drive(video_path, script_data, doc_id, folder_id, custom_name, include_english)
        return jsonify({
            "success": True,
            "profile": profile_key,
            "profile_label": profile_label,
            "doc_id": result["doc_id"],
            "video_file_id": result["video_file_id"],
            "video_folder_id": folder_id,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =========================================================================
# PETS REPLICATOR ROUTES — isolated from PestLab Replicator and from
# Indoor/Outdoor. Uses replicator_pets.PETS_BRAND_OVERRIDES (getfurlife → Bugo).
# =========================================================================

@app.route("/process-replicator-pets/<job_id>")
def process_replicator_pets(job_id):
    """SSE endpoint: transcribe → literal Hebrew translation (pets overrides) → 4 hooks."""
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

            yield send_status("translating")
            try:
                hebrew = translate_literal_to_hebrew_pets(transcript_text, language)
            except Exception as e:
                yield send_status("error")
                yield f"event: result\ndata: {json.dumps({'video': filename, 'error': f'Translation failed: {e}'})}\n\n"
                continue

            yield send_status("generating-hooks")
            try:
                original_hook, body = split_hook_and_body(hebrew)
                extras = generate_extra_hooks(original_hook, body)
                hooks_he = build_hooks_block(original_hook, extras)
            except Exception as e:
                print(f"  Hook generation failed for {filename}: {e}")
                hooks_he = ""

            yield send_status("done")
            result_data = {
                "video": filename,
                "hebrew": hebrew,
                "hooks_he": hooks_he,
                "english": "",
                "hooks_en": "",
                "job_id": job_id,
            }
            yield f"event: result\ndata: {json.dumps(result_data, ensure_ascii=False)}\n\n"

        yield "event: done\ndata: {}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/regenerate-extra-hooks-pets/<job_id>", methods=["POST"])
def regenerate_extra_hooks_pets(job_id):
    """Re-roll the 4 alternative hooks against the user's currently-edited Hebrew (pets tab)."""
    data = request.get_json() or {}
    hebrew = data.get("hebrew", "").strip()
    if not hebrew:
        return jsonify({"error": "No Hebrew text provided"}), 400

    try:
        original_hook, body = split_hook_and_body(hebrew)
        extras = generate_extra_hooks(original_hook, body)
        hooks_he = build_hooks_block(original_hook, extras)
        return jsonify({"hooks_he": hooks_he})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/retranslate-replicator-pets", methods=["POST"])
def retranslate_replicator_pets():
    """Take edited Hebrew script + hooks (pets) and re-translate both to English (literal)."""
    data = request.get_json() or {}
    hebrew_script = data.get("hebrew", "").strip()
    hebrew_hooks = data.get("hooks_he", "").strip()
    if not hebrew_script:
        return jsonify({"error": "No Hebrew text provided"}), 400

    import anthropic
    try:
        client = anthropic.Anthropic()

        overrides_en = overrides_instructions_en_pets()

        prompt_parts = [
            "Here is a Hebrew video script to translate:\n\n",
            hebrew_script,
        ]
        if hebrew_hooks:
            prompt_parts.append(f"\n\nHebrew hooks:\n{hebrew_hooks}")

        prompt_parts.append(
            "\n\nTranslate this into natural, fluent US English. "
            "Stay as close to the Hebrew as possible — minimal adaptation, just an accurate "
            "idiomatic translation. Keep the same structure, tone, and emotional beats. "
            "Do not rewrite or embellish.\n\n"
            f"Apply these brand substitutions exactly:\n{overrides_en}\n\n"
        )

        if hebrew_hooks:
            prompt_parts.append(
                "Return the English script first, then on a new line write ---HOOKS--- "
                "and then the English hooks (numbered).\n"
                "Translate ALL hooks exactly — same number as in the Hebrew, no more no less.\n"
                "No explanations, just the script and hooks."
            )
        else:
            prompt_parts.append("Return only the English script, no explanations.")

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=(
                "You are a translator. Translate Hebrew video scripts into natural, "
                "fluent US English. No brand voice — render exactly what the Hebrew says."
            ),
            messages=[{"role": "user", "content": "".join(prompt_parts)}],
        )

        raw = message.content[0].text.strip()
        if hebrew_hooks and "---HOOKS---" in raw:
            parts = raw.split("---HOOKS---", 1)
            return jsonify({"english": parts[0].strip(), "hooks_en": parts[1].strip()})
        return jsonify({"english": raw, "hooks_en": ""})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/approve-upload-replicator-pets", methods=["POST"])
def approve_upload_replicator_pets():
    """Profile-aware upload for the Pets Replicator tab."""
    data = request.get_json() or {}

    job_id = data.get("job_id", "")
    filename = data.get("video", "")
    profile_key = (data.get("profile") or "").strip().lower()
    custom_name = (data.get("custom_name") or "").strip()
    include_english = bool(data.get("include_english", False))

    profiles = get_profiles()
    if profile_key not in profiles:
        return jsonify({"error": "יש לבחור פרופיל לפני ההעלאה"}), 400

    profile = profiles[profile_key]
    profile_label = profile["label_he"]
    doc_id = profile["doc_id"]
    folder_id = profile["folder_id"]

    if not doc_id:
        return jsonify({"error": f"{profile_label} לא הוגדר ב-.env — חסר {profile_key.upper()}_GOOGLE_DOC_ID"}), 500
    if not folder_id:
        return jsonify({"error": f"{profile_label} לא הוגדר ב-.env — חסר {profile_key.upper()}_GOOGLE_DRIVE_VIDEO_FOLDER_ID"}), 500

    script_data = {
        "hebrew": data.get("hebrew", ""),
        "hooks_he": data.get("hooks_he", ""),
        "english": data.get("english", ""),
        "hooks_en": data.get("hooks_en", ""),
    }

    job = _jobs.get(job_id)
    video_path = None
    if job:
        candidate = os.path.join(job["dir"], filename)
        if os.path.exists(candidate):
            video_path = candidate
    if not video_path:
        return jsonify({"error": f"Video file not found: {filename}"}), 404

    try:
        result = upload_to_drive(video_path, script_data, doc_id, folder_id, custom_name, include_english)
        return jsonify({
            "success": True,
            "profile": profile_key,
            "profile_label": profile_label,
            "doc_id": result["doc_id"],
            "video_file_id": result["video_file_id"],
            "video_folder_id": folder_id,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    print("=" * 50)
    print("  Bugo Content Adapter")
    print(f"  http://localhost:{port}")
    print("=" * 50)
    # Port 5001 by default — macOS AirPlay Receiver grabs 5000 and returns 403.
    app.run(debug=False, port=port, threaded=True)
