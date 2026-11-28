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
import re

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

# Emphasis: the LLM marks words that are visibly emphasized in the original
# lettering with **...**; the markers are taken out of the text right away
# (TextBlock.emphasis keeps the words) and Render puts those words in bold.
EMPHASIS_INSTRUCTION = (
    "\nIf some words in a block's original lettering are visibly emphasized in the image (bold "
    "or heavier lettering than the rest of that balloon), wrap the words of your translation "
    "that carry that emphasis in double asterisks, like **this**. Only for emphasis you can "
    "see in the image - never add it otherwise."
)
EMPHASIS_INSTRUCTION_PRINTED = (
    "\nIf some words are printed in bold (heavier lettering than the rest of that balloon), "
    "wrap them in double asterisks, like **this**. Only for bold you can see in the image."
)
_EMPHASIS_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)

_SETTINGS_KEY = "text_styles/styles"


def extract_emphasis(blk_list) -> int:
    """Takes the **...** markers out of freshly set translations and keeps
    the marked words in blk.emphasis. Returns how many blocks had some."""
    marked = 0
    for blk in blk_list:
        text = getattr(blk, "translation", None)
        if not isinstance(text, str):
            continue
        words = [w.strip() for w in _EMPHASIS_RE.findall(text) if w.strip()]
        if "**" in text:
            text = _EMPHASIS_RE.sub(r"\1", text).replace("**", "")
            blk.translation = text
        blk.emphasis = words
        if words:
            marked += 1
    return marked


def apply_emphasis(item, words) -> int:
    """Puts each emphasized word/phrase in bold inside a rendered text item
    (whole words, any case, soft hyphens allowed between letters). Returns
    how many places were bolded."""
    from PySide6.QtGui import QFont, QTextCharFormat, QTextCursor

    if not words:
        return 0
    doc = item.document()
    plain = doc.toPlainText()
    fmt = QTextCharFormat()
    fmt.setFontWeight(QFont.Weight.Bold)
    count = 0
    for word in words:
        body = "\u00ad*".join(re.escape(ch) for ch in word)
        pattern = re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)
        for m in pattern.finditer(plain):
            cursor = QTextCursor(doc)
            cursor.setPosition(m.start())
            cursor.setPosition(m.end(), QTextCursor.MoveMode.KeepAnchor)
            cursor.mergeCharFormat(fmt)
            count += 1
    return count


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
        "shaped": bool(getattr(item, "shaped", False)),
    }


# Box types lettered in an oval by default (until their style says otherwise).
OVAL_KINDS = {"dialogue", "shout", "whisper"}


def shaped_for_block(styles: dict, blk) -> bool:
    """Whether Render letters this box in an oval: its type's style decides
    when it says so, otherwise balloons (dialogue, shout, whisper) are oval
    and captions, signs, sound effects and untyped boxes rectangular."""
    style = style_for_block(styles, blk)
    if style and "shaped" in style:
        return bool(style["shaped"])
    return block_kind(blk) in OVAL_KINDS


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
    if "shaped" in style and hasattr(item, "set_shaped"):
        item.set_shaped(bool(style["shaped"]))


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
