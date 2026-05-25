"""
Replicator module — near-1:1 translation of PestLab videos to Hebrew/English
with narrow brand-name + brand-fact overrides, plus fact-grounded hook generation.

This module is completely isolated from adapt.py / adapt_outdoor.py. Importing or
running it has zero side-effects on the Indoor / Outdoor flows.
"""

import logging
import os
import re
from pathlib import Path
from typing import Tuple, List, Optional

import anthropic

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurable substitutions — add a new override by appending one entry here.
# Each entry has "from" (list of source phrases to look for) and either:
#   - "to": single replacement (used for both Hebrew and English), OR
#   - "to_he" + "to_en": language-specific replacements.
# ---------------------------------------------------------------------------
BRAND_OVERRIDES: List[dict] = [
    {
        "from": ["PestLab", "Pest Lab", "pest lab", "PESTLAB", "פסט לאב", "פסטלאב"],
        "to": "באגו",
        "to_en": "Bugo",
    },
    {
        "from": [
            "90 day money-back", "90-day money-back",
            "90 day guarantee", "90-day guarantee",
            "90 days money-back", "90 days guarantee",
            "תשעים יום אחריות", "90 יום אחריות",
        ],
        "to_he": "30 יום אחריות להחזר כספי",
        "to_en": "30-day money-back guarantee",
    },
]


# ---------------------------------------------------------------------------
# Profile picker — Drive/Docs destination per user profile.
# Read lazily from env so a .env reload picks up changes without restart.
# ---------------------------------------------------------------------------
def get_profiles() -> dict:
    return {
        "ido": {
            "doc_id": os.environ.get("IDO_GOOGLE_DOC_ID", "").strip(),
            "folder_id": os.environ.get("IDO_GOOGLE_DRIVE_VIDEO_FOLDER_ID", "").strip(),
            "label_he": "עידו",
        },
        "hadar": {
            "doc_id": os.environ.get("HADAR_GOOGLE_DOC_ID", "").strip(),
            "folder_id": os.environ.get("HADAR_GOOGLE_DRIVE_VIDEO_FOLDER_ID", "").strip(),
            "label_he": "הדר",
        },
    }


# ---------------------------------------------------------------------------
# Facts library — toxicology-report.md + proof-points.md, cached after first read.
# ---------------------------------------------------------------------------
_FACTS_CACHE: Optional[str] = None

_FACTS_SOURCES = [
    Path(__file__).parent.parent / "bugo-brand-strategy" / "sections" / "drafts" / "toxicology-report.md",
    Path(__file__).parent.parent / "bugo-brand-strategy" / "sections" / "drafts" / "proof-points.md",
]


def load_facts_library() -> str:
    """Concatenate available facts files. Cached. Missing files are skipped silently."""
    global _FACTS_CACHE
    if _FACTS_CACHE is not None:
        return _FACTS_CACHE

    chunks: List[str] = []
    for src in _FACTS_SOURCES:
        if src.exists():
            try:
                chunks.append(f"\n\n=== FROM {src.name} ===\n\n" + src.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("Failed to read facts file %s: %s", src, e)
        else:
            logger.warning("Facts file not found: %s", src)

    _FACTS_CACHE = "\n".join(chunks).strip() if chunks else ""
    return _FACTS_CACHE


# ---------------------------------------------------------------------------
# Override formatting for prompts
# ---------------------------------------------------------------------------
def _format_overrides_for_prompt(target_lang: str) -> str:
    """Render BRAND_OVERRIDES as a bulleted instruction block for the target language."""
    key = "to_he" if target_lang == "he" else "to_en"
    lines: List[str] = []
    for entry in BRAND_OVERRIDES:
        target = entry.get(key) or entry.get("to") or ""
        if not target:
            continue
        sources_str = ", ".join(f'"{s}"' for s in entry["from"])
        lines.append(f"- If you encounter any of {sources_str}, render it as: \"{target}\".")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Near-literal Hebrew translation
# ---------------------------------------------------------------------------
def translate_literal_to_hebrew(transcription: str, source_language: str) -> str:
    """
    Translate the transcript into natural Hebrew with minimal adaptation.
    Applies BRAND_OVERRIDES exactly (PestLab → באגו, 90-day → 30 יום, etc.).

    Returns plain Hebrew script text (no markers, no hooks).
    """
    if not transcription.strip():
        return ""

    client = anthropic.Anthropic()

    overrides_block = _format_overrides_for_prompt("he")

    system_prompt = (
        "You are a translator. Translate the input video script into natural, "
        "fluent, colloquial Israeli Hebrew. Stay as close to the source as possible — "
        "minimal adaptation, just an accurate idiomatic translation. Keep the same "
        "structure, tone, and emotional beats. Do not rewrite or embellish. "
        "Apply the brand substitutions listed below exactly."
    )

    user_message = (
        f"Source language: {source_language}\n\n"
        f"Brand substitutions — apply each one exactly when you encounter the source phrase, "
        f"otherwise translate normally:\n{overrides_block}\n\n"
        f"Source transcript:\n\n{transcription}\n\n"
        f"Return ONLY the Hebrew translation as plain text. No explanations, no labels, "
        f"no markers."
    )

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return message.content[0].text.strip()


# ---------------------------------------------------------------------------
# Hook + body split (pure Python, no Claude call)
# ---------------------------------------------------------------------------
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?״׳])\s+|\n\s*\n")


def split_hook_and_body(hebrew_script: str) -> Tuple[str, str]:
    """Return (first_sentence, rest_of_script). Falls back to first paragraph if no
    sentence terminator is found within the first 200 chars."""
    text = hebrew_script.strip()
    if not text:
        return "", ""

    parts = _SENTENCE_BOUNDARY.split(text, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and len(parts[0]) <= 240:
        return parts[0].strip(), parts[1].strip()

    # Fallback — take everything before the first newline, or the whole thing if it's short.
    nl = text.find("\n")
    if 0 < nl <= 240:
        return text[:nl].strip(), text[nl:].strip()

    # Last resort — take a leading slice.
    if len(text) > 240:
        return text[:240].strip(), text[240:].strip()
    return text, ""


# ---------------------------------------------------------------------------
# 4 alternative hooks, fact-grounded, distinct angles
# ---------------------------------------------------------------------------
_NUMBERED_LINE = re.compile(r"^\s*(\d+)[\.\)]\s+(.+?)\s*$", re.MULTILINE)


def generate_extra_hooks(original_hook: str, rest_of_script: str) -> List[str]:
    """Return 4 fact-grounded Hebrew hooks touching distinct angles.
    Pads with '' if Claude returns fewer."""
    if not original_hook.strip():
        return ["", "", "", ""]

    client = anthropic.Anthropic()
    facts = load_facts_library()

    system_prompt = (
        "You write short, punchy Hebrew video hooks for short-form social media "
        "(TikTok / Reels). One sentence each, max ~12 words. Each hook must be "
        "grounded in a real fact from the facts library provided. Each hook must "
        "touch a different angle. Output is plain numbered lines, nothing else."
    )

    facts_block = facts if facts else "(facts library not available — use general pest-control common sense)"

    user_message = (
        f"זה ההוק המקורי של הסרטון:\n{original_hook}\n\n"
        f"אשמח שתכתוב לי 4 הוקים נוספים, שיתאימו ויתחברו בצורה הגיונית להמשך הסרטון.\n\n"
        f"המשך הסרטון:\n{rest_of_script}\n\n"
        f"חשוב שההוקים הנוספים יגעו ב-4 זוויות שונות — לדוגמא: רעלים, ילדים, "
        f"חיות מחמד, גודל בעיית המזיקים, היגיינה, סיכוני בריאות. אסור ששני הוקים "
        f"יגעו באותה הזווית.\n\n"
        f"כל הוק חייב להיות מבוסס על עובדה אמיתית מספריית העובדות הבאה. השתמש "
        f"בנתונים מדויקים (מספרים, אחוזים) כשאפשר. אם יש עובדות ספציפיות לישראל, "
        f"העדף אותן.\n\n"
        f"--- FACTS LIBRARY ---\n{facts_block}\n--- END FACTS LIBRARY ---\n\n"
        f"החזר רק את 4 ההוקים, ממוספרים 1-4, ללא הסבר נוסף, ללא כותרת זווית."
    )

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = message.content[0].text.strip()

    hooks: List[str] = []
    for match in _NUMBERED_LINE.finditer(raw):
        text = match.group(2).strip()
        if text:
            hooks.append(text)
        if len(hooks) >= 4:
            break

    while len(hooks) < 4:
        hooks.append("")
    return hooks[:4]


# ---------------------------------------------------------------------------
# Helpers for app.py
# ---------------------------------------------------------------------------
def build_hooks_block(original_hook: str, extras: List[str]) -> str:
    """Format the 5 hooks as the same `1. ...\\n2. ...` string the existing UI expects."""
    items = [original_hook] + list(extras)
    return "\n".join(f"{i + 1}. {h}" for i, h in enumerate(items) if h is not None)


def overrides_instructions_en() -> str:
    """English-side override instructions, appended to /retranslate-replicator prompt."""
    return _format_overrides_for_prompt("en")
