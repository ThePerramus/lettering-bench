import math
import numpy as np
from typing import List, Dict

from PySide6 import QtWidgets, QtCore, QtGui
from PySide6.QtWidgets import QGraphicsPathItem, QGraphicsPixmapItem
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QColor, QBrush, QPen, QPainterPath, QCursor, QPixmap, QImage, QPainter

from app.ui.commands.brush import BrushStrokeCommand, ClearBrushStrokesCommand, \
                            SegmentBoxesCommand, EraseUndoCommand
from app.ui.commands.base import PathCommandBase as pcb
import imkit as imk
from modules.utils.image_utils import build_block_mask_data, clip_mask_to_bubble, clip_mask_components_to_bubble
from modules.utils.textblock import adjust_text_line_coordinates
from modules.detection.utils.content import detect_content_mask_in_bbox


class DrawingManager:
    """Manages all drawing-related tools and state."""

    def __init__(self, viewer):
        self.viewer = viewer
        self._scene = viewer._scene

        self.brush_color = QColor(255, 0, 0, 100)
        self.brush_size = 25
        self.eraser_size = 25

        # Pencil: paints an opaque flat color directly onto the image, size shared
        # with the brush/eraser slider. Color persists across pages (never reset).
        self.pencil_color = QColor(255, 255, 255)
        self.pencil_size = 25
        self._pencil_scaled_size = 25

        # Patch eraser: punches a transparent hole in whichever inpaint patch is
        # topmost under the stroke, revealing an older patch or the original image
        # underneath. Size shared with the same brush/eraser/pencil slider.
        self.patch_eraser_size = 25
        self._patch_eraser_scaled_size = 25

        # Reveal pencil: like the pencil, but instead of a flat picked color it
        # samples per-pixel from the current page's aligned reference scan
        # (viewer.reveal_source) - for bringing over text that's already
        # translated in a second, often lower-quality, edition.
        self.reveal_pencil_size = 25
        self._reveal_pencil_scaled_size = 25

        self.brush_cursor = self.create_inpaint_cursor('brush', self.brush_size)
        self.eraser_cursor = self.create_inpaint_cursor('eraser', self.eraser_size)
        self.pencil_cursor = self.create_inpaint_cursor('pencil', self.pencil_size)
        self.patch_eraser_cursor = self.create_inpaint_cursor('patch_eraser', self.patch_eraser_size)
        self.reveal_pencil_cursor = self.create_inpaint_cursor('reveal_pencil', self.reveal_pencil_size)

        self.current_path = None
        self.current_path_item = None

        # Shift-constrain (Photoshop-style): while held, the stroke snaps to a
        # straight horizontal/vertical line from wherever it was when the
        # constraint (re)engaged, instead of following the raw mouse path.
        # Reset to None on release so freehand resumes exactly from there, and
        # to the current point whenever Shift isn't held, so re-engaging Shift
        # later locks relative to the freehand point you're at then - not the
        # stroke's original start.
        self._shift_lock_origin = None

        # Live "what will actually be revealed" preview for the reveal pencil,
        # updated on every mouse move while dragging (see _update_reveal_preview).
        self.reveal_preview_item = None

        # Live hover preview: a scene-space circle (not an OS cursor) so it scales
        # correctly with zoom and always shows exactly where/how big the next
        # stroke will be, even before you start dragging.
        self.hover_preview_item = None
        self.last_human_mask = None

        self.before_erase_state = []
        self.after_erase_state = []

    def _effective_size(self, raw_size):
        """Scales a raw slider value (1-100) up to this page's actual pixel
        scale, the same way create_inpaint_cursor's cursor bitmap already is
        (see ToolStateMixin.scale_size) - without this, the OS cursor shown
        while hovering looks properly sized on a large scan, but the real
        stroke drawn on click was always the tiny unscaled 1-100 value,
        regardless of resolution or where the slider was set."""
        rect = self.viewer.photo.boundingRect()
        w, h = rect.width(), rect.height()
        if w <= 0 or h <= 0:
            return raw_size
        diagonal = (w ** 2 + h ** 2) ** 0.5
        return raw_size * (diagonal / 1000.0)

    def start_stroke(self, scene_pos: QPointF):
        """Starts a new drawing or erasing stroke."""
        self.viewer.drawing_path = QPainterPath() # drawing_path is on viewer in original
        self.viewer.drawing_path.moveTo(scene_pos)

        self.current_path = QPainterPath()
        self.current_path.moveTo(scene_pos)
        self._shift_lock_origin = QPointF(scene_pos)

        if self.viewer.current_tool == 'brush':
            pen = QPen(self.brush_color, self._effective_size(self.brush_size),
                       Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            self.current_path_item = self._scene.addPath(self.current_path, pen)
            self.current_path_item.setZValue(0.8)

        elif self.viewer.current_tool == 'pencil':
            pen = QPen(self.pencil_color, self._effective_size(self.pencil_size),
                       Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            self.current_path_item = self._scene.addPath(self.current_path, pen)
            self.current_path_item.setZValue(0.9)

        elif self.viewer.current_tool == 'patch_eraser':
            pen = QPen(QColor(220, 40, 40, 140), self._effective_size(self.patch_eraser_size),
                       Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            self.current_path_item = self._scene.addPath(self.current_path, pen)
            self.current_path_item.setZValue(0.9)

        elif self.viewer.current_tool == 'reveal_pencil':
            # A faint outline only - the actual revealed pixels are shown live
            # via reveal_preview_item (see _update_reveal_preview), not a flat
            # tinted highlight, so you can see what you're revealing as you draw.
            pen = QPen(QColor(40, 190, 220, 60), self._effective_size(self.reveal_pencil_size),
                       Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            self.current_path_item = self._scene.addPath(self.current_path, pen)
            self.current_path_item.setZValue(0.9)
            self._update_reveal_preview()

        elif self.viewer.current_tool == 'eraser':
            # Capture the current state before starting erase operation
            self.before_erase_state = []
            try:
                photo_item = getattr(self.viewer, 'photo', None)
                for item in self._scene.items():
                    if (isinstance(item, QGraphicsPathItem) and 
                        item != photo_item and
                        hasattr(item, 'path')):  # Ensure item has path method
                        props = pcb.save_path_properties(item)
                        if props:  # Only add valid properties
                            self.before_erase_state.append(props)
            except Exception as e:
                print(f"Warning: Error capturing before_erase_state: {e}")
                import traceback
                traceback.print_exc()
                self.before_erase_state = []

    def continue_stroke(self, scene_pos: QPointF, shift_constrain: bool = False):
        """Continues an existing drawing or erasing stroke. While
        shift_constrain is True, the point is snapped to a straight
        horizontal/vertical line first (see _constrain_to_axis)."""
        if not self.current_path:
            return

        if shift_constrain:
            scene_pos = self._constrain_to_axis(scene_pos)
        else:
            self._shift_lock_origin = QPointF(scene_pos)

        self.current_path.lineTo(scene_pos)
        if self.viewer.current_tool in ('brush', 'pencil', 'patch_eraser', 'reveal_pencil') and self.current_path_item:
            self.current_path_item.setPath(self.current_path)
            if self.viewer.current_tool == 'reveal_pencil':
                self._update_reveal_preview()
        elif self.viewer.current_tool == 'eraser':
            self.erase_at(scene_pos)

    def _constrain_to_axis(self, scene_pos: QPointF) -> QPointF:
        """Snaps scene_pos onto a horizontal or vertical line through
        _shift_lock_origin, picking whichever axis has the larger delta -
        the same "straight line while held" behavior as Photoshop's brush."""
        origin = self._shift_lock_origin
        if origin is None:
            self._shift_lock_origin = QPointF(scene_pos)
            return scene_pos
        dx = scene_pos.x() - origin.x()
        dy = scene_pos.y() - origin.y()
        if abs(dx) >= abs(dy):
            return QPointF(scene_pos.x(), origin.y())
        return QPointF(origin.x(), scene_pos.y())

    def end_stroke(self):
        """Finalizes the current stroke and creates an undo command."""
        if self.current_path_item:
            if self.viewer.current_tool == 'brush':
                command = BrushStrokeCommand(self.viewer, self.current_path_item)
                self.viewer.command_emitted.emit(command)
            elif self.viewer.current_tool == 'pencil':
                self._commit_pencil_stroke()
            elif self.viewer.current_tool == 'patch_eraser':
                self._commit_patch_erase()
            elif self.viewer.current_tool == 'reveal_pencil':
                self._commit_reveal_stroke()
                self._clear_reveal_preview()

        if self.viewer.current_tool == 'eraser':
            # Capture the current state after erase operation
            self.after_erase_state = []
            try:
                photo_item = getattr(self.viewer, 'photo', None)
                for item in self._scene.items():
                    if (isinstance(item, QGraphicsPathItem) and 
                        item != photo_item and
                        hasattr(item, 'path')):  # Ensure item has path method
                        props = pcb.save_path_properties(item)
                        if props:  # Only add valid properties
                            self.after_erase_state.append(props)
            except Exception as e:
                print(f"Warning: Error capturing after_erase_state: {e}")
                import traceback
                traceback.print_exc()
                self.after_erase_state = []
            
            # Only create undo command if we have valid before/after states
            if hasattr(self, 'before_erase_state'):
                # Create copies of the lists before passing them to avoid clearing issues
                before_copy = list(self.before_erase_state)
                after_copy = list(self.after_erase_state)
                command = EraseUndoCommand(self.viewer, before_copy, after_copy)
                self.viewer.command_emitted.emit(command)
                self.before_erase_state.clear()
                self.after_erase_state.clear()
            else:
                print("Warning: No before_erase_state found, skipping undo command creation")
        
        self.current_path = None
        self.current_path_item = None
        self._shift_lock_origin = None
        self.viewer.drawing_path = None

    def erase_at(self, pos: QPointF):
        radius = self._effective_size(self.eraser_size)
        erase_path = QPainterPath()
        erase_path.addEllipse(pos, radius, radius)

        for item in self._scene.items(erase_path):
            if isinstance(item, QGraphicsPathItem) and item != self.viewer.photo:
                self._erase_item_path(item, erase_path, pos)

    def _erase_item_path(self, item, erase_path, pos):
        path = item.path()
        new_path = QPainterPath()
        
        brush_color = QColor(item.brush().color().name(QColor.HexArgb))
        if brush_color == "#80ff0000":  # Generated (filled) segmentation path
            # Map erase shape into item's local coordinates to ensure robust boolean ops
            try:
                local_erase_path = item.mapFromScene(erase_path)
            except Exception:
                # Fallback: translate by item position if mapping isn't available
                local_erase_path = QPainterPath(erase_path)
                local_erase_path.translate(-item.pos().x(), -item.pos().y())

            # Ensure consistent fill rule for robust subtraction of filled polygons
            path.setFillRule(Qt.FillRule.WindingFill)
            local_erase_path.setFillRule(Qt.FillRule.WindingFill)

            result = path.subtracted(local_erase_path)
            if not result.isEmpty():
                new_path = result
        else: # Human-drawn stroke
            element_count = path.elementCount()
            i = 0
            while i < element_count:
                e = path.elementAt(i)
                point = QPointF(e.x, e.y)
                if not erase_path.contains(point):
                    if e.type == QPainterPath.ElementType.MoveToElement: new_path.moveTo(point)
                    elif e.type == QPainterPath.ElementType.LineToElement: new_path.lineTo(point)
                    elif e.type == QPainterPath.ElementType.CurveToElement:
                        if i + 2 < element_count:
                            c1, c2 = path.elementAt(i + 1), path.elementAt(i + 2)
                            c1_p, c2_p = QPointF(c1.x, c1.y), QPointF(c2.x, c2.y)
                            if not (erase_path.contains(c1_p) or erase_path.contains(c2_p)):
                                new_path.cubicTo(point, c1_p, c2_p)
                        i += 2
                else:
                    if (i + 1) < element_count:
                        next_e = path.elementAt(i + 1)
                        next_p = QPointF(next_e.x, next_e.y)
                        if not erase_path.contains(next_p):
                            new_path.moveTo(next_p)
                            if next_e.type == QPainterPath.ElementType.CurveToDataElement:
                                i += 2
                i += 1

        if new_path.isEmpty():
            self._scene.removeItem(item)
        else:
            item.setPath(new_path)

    def set_brush_size(self, size, scaled_size):
        self.brush_size = size
        self.brush_cursor = self.create_inpaint_cursor("brush", scaled_size)

    def set_eraser_size(self, size, scaled_size):
        self.eraser_size = size
        self.eraser_cursor = self.create_inpaint_cursor("eraser", scaled_size)

    def set_pencil_size(self, size, scaled_size):
        self.pencil_size = size
        self._pencil_scaled_size = scaled_size
        self.pencil_cursor = self.create_inpaint_cursor("pencil", scaled_size)

    def set_pencil_color(self, color: QColor):
        self.pencil_color = color
        self.pencil_cursor = self.create_inpaint_cursor("pencil", self._pencil_scaled_size)

    def _commit_pencil_stroke(self):
        """Bakes the just-drawn pencil path into the actual image as a patch,
        the same way an automatic inpaint result is applied (undo/redo included)."""
        item = self.current_path_item
        if item is None:
            return

        if self.viewer.webtoon_mode or not self.viewer.hasPhoto():
            self._scene.removeItem(item)
            return

        path = item.path()
        pen_width = item.pen().widthF()
        stroke_rect = path.boundingRect().adjusted(-pen_width, -pen_width, pen_width, pen_width)

        image_rect = self.viewer.photo.boundingRect()
        img_w, img_h = int(image_rect.width()), int(image_rect.height())

        x1 = max(0, int(math.floor(stroke_rect.left())))
        y1 = max(0, int(math.floor(stroke_rect.top())))
        x2 = min(img_w, int(math.ceil(stroke_rect.right())))
        y2 = min(img_h, int(math.ceil(stroke_rect.bottom())))
        w, h = x2 - x1, y2 - y1

        if w <= 0 or h <= 0:
            self._scene.removeItem(item)
            return

        # Rasterize the exact stroke path (same pen) into a small local mask
        mask_qimg = QImage(w, h, QImage.Format_Grayscale8)
        mask_qimg.fill(0)
        mask_painter = QPainter(mask_qimg)
        mask_painter.translate(-x1, -y1)
        mask_pen = QPen(QColor(255, 255, 255), pen_width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        mask_painter.setPen(mask_pen)
        mask_painter.setBrush(Qt.NoBrush)
        mask_painter.drawPath(path)
        mask_painter.end()

        mask_ptr = mask_qimg.constBits()
        mask = np.array(mask_ptr).reshape(mask_qimg.height(), mask_qimg.bytesPerLine())[:, :w]

        self._scene.removeItem(item)

        base_rgb = self.viewer.get_image_array(include_patches=True)
        if base_rgb is None:
            return

        patch_rgb = base_rgb[y1:y2, x1:x2]
        if patch_rgb.shape[:2] != (h, w):
            return

        color = self.pencil_color
        # Real alpha, opaque only where the stroke actually landed - not a
        # flat opaque rectangle - so tools that reason about "what's really
        # drawn here" (the patch eraser's topmost-patch peeling, auto-reveal's
        # patch selection) see this correction's true shape instead of its
        # whole bounding box. Visually identical either way while nothing
        # underneath changes, since the untouched pixels just show through.
        patch_rgba = np.zeros((h, w, 4), dtype=np.uint8)
        patch_rgba[..., :3] = patch_rgb
        patch_rgba[mask == 255, :3] = (color.red(), color.green(), color.blue())
        patch_rgba[..., 3] = np.where(mask == 255, 255, 0).astype(np.uint8)

        patch = {'bbox': [x1, y1, w, h], 'image': patch_rgba, 'kind': 'pencil'}
        self.viewer.pencil_patch_ready.emit(patch)

    def set_reveal_pencil_size(self, size, scaled_size):
        self.reveal_pencil_size = size
        self._reveal_pencil_scaled_size = scaled_size
        self.reveal_pencil_cursor = self.create_inpaint_cursor("reveal_pencil", scaled_size)

    def _clear_reveal_preview(self):
        if self.reveal_preview_item is not None:
            self._scene.removeItem(self.reveal_preview_item)
            self.reveal_preview_item = None

    def _update_reveal_preview(self):
        """Shows, live while dragging, exactly the pixels the reveal pencil
        would bake in if released right now - masked to the actual stroke
        shape, not a flat tinted highlight - so revealing isn't done blind."""
        item = self.current_path_item
        reveal_source = getattr(self.viewer, 'reveal_source', None)
        if item is None or reveal_source is None or self.viewer.webtoon_mode:
            return

        path = item.path()
        pen_width = item.pen().widthF()
        stroke_rect = path.boundingRect().adjusted(-pen_width, -pen_width, pen_width, pen_width)

        image_rect = self.viewer.photo.boundingRect()
        img_w, img_h = int(image_rect.width()), int(image_rect.height())
        if reveal_source.shape[:2] != (img_h, img_w):
            return

        x1 = max(0, int(math.floor(stroke_rect.left())))
        y1 = max(0, int(math.floor(stroke_rect.top())))
        x2 = min(img_w, int(math.ceil(stroke_rect.right())))
        y2 = min(img_h, int(math.ceil(stroke_rect.bottom())))
        w, h = x2 - x1, y2 - y1
        if w <= 0 or h <= 0:
            return

        mask_qimg = QImage(w, h, QImage.Format_Grayscale8)
        mask_qimg.fill(0)
        mask_painter = QPainter(mask_qimg)
        mask_painter.translate(-x1, -y1)
        mask_pen = QPen(QColor(255, 255, 255), pen_width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        mask_painter.setPen(mask_pen)
        mask_painter.setBrush(Qt.NoBrush)
        mask_painter.drawPath(path)
        mask_painter.end()

        mask_ptr = mask_qimg.constBits()
        mask = np.array(mask_ptr).reshape(mask_qimg.height(), mask_qimg.bytesPerLine())[:, :w]

        ref_crop = reveal_source[y1:y2, x1:x2, :3]
        rgba = np.zeros((h, w, 4), dtype=np.uint8)
        rgba[..., :3] = ref_crop
        rgba[..., 3] = np.where(mask == 255, 255, 0).astype(np.uint8)

        qimg = QImage(rgba.data, w, h, rgba.strides[0], QImage.Format.Format_RGBA8888).copy()
        pixmap = QPixmap.fromImage(qimg)

        if self.reveal_preview_item is None:
            self.reveal_preview_item = self._scene.addPixmap(pixmap)
            self.reveal_preview_item.setZValue(0.95)  # above the faint outline, below hover preview
        else:
            self.reveal_preview_item.setPixmap(pixmap)
        self.reveal_preview_item.setPos(x1, y1)

    def _commit_reveal_stroke(self):
        """Bakes the just-drawn stroke into a patch that reveals the current
        page's aligned reference scan underneath, pixel-for-pixel, instead of a
        flat picked color - the manual "reveal" counterpart to the pencil, for
        bringing over text that's already translated in a second edition.
        Reuses the exact same patch pipeline as the pencil (undo/redo, save,
        render) since the result is just another RGB patch."""
        item = self.current_path_item
        if item is None:
            return

        reveal_source = getattr(self.viewer, 'reveal_source', None)
        if self.viewer.webtoon_mode or not self.viewer.hasPhoto() or reveal_source is None:
            self._scene.removeItem(item)
            return

        path = item.path()
        pen_width = item.pen().widthF()
        stroke_rect = path.boundingRect().adjusted(-pen_width, -pen_width, pen_width, pen_width)

        image_rect = self.viewer.photo.boundingRect()
        img_w, img_h = int(image_rect.width()), int(image_rect.height())

        if reveal_source.shape[:2] != (img_h, img_w):
            # Stale/mismatched cached array (shouldn't happen - it's baked at
            # this page's own size - but never paint from the wrong page).
            self._scene.removeItem(item)
            return

        x1 = max(0, int(math.floor(stroke_rect.left())))
        y1 = max(0, int(math.floor(stroke_rect.top())))
        x2 = min(img_w, int(math.ceil(stroke_rect.right())))
        y2 = min(img_h, int(math.ceil(stroke_rect.bottom())))
        w, h = x2 - x1, y2 - y1

        if w <= 0 or h <= 0:
            self._scene.removeItem(item)
            return

        mask_qimg = QImage(w, h, QImage.Format_Grayscale8)
        mask_qimg.fill(0)
        mask_painter = QPainter(mask_qimg)
        mask_painter.translate(-x1, -y1)
        mask_pen = QPen(QColor(255, 255, 255), pen_width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        mask_painter.setPen(mask_pen)
        mask_painter.setBrush(Qt.NoBrush)
        mask_painter.drawPath(path)
        mask_painter.end()

        mask_ptr = mask_qimg.constBits()
        mask = np.array(mask_ptr).reshape(mask_qimg.height(), mask_qimg.bytesPerLine())[:, :w]

        self._scene.removeItem(item)

        base_rgb = self.viewer.get_image_array(include_patches=True)
        if base_rgb is None:
            return

        patch_rgb = base_rgb[y1:y2, x1:x2]
        if patch_rgb.shape[:2] != (h, w):
            return

        ref_crop = reveal_source[y1:y2, x1:x2, :3]
        # Real alpha, opaque only where the stroke actually landed - see the
        # matching comment in _commit_pencil_stroke() for why.
        patch_rgba = np.zeros((h, w, 4), dtype=np.uint8)
        patch_rgba[..., :3] = patch_rgb
        patch_rgba[mask == 255, :3] = ref_crop[mask == 255]
        patch_rgba[..., 3] = np.where(mask == 255, 255, 0).astype(np.uint8)

        patch = {'bbox': [x1, y1, w, h], 'image': patch_rgba, 'kind': 'reveal_pencil'}
        self.viewer.pencil_patch_ready.emit(patch)

    def set_patch_eraser_size(self, size, scaled_size):
        self.patch_eraser_size = size
        self._patch_eraser_scaled_size = scaled_size
        self.patch_eraser_cursor = self.create_inpaint_cursor("patch_eraser", scaled_size)

    def _commit_patch_erase(self):
        """Punches a hole in whichever inpaint patch is topmost under the stroke,
        one layer at a time, so an older patch (or the original image) shows
        through - the inverse of the pencil."""
        item = self.current_path_item
        if item is None:
            return

        if self.viewer.webtoon_mode or not self.viewer.hasPhoto():
            self._scene.removeItem(item)
            return

        path = item.path()
        pen_width = item.pen().widthF()
        stroke_rect = path.boundingRect().adjusted(-pen_width, -pen_width, pen_width, pen_width)
        self._scene.removeItem(item)

        image_rect = self.viewer.photo.boundingRect()
        img_w, img_h = int(image_rect.width()), int(image_rect.height())

        ex1 = max(0, int(math.floor(stroke_rect.left())))
        ey1 = max(0, int(math.floor(stroke_rect.top())))
        ex2 = min(img_w, int(math.ceil(stroke_rect.right())))
        ey2 = min(img_h, int(math.ceil(stroke_rect.bottom())))
        ew, eh = ex2 - ex1, ey2 - ey1
        if ew <= 0 or eh <= 0:
            return

        mask_qimg = QImage(ew, eh, QImage.Format_Grayscale8)
        mask_qimg.fill(0)
        mask_painter = QPainter(mask_qimg)
        mask_painter.translate(-ex1, -ey1)
        mask_pen = QPen(QColor(255, 255, 255), pen_width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        mask_painter.setPen(mask_pen)
        mask_painter.setBrush(Qt.NoBrush)
        mask_painter.drawPath(path)
        mask_painter.end()
        mask_ptr = mask_qimg.constBits()
        erase_mask = np.array(mask_ptr).reshape(mask_qimg.height(), mask_qimg.bytesPerLine())[:, :ew] == 255

        remaining = erase_mask.copy()
        results = []

        # scene.items() returns topmost-first, which is exactly the peel order we
        # want: erase from the highest patch actually covering each pixel first,
        # and only fall through to an older one where the top patch was already
        # transparent (or doesn't reach there).
        for it in self._scene.items():
            if not remaining.any():
                break
            if not isinstance(it, QGraphicsPixmapItem) or it is self.viewer.photo:
                continue
            patch_hash = it.data(0)  # PatchCommandBase.HASH_KEY
            if patch_hash is None:
                continue

            pw, ph = it.pixmap().width(), it.pixmap().height()
            ppos = it.pos()
            px1, py1 = int(ppos.x()), int(ppos.y())
            px2, py2 = px1 + pw, py1 + ph

            ox1, oy1 = max(ex1, px1), max(ey1, py1)
            ox2, oy2 = min(ex2, px2), min(ey2, py2)
            if ox2 <= ox1 or oy2 <= oy1:
                continue

            qimg = it.pixmap().toImage().convertToFormat(QImage.Format.Format_RGBA8888)
            buf = np.array(qimg.constBits()).reshape(qimg.height(), qimg.bytesPerLine())[:, :pw * 4].reshape(ph, pw, 4).copy()

            lpx1, lpy1, lpx2, lpy2 = ox1 - px1, oy1 - py1, ox2 - px1, oy2 - py1
            lex1, ley1, lex2, ley2 = ox1 - ex1, oy1 - ey1, ox2 - ex1, oy2 - ey1

            patch_alpha_region = buf[lpy1:lpy2, lpx1:lpx2, 3]
            remaining_region = remaining[ley1:ley2, lex1:lex2]

            was_opaque = patch_alpha_region > 0
            hit = remaining_region & was_opaque
            if hit.any():
                patch_alpha_region[hit] = 0
                results.append({'hash': patch_hash, 'new_image': buf})

            # Whether or not we just erased it, this patch's opaque footprint is
            # now resolved for this stroke; only its already-transparent pixels
            # continue on to whatever patch is underneath.
            remaining_region &= ~was_opaque

        if results:
            self.viewer.patch_erase_ready.emit(results)

    def pick_color(self, scene_pos: QPointF):
        """Samples the current on-screen color at scene_pos and stores it for the pencil tool."""
        if self.viewer.webtoon_mode or not self.viewer.hasPhoto():
            return None

        image = self.viewer.get_image_array(include_patches=True)
        if image is None:
            return None

        x, y = int(scene_pos.x()), int(scene_pos.y())
        h, w = image.shape[:2]
        if not (0 <= x < w and 0 <= y < h):
            return None

        r, g, b = (int(v) for v in image[y, x][:3])
        self.set_pencil_color(QColor(r, g, b))
        return self.pencil_color

    def create_inpaint_cursor(self, cursor_type, size):
        # Windows (and other platforms) cap how large a custom cursor bitmap
        # can actually display - past that cap the OS cursor icon silently
        # stops growing no matter what value we hand it, which looks exactly
        # like "the size slider does nothing" on a high-resolution page where
        # the real (scaled) size regularly exceeds it. Clamp the cursor
        # bitmap itself; the scene-space hover-preview circle (drawn as a
        # normal graphics item, not an OS cursor) has no such limit and stays
        # the source of truth for the actual stroke size at any size.
        size = max(1, min(size, 48))
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        if cursor_type == "brush":
            painter.setBrush(QBrush(QColor(255, 0, 0, 127)))
            painter.setPen(Qt.PenStyle.NoPen)
        elif cursor_type == "eraser":
            painter.setBrush(QBrush(QColor(0, 0, 0, 0)))
            painter.setPen(QColor(0, 0, 0, 127))
        elif cursor_type == "pencil":
            painter.setBrush(QBrush(self.pencil_color))
            painter.setPen(QColor(0, 0, 0, 180))
        elif cursor_type == "patch_eraser":
            painter.setBrush(QBrush(QColor(0, 0, 0, 0)))
            painter.setPen(QColor(220, 40, 40, 200))
        elif cursor_type == "reveal_pencil":
            painter.setBrush(QBrush(QColor(0, 0, 0, 0)))
            painter.setPen(QColor(40, 190, 220, 220))
        else:
            painter.setBrush(QBrush(QColor(0, 0, 0, 127)))
            painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(0, 0, (size - 1), (size - 1))
        painter.end()
        return QCursor(pixmap, size // 2, size // 2)
    
    def update_hover_preview(self, scene_pos: QPointF, tool: str):
        """Shows a scene-space circle at scene_pos sized to the active tool's
        current brush/eraser/pencil size, so you can see exactly where and how
        big the next stroke will be before you start dragging. Drawn in scene
        coordinates (not as an OS cursor) so it scales correctly with zoom."""
        sizes = {
            'brush': self.brush_size,
            'eraser': self.eraser_size,
            'pencil': self.pencil_size,
            'patch_eraser': self.patch_eraser_size,
            'reveal_pencil': self.reveal_pencil_size,
        }
        if tool not in sizes or not self.viewer.hasPhoto():
            self.hide_hover_preview()
            return

        size = max(1, self._effective_size(sizes[tool]))
        radius = size / 2.0

        if self.hover_preview_item is None:
            self.hover_preview_item = self._scene.addEllipse(0, 0, size, size)
            self.hover_preview_item.setZValue(1000)  # always drawn on top

        pen = QPen(QColor(0, 0, 0, 220), 1)
        pen.setCosmetic(True)  # outline stays a constant screen-thickness at any zoom
        pen.setStyle(Qt.PenStyle.DashLine)
        self.hover_preview_item.setPen(pen)
        self.hover_preview_item.setBrush(Qt.NoBrush)
        self.hover_preview_item.setRect(scene_pos.x() - radius, scene_pos.y() - radius, size, size)
        self.hover_preview_item.setVisible(True)

    def hide_hover_preview(self):
        if self.hover_preview_item is not None:
            self.hover_preview_item.setVisible(False)

    def save_brush_strokes(self) -> List[Dict]:
        strokes = []
        
        # Also collect any currently visible strokes
        for item in self._scene.items():
            if isinstance(item, QGraphicsPathItem) and item != self.viewer.photo:
                strokes.append({
                    'path': item.path(),
                    'pen': item.pen().color().name(QColor.HexArgb),
                    'brush': item.brush().color().name(QColor.HexArgb),
                    'width': item.pen().width()
                })
        return strokes

    def load_brush_strokes(self, strokes: List[Dict]):
        self.clear_brush_strokes(page_switch=True)
        for stroke in reversed(strokes):
            pen = QPen()
            pen.setColor(QColor(stroke['pen']))
            pen.setWidth(stroke['width'])
            pen.setStyle(Qt.SolidLine)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            brush = QBrush(QColor(stroke['brush']))
            if brush.color() == QColor("#80ff0000"):
                self._scene.addPath(stroke['path'], pen, brush)
            else:
                self._scene.addPath(stroke['path'], pen)
                
    def clear_brush_strokes(self, page_switch=False):
        if page_switch:      
            items_to_remove = [item for item in self._scene.items()
                               if isinstance(item, QGraphicsPathItem) and item != self.viewer.photo]
            for item in items_to_remove:
                self._scene.removeItem(item)
            self._scene.update()
        else:
            command = ClearBrushStrokesCommand(self.viewer)
            self.viewer.command_emitted.emit(command)
            
    def clear_brush_strokes_in_scene_rects(self, scene_rects):
        """Clear only strokes covered by completed webtoon inpainting patches."""
        if not scene_rects:
            return
        self.viewer.command_emitted.emit(ClearBrushStrokesCommand(self.viewer, scene_rects))
    def has_drawn_elements(self):
        for item in self._scene.items():
            if isinstance(item, QGraphicsPathItem) and item != self.viewer.photo:
                return True
        return False
        
    def generate_mask_from_strokes(self):
        if not self.viewer.hasPhoto(): 
            return None
        
        # Check if there are any brush strokes to process
        if not self.has_drawn_elements():
            return None

        # Handle webtoon mode vs regular mode for getting dimensions
        is_webtoon_mode = self.viewer.webtoon_mode
        if is_webtoon_mode:
            # In webtoon mode, use visible area dimensions
            visible_image, mappings = self.viewer.get_visible_area_image()
            if visible_image is None:
                return None
            height, width = visible_image.shape[:2]
        else:
            # Regular mode - use photo dimensions
            image_rect = self.viewer.photo.boundingRect()
            width, height = int(image_rect.width()), int(image_rect.height())
        
        # Ensure we have valid dimensions
        if width <= 0 or height <= 0:
            return None
        
        human_qimg = QImage(width, height, QImage.Format_Grayscale8)
        gen_qimg = QImage(width, height, QImage.Format_Grayscale8)
        human_qimg.fill(0)
        gen_qimg.fill(0)

        human_painter, gen_painter = QPainter(human_qimg), QPainter(gen_qimg)
        
        # Get transformation values for debug logging
        visible_scene_top = 0
        visible_scene_left = 0
        
        # Set up coordinate transformation for webtoon mode
        if is_webtoon_mode:
            
            # Don't use viewport bounds - use the actual visible area bounds from the mappings
            visible_image, mappings = self.viewer.get_visible_area_image()
            if mappings:
                # Get the top-left corner of the visible area in scene coordinates
                # Use scene_y_start which is the actual scene coordinate where the visible area starts
                visible_scene_top = mappings[0]['scene_y_start']
                visible_scene_left = 0  # Assuming webtoon width starts at 0
                
                # Transform from scene coordinates to visible area image coordinates
                human_painter.translate(-visible_scene_left, -visible_scene_top)
                gen_painter.translate(-visible_scene_left, -visible_scene_top)
            else:
                # Fallback: no transformation if no mappings
                print(f"[DEBUG] No mappings available, using direct scene coordinates")
        
        human_pen = QPen(QColor(255, 255, 255), self.brush_size)
        # Generated segmentation paths already encode the dilated automatic
        # mask.  Rasterize their fill only: adding an outline and dilating it
        # again makes manual Clean cover substantially more than Automatic.
        gen_pen = QPen(Qt.PenStyle.NoPen)
        human_painter.setPen(human_pen)
        gen_painter.setPen(gen_pen)
        brush = QBrush(QColor(255, 255, 255))
        human_painter.setBrush(brush)
        gen_painter.setBrush(brush)

        for item in self._scene.items():
            if isinstance(item, QGraphicsPathItem) and item != self.viewer.photo:
                painter = gen_painter if QColor(item.brush().color().name(QColor.HexArgb)) == "#80ff0000" else human_painter
                # Get the path bounding rect to see where the stroke is
                item_pos = item.pos()
                # Draw the path - the painter already has the transformation applied
                # We need to draw at the item position + path coordinates
                painter.save()
                painter.translate(item_pos)
                painter.drawPath(item.path())
                painter.restore()
        
        human_painter.end()
        gen_painter.end()
        
        def qimage_to_np(qimg):
            # Check for valid dimensions
            if qimg.width() <= 0 or qimg.height() <= 0:
                return np.zeros((max(1, qimg.height()), max(1, qimg.width())), dtype=np.uint8)
            
            ptr = qimg.constBits()
            arr = np.array(ptr).reshape(qimg.height(), qimg.bytesPerLine())
            return arr[:, :qimg.width()]
            
        human_mask = qimage_to_np(human_qimg)
        gen_mask = qimage_to_np(gen_qimg)

        # Human brush strokes are intentionally expanded for forgiving manual
        # cleanup.  Generated segmentation paths must retain their original
        # automatic-mask geometry.
        # A one-pixel halo catches antialiased edges without making the actual
        # cleanup area noticeably wider than the brush the user painted.
        human_mask = imk.dilate(human_mask, np.ones((3, 3), np.uint8), iterations=1)
        # Clean needs to know which pixels the user painted by hand: those
        # must never be dropped as bubble-outline "spill" (pipeline/inpainting.py).
        self.last_human_mask = human_mask if np.any(human_mask) else None

        # Combine masks (bitwise_or equivalent)
        final_mask = np.where((human_mask > 0) | (gen_mask > 0), 255, 0).astype(np.uint8)
        return final_mask
    
    def draw_segmentation_lines(self, text_bbox, image=None, stroke=None):
        if stroke is None:
            stroke = self.make_segmentation_stroke_data(text_bbox, image)
        if stroke is None:
            return

        # Wrap in one GraphicsPathItem & emit
        fill_color = QtGui.QColor(255, 0, 0, 128)  # Semi-transparent red
        outline_color = QtGui.QColor(255, 0, 0)    # Solid red
        item = QtWidgets.QGraphicsPathItem(stroke['path'])
        item.setPen(QtGui.QPen(outline_color, 2, QtCore.Qt.SolidLine))
        item.setBrush(QtGui.QBrush(fill_color))

        self.viewer.command_emitted.emit(SegmentBoxesCommand(self.viewer, [item]))
        
        # Ensure the rectangles are visible
        self.viewer._scene.update()

    def make_segmentation_stroke_data(self, text_bbox, image=None):
        blk = text_bbox if hasattr(text_bbox, "xyxy") else None
        bbox = blk.xyxy if blk is not None else text_bbox
        if bbox is None or len(bbox) < 4:
            return None

        # 1) Use text_bbox coordinates directly
        min_x, min_y, max_x, max_y = [int(v) for v in bbox]
        w, h = max_x - min_x + 1, max_y - min_y + 1

        # 2) Get the image
        visible_scene_top = 0
        visible_scene_left = 0
        if image is None:
            if self.viewer.webtoon_mode:
                visible_image, mappings = self.viewer.get_visible_area_image()
                if visible_image is not None and mappings:
                    image = visible_image
                    visible_scene_top = mappings[0]['scene_y_start']
                    visible_scene_left = 0
                    img_min_x = int(min_x - visible_scene_left)
                    img_min_y = int(min_y - visible_scene_top)
                    img_max_x = int(max_x - visible_scene_left)
                    img_max_y = int(max_y - visible_scene_top)
                else:
                    image = None
            else:
                image = self.viewer.get_image_array()
        else:
            # Image is provided directly (e.g. from background or multi-page worker)
            if self.viewer.webtoon_mode:
                img_min_x = min_x
                img_min_y = min_y
                img_max_x = max_x
                img_max_y = max_y

        crop_mask = None
        cx1, cy1 = 0, 0
        if image is not None:
            try:
                if blk is not None and not self.viewer.webtoon_mode:
                    crop_mask, bounds = build_block_mask_data(
                        image,
                        blk,
                        default_padding=5,
                        require_text_or_translation=False,
                        clip_to_bubble=True,
                    )
                    if crop_mask is not None and bounds is not None:
                        cx1, cy1, _cx2, _cy2 = [int(v) for v in bounds]
                else:
                    if self.viewer.webtoon_mode:
                        crop_bbox = [img_min_x, img_min_y, img_max_x, img_max_y]
                    else:
                        crop_bbox = [min_x, min_y, max_x, max_y]

                    cx1, cy1, cx2, cy2 = adjust_text_line_coordinates(crop_bbox, 10, 10, image)
                    crop = image[cy1:cy2, cx1:cx2]

                    crop_mask = detect_content_mask_in_bbox(crop)
                    if crop_mask is not None and np.any(crop_mask):
                        close_kernel = imk.get_structuring_element(imk.MORPH_RECT, (3, 3))
                        crop_mask = imk.morphology_ex(crop_mask, imk.MORPH_CLOSE, close_kernel)
                        dil_kernel = np.ones((5, 5), np.uint8)
                        crop_mask = imk.dilate(crop_mask, dil_kernel, iterations=1)
            except Exception as e:
                print(f"Failed to generate pixel-accurate mask in make_segmentation_stroke_data: {e}")
                crop_mask = None

        if crop_mask is not None and np.any(crop_mask):
            contours, _ = imk.find_contours(crop_mask)
            path = QtGui.QPainterPath()
            path.setFillRule(Qt.FillRule.WindingFill)
            for cnt in contours:
                pts = cnt.squeeze(1)
                if pts.ndim != 2 or pts.shape[0] < 3:
                    continue
                x0, y0 = pts[0]
                offset_x = cx1 + visible_scene_left
                offset_y = cy1 + visible_scene_top
                path.moveTo(x0 + offset_x, y0 + offset_y)
                for x, y in pts[1:]:
                    path.lineTo(x + offset_x, y + offset_y)
                path.closeSubpath()
        else:
            # Fallback to block bounding box if crop mask generation fails
            path = QtGui.QPainterPath()
            path.addRect(min_x, min_y, w, h)

        if path.isEmpty():
            return None

        stroke = {
            'path': path,
            'pen': QColor(255, 0, 0).name(QColor.HexArgb),
            'brush': QColor(255, 0, 0, 128).name(QColor.HexArgb),
            'width': 2,
        }
        return stroke

    # def draw_segmentation_lines(self, bboxes, layers: int = 1, scale_factor: float = 1.0):
    #     if not self.viewer.hasPhoto() or not bboxes: return
        
    #     all_points = np.array(bboxes).reshape(-1, 2)
    #     centroid = np.mean(all_points, axis=0)

    #     scaled_segments = []
    #     for x1, y1, x2, y2 in bboxes:
    #         p1 = (np.array([x1, y1]) - centroid) * scale_factor + centroid
    #         p2 = (np.array([x2, y2]) - centroid) * scale_factor + centroid
    #         scaled_segments.append((*p1, *p2))
            
    #     fill_color = QColor(255, 0, 0, 128)
    #     outline_color = QColor(255, 0, 0)
    #     pen = QPen(outline_color, 2, Qt.SolidLine)
    #     brush = QBrush(fill_color)

    #     items = []
    #     for _ in range(layers):
    #         for x1, y1, x2, y2 in scaled_segments:
    #             path = QPainterPath()
    #             path.addRect(QtCore.QRectF(x1, y1, x2 - x1, y2 - y1))
    #             path_item = QGraphicsPathItem(path)
    #             path_item.setPen(pen)
    #             path_item.setBrush(brush)
    #             items.append(path_item)
        
    #     if items:
    #         command = SegmentBoxesCommand(self.viewer, items)
    #         self.viewer.command_emitted.emit(command)
