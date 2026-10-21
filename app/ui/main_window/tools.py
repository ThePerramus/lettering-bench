import os

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtGui import QFontDatabase
import imkit as imk

from .constants import user_font_path
from app.path_materialization import ensure_path_materialized
from app.ui.messages import Messages


class ToolStateMixin:
    def toggle_text_panel(self):
        visible = self.text_panel_toggle_button.isChecked()
        self.text_section_widget.setVisible(visible)
        self._reposition_canvas_overlays()

    def toggle_reference_panel(self):
        visible = self.reference_panel_toggle_button.isChecked()
        self.reference_section_widget.setVisible(visible)
        self._reposition_canvas_overlays()

    def toggle_pan_tool(self):
        if self.pan_button.isChecked():
            self.set_tool("pan")
        else:
            self.set_tool(None)

    def toggle_box_tool(self):
        if self.box_button.isChecked():
            self.set_tool("box")
        else:
            self.set_tool(None)

    def toggle_brush_tool(self):
        if self.brush_button.isChecked():
            self.set_tool("brush")
            size = self.image_viewer.brush_size
            self.set_slider_size(size)
        else:
            self.set_tool(None)

    def toggle_eraser_tool(self):
        if self.eraser_button.isChecked():
            self.set_tool("eraser")
            size = self.image_viewer.eraser_size
            self.set_slider_size(size)
        else:
            self.set_tool(None)

    def toggle_eyedropper_tool(self):
        if self.eyedropper_button.isChecked():
            self.set_tool("eyedropper")
        else:
            self.set_tool(None)

    def toggle_pencil_tool(self):
        if self.pencil_button.isChecked():
            self.set_tool("pencil")
            size = self.image_viewer.drawing_manager.pencil_size
            self.set_slider_size(size)
        else:
            self.set_tool(None)

    def toggle_patch_eraser_tool(self):
        if self.patch_eraser_button.isChecked():
            self.set_tool("patch_eraser")
            size = self.image_viewer.drawing_manager.patch_eraser_size
            self.set_slider_size(size)
        else:
            self.set_tool(None)

    def on_color_picked(self):
        """Called after the eyedropper samples a color: switch straight to the pencil."""
        self.set_tool("pencil")
        size = self.image_viewer.drawing_manager.pencil_size
        self.set_slider_size(size)

    def toggle_reveal_pencil_tool(self):
        if self.reveal_pencil_button.isChecked():
            self.set_tool("reveal_pencil")
            size = self.image_viewer.drawing_manager.reveal_pencil_size
            self.set_slider_size(size)
        else:
            self.set_tool(None)

    def load_reference_book(self):
        """Load a whole second edition (cbz/cbr/pdf/...) of the same comic, so its
        pages can be auto-paired with this book's pages by index + an offset,
        instead of picking a reference image one page at a time."""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, self.tr("Select Reference Book"), os.path.expanduser("~"),
            self.tr("Comic Archives") + " (*.cbr *.cbz *.cbt *.cb7 *.zip *.rar *.7z *.tar *.pdf *.epub)"
        )
        if not path:
            return
        self._load_reference_book_from_path(path, reset_state=True)

    def _load_reference_book_from_path(self, path: str, reset_state: bool):
        self.reference_book_handler.prepare_files([path])
        self.reference_book_path = path
        if reset_state:
            # Any alignment confirmed so far paired pages using the *previous*
            # book/offsets, which is now meaningless - drop it so the next
            # "load reference" click re-resolves fresh instead of silently
            # reusing a stale (likely wrong) reference image.
            self.reference_images.clear()
            self.reference_page_offsets = []
            self.reference_offset_spin.blockSignals(True)
            self.reference_offset_spin.setValue(0)
            self.reference_offset_spin.blockSignals(False)
        self._update_reference_offset_label()
        self._sync_reveal_source()
        self._sync_hbutton_group_for_reference()

    def try_restore_reference_book(self):
        """Called right after a project finishes loading: re-index whatever
        reference book was loaded when the project was last saved, so
        existing alignments/offsets keep working without making the user
        reload it by hand every time. Warns instead of silently doing
        nothing if the file has since moved or been deleted."""
        path = self.reference_book_path
        if not path:
            return
        if not os.path.isfile(path):
            Messages.show_reference_book_missing_error(self, path)
            self.reference_book_path = None
            return
        self._load_reference_book_from_path(path, reset_state=False)

    def _effective_reference_offset(self, hq_index: int) -> int:
        """Offsets are set per-breakpoint ("from this page onward"), not a
        single global number, so a divider/insert page partway through the
        reference book can be corrected without breaking pairing for every
        page before it. Returns the value of the last breakpoint at or before
        hq_index, or 0 if none has been set yet."""
        offset = 0
        for start_index, value in self.reference_page_offsets:
            if start_index <= hq_index:
                offset = value
            else:
                break
        return offset

    def set_reference_page_offset(self, value: int):
        """Sets the offset starting at the current page and onward, until the
        next breakpoint (if any). Pages before the current one are untouched."""
        index = self.curr_img_idx
        if self._effective_reference_offset(index) == value and not any(
                start == index for start, _ in self.reference_page_offsets):
            return  # no-op: matches what would already apply here, not a real edit

        self.reference_page_offsets = [
            (start, v) for start, v in self.reference_page_offsets if start != index
        ]
        self.reference_page_offsets.append((index, value))
        self.reference_page_offsets.sort(key=lambda pair: pair[0])

        # Only pages from here onward could have had their effective offset
        # change because of this edit.
        for path in self.image_files[index:]:
            self.reference_images.pop(path, None)

        self._update_reference_offset_label()
        self._sync_reveal_source()

    def toggle_reference_page_excluded(self, checked: int):
        """Mark/unmark the current page as having no counterpart at all in the
        reference book (e.g. a bonus page only present in this edition), so it
        gets skipped by the reference workflow and translated the normal way."""
        if not self.image_files:
            return
        file_path = self.image_files[self.curr_img_idx]
        if checked:
            self.reference_excluded_pages.add(file_path)
            # Whatever was aligned here (if anything) no longer applies.
            self.reference_images.pop(file_path, None)
            if self.image_viewer.reference_manager.active_file_path == file_path:
                self.image_viewer.reference_manager.cancel()
                self.load_reference_button.setChecked(False)
                self.confirm_reference_button.setEnabled(False)
        else:
            self.reference_excluded_pages.discard(file_path)
        self._update_reference_offset_label()
        self._sync_reveal_source()
        self._sync_hbutton_group_for_reference()

    def _ensure_warped(self, file_path: str):
        """Reloading a project restores ref_path + corners but never the baked
        'warped' array (too big to persist) - recompute it once, on first use
        after loading, and cache it back onto the entry exactly like a fresh
        confirm() would have produced."""
        entry = self.reference_images.get(file_path)
        if not entry or entry.get('warped') is not None:
            return
        ref_path = entry.get('ref_path')
        corners = entry.get('corners')
        if not ref_path or not corners:
            return

        ensure_path_materialized(ref_path)
        ref_image = imk.read_image(ref_path)
        if ref_image is None:
            return

        if (self.image_files and file_path == self.image_files[self.curr_img_idx]
                and self.image_viewer.hasPhoto()):
            page_rect = self.image_viewer.photo.boundingRect()
            out_w, out_h = int(round(page_rect.width())), int(round(page_rect.height()))
        else:
            ensure_path_materialized(file_path)
            page_img = imk.read_image(file_path)
            if page_img is None:
                return
            out_h, out_w = page_img.shape[:2]

        rh, rw = ref_image.shape[:2]
        src = np.array([[0, 0], [rw, 0], [rw, rh], [0, rh]], dtype=np.float64)
        dst = np.array(corners, dtype=np.float64)
        try:
            matrix = imk.get_perspective_transform(src, dst)
            entry['warped'] = imk.warp_perspective(ref_image, matrix, (out_w, out_h))
        except Exception:
            pass

    def _sync_reveal_source(self):
        """Keep image_viewer.reveal_source (what the reveal pencil samples
        from) in lockstep with whatever confirmed alignment - if any - exists
        for the page currently on screen."""
        if not self.image_files:
            self.image_viewer.reveal_source = None
            self.reveal_pencil_button.setEnabled(False)
            return
        file_path = self.image_files[self.curr_img_idx]
        self._ensure_warped(file_path)
        entry = self.reference_images.get(file_path)
        warped = entry.get('warped') if entry else None
        self.image_viewer.reveal_source = warped
        self.reveal_pencil_button.setEnabled(warped is not None)

    def _page_uses_reference_workflow(self, file_path: str) -> bool:
        """True when this page should skip Recognize/Translate and have
        Render reveal the aligned reference instead: either it already has a
        confirmed alignment, or a reference book is loaded at all (so pages
        not yet aligned still count, since the intent is clear before you've
        gotten around to dragging their corners) - unless explicitly excluded."""
        if file_path in self.reference_excluded_pages:
            return False
        if file_path in self.reference_images:
            return True
        return bool(self.reference_book_handler.file_paths)

    @staticmethod
    def _square_erode(mask: np.ndarray, margin: int) -> np.ndarray:
        """Binary erosion by a square (2*margin+1) kernel, treating anything
        outside the array as background - implemented directly with numpy
        since imk.erode()/get_structuring_element(MORPH_ELLIPSE, ...) turned
        out not to behave like a normal binary erosion at small kernel sizes
        (the mahotas disk() approximation collapses to a single center pixel,
        and mh.erode itself doesn't treat out-of-bounds as background), and a
        square kernel is separable so this stays cheap even for a modest
        margin. Verified directly against imk.erode's actual behavior before
        writing this - it left every pixel of an all-foreground mask
        unchanged, corners included, regardless of kernel size."""
        if margin <= 0:
            return mask
        h, w = mask.shape
        padded = np.pad(mask, ((0, 0), (margin, margin)), mode='constant', constant_values=False)
        result = np.ones_like(mask, dtype=bool)
        for shift in range(2 * margin + 1):
            result &= padded[:, shift:shift + w]
        padded = np.pad(result, ((margin, margin), (0, 0)), mode='constant', constant_values=False)
        result = np.ones_like(mask, dtype=bool)
        for shift in range(2 * margin + 1):
            result &= padded[shift:shift + h, :]
        return result

    @staticmethod
    def _square_dilate(mask: np.ndarray, margin: int) -> np.ndarray:
        """Binary dilation by a square (2*margin+1) kernel - the OR-based
        mirror of _square_erode, same separable two-pass approach."""
        if margin <= 0:
            return mask
        h, w = mask.shape
        padded = np.pad(mask, ((0, 0), (margin, margin)), mode='constant', constant_values=False)
        result = np.zeros_like(mask, dtype=bool)
        for shift in range(2 * margin + 1):
            result |= padded[:, shift:shift + w]
        padded = np.pad(result, ((margin, margin), (0, 0)), mode='constant', constant_values=False)
        result = np.zeros_like(mask, dtype=bool)
        for shift in range(2 * margin + 1):
            result |= padded[shift:shift + h, :]
        return result

    def auto_reveal_pages(self, file_paths: list) -> bool:
        """For each page, replaces every existing (Segment+Clean) patch's
        opaque pixels with the corresponding pixels from that page's aligned
        reference - the automatic counterpart to the reveal pencil, applied to
        whatever's already been cleaned instead of a hand-drawn stroke.
        Returns True if at least one page actually got revealed into."""
        from app.ui.commands.base import _load_patch_image_rgba
        from app.ui.commands.inpaint import PatchEraseCommand
        from modules.detection.utils.content import detect_content_mask_in_bbox

        any_done = False
        for file_path in file_paths:
            self._ensure_warped(file_path)
            entry = self.reference_images.get(file_path)
            reveal_source = entry.get('warped') if entry else None
            patches = self.image_patches.get(file_path, [])
            stack = self.undo_stacks.get(file_path)
            if reveal_source is None or not patches or stack is None:
                continue

            erased = []
            for prop in list(patches):
                if prop.get('kind') in ('pencil', 'reveal_pencil'):
                    # A manual correction, not Clean's own output - leave it
                    # exactly as painted. Re-revealing into it independently
                    # (its own tiny crop, its own content-mask decision) could
                    # disagree with the surrounding, already-correctly-revealed
                    # text and show up as a visible seam right where the user
                    # just fixed something.
                    continue
                ensure_path_materialized(prop['png_path'])
                img = _load_patch_image_rgba(png_path=prop['png_path'])
                if img is None:
                    continue
                x, y, w, h = prop['bbox']
                if reveal_source.shape[0] < y + h or reveal_source.shape[1] < x + w:
                    continue
                ref_crop = reveal_source[y:y + h, x:x + w, :3]
                if ref_crop.shape[:2] != img.shape[:2]:
                    continue
                raw_mask = img[:, :, 3] > 0
                if not raw_mask.any():
                    continue

                page_h, page_w = reveal_source.shape[:2]
                diagonal = (page_h ** 2 + page_w ** 2) ** 0.5
                margin = max(1, round(2 * diagonal / 1000.0))

                # The patch itself is just a plain opaque rectangle (Segment's
                # actual bubble/content outline only ever determined its
                # bbox, not its shape) - revealing the whole thing covers an
                # irregular bubble's corners with the reference's own
                # artwork/background there, which then had to be cleaned up
                # by hand with the patch eraser. Detect where the reference's
                # own text actually is instead (the same content-mask
                # detector Segment itself uses, just run on the aligned
                # reference crop instead of this page), and reveal only
                # that - not the bubble's interior, not the rectangle, just
                # the ink that's actually being brought over.
                content_mask = detect_content_mask_in_bbox(ref_crop) > 0
                if content_mask.any():
                    # A low-quality scan's text is rarely solid-colored
                    # (halftone dots, JPEG ringing, soft anti-aliasing), so
                    # the raw content mask only catches each stroke's
                    # darkest core pixels - revealing just that leaves
                    # "weak", thinned-out letters next to Clean's own fill.
                    # Fatten it back out a little so the full stroke width
                    # comes through.
                    content_mask = self._square_dilate(content_mask, max(1, margin // 2))
                    mask = content_mask & raw_mask
                else:
                    # No content detected (e.g. a genuinely blank bubble) -
                    # fall back to the previous behavior instead of silently
                    # revealing nothing. Clean's mask is deliberately a bit
                    # generous (any leftover untouched foreign-text pixel
                    # looks worse for ML inpaint than painting a few extra
                    # background pixels), which is fine for inpainting - the
                    # fill blends with its surroundings - but a reveal pastes
                    # EXACT reference pixels, so the same margin would show
                    # up as a visible sliver of the reference's own artwork
                    # around the text. Shrink the mask a little first;
                    # whatever falls outside it just keeps Clean's own
                    # result.
                    mask = self._square_erode(raw_mask, margin)
                if not mask.any():
                    continue
                new_img = img.copy()
                new_img[mask, :3] = ref_crop[mask]
                erased.append({'old_patch': prop, 'new_image': new_img, 'revealed': True})

            if not erased:
                continue

            # PatchEraseCommand is really "replace this patch's pixels, same
            # bbox/hash lineage, undo restores the original" - reused as-is,
            # it doesn't care whether the replacement has less alpha (a punched
            # hole) or the same alpha with different colors (a reveal).
            command = PatchEraseCommand(self, erased, file_path)
            stack.push(command)
            any_done = True

        return any_done

    def _sync_hbutton_group_for_reference(self):
        """Applies the Detect/Recognize/Translate reference-page gating,
        hides the text/style panel, and auto-expands the Reference Page
        tools section - called on every page switch. Detect is skippable on
        a reference page because Segment already runs detection itself when
        it has no blocks yet (load_segmentation_points, manual_workflow.py)
        - confirmed to produce the same result as clicking Detect first.
        Uses Segment's own enabled state (never touched by this gating) as
        a proxy for "is the manual button group actually active right now"
        so this never fights disable_hbutton_group() while work is in
        progress, or re-enables anything while Automatic mode has the group
        disabled."""
        if not self.image_files:
            return
        buttons = self.hbutton_group.get_button_group().buttons()
        uses_reference = self._page_uses_reference_workflow(self.image_files[self.curr_img_idx])
        group_active = buttons[3].isEnabled()  # Segment
        if uses_reference:
            buttons[0].setEnabled(False)  # Detect
            buttons[1].setEnabled(False)  # Recognize
            buttons[2].setEnabled(False)  # Translate
        elif group_active:
            buttons[0].setEnabled(True)
            buttons[1].setEnabled(True)
            buttons[2].setEnabled(True)

        if hasattr(self, "text_section_widget"):
            self.text_section_widget.setVisible(not uses_reference)
            if not uses_reference:
                self._reposition_text_flyout()
                self.text_section_widget.raise_()
        if hasattr(self, "text_panel_toggle_button"):
            with QtCore.QSignalBlocker(self.text_panel_toggle_button):
                self.text_panel_toggle_button.setChecked(not uses_reference)
        if hasattr(self, "reference_section_widget"):
            self.reference_section_widget.setVisible(uses_reference)
            if uses_reference:
                self._reposition_reference_flyout()
                self.reference_section_widget.raise_()
        if hasattr(self, "reference_panel_toggle_button"):
            with QtCore.QSignalBlocker(self.reference_panel_toggle_button):
                self.reference_panel_toggle_button.setChecked(uses_reference)
        self._reposition_canvas_overlays()

    def render_or_reveal(self):
        """The Render button's handler: for pages using the reference
        workflow, there's no translated text to render (Recognize/Translate
        are skipped for them) - reveal the aligned reference into whatever's
        been cleaned instead. Mirrors render_text()'s own single/multi-page
        split so the same button keeps working the same way either way."""
        selected_paths = self.get_selected_page_paths()
        if len(selected_paths) > 1:
            target_paths = selected_paths
        elif self.image_files:
            target_paths = [self.image_files[self.curr_img_idx]]
        else:
            target_paths = []

        reveal_paths = [p for p in target_paths if self._page_uses_reference_workflow(p)]
        if reveal_paths:
            revealed_any = self.auto_reveal_pages(reveal_paths)
            if not revealed_any:
                self._explain_nothing_revealed(reveal_paths)
        if len(reveal_paths) < len(target_paths):
            self.text_ctrl.render_text()

    def _explain_nothing_revealed(self, reveal_paths: list):
        """render_or_reveal() found pages in reference mode but couldn't
        actually reveal anything into them - tell the user why instead of
        silently doing nothing, which just looks like Render is broken."""
        if len(reveal_paths) != 1:
            Messages.show_nothing_to_reveal_error(
                self, self.tr("none of the selected pages have both an aligned reference and cleaned patches yet."))
            return
        file_path = reveal_paths[0]
        if file_path not in self.reference_images:
            Messages.show_nothing_to_reveal_error(
                self, self.tr("this page hasn't been aligned to a reference yet (use the Reference Page tools above)."))
        elif not self.image_patches.get(file_path):
            Messages.show_nothing_to_reveal_error(
                self, self.tr("nothing has been cleaned on this page yet (run Segment, then Clean, first)."))
        else:
            Messages.show_nothing_to_reveal_error(
                self, self.tr("the aligned reference doesn't line up with this page's cleaned areas."))

    def _update_reference_offset_label(self):
        file_path = self.image_files[self.curr_img_idx] if self.image_files else None
        excluded = file_path is not None and file_path in self.reference_excluded_pages

        self.reference_excluded_checkbox.blockSignals(True)
        self.reference_excluded_checkbox.setChecked(excluded)
        self.reference_excluded_checkbox.blockSignals(False)
        self.load_reference_button.setEnabled(not excluded)

        if excluded:
            self.reference_offset_label.setText(
                self.tr("No reference for this page - will use traditional translation."))
            self._set_reference_preview(None)
            return

        paths = self.reference_book_handler.file_paths
        offset = self._effective_reference_offset(self.curr_img_idx)
        self.reference_offset_spin.blockSignals(True)
        self.reference_offset_spin.setValue(offset)
        self.reference_offset_spin.blockSignals(False)

        if not paths:
            self.reference_offset_label.setText(self.tr("No reference book loaded"))
            self._set_reference_preview(None)
            return
        index = self.curr_img_idx + offset
        if 0 <= index < len(paths):
            ref_path = paths[index]
            name = os.path.basename(ref_path)
            self.reference_offset_label.setText(
                self.tr("Reference page {0}/{1}: {2} (offset {3})").format(
                    index + 1, len(paths), name, offset))
            self._set_reference_preview(ref_path)
        else:
            self.reference_offset_label.setText(self.tr("Reference page out of range for this offset"))
            self._set_reference_preview(None)

    def _set_reference_preview(self, ref_path: str | None):
        """Small thumbnail of the reference page currently paired with the page
        you're on, so tuning the offset doesn't require opening the reference
        book separately to count pages."""
        if not ref_path:
            self.reference_preview_label.clear()
            self.reference_preview_label.setText(self.tr("(no preview)"))
            return
        try:
            ensure_path_materialized(ref_path)
            img = imk.read_image(ref_path)
            if img is None:
                raise ValueError("could not read reference image")
            h, w = img.shape[:2]
            qimage = QtGui.QImage(img.data, w, h, img.strides[0],
                                   QtGui.QImage.Format.Format_RGB888).copy()
            pixmap = QtGui.QPixmap.fromImage(qimage).scaledToWidth(
                140, QtCore.Qt.TransformationMode.SmoothTransformation)
            self.reference_preview_label.setPixmap(pixmap)
        except Exception:
            self.reference_preview_label.clear()
            self.reference_preview_label.setText(self.tr("(preview unavailable)"))

    def _resolve_reference_book_page(self) -> str | None:
        """The reference-book page paired with the currently displayed page,
        per the configured offset, or None if no book is loaded / out of range /
        this page was explicitly marked as having no reference counterpart."""
        paths = self.reference_book_handler.file_paths
        if not paths or not self.image_files:
            return None
        if self.image_files[self.curr_img_idx] in self.reference_excluded_pages:
            return None
        index = self.curr_img_idx + self._effective_reference_offset(self.curr_img_idx)
        if index < 0 or index >= len(paths):
            return None
        ref_path = paths[index]
        ensure_path_materialized(ref_path)
        return ref_path

    def _sync_reference_alignment_buttons(self):
        """Navigating pages silently cancels any in-progress alignment (see
        ImageViewer.clear_scene), but that left the toggle button showing
        "checked" with nothing actually on screen - so the next click just
        turned the (already-gone) overlay off instead of loading anything,
        and it took a second click to get it to reappear. Keep the button's
        checked state (and the confirm button's enabled state) truthful."""
        active = self.image_viewer.reference_manager.active
        self.load_reference_button.blockSignals(True)
        self.load_reference_button.setChecked(active)
        self.load_reference_button.blockSignals(False)
        self.confirm_reference_button.setEnabled(active)
        self.reset_reference_button.setEnabled(active)

    def toggle_reference_alignment(self):
        """Overlay a second scan of the current page (semi-transparent, 4 draggable
        corners) so its art can be dragged into alignment with this page's own."""
        if self.load_reference_button.isChecked():
            if not self.image_viewer.hasPhoto() or not self.image_files:
                self.load_reference_button.setChecked(False)
                return

            file_path = self.image_files[self.curr_img_idx]
            existing = self.reference_images.get(file_path)
            saved_corners = existing.get('corners') if existing else None
            ref_path = existing.get('ref_path') if existing else None

            if not ref_path:
                ref_path = self._resolve_reference_book_page()

            if not ref_path:
                ref_path, _ = QtWidgets.QFileDialog.getOpenFileName(
                    self, self.tr("Select Reference Page Image"), os.path.expanduser("~"),
                    self.tr("Images") + " (*.png *.jpg *.jpeg *.bmp *.webp)"
                )
                if not ref_path:
                    self.load_reference_button.setChecked(False)
                    return
                saved_corners = None

            if saved_corners is None:
                # Best-effort automatic starting position instead of always
                # defaulting to "stretch the reference over the whole page" -
                # a bad guess is never worse than that default (you still drag
                # the same 4 corners to fix it), a good one saves doing it by
                # hand on every single page.
                saved_corners = self._auto_align_corners(file_path, ref_path)

            started = self.image_viewer.reference_manager.start(file_path, ref_path, saved_corners)
            if not started:
                self.load_reference_button.setChecked(False)
                return

            self.image_viewer.reference_manager.set_opacity(self.reference_opacity_slider.value() / 100.0)
            self.set_tool("align_reference")
            self.confirm_reference_button.setEnabled(True)
            self.reset_reference_button.setEnabled(True)
        else:
            self.image_viewer.reference_manager.cancel()
            self.set_tool(None)
            self.confirm_reference_button.setEnabled(False)
            self.reset_reference_button.setEnabled(False)

    def reset_reference_alignment(self):
        """Restarts the in-progress alignment at the default full-page
        rectangle, discarding whatever corners it currently has. The
        automatic starting guess (_auto_align_corners) is only ever a
        best-effort head start - on a bad page it can put a corner so far
        outside the visible area that dragging it back by hand (even with
        the scene-rect-expansion that normally makes off-page handles
        reachable) is impractical. This is the escape hatch: same manual
        drag-from-scratch flow as if auto-align had found nothing at all."""
        rm = self.image_viewer.reference_manager
        if not rm.active:
            return
        file_path, ref_path = rm.active_file_path, rm.ref_path
        if not rm.start(file_path, ref_path, saved_corners=None):
            return
        rm.set_opacity(self.reference_opacity_slider.value() / 100.0)

    def _auto_align_corners(self, file_path: str, ref_path: str):
        """Best-effort automatic starting alignment: detect text boxes AND
        rough panel boundaries on both the HQ page and the reference page,
        match them by position, and fit a perspective transform from the
        matches. Returns 4 [x, y] points - where the reference image's own
        corners should land in the HQ page's coordinates - or None if there
        weren't enough confident matches to bother; the manual corner drag is
        the fallback either way, this only changes where the handles start.

        Text blocks alone tend to cluster wherever the dialogue is (often the
        upper/middle area of a page), leaving panels with little or no text
        poorly constrained - the fit fine near the text and drifting further
        from it. Panel boxes (from detect_panel_boxes(), a plain connected-
        components heuristic, no model) are far more evenly spread across the
        whole page, so mixing both kinds of points in as one set of "things
        that should land in the same relative spot on both scans" gives much
        better coverage without needing a second detection model."""
        try:
            from modules.detection.processor import TextBlockDetector
            from modules.utils.image_utils import detect_panel_boxes
        except Exception:
            return None

        hq_img = self.image_data.get(file_path)
        if hq_img is None:
            try:
                hq_img = self.load_image(file_path)
            except Exception:
                return None
        ensure_path_materialized(ref_path)
        ref_img = imk.read_image(ref_path)
        if hq_img is None or ref_img is None:
            return None

        def block_centers(blocks):
            return [(float(b.center[0]), float(b.center[1])) for b in blocks]

        def box_centers(boxes):
            return [((x1 + x2) / 2.0, (y1 + y2) / 2.0) for x1, y1, x2, y2 in boxes]

        try:
            detector = TextBlockDetector(self.settings_page)
            # Reuse an already-detected page's blocks instead of re-running
            # detection on it - the reference image never has one to reuse.
            hq_state_blocks = self.image_states.get(file_path, {}).get('blk_list')
            hq_blocks = list(hq_state_blocks) if hq_state_blocks else detector.detect(hq_img)
            ref_blocks = detector.detect(ref_img)
        except Exception:
            hq_blocks, ref_blocks = [], []

        try:
            hq_panels = detect_panel_boxes(hq_img)
            ref_panels = detect_panel_boxes(ref_img)
        except Exception:
            hq_panels, ref_panels = [], []

        hq_points = block_centers(hq_blocks) + box_centers(hq_panels)
        ref_points = block_centers(ref_blocks) + box_centers(ref_panels)

        if len(hq_points) < 4 or len(ref_points) < 4:
            return None

        hq_h, hq_w = hq_img.shape[:2]
        ref_h, ref_w = ref_img.shape[:2]

        # Match by position normalized to each image's own size, so a
        # different-resolution/aspect scan still lines up by relative
        # location on the page rather than raw pixel coordinates.
        hq_norm = [(x / hq_w, y / hq_h) for x, y in hq_points]
        ref_norm = [(x / ref_w, y / ref_h) for x, y in ref_points]

        max_dist = 0.15  # loose gate: 15% of page size: works across the count/
                          # order mismatches expected between two editions.
        used_ref = set()
        src_pts, dst_pts = [], []
        for i, (hx, hy) in enumerate(hq_norm):
            best_j, best_d = None, None
            for j, (rx, ry) in enumerate(ref_norm):
                if j in used_ref:
                    continue
                d = (hx - rx) ** 2 + (hy - ry) ** 2
                if best_d is None or d < best_d:
                    best_d, best_j = d, j
            if best_j is not None and best_d <= max_dist ** 2:
                used_ref.add(best_j)
                src_pts.append(ref_points[best_j])
                dst_pts.append(hq_points[i])

        if len(src_pts) < 4:
            return None

        matrix = self._fit_homography_robust(np.array(src_pts, dtype=np.float64),
                                              np.array(dst_pts, dtype=np.float64))
        if matrix is None:
            return None

        ref_corners = np.array([[0, 0], [ref_w, 0], [ref_w, ref_h], [0, ref_h]], dtype=np.float64)
        ones = np.ones((4, 1))
        mapped = np.hstack([ref_corners, ones]) @ matrix.T
        mapped = mapped[:, :2] / mapped[:, 2:3]
        return mapped.tolist()

    @staticmethod
    def _solve_homography_lstsq(src: np.ndarray, dst: np.ndarray):
        """3x3 homography from N>=4 point correspondences via least squares -
        the same Direct Linear Transform imk.get_perspective_transform uses,
        generalized past its exactly-4-points square solve since automatic
        matches need outlier tolerance a single exact fit can't provide."""
        n = src.shape[0]
        A = np.zeros((2 * n, 8))
        b = np.zeros(2 * n)
        xs, ys = src[:, 0], src[:, 1]
        xd, yd = dst[:, 0], dst[:, 1]
        A[0::2, 0] = xs
        A[0::2, 1] = ys
        A[0::2, 2] = 1
        A[0::2, 6] = -xs * xd
        A[0::2, 7] = -ys * xd
        b[0::2] = xd
        A[1::2, 3] = xs
        A[1::2, 4] = ys
        A[1::2, 5] = 1
        A[1::2, 6] = -xs * yd
        A[1::2, 7] = -ys * yd
        b[1::2] = yd
        try:
            h, _residuals, rank, _sv = np.linalg.lstsq(A, b, rcond=None)
        except np.linalg.LinAlgError:
            return None
        if rank < 8:
            return None
        return np.append(h, 1).reshape(3, 3)

    @classmethod
    def _fit_homography_robust(cls, src: np.ndarray, dst: np.ndarray):
        """Fits _solve_homography_lstsq, then iteratively drops the single
        worst-fitting point and refits while its reprojection error is
        clearly out of line with the rest - a small, dependency-free stand-in
        for RANSAC, adequate for the modest point counts (a page's detected
        text boxes) this is used for."""
        pts_src, pts_dst = src, dst
        matrix = None
        for _ in range(len(src) - 4 + 1):
            if len(pts_src) < 4:
                return None
            matrix = cls._solve_homography_lstsq(pts_src, pts_dst)
            if matrix is None:
                return None
            ones = np.ones((len(pts_src), 1))
            mapped = np.hstack([pts_src, ones]) @ matrix.T
            mapped = mapped[:, :2] / mapped[:, 2:3]
            errors = np.linalg.norm(mapped - pts_dst, axis=1)
            worst_idx = int(np.argmax(errors))
            worst = errors[worst_idx]
            median = float(np.median(errors))
            if worst < max(25.0, median * 3):
                break
            pts_src = np.delete(pts_src, worst_idx, axis=0)
            pts_dst = np.delete(pts_dst, worst_idx, axis=0)
        return matrix

    def confirm_reference_alignment(self):
        result = self.image_viewer.reference_manager.confirm()
        if result is None:
            return
        file_path, ref_path, corners, warped = result
        self.reference_images[file_path] = {
            'ref_path': ref_path,
            'corners': corners,
            'warped': warped,
        }
        self.load_reference_button.setChecked(False)
        self.confirm_reference_button.setEnabled(False)
        self.reset_reference_button.setEnabled(False)
        self.set_tool(None)
        self._sync_reveal_source()
        self._sync_hbutton_group_for_reference()

    def set_reference_opacity(self, value: int):
        self.image_viewer.reference_manager.set_opacity(value / 100.0)

    def set_slider_size(self, size: int):
        self.brush_eraser_slider.blockSignals(True)
        self.brush_eraser_slider.setValue(size)
        self.brush_eraser_slider.blockSignals(False)

    def set_tool(self, tool_name: str):
        self.image_viewer.unsetCursor()
        self.image_viewer.set_tool(tool_name)

        for name, button in self.tool_buttons.items():
            if name != tool_name:
                button.setChecked(False)
            elif tool_name is not None:
                button.setChecked(True)

        if not tool_name:
            for button in self.tool_buttons.values():
                button.setChecked(False)
            self.image_viewer.setDragMode(QtWidgets.QGraphicsView.DragMode.NoDrag)

        if hasattr(self, "paint_options_widget"):
            self.paint_options_widget.setVisible(
                tool_name in ("brush", "eraser", "pencil", "patch_eraser", "reveal_pencil")
            )
            self._reposition_canvas_overlays()

    def set_brush_eraser_size(self, size: int):
        try:
            current_tool = self.image_viewer.current_tool
        except Exception:
            current_tool = None

        if current_tool == "brush":
            self.image_viewer.brush_size = size
        elif current_tool == "eraser":
            self.image_viewer.eraser_size = size
        elif current_tool == "pencil":
            self.image_viewer.drawing_manager.set_pencil_size(size, size)
        elif current_tool == "patch_eraser":
            self.image_viewer.drawing_manager.set_patch_eraser_size(size, size)
        elif current_tool == "reveal_pencil":
            self.image_viewer.drawing_manager.set_reveal_pencil_size(size, size)
        else:
            self.image_viewer.brush_size = size
            self.image_viewer.eraser_size = size
            self.image_viewer.drawing_manager.set_pencil_size(size, size)
            self.image_viewer.drawing_manager.set_patch_eraser_size(size, size)
            self.image_viewer.drawing_manager.set_reveal_pencil_size(size, size)

        if self.image_viewer.hasPhoto():
            image = self.image_viewer.get_image_array()
            if image is not None:
                h, w = image.shape[:2]
                scaled_size = self.scale_size(size, w, h)

                if current_tool in {"brush", "eraser", "pencil", "patch_eraser", "reveal_pencil"}:
                    self.image_viewer.set_br_er_size(size, scaled_size)
                else:
                    self.image_viewer.drawing_manager.set_brush_size(size, scaled_size)
                    self.image_viewer.drawing_manager.set_eraser_size(size, scaled_size)
                    self.image_viewer.drawing_manager.set_pencil_size(size, scaled_size)
                    self.image_viewer.drawing_manager.set_patch_eraser_size(size, scaled_size)
                    self.image_viewer.drawing_manager.set_reveal_pencil_size(size, scaled_size)

    def scale_size(self, base_size, image_width, image_height):
        image_diagonal = (image_width**2 + image_height**2) ** 0.5
        reference_diagonal = 1000
        scaling_factor = image_diagonal / reference_diagonal
        scaled_size = base_size * scaling_factor
        return scaled_size

    def _ensure_custom_font_caches(self) -> None:
        if not hasattr(self, "_custom_font_path_to_family"):
            self._custom_font_path_to_family = {}
        if not hasattr(self, "_custom_font_family_cache"):
            self._custom_font_family_cache = {}
        if not hasattr(self, "_custom_font_miss_cache"):
            self._custom_font_miss_cache = set()

    def _load_custom_font_file(self, font_path: str) -> str | None:
        self._ensure_custom_font_caches()
        if not font_path or not os.path.isfile(font_path):
            return None

        font_path = os.path.normpath(font_path)
        if font_path in self._custom_font_path_to_family:
            return self._custom_font_path_to_family[font_path]

        font_id = QFontDatabase.addApplicationFont(font_path)
        if font_id == -1:
            return None

        families = QFontDatabase.applicationFontFamilies(font_id)
        if not families:
            return None

        primary = families[0]
        self._custom_font_path_to_family[font_path] = primary
        for family in families:
            self._custom_font_family_cache[family.casefold()] = family
        return primary

    def ensure_custom_font_loaded(self, font_input: str) -> str:
        if not isinstance(font_input, str):
            return font_input

        requested = font_input.strip()
        if not requested:
            return requested

        self._ensure_custom_font_caches()
        lower = requested.casefold()
        if lower in self._custom_font_family_cache:
            return self._custom_font_family_cache[lower]
        if lower in self._custom_font_miss_cache:
            return requested

        ext = os.path.splitext(requested)[1].lower()
        if ext in [".ttf", ".ttc", ".otf", ".woff", ".woff2"]:
            loaded = self._load_custom_font_file(requested)
            return loaded or requested

        for family in QFontDatabase().families():
            if family.casefold() == lower:
                self._custom_font_family_cache[lower] = family
                return family

        if os.path.isdir(user_font_path):
            for name in os.listdir(user_font_path):
                if os.path.splitext(name)[1].lower() not in [".ttf", ".ttc", ".otf", ".woff", ".woff2"]:
                    continue
                path = os.path.join(user_font_path, name)
                loaded = self._load_custom_font_file(path)
                if loaded and loaded.casefold() == lower:
                    return loaded

        self._custom_font_miss_cache.add(lower)
        return requested

    def get_font_family(self, font_input: str) -> str:
        return self.ensure_custom_font_loaded(font_input)

    def add_custom_font(self, font_input: str):
        if os.path.splitext(font_input)[1].lower() in [".ttf", ".ttc", ".otf", ".woff", ".woff2"]:
            self._load_custom_font_file(font_input)

    def get_color(self):
        default_color = QtGui.QColor("#000000")
        color_dialog = QtWidgets.QColorDialog()
        color_dialog.setCurrentColor(default_color)
        if color_dialog.exec() == QtWidgets.QDialog.Accepted:
            return color_dialog.selectedColor()

    def set_font(self, font_family: str):
        resolved_family = self.ensure_custom_font_loaded(font_family)
        self.font_dropdown.setCurrentFont(QtGui.QFont(resolved_family))
        if self.font_dropdown.currentText() != resolved_family:
            self.font_dropdown.setCurrentText(resolved_family)
