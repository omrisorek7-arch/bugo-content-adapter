import os
from pathlib import Path

import anthropic

BRAND_BOOK_PATH = Path(__file__).parent / "brand_book.md"

SYSTEM_PROMPT = "You are a copywriter for Bugo, an ultrasonic pest repeller brand sold in Israel."


def _parse_response(text: str) -> dict:
    """Parse Claude's response by splitting on --- markers."""
    result = {"hebrew": "", "hooks_he": "", "english": "", "hooks_en": ""}

    # Split into sections by the markers
    sections = {}
    current_key = None
    current_lines = []

    for line in text.split("\n"):
        stripped = line.strip()
        if stripped == "---HEBREW---":
            current_key = "hebrew"
            current_lines = []
        elif stripped == "---HOOKS_HE---":
            if current_key and current_lines:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = "hooks_he"
            current_lines = []
        elif stripped == "---ENGLISH---":
            if current_key and current_lines:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = "english"
            current_lines = []
        elif stripped == "---HOOKS_EN---":
            if current_key and current_lines:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = "hooks_en"
            current_lines = []
        else:
            if current_key is not None:
                current_lines.append(line)

    # Capture the last section
    if current_key and current_lines:
        sections[current_key] = "\n".join(current_lines).strip()

    result.update(sections)
    return result


def adapt_script(transcription: str, language: str) -> dict:
    """
    Call Claude API to adapt a transcription into dual-language Bugo scripts.

    Args:
        transcription: The full transcription text from Whisper.
        language: Detected language code (e.g. "he", "en").

    Returns:
        dict with keys: hebrew, hooks_he, english, hooks_en
    """
    brand_book = ""
    if BRAND_BOOK_PATH.exists():
        brand_book = BRAND_BOOK_PATH.read_text(encoding="utf-8")

    client = anthropic.Anthropic()

    brand_section = f"\n\nBrand book for reference:\n{brand_book}" if brand_book.strip() else ""

    user_message = (
        f"Here is a video transcription:\n\n{transcription}\n\n"
        f"Original language: {language}{brand_section}\n\n"
        f"Write TWO complete ad scripts based on this video. "
        f"Use the same structure and emotional beats.\n\n"
        f"SCRIPT 1 - Hebrew:\n"
        f"Write a complete Hebrew script for Bugo. Colloquial Israeli Hebrew. No scene directions.\n\n"
        f"SCRIPT 2 - English:\n"
        f"Write a complete English script for Bugo. US English. No scene directions.\n\n"
        f"Format your response EXACTLY like this:\n\n"
        f"---HEBREW---\n"
        f"[Hebrew script here]\n\n"
        f"---HOOKS_HE---\n"
        f"1. [hook 1]\n"
        f"2. [hook 2]\n"
        f"3. [hook 3]\n\n"
        f"---ENGLISH---\n"
        f"[English script here]\n\n"
        f"---HOOKS_EN---\n"
        f"1. [hook 1]\n"
        f"2. [hook 2]\n"
        f"3. [hook 3]"
    )

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = message.content[0].text
    return _parse_response(raw)
