"""
Pets Replicator module — near-1:1 translation of getfurlife videos to Hebrew/English
with a single brand-name override (getfurlife → Bugo), plus fact-grounded hook generation.

This module is completely isolated from adapt.py / adapt_outdoor.py / replicator.py.
It reuses helper utilities from replicator.py (split_hook_and_body, generate_extra_hooks,
build_hooks_block, load_facts_library) so we don't duplicate logic, but defines its own
BRAND_OVERRIDES and its own translate function so the existing PestLab flow is untouched.
"""

import logging
from typing import List

import anthropic

# Reuse helpers from the existing replicator without modifying it.
from replicator import (
    build_hooks_block,
    generate_extra_hooks,
    split_hook_and_body,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pets-specific brand overrides.
# Per the user's direction (2026-05-28): only the brand name. Everything else
# translates 1:1 from the reference video. To add more overrides later, append
# entries here following the same shape as replicator.BRAND_OVERRIDES.
# ---------------------------------------------------------------------------
PETS_BRAND_OVERRIDES: List[dict] = [
    {
        "from": [
            "GetFurLife", "Getfurlife", "getfurlife", "GETFURLIFE",
            "Get Fur Life", "get fur life", "Get fur life",
            "גטפרלייף", "גט פר לייף",
        ],
        "to": "באגו",
        "to_en": "Bugo",
    },
]


def _format_overrides_for_prompt(target_lang: str) -> str:
    """Render PETS_BRAND_OVERRIDES as a bulleted instruction block for the target language."""
    key = "to_he" if target_lang == "he" else "to_en"
    lines: List[str] = []
    for entry in PETS_BRAND_OVERRIDES:
        target = entry.get(key) or entry.get("to") or ""
        if not target:
            continue
        sources_str = ", ".join(f'"{s}"' for s in entry["from"])
        lines.append(f'- If you encounter any of {sources_str}, render it as: "{target}".')
    return "\n".join(lines)


def translate_literal_to_hebrew_pets(transcription: str, source_language: str) -> str:
    """
    Translate the transcript into natural Hebrew with minimal adaptation.
    Applies only the getfurlife → באגו override; everything else stays 1:1.

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


def overrides_instructions_en_pets() -> str:
    """English-side override instructions, appended to /retranslate-replicator-pets prompt."""
    return _format_overrides_for_prompt("en")


# Re-export the helpers app.py needs, so the pets flow can mirror the
# existing replicator imports cleanly.
__all__ = [
    "PETS_BRAND_OVERRIDES",
    "translate_literal_to_hebrew_pets",
    "overrides_instructions_en_pets",
    "split_hook_and_body",
    "generate_extra_hooks",
    "build_hooks_block",
]
