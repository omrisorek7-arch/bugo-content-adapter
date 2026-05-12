import os
from pathlib import Path

import anthropic

BRAND_BOOK_OUTDOOR_PATH = Path(__file__).parent / "brand_book_outdoor.md"

SYSTEM_PROMPT_OUTDOOR = (
    "You are a copywriter for Bugo Outdoor, a solar-powered ground-vibration pest "
    "repeller for outdoor use in Israel. The device pulses ground vibrations every "
    "30 seconds, covers up to 300 m² per unit, and targets snakes (including the "
    "Israeli viper), rats, mice, moles (Spalax), garden ants, mosquitoes (including "
    "West Nile virus carriers), and subterranean insects. It is chemical-free, "
    "poison-free, silent, and safe for children and pets."
)


def _parse_response(text: str) -> dict:
    """Parse Claude's response by splitting on --- markers."""
    result = {"hebrew": "", "hooks_he": "", "english": "", "hooks_en": ""}

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

    if current_key and current_lines:
        sections[current_key] = "\n".join(current_lines).strip()

    result.update(sections)
    return result


def adapt_script_outdoor(transcription: str, language: str) -> dict:
    """
    Call Claude API to adapt a transcription into dual-language Bugo Outdoor scripts.

    Stays as close to the original reference as possible — minimal changes, only
    Hebrew localization and brand-name swap when the source is already about a
    similar outdoor electronic pest repeller.

    Args:
        transcription: The full transcription text from Whisper.
        language: Detected language code (e.g. "he", "en").

    Returns:
        dict with keys: hebrew, hooks_he, english, hooks_en
    """
    brand_book = ""
    if BRAND_BOOK_OUTDOOR_PATH.exists():
        brand_book = BRAND_BOOK_OUTDOOR_PATH.read_text(encoding="utf-8")

    client = anthropic.Anthropic()

    brand_section = (
        f"\n\nBrand book for reference:\n{brand_book}" if brand_book.strip() else ""
    )

    user_message = (
        f"Here is a video transcription:\n\n{transcription}\n\n"
        f"Original language: {language}{brand_section}\n\n"
        f"CRITICAL ADAPTATION RULES — READ CAREFULLY:\n"
        f"1. Stay AS CLOSE AS POSSIBLE to the original reference. Preserve structure, "
        f"hook, pacing, emotional beats, and concrete claims of the source.\n"
        f"2. If the reference is already advertising an outdoor electronic/solar pest "
        f"repeller targeting similar pests (snakes, mice, rats, rodents, moles, "
        f"reptiles), make NEAR-ZERO changes — only translate to natural Israeli "
        f"Hebrew if needed, and swap any competitor brand name for 'Bugo'. Do NOT "
        f"rewrite, reframe, or 'improve' the script.\n"
        f"3. Only deviate from the source when the source makes a claim Bugo Outdoor "
        f"cannot truthfully make (wrong pest category, indoor-only context). Change "
        f"the minimum number of words.\n"
        f"4. Use colloquial Israeli Hebrew. No scene directions. No stage cues.\n\n"
        f"Write TWO complete ad scripts — Hebrew and US English — plus 3 alternative "
        f"opening hooks per language.\n\n"
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
        system=SYSTEM_PROMPT_OUTDOOR,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = message.content[0].text
    return _parse_response(raw)
