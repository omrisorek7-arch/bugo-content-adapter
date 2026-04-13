import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
]
CREDENTIALS_PATH = Path(__file__).parent / "credentials.json"
TOKEN_PATH = Path(__file__).parent / "token.json"


def _get_credentials():
    """Authenticate and return Google API credentials (with token caching)."""
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())
    return creds


def _build_services(creds):
    drive = build("drive", "v3", credentials=creds)
    docs = build("docs", "v1", credentials=creds)
    return drive, docs


# ---------------------------------------------------------------------------
# Video upload – to dedicated video folder, NO subfolders
# ---------------------------------------------------------------------------

def _upload_video(drive, video_folder_id: str, video_path: str) -> str:
    """Upload a video file to the video reference folder. Returns file ID."""
    file_name = os.path.basename(video_path)
    file_size = os.path.getsize(video_path)

    metadata = {"name": file_name, "parents": [video_folder_id]}

    if file_size > 5 * 1024 * 1024:
        media = MediaFileUpload(video_path, resumable=True, chunksize=10 * 1024 * 1024)
    else:
        media = MediaFileUpload(video_path)

    uploaded = drive.files().create(
        body=metadata, media_body=media, fields="id"
    ).execute()
    return uploaded["id"]


# ---------------------------------------------------------------------------
# Google Doc – append-only to a single fixed doc (GOOGLE_DOC_ID)
# ---------------------------------------------------------------------------

def _get_doc_end_index(docs, doc_id: str) -> int:
    """Get the end index of the document body (where to append new text)."""
    doc = docs.documents().get(documentId=doc_id).execute()
    content = doc.get("body", {}).get("content", [])
    if content:
        return content[-1].get("endIndex", 1) - 1
    return 1


def _count_existing_sections(docs, doc_id: str) -> int:
    """Count how many 'Vid N' sections already exist in the document."""
    doc = docs.documents().get(documentId=doc_id).execute()
    content = doc.get("body", {}).get("content", [])

    count = 0
    for element in content:
        paragraph = element.get("paragraph", {})
        for text_run in paragraph.get("elements", []):
            text = text_run.get("textRun", {}).get("content", "")
            if re.search(r"Vid \d+\s*–", text):
                count += 1
    return count


def _append_video_section(docs, doc_id: str, vid_number: int, script_data: dict):
    """Append a formatted video section to the end of the document.

    Args:
        script_data: dict with keys hebrew, hooks_he, english, hooks_en
    """
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")

    separator = "═" * 23
    thin_sep = "─" * 21
    section_text = (
        f"\n{separator}\n"
        f"Vid {vid_number} – {date_str} {time_str}\n"
        f"{separator}\n\n"
        f"🇮🇱 סקריפט עברית\n\n"
        f"{script_data.get('hebrew', '')}\n\n"
        f"הוקים:\n"
        f"{script_data.get('hooks_he', '')}\n\n"
        f"{thin_sep}\n\n"
        f"🇺🇸 English Script\n\n"
        f"{script_data.get('english', '')}\n\n"
        f"Hooks:\n"
        f"{script_data.get('hooks_en', '')}\n"
    )

    end_index = _get_doc_end_index(docs, doc_id)

    # Insert the section text
    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{
            "insertText": {
                "location": {"index": end_index},
                "text": section_text,
            }
        }]}
    ).execute()

    # --- Second pass: apply heading styles ---
    doc = docs.documents().get(documentId=doc_id).execute()
    content = doc.get("body", {}).get("content", [])

    vid_title = f"Vid {vid_number} – {date_str} {time_str}"
    format_requests = []

    for element in content:
        paragraph = element.get("paragraph", {})
        for text_run in paragraph.get("elements", []):
            text = text_run.get("textRun", {}).get("content", "")
            start = text_run.get("startIndex", 0)
            end = text_run.get("endIndex", start)

            # Only format elements within our newly inserted section
            if start < end_index:
                continue

            # Vid title -> Heading 1 + bold
            if vid_title in text:
                format_requests.append({
                    "updateParagraphStyle": {
                        "range": {"startIndex": start, "endIndex": end},
                        "paragraphStyle": {"namedStyleType": "HEADING_1"},
                        "fields": "namedStyleType",
                    }
                })
                format_requests.append({
                    "updateTextStyle": {
                        "range": {"startIndex": start, "endIndex": end},
                        "textStyle": {"bold": True, "fontSize": {"magnitude": 14, "unit": "PT"}},
                        "fields": "bold,fontSize",
                    }
                })

            # Language headers -> Heading 2
            stripped = text.strip()
            if stripped in ("🇮🇱 סקריפט עברית", "🇺🇸 English Script"):
                format_requests.append({
                    "updateParagraphStyle": {
                        "range": {"startIndex": start, "endIndex": end},
                        "paragraphStyle": {"namedStyleType": "HEADING_2"},
                        "fields": "namedStyleType",
                    }
                })

    if format_requests:
        docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": format_requests}
        ).execute()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upload_to_drive(
    video_path: str,
    script_data: dict,
    doc_id: str,
    video_folder_id: str,
) -> dict:
    """
    Upload video to the reference video folder and append script to the fixed doc.

    No subfolders are created. No new docs are created.

    Args:
        video_path: Path to the original video file.
        script_data: dict with keys hebrew, hooks_he, english, hooks_en.
        doc_id: Google Doc ID to append to (from GOOGLE_DOC_ID env).
        video_folder_id: Folder ID for reference videos (from GOOGLE_DRIVE_VIDEO_FOLDER_ID env).

    Returns:
        dict with video_file_id, doc_id.
    """
    creds = _get_credentials()
    drive, docs = _build_services(creds)

    # Upload video to the flat reference video folder
    print(f"  Uploading video to Drive...")
    video_file_id = _upload_video(drive, video_folder_id, video_path)

    # Append to the fixed doc
    print(f"  Appending script to doc...")
    vid_number = _count_existing_sections(docs, doc_id) + 1
    print(f"  Writing as Vid {vid_number}...")
    _append_video_section(docs, doc_id, vid_number, script_data)

    return {
        "video_file_id": video_file_id,
        "doc_id": doc_id,
    }
