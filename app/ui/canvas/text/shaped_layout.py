"""Balloon-shaped text layout: each line gets its own width, following an oval.

A letterer fits dialogue to the balloon - short lines at the top and bottom,
long ones in the middle - instead of a rectangle of equal lines. This
document layout does that for a horizontal TextBlockItem: given the box
width, it looks for the shortest oval (an ellipse as wide as the box) whose
chords fit the text line by line, centers the text vertically in it, and lays
each line out at the chord width at its height. Text that doesn't fit an
oval at all (e.g. one very long word) falls back to the normal rectangle.

Line breaking, soft hyphens, alignment, per-word formatting, the caret and
selection drawing are all Qt's own (QTextLayout per block); this class only
decides each line's width and position.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt
from PySide6.QtGui import (
    QAbstractTextDocumentLayout, QPalette, QTextBlockFormat, QTextLayout, QTextOption,
)

# The oval is searched from the rectangular text height up to this many times it.
_MAX_HEIGHT_FACTOR = 3.0
_HEIGHT_STEP = 1.06
# Never squeeze a line narrower than this fraction of the box width.
_MIN_CHORD_FRACTION = 0.3


class ShapedTextDocumentLayout(QAbstractTextDocumentLayout):
    def __init__(self, document):
        super().__init__(document)
        self._size = QSizeF(0, 0)
        self._block_rects: dict[int, QRectF] = {}
        self.shaped_ok = False  # False when it fell back to a rectangle

    # --- geometry --------------------------------------------------------

    def _content_width(self) -> float:
        doc = self.document()
        width = doc.textWidth()
        if width is None or width <= 0:
            width = doc.idealWidth() if doc.idealWidth() > 0 else 200.0
        return max(1.0, width - 2 * doc.documentMargin())

    @staticmethod
    def _line_factor(block) -> float:
        fmt = block.blockFormat()
        if fmt.lineHeightType() == QTextBlockFormat.LineHeightTypes.ProportionalHeight.value:
            value = fmt.lineHeight()
            if value > 0:
                return value / 100.0
        return 1.0

    def _prepare_block(self, block, width: float):
        layout = block.layout()
        option = QTextOption(self.document().defaultTextOption())
        option.setAlignment(block.blockFormat().alignment())
        # Words (and soft hyphens) only: a word that doesn't fit makes that
        # oval fail instead of being chopped mid-word.
        option.setWrapMode(QTextOption.WrapMode.WordWrap)
        layout.setTextOption(option)
        layout.setFont(self.document().defaultFont())
        return layout

    def _run(self, width: float, chord_for, top: float) -> tuple[float, bool]:
        """Lays every block out from y=top with chord_for(y0, y1) -> (x, w)
        giving each line's span. Returns (bottom y, every line fits)."""
        doc = self.document()
        margin = doc.documentMargin()
        y = top
        fits = True
        block = doc.begin()
        while block.isValid():
            layout = self._prepare_block(block, width)
            factor = self._line_factor(block)
            layout.beginLayout()
            guess = None
            while True:
                line = layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(width)
                height = line.height() if guess is None else guess
                x, w = chord_for(y, y + height)
                line.setLineWidth(max(1.0, w))
                if line.naturalTextWidth() > w + 0.5:
                    fits = False
                line.setPosition(QPointF(margin + x, margin + y))
                guess = line.height()
                y += line.height() * factor
            layout.endLayout()
            block = block.next()
        return y, fits

    def _relayout(self):
        width = self._content_width()
        full = lambda y0, y1: (0.0, width)
        natural_bottom, _ = self._run(width, full, 0.0)
        natural_height = max(1.0, natural_bottom)

        self.shaped_ok = False
        height = natural_height * 1.1
        best = None
        while height <= natural_height * _MAX_HEIGHT_FACTOR:
            half_h, half_w, cx = height / 2.0, width / 2.0, width / 2.0
            min_chord = width * _MIN_CHORD_FRACTION

            def chord(y0, y1, half_h=half_h, half_w=half_w, cx=cx, min_chord=min_chord):
                # Narrowest chord of the ellipse across the line's band.
                widths = []
                for y in (y0, y1):
                    d = (y - half_h) / half_h
                    widths.append(2 * half_w * math.sqrt(max(0.0, 1 - d * d)))
                w = max(min_chord, min(widths))
                return cx - w / 2.0, w

            # Center the text vertically in the oval (a couple of passes,
            # since the line widths - and so the text height - depend on it).
            top = max(0.0, (height - natural_height) / 2.0)
            fits = False
            for _ in range(3):
                bottom, fits = self._run(width, chord, top)
                used = bottom - top
                new_top = max(0.0, (height - used) / 2.0)
                if abs(new_top - top) < 0.5:
                    break
                top = new_top
            if fits and bottom <= height + 0.5:
                best = (height, top)
                break
            height *= _HEIGHT_STEP

        if best is None:
            bottom, _ = self._run(width, full, 0.0)
            total_height = bottom
        else:
            self.shaped_ok = True
            total_height = best[0]
            # the last _run() above already left the lines in the fitting layout

        margin = self.document().documentMargin()
        self._size = QSizeF(width + 2 * margin, total_height + 2 * margin)
        self._block_rects = {}
        block = self.document().begin()
        while block.isValid():
            rect = block.layout().boundingRect()
            rect.translate(0, 0)
            self._block_rects[block.blockNumber()] = rect
            block = block.next()

    # --- QAbstractTextDocumentLayout -----------------------------------

    def documentChanged(self, position: int, chars_removed: int, chars_added: int):
        old = QSizeF(self._size)
        self._relayout()
        if self._size != old:
            self.documentSizeChanged.emit(self._size)
        self.update.emit()  # whole document

    def documentSize(self) -> QSizeF:
        return self._size

    def pageCount(self) -> int:
        return 1

    def frameBoundingRect(self, frame) -> QRectF:
        return QRectF(0, 0, self._size.width(), self._size.height())

    def blockBoundingRect(self, block) -> QRectF:
        rect = block.layout().boundingRect() if block.isValid() else QRectF()
        if rect.isNull():
            return QRectF()
        return rect

    def hitTest(self, point: QPointF, accuracy) -> int:
        doc = self.document()
        best_pos, best_dist = -1, None
        block = doc.begin()
        while block.isValid():
            layout = block.layout()
            for i in range(layout.lineCount()):
                line = layout.lineAt(i)
                top = line.y()
                bottom = top + line.height()
                if top <= point.y() <= bottom:
                    return block.position() + line.xToCursor(point.x())
                dist = min(abs(point.y() - top), abs(point.y() - bottom))
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best_pos = block.position() + line.xToCursor(point.x())
            block = block.next()
        if accuracy == Qt.HitTestAccuracy.ExactHit:
            return -1
        return best_pos

    def draw(self, painter, context):
        doc = self.document()
        painter.save()
        painter.setPen(context.palette.color(QPalette.ColorRole.Text))
        block = doc.begin()
        while block.isValid():
            layout = block.layout()
            start, length = block.position(), block.length()
            ranges = []
            for sel in context.selections:
                a, b = sel.cursor.selectionStart(), sel.cursor.selectionEnd()
                lo, hi = max(a, start), min(b, start + length)
                if hi > lo:
                    fr = QTextLayout.FormatRange()
                    fr.start, fr.length, fr.format = lo - start, hi - lo, sel.format
                    ranges.append(fr)
            layout.draw(painter, QPointF(0, 0), ranges)
            pos = context.cursorPosition
            if start <= pos < start + length:
                layout.drawCursor(painter, QPointF(0, 0), pos - start, 1)
            block = block.next()
        painter.restore()

    # Same names the vertical layout has, so TextBlockItem can treat both alike.
    def set_max_size(self, max_width, max_height, relayout=True):
        if relayout:
            self.documentChanged(0, 0, 0)
