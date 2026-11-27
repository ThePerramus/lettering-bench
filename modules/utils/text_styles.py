"""Text styles by box type (dialogue, caption, shout...).

Each text block can carry a type (TextBlock.text_style), set by the LLM
translator when it classifies the boxes or picked by hand in the Text panel.
A project keeps one style per type (font, bold/italic/underline, alignment,
color, outline, line spacing); Render letters each box with its type's style
instead of the Text panel's settings. Types without a style defined render
with the panel's settings, as before.

Styles live in the project file and, as the starting point for new
projects, in QSettings.
"""

from __future__ import annotations

import json

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor

# (key, label). The keys are what the LLM answers with - keep them stable.
STYLE_KINDS = [
    ("dialogue", "Dialogue"),
    ("caption", "Caption"),
    ("shout", "Shout"),
    ("whisper", "Whisper / Thought"),
    ("sfx", "Sound Effect"),
    ("sign", "Sign / Text in Scene"),
]
STYLE_KEYS = {key for key, _label in STYLE_KINDS}

# Sent to the LLM along with the texts: what each type means.
STYLE_KIND_DESCRIPTIONS = (
    '"dialogue" (a character speaking in a speech balloon), '
    '"caption" (narration or a narrator\'s box, usually rectangular, often at a panel edge), '
    '"shout" (yelled speech: spiky/burst balloon or very large/bold lettering), '
    '"whisper" (whispered speech or thoughts: dashed outline or thought bubble), '
    '"sfx" (a sound effect drawn into the art), '
    '"sign" (text that is part of the scene: signs, posters, labels, screens, letters)'
)

# Appended to the LLM prompts that see the page (translation, re-letter).
CLASSIFY_INSTRUCTION = (
    "\nAlso classify every block by what kind of text it is on the page. Add one extra key "
    "\"_types\" to the JSON you return, mapping each block key to one of: "
    + STYLE_KIND_DESCRIPTIONS
    + ". Do not put anything else under \"_types\"."
)

_SETTINGS_KEY = "text_styles/styles"


def normalize_kind(value) -> str:
    value = value.strip().lower() if isinstance(value, str) else ""
    return value if value in STYLE_KEYS else ""


def block_kind(blk) -> str:
    return normalize_kind(getattr(blk, "text_style", ""))


def kind_label(kind: str) -> str:
    return next((label for key, label in STYLE_KINDS if key == kind), "")


def set_block_kinds_from_json(blk_list, json_string: str) -> int:
    """Reads the "_types" an LLM added to its JSON answer and tags the
    blocks with them. A type picked by hand (text_style_manual) is kept."""
    import re

    match = re.search(r"\{[\s\S]*\}", json_string or "")
    if not match:
        return 0
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return 0
    types = data.get("_types") if isinstance(data, dict) else None
    if not isinstance(types, dict):
        return 0
    tagged = 0
    for idx, blk in enumerate(blk_list):
        kind = normalize_kind(types.get(f"block_{idx}"))
        if kind and not getattr(blk, "text_style_manual", False):
            blk.text_style = kind
            tagged += 1
    return tagged


def style_from_item(item) -> dict:
    """The whole-box formatting of a text item, as a style."""
    alignment = item.alignment
    try:
        alignment = int(alignment.value) if hasattr(alignment, "value") else int(alignment)
    except Exception:
        alignment = int(Qt.AlignmentFlag.AlignCenter.value)
    return {
        "font_family": item.font_family or "",
        "bold": bool(item.bold),
        "italic": bool(item.italic),
        "underline": bool(item.underline),
        "alignment": alignment,
        "text_color": QColor(item.text_color).name() if item.text_color is not None else "#000000",
        "outline": bool(item.outline),
        "outline_color": QColor(item.outline_color).name() if item.outline_color is not None else None,
        "outline_width": float(item.outline_width or 1.0),
        "line_spacing": float(item.line_spacing or 1.0),
    }


def style_alignment(style: dict):
    return Qt.AlignmentFlag(int(style.get("alignment", int(Qt.AlignmentFlag.AlignCenter.value))))


def style_outline_color(style: dict):
    if style.get("outline") and style.get("outline_color"):
        return QColor(style["outline_color"])
    return None


def apply_style_to_item(item, style: dict):
    """Applies a style to a whole text item (keeps its font size)."""
    item.set_font(style.get("font_family") or item.font_family, item.font_size)
    item.set_bold(bool(style.get("bold")))
    item.set_italic(bool(style.get("italic")))
    item.set_underline(bool(style.get("underline")))
    item.set_alignment(style_alignment(style))
    if style.get("text_color"):
        item.set_color(QColor(style["text_color"]))
    outline_color = style_outline_color(style)
    if outline_color is not None:
        item.set_outline(outline_color, float(style.get("outline_width") or 1.0))
    else:
        item.set_outline(None, None)
    item.set_line_spacing(float(style.get("line_spacing") or 1.0))


def style_for_block(styles: dict, blk) -> dict | None:
    """The style Render should use for this block, or None for the panel's."""
    kind = block_kind(blk)
    if not kind:
        return None
    return (styles or {}).get(kind) or None


def load_default_styles() -> dict:
    raw = QSettings("ComicLabs", "ComicTranslate").value(_SETTINGS_KEY, "")
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        data = {}
    return {k: v for k, v in data.items() if k in STYLE_KEYS and isinstance(v, dict)}


def save_default_styles(styles: dict):
    QSettings("ComicLabs", "ComicTranslate").setValue(_SETTINGS_KEY, json.dumps(styles or {}))
