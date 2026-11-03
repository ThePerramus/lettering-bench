import numpy as np
import time
import logging
import inspect
import imkit as imk

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QBrush

from modules.utils.device import resolve_device
from modules.utils.image_utils import build_block_mask_data, build_bubble_clip_mask, clip_mask_to_bubble, clip_mask_components_to_bubble
from modules.utils.pipeline_config import inpaint_map, get_config, get_inpainter_backend
from modules.utils.textblock import adjust_text_line_coordinates
from pipeline.inpainting_boxes import merge_overlapping_padded_boxes
from pipeline.webtoon_utils import filter_and_convert_visible_blocks, restore_original_block_coordinates

logger = logging.getLogger(__name__)

FAST_FILL_BUBBLE_INSET = 7
FAST_FILL_UNIFORM_COLOR_TOLERANCE = 12
FAST_FILL_MIN_UNIFORM_COVERAGE = 0.94
# Paper grain / JPEG speckle in a scanned white bubble sits 13-27 levels off
# its median - enough to fail the strict tolerance, far below real artwork.
# Only for paper-light backgrounds: dark artwork (brush strokes, shading) has
# low absolute contrast and would pass the loose tolerance too.
FAST_FILL_LOOSE_COLOR_TOLERANCE = 32
FAST_FILL_MIN_LOOSE_COVERAGE = 0.98
FAST_FILL_LOOSE_MIN_BRIGHTNESS = 200
# The uniformity sample is limited to this band (px) around the text: the
# bubble clip can leak through a gap in the outline into neighbouring artwork,
# and only the background right around the glyphs matters for a flat fill.
FAST_FILL_NEAR_TEXT_RADIUS = 12

# Hand-painted strokes left over after the bubble fast-fill (usually text the
# detector missed) are flat-filled when the ring of pixels around them is
# flat, and handed to the NN otherwise (text painted over artwork/texture).
HUMAN_FILL_RING_GAP = 3
HUMAN_FILL_RING_WIDTH = 12
# A bubble/caption outline next to the stroke is tolerated: a few connected
# dark pieces. Screentone dots or hatching break into many pieces and still go
# to the NN. The ratio is only a sanity cap - in a tight caption the outline
# can outweigh the thin strip of background between it and the text.
HUMAN_FILL_DARK_DELTA = 80
HUMAN_FILL_MAX_DARK_RATIO = 0.5
HUMAN_FILL_MAX_DARK_COMPONENTS = 3


def call_inpaint_image(inpainting_handler, image: np.ndarray, mask: np.ndarray, config, blk_list: list | None = None):
    inpaint_image = inpainting_handler.inpaint_image
    try:
        parameters = inspect.signature(inpaint_image).parameters
        accepts_blk_list = (
            "blk_list" in parameters
            or any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values())
        )
    except (TypeError, ValueError):
        accepts_blk_list = True

    if accepts_blk_list:
        return inpaint_image(image, mask, config, blk_list=blk_list)
    return inpaint_image(image, mask, config)


class InpaintingHandler:
    """Handles image inpainting functionality."""
    
    def __init__(self, main_page):
        self.main_page = main_page
        self.inpainter_cache = None
        self.cached_inpainter_key = None

    def _ensure_inpainter(self):
        settings_page = self.main_page.settings_page
        inpainter_key = settings_page.get_tool_selection('inpainter')
        if self.inpainter_cache is None or self.cached_inpainter_key != inpainter_key:
            backend = get_inpainter_backend(inpainter_key)
            device = resolve_device(settings_page.is_gpu_enabled(), backend)
            InpainterClass = inpaint_map[inpainter_key]
            logger.info("pre-inpaint: initializing inpainter '%s' on device %s", inpainter_key, device)
            t0 = time.time()
            self.inpainter_cache = InpainterClass(device, backend=backend)
            self.cached_inpainter_key = inpainter_key
            logger.info("pre-inpaint: inpainter initialized in %.2fs", time.time() - t0)
        return self.inpainter_cache

    def manual_inpaint(self):
        image_viewer = self.main_page.image_viewer
        settings_page = self.main_page.settings_page
        mask = image_viewer.get_mask_for_inpainting()
        human_mask = getattr(image_viewer.drawing_manager, "last_human_mask", None)
        
        # Handle webtoon mode vs regular mode differently
        mappings = None
        if self.main_page.webtoon_mode:
            # In webtoon mode, use visible area image for inpainting
            image, mappings = image_viewer.get_visible_area_image()
        else:
            # Regular mode - get the full image
            image = image_viewer.get_image_array()

        if image is None or mask is None:
            return None

        config = get_config(settings_page)
        inpaint_blocks = self._get_manual_fast_fill_blocks(mappings)
        inpaint_input_img = self.inpaint_image(
            image, mask, config, blk_list=inpaint_blocks or None, human_mask=human_mask
        )
        inpaint_input_img = imk.convert_scale_abs(inpaint_input_img) 

        return inpaint_input_img

    def _qimage_to_np(self, qimg: QImage):
        if qimg.width() <= 0 or qimg.height() <= 0:
            return np.zeros((max(1, qimg.height()), max(1, qimg.width())), dtype=np.uint8)
        ptr = qimg.constBits()
        arr = np.array(ptr).reshape(qimg.height(), qimg.bytesPerLine())
        return arr[:, :qimg.width()]

    def _generate_mask_from_saved_strokes(self, strokes: list[dict], image: np.ndarray, return_human: bool = False):
        if image is None or not strokes:
            return None
        height, width = image.shape[:2]
        if width <= 0 or height <= 0:
            return None

        human_qimg = QImage(width, height, QImage.Format_Grayscale8)
        gen_qimg = QImage(width, height, QImage.Format_Grayscale8)
        human_qimg.fill(0)
        gen_qimg.fill(0)

        human_painter = QPainter(human_qimg)
        gen_painter = QPainter(gen_qimg)

        human_painter.setPen(QPen(QColor(255, 255, 255), 1, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        # Auto-generated segmentation paths already represent the automatic
        # mask.  Do not widen their contour when rebuilding saved strokes.
        gen_painter.setPen(QPen(Qt.PenStyle.NoPen))
        human_painter.setBrush(QBrush(QColor(255, 255, 255)))
        gen_painter.setBrush(QBrush(QColor(255, 255, 255)))

        has_any = False
        for stroke in strokes:
            path = stroke.get('path')
            if path is None:
                continue
            brush_hex = QColor(stroke.get('brush', '#00000000')).name(QColor.HexArgb)
            if brush_hex == "#80ff0000":
                gen_painter.drawPath(path)
                has_any = True
                continue

            width_px = max(1, int(stroke.get('width', 25)))
            human_pen = QPen(QColor(255, 255, 255), width_px, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            human_painter.setPen(human_pen)
            human_painter.drawPath(path)
            has_any = True

        human_painter.end()
        gen_painter.end()

        if not has_any:
            return None

        human_mask = self._qimage_to_np(human_qimg)
        gen_mask = self._qimage_to_np(gen_qimg)
        # Match the live canvas: retain only a one-pixel antialiasing halo
        # around hand-painted strokes.
        human_mask = imk.dilate(human_mask, np.ones((3, 3), np.uint8), iterations=1)
        mask = np.where((human_mask > 0) | (gen_mask > 0), 255, 0).astype(np.uint8)
        if np.count_nonzero(mask) == 0:
            return (None, None) if return_human else None
        if return_human:
            return mask, (human_mask if np.any(human_mask) else None)
        return mask

    def _get_manual_fast_fill_blocks(self, mappings: list[dict] | None = None) -> list:
        blocks = getattr(self.main_page, "blk_list", None) or []
        if not blocks:
            return []
        if not getattr(self.main_page, "webtoon_mode", False):
            return blocks
        if not mappings:
            return []

        visible_blocks = []
        try:
            visible_blocks = filter_and_convert_visible_blocks(
                self.main_page,
                self.main_page.pipeline,
                mappings,
                single_block=False,
            )
            return [
                block.deep_copy() if hasattr(block, "deep_copy") else block
                for block in visible_blocks
            ]
        finally:
            restore_original_block_coordinates(visible_blocks)

    def _get_regular_patches(self, mask: np.ndarray, inpainted_image: np.ndarray):
        contours, _ = imk.find_contours(mask)
        if not contours:
            return []
        boxes = [imk.bounding_rect(c) for c in contours]
        merged_boxes = merge_overlapping_padded_boxes(boxes, inpainted_image.shape, pad=8)
        if len(merged_boxes) < len(boxes):
            logger.info("Inpaint hybrid: merged %d mask contours into %d output patches", len(boxes), len(merged_boxes))
        patches = []
        for x1, y1, x2, y2 in merged_boxes:
            w = x2 - x1
            h = y2 - y1
            patch = inpainted_image[y1:y2, x1:x2]
            patches.append({'bbox': [x1, y1, w, h], 'image': patch.copy()})
        return patches

    def _inpaint_full_image(self, image: np.ndarray, mask: np.ndarray, config):
        if image is None:
            return None
        if mask is None or not np.any(mask):
            return image.copy()
        return self.inpainter_cache(image, mask, config)

    def _inpaint_by_patches(self, image: np.ndarray, mask: np.ndarray, config):
        inpainted_image = image.copy()
        contours, _ = imk.find_contours(mask)
        
        if not contours:
            return inpainted_image

        # 1. Extract and merge bounding boxes
        boxes = [imk.bounding_rect(c) for c in contours]
        merged_boxes = merge_overlapping_padded_boxes(boxes, image.shape)

        # 2. Patch inference
        for x1, y1, x2, y2 in merged_boxes:
            img_patch = image[y1:y2, x1:x2]
            mask_patch = mask[y1:y2, x1:x2]
            
            patch_inpainted = self.inpainter_cache(img_patch, mask_patch, config)
            inpainted_image[y1:y2, x1:x2] = patch_inpainted
            
        return inpainted_image

    def _get_fast_fill_regions(
        self,
        block,
        bounds: tuple[int, int, int, int],
        crop_mask: np.ndarray,
        residual_crop: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray] | tuple[None, None]:
        if getattr(block, "text_class", None) != "text_bubble":
            return None, None
        if crop_mask is None or residual_crop is None:
            return None, None

        masked_region = (crop_mask > 0) & (residual_crop > 0)
        if not np.any(masked_region):
            return None, None

        h, w = crop_mask.shape[:2]
        x1, y1, _, _ = bounds
        background_region = crop_mask == 0

        text_bounds = getattr(block, "xyxy", None)
        if text_bounds is not None:
            tx1, ty1, tx2, ty2 = [int(round(float(v))) for v in text_bounds]
            lx1 = max(0, min(w, tx1 - x1))
            ly1 = max(0, min(h, ty1 - y1))
            lx2 = max(lx1, min(w, tx2 - x1))
            ly2 = max(ly1, min(h, ty2 - y1))
            block_region = np.zeros((h, w), dtype=bool)
            if lx2 > lx1 and ly2 > ly1:
                block_region[ly1:ly2, lx1:lx2] = True
                # When the user stroke falls on the text itself, prefer sampling
                # only from the text block background. When the stroke is elsewhere
                # in the bubble, keep the full bubble background so those strokes
                # can still be fast-filled instead of falling through to NN inpaint.
                masked_in_text = masked_region & block_region
                masked_pixels = int(np.count_nonzero(masked_region))
                masked_in_text_pixels = int(np.count_nonzero(masked_in_text))
                overlap_ratio = (
                    masked_in_text_pixels / float(masked_pixels)
                    if masked_pixels > 0 else 0.0
                )
                if masked_in_text_pixels >= 8 and overlap_ratio >= 0.6:
                    block_background = background_region & block_region
                    if np.count_nonzero(block_background) >= 32:
                        background_region = block_background

        # Drop a thin halo around the mask so antialiased glyph edges do not vote as background.
        halo_kernel = np.ones((3, 3), np.uint8)
        halo = imk.dilate((crop_mask > 0).astype(np.uint8), halo_kernel, iterations=1) > 0
        safe_background = background_region & ~halo
        if np.count_nonzero(safe_background) >= 32:
            background_region = safe_background

        if np.count_nonzero(background_region) < 32:
            return None, None
        return masked_region, background_region

    def _get_fast_fill_bounds(self, block, image: np.ndarray) -> tuple[int, int, int, int] | None:
        base_bounds = getattr(block, "xyxy", None)
        if base_bounds is None or len(base_bounds) < 4:
            return None

        if getattr(block, "text_class", None) == "text_bubble":
            bubble_bounds = getattr(block, "bubble_xyxy", None)
            if bubble_bounds is not None and len(bubble_bounds) >= 4:
                bounds = adjust_text_line_coordinates(bubble_bounds, 10, 10, image)
                return tuple(int(value) for value in bounds)

        bounds = adjust_text_line_coordinates(base_bounds, 10, 10, image)
        return tuple(int(value) for value in bounds)

    @staticmethod
    def _same_bounds(
        lhs: tuple[int, int, int, int] | None,
        rhs: tuple[int, int, int, int] | None,
    ) -> bool:
        if lhs is None or rhs is None:
            return lhs is rhs
        return tuple(int(v) for v in lhs) == tuple(int(v) for v in rhs)

    def _format_block_debug_label(self, block) -> str:
        text_bounds = getattr(block, "xyxy", None)
        bubble_bounds = getattr(block, "bubble_xyxy", None)
        return (
            f"class={getattr(block, 'text_class', None)} "
            f"text={tuple(int(round(float(v))) for v in text_bounds) if text_bounds is not None else None} "
            f"bubble={tuple(int(round(float(v))) for v in bubble_bounds) if bubble_bounds is not None else None}"
        )

    def _summarize_mask_overlap_with_blocks(
        self,
        mask: np.ndarray,
        blk_list: list | None,
        image: np.ndarray,
    ) -> list[str]:
        if mask is None or not np.any(mask) or not blk_list:
            return []

        summaries: list[str] = []
        for idx, block in enumerate(blk_list):
            bounds = self._get_fast_fill_bounds(block, image)
            if bounds is None:
                continue
            x1, y1, x2, y2 = bounds
            overlap = int(np.count_nonzero(mask[y1:y2, x1:x2]))
            if overlap <= 0:
                continue
            summaries.append(
                f"block[{idx}] overlap={overlap} bounds={(x1, y1, x2, y2)} {self._format_block_debug_label(block)}"
            )
        return summaries

    def _estimate_fast_fill_color(
        self,
        crop: np.ndarray,
        masked_region: np.ndarray,
        background_region: np.ndarray,
    ) -> tuple[np.ndarray | None, str]:
        if crop.size == 0 or not np.any(masked_region) or not np.any(background_region):
            return None, "empty-crop-or-regions"

        background_pixels = crop[background_region]
        if background_pixels.shape[0] < 64:
            return None, f"background-too-small:{background_pixels.shape[0]}"

        # No masked-vs-background ratio gate here: Segment's mask covers
        # nearly a whole bubble's interior, not just its glyphs, so 95-98%
        # masked is normal for every bubble and the gate rejected most of
        # them. The real guard against flat-filling a textured/gradient
        # bubble is _is_fast_fill_bubble_background_uniform (already passed
        # before this runs) plus the cluster coverage/spread checks below.

        pixels = background_pixels.reshape(-1, 3)
        pixels_u16 = pixels.astype(np.uint16)
        quantized = pixels_u16 // 16
        keys = (quantized[:, 0] << 8) | (quantized[:, 1] << 4) | quantized[:, 2]
        unique_keys, inverse, counts = np.unique(keys, return_inverse=True, return_counts=True)
        if unique_keys.size == 0:
            return None, "no-quantized-clusters"

        min_cluster = max(48, int(pixels.shape[0] * 0.015))
        order = np.argsort(counts)[::-1][:16]
        candidates = []
        for key_index in order:
            count = int(counts[key_index])
            if count < min_cluster:
                continue
            cluster = pixels[inverse == key_index].astype(np.float32)
            if cluster.size == 0:
                continue
            median = np.median(cluster, axis=0)
            p10 = np.percentile(cluster, 10, axis=0)
            p90 = np.percentile(cluster, 90, axis=0)
            spread = p90 - p10
            candidates.append({
                "count": count,
                "coverage": count / float(pixels.shape[0]),
                "median": median,
                "brightness": float(np.mean(median)),
                "spread": float(np.max(spread)),
            })

        if not candidates:
            return None, f"no-candidates:min_cluster={min_cluster}"

        candidates.sort(key=lambda item: item["count"], reverse=True)
        largest = candidates[0]
        bright_candidates = [
            item for item in candidates
            if item["brightness"] >= 150.0 and item["count"] >= max(min_cluster, int(largest["count"] * 0.12))
        ]
        selected = max(bright_candidates, key=lambda item: item["count"]) if bright_candidates else largest

        # Check if the selected color dominates the background region, ignoring dark outlines/borders for light bubbles
        selected_brightness = selected["brightness"]
        if selected_brightness >= 80.0:
            pixel_brightness = 0.299 * pixels[:, 2] + 0.587 * pixels[:, 1] + 0.114 * pixels[:, 0]
            valid_pixels = pixels[pixel_brightness >= 50.0]
        else:
            valid_pixels = pixels

        if valid_pixels.size == 0:
            valid_pixels = pixels

        diff = np.abs(valid_pixels - selected["median"])
        is_close = np.all(diff <= 15, axis=1)
        neighborhood_coverage = np.count_nonzero(is_close) / float(valid_pixels.shape[0])

        if neighborhood_coverage < 0.55:
            return None, f"selected-too-low-coverage:coverage={neighborhood_coverage:.3f}"
        if selected["count"] < 96 and selected["coverage"] < 0.03:
            return None, f"selected-too-small:count={selected['count']},coverage={selected['coverage']:.3f}"
        if selected["spread"] > 48.0:
            return None, f"selected-too-wide:spread={selected['spread']:.3f}"

        return selected["median"], (
            "ok:"
            f"count={selected['count']},coverage={selected['coverage']:.3f},"
            f"brightness={selected['brightness']:.1f},spread={selected['spread']:.1f}"
        )

    def _is_fast_fill_bubble_background_uniform(
        self,
        image: np.ndarray,
        block,
        bounds: tuple[int, int, int, int],
        crop_mask: np.ndarray,
    ) -> tuple[bool, str]:
        """Reject flat fills when the bubble preserves visible underlying artwork."""
        x1, y1, x2, y2 = bounds
        crop = image[y1:y2, x1:x2]
        if crop.size == 0 or crop.ndim != 3 or crop.shape[2] < 3:
            return False, "bubble-background-invalid"

        bubble_mask = build_bubble_clip_mask(
            crop.shape[:2],
            bounds,
            block.bubble_xyxy,
            inset=FAST_FILL_BUBBLE_INSET,
            image=image,
            seed_bbox=block.xyxy,
        )
        if bubble_mask is None:
            return False, "bubble-background-unavailable"

        # First use a halo to remove antialiased glyph edges from the normal
        # bubble-wide sample.
        binary_mask = (crop_mask > 0).astype(np.uint8)
        # A small bubble can lose almost its whole visible rim to the wide
        # halo - retry with a thinner one before giving up on a flat fill.
        for kernel, iterations in ((5, 2), (3, 1)):
            mask_halo = imk.dilate(
                binary_mask,
                np.ones((kernel, kernel), np.uint8),
                iterations=iterations,
            ) > 0
            sample_region = bubble_mask & ~mask_halo
            sample_count = int(np.count_nonzero(sample_region))
            if sample_count >= 64:
                break
        if sample_count < 64:
            return False, f"bubble-background-too-small:{sample_count}"

        near_text = imk.dilate(
            binary_mask,
            np.ones((2 * FAST_FILL_NEAR_TEXT_RADIUS + 1, 2 * FAST_FILL_NEAR_TEXT_RADIUS + 1), np.uint8),
            iterations=1,
        ) > 0
        near_region = sample_region & near_text
        near_count = int(np.count_nonzero(near_region))
        if near_count >= 64:
            sample_region, sample_count = near_region, near_count

        pixels = crop[sample_region, :3].astype(np.float32)
        median = np.median(pixels, axis=0)
        close_to_median = np.all(
            np.abs(pixels - median) <= FAST_FILL_UNIFORM_COLOR_TOLERANCE,
            axis=1,
        )
        uniform_coverage = float(np.count_nonzero(close_to_median)) / float(sample_count)
        if uniform_coverage < FAST_FILL_MIN_UNIFORM_COVERAGE:
            loosely_close = np.all(
                np.abs(pixels - median) <= FAST_FILL_LOOSE_COLOR_TOLERANCE,
                axis=1,
            )
            loose_coverage = float(np.count_nonzero(loosely_close)) / float(sample_count)
            if (
                loose_coverage < FAST_FILL_MIN_LOOSE_COVERAGE
                or float(np.min(median)) < FAST_FILL_LOOSE_MIN_BRIGHTNESS
            ):
                return False, (
                    "bubble-background-nonuniform:"
                    f"coverage={uniform_coverage:.3f},loose={loose_coverage:.3f}"
                )

        bubble_ys, bubble_xs = np.nonzero(bubble_mask)
        if bubble_ys.size and bubble_xs.size:
            bubble_y1, bubble_y2 = int(bubble_ys.min()), int(bubble_ys.max()) + 1
            bubble_x1, bubble_x2 = int(bubble_xs.min()), int(bubble_xs.max()) + 1
            spatial_min_count = max(64, int(round(sample_count * 0.015)))
            for grid_y in range(3):
                cell_y1 = bubble_y1 + round(grid_y * (bubble_y2 - bubble_y1) / 3)
                cell_y2 = bubble_y1 + round((grid_y + 1) * (bubble_y2 - bubble_y1) / 3)
                for grid_x in range(3):
                    cell_x1 = bubble_x1 + round(grid_x * (bubble_x2 - bubble_x1) / 3)
                    cell_x2 = bubble_x1 + round((grid_x + 1) * (bubble_x2 - bubble_x1) / 3)
                    cell_region = sample_region[cell_y1:cell_y2, cell_x1:cell_x2]
                    cell_count = int(np.count_nonzero(cell_region))
                    if cell_count < spatial_min_count:
                        continue
                    cell_pixels = crop[cell_y1:cell_y2, cell_x1:cell_x2][cell_region, :3].astype(np.float32)
                    cell_median = np.median(cell_pixels, axis=0)
                    median_shift = float(np.max(np.abs(cell_median - median)))
                    if median_shift > FAST_FILL_UNIFORM_COLOR_TOLERANCE:
                        return False, (
                            "bubble-background-nonuniform:"
                            f"spatial-color-shift={median_shift:.1f},"
                            f"cell={grid_y}:{grid_x},count={cell_count}"
                        )
        # Recovered segmentation can cover enough of a translucent bubble that
        # the outside-mask sample sees only one flat tonal region. For a broad
        # mask, compare its robust source colour with the sampled background.
        # Normal thin glyph masks stay below this coverage gate, so dark text in
        # an otherwise opaque bubble does not create a false translucency signal.
        masked_bubble = bubble_mask & (binary_mask > 0)
        bubble_count = int(np.count_nonzero(bubble_mask))
        masked_count = int(np.count_nonzero(masked_bubble))
        masked_coverage = masked_count / float(max(1, bubble_count))
        if masked_count >= 64 and masked_coverage >= 0.20:
            masked_median = np.median(crop[masked_bubble, :3].astype(np.float32), axis=0)
            median_shift = float(np.max(np.abs(masked_median - median)))
            if median_shift > FAST_FILL_UNIFORM_COLOR_TOLERANCE:
                return False, (
                    "bubble-background-nonuniform:"
                    f"expanded-mask-color-shift={median_shift:.1f},"
                    f"mask-coverage={masked_coverage:.3f}"
                )

        return True, (
            "bubble-background-uniform:"
            f"coverage={uniform_coverage:.3f},mask-coverage={masked_coverage:.3f}"
        )

    def _get_fast_fill_uniformity_mask(
        self,
        image: np.ndarray,
        block,
        bounds: tuple[int, int, int, int],
        fallback_mask: np.ndarray,
    ) -> np.ndarray:
        """Build a block-local mask so GUI stroke dilation cannot hide bubble tones."""
        try:
            block_mask, block_bounds = build_block_mask_data(
                image,
                block,
                require_text_or_translation=False,
                clip_to_bubble=True,
            )
        except Exception as exc:
            logger.debug("Inpaint fast-fill: failed to rebuild routing mask: %s", exc)
            return fallback_mask

        if block_mask is None or block_bounds is None or not np.any(block_mask):
            return fallback_mask

        x1, y1, x2, y2 = [int(v) for v in bounds]
        bx1, by1, bx2, by2 = [int(v) for v in block_bounds]
        ix1, iy1 = max(x1, bx1), max(y1, by1)
        ix2, iy2 = min(x2, bx2), min(y2, by2)
        if ix2 <= ix1 or iy2 <= iy1:
            return fallback_mask

        routing_mask = np.zeros_like(fallback_mask)
        routing_mask[iy1 - y1:iy2 - y1, ix1 - x1:ix2 - x1] = block_mask[
            iy1 - by1:iy2 - by1,
            ix1 - bx1:ix2 - bx1,
        ]
        return routing_mask if np.any(routing_mask) else fallback_mask
    def _apply_fast_bubble_cleanup(
        self,
        image: np.ndarray,
        mask: np.ndarray,
        blk_list: list | None,
        human_mask: np.ndarray | None = None,
    ) -> tuple[np.ndarray, np.ndarray, int]:
        if image is None or mask is None or not np.any(mask) or not blk_list:
            return image.copy(), mask.copy(), 0

        cleaned_image = image.copy()
        residual_mask = mask.copy()
        cleaned_blocks = 0
        cleaned_bubble_blocks = []

        for idx, block in enumerate(blk_list):
            if getattr(block, "xyxy", None) is None or len(block.xyxy) < 4:
                continue
            if getattr(block, "text_class", None) != "text_bubble" or getattr(block, "bubble_xyxy", None) is None:
                continue
            bounds = self._get_fast_fill_bounds(block, image)
            if bounds is None:
                continue
            x1, y1, x2, y2 = bounds
            residual_crop = residual_mask[y1:y2, x1:x2]
            if not np.any(residual_crop):
                continue
            crop_mask = np.where(residual_crop > 0, 255, 0).astype(np.uint8)
            background_is_uniform, uniformity_reason = self._is_fast_fill_bubble_background_uniform(
                image, block, bounds, crop_mask
            )
            if background_is_uniform:
                uniformity_mask = self._get_fast_fill_uniformity_mask(
                    image,
                    block,
                    bounds,
                    crop_mask,
                )
                background_is_uniform, uniformity_reason = self._is_fast_fill_bubble_background_uniform(
                    image, block, bounds, uniformity_mask
                )
            if not background_is_uniform:
                logger.info("Inpaint fast-fill: block[%d] routed to NN (%s)", idx, uniformity_reason)
                continue

            if getattr(block, "text_class", None) == "text_bubble" and getattr(block, "bubble_xyxy", None) is not None:
                crop_mask = clip_mask_components_to_bubble(
                    crop_mask,
                    bounds,
                    block.bubble_xyxy,
                    inset=FAST_FILL_BUBBLE_INSET,
                    image=image,
                    seed_bbox=block.xyxy,
                )
                
            initial_overlap = int(np.count_nonzero(crop_mask))
            if initial_overlap <= 0:
                continue
            success, reason = self._fast_fill_block(cleaned_image, residual_mask, block, bounds, crop_mask)
            if not success:
                fallback_mask, fallback_bounds = build_block_mask_data(
                    image,
                    block,
                    require_text_or_translation=False,
                    clip_to_bubble=True,
                )
                if fallback_mask is None or fallback_bounds is None:
                    logger.info(
                        "Inpaint fast-fill: block[%d] failed without fallback mask (%s) overlap=%d reason=%s",
                        idx,
                        self._format_block_debug_label(block),
                        initial_overlap,
                        reason,
                    )
                    continue
                success, fallback_reason = self._fast_fill_block(
                    cleaned_image,
                    residual_mask,
                    block,
                    fallback_bounds,
                    fallback_mask,
                )
                if not success:
                    logger.info(
                        "Inpaint fast-fill: block[%d] failed (%s) overlap=%d primary=%s fallback=%s fallback_bounds=%s",
                        idx,
                        self._format_block_debug_label(block),
                        initial_overlap,
                        reason,
                        fallback_reason,
                        fallback_bounds,
                    )
                    continue
                reason = f"fallback:{fallback_reason}"

                # If fallback only covered the text region, retry the wider bubble
                # bounds against the remaining residual. That catches user brush
                # strokes on bubble background after the text itself has been cleared.
                if not self._same_bounds(bounds, fallback_bounds):
                    retry_crop = residual_mask[y1:y2, x1:x2]
                    if np.any(retry_crop):
                        retry_mask = np.where(retry_crop > 0, 255, 0).astype(np.uint8)
                        retry_success, retry_reason = self._fast_fill_block(
                            cleaned_image,
                            residual_mask,
                            block,
                            bounds,
                            retry_mask,
                        )
                        if retry_success:
                            reason = f"{reason}+retry:{retry_reason}"
                        else:
                            reason = f"{reason}+retry_failed:{retry_reason}"

            cleaned_blocks += 1
            cleaned_bubble_blocks.append(block)
            remaining_overlap = int(np.count_nonzero(residual_mask[y1:y2, x1:x2]))
            logger.info(
                "Inpaint fast-fill: block[%d] cleaned overlap=%d remaining=%d bounds=%s %s reason=%s",
                idx,
                initial_overlap,
                remaining_overlap,
                bounds,
                self._format_block_debug_label(block),
                reason,
            )

        if cleaned_blocks:
            bubble_scope = np.zeros(mask.shape, dtype=bool)
            bubble_allowed = np.zeros(mask.shape, dtype=bool)
            for bubble_block in cleaned_bubble_blocks:
                if getattr(bubble_block, "text_class", None) != "text_bubble" or getattr(bubble_block, "bubble_xyxy", None) is None:
                    continue
                bubble_bounds = self._get_fast_fill_bounds(bubble_block, image)
                if bubble_bounds is None:
                    continue
                bx1, by1, bx2, by2 = bubble_bounds
                bubble_scope[by1:by2, bx1:bx2] = True
                bubble_clip = build_bubble_clip_mask((by2 - by1, bx2 - bx1), bubble_bounds, bubble_block.bubble_xyxy, inset=FAST_FILL_BUBBLE_INSET, image=image, seed_bbox=bubble_block.xyxy)
                if bubble_clip is not None:
                    bubble_allowed[by1:by2, bx1:bx2] |= bubble_clip
            spill = (residual_mask > 0) & bubble_scope & ~bubble_allowed
            # Hand-painted strokes are deliberate - e.g. letters touching a
            # caption's border, outside the inset interior. Hand those to the
            # NN inpainter instead of silently discarding them.
            if human_mask is not None and human_mask.shape == spill.shape:
                spill &= ~(human_mask > 0)
            if np.any(spill):
                # Letters close to the outline that the inset fill didn't reach:
                # flat-fill them when only clean background surrounds them.
                # Anything touching the outline is still dropped, so the NN
                # never gets to smear (or the fill to thin) the outline.
                self._apply_ring_fill(
                    cleaned_image, residual_mask, spill, "bubble-edge leftover",
                    allow_outline_contact=False,
                )
                spill &= residual_mask > 0
                residual_mask[spill] = 0
        return cleaned_image, residual_mask, cleaned_blocks

    def _apply_ring_fill(
        self,
        image: np.ndarray,
        residual_mask: np.ndarray,
        seeds: np.ndarray,
        kind: str,
        *,
        allow_outline_contact: bool = True,
    ) -> int:
        """Flat-fill residual areas (those containing seeds) whose surroundings are flat.

        Works in place on image/residual_mask; returns how many areas it filled.
        """
        if seeds.shape != residual_mask.shape:
            return 0
        residual = residual_mask > 0
        seeds = residual & (seeds > 0)
        if not np.any(seeds):
            return 0

        _num, labels, stats, _centroids = imk.connected_components_with_stats(
            residual.astype(np.uint8),
            connectivity=8,
        )
        seed_labels = np.unique(labels[seeds])
        seed_labels = seed_labels[seed_labels > 0]
        height, width = residual.shape
        pad = HUMAN_FILL_RING_GAP + HUMAN_FILL_RING_WIDTH + 1
        filled = 0
        for label in seed_labels:
            x = int(stats[label, imk.CC_STAT_LEFT])
            y = int(stats[label, imk.CC_STAT_TOP])
            w = int(stats[label, imk.CC_STAT_WIDTH])
            h = int(stats[label, imk.CC_STAT_HEIGHT])
            x1, y1 = max(0, x - pad), max(0, y - pad)
            x2, y2 = min(width, x + w + pad), min(height, y + h + pad)
            area = labels[y1:y2, x1:x2] == label
            crop = image[y1:y2, x1:x2]
            fill_color, reason = self._estimate_ring_fill_color(
                crop, area, residual[y1:y2, x1:x2], allow_outline_contact=allow_outline_contact
            )
            if fill_color is None:
                logger.info("Inpaint fast-fill: %s %s not flat-filled (%s)", kind, (x, y, w, h), reason)
                continue

            soft_mask = imk.gaussian_blur(area.astype(np.uint8) * 255, 1.0).astype(np.float32) / 255.0
            soft_mask = np.clip(soft_mask, 0.0, 1.0)[..., np.newaxis]
            fill_rgb = np.broadcast_to(fill_color, crop.shape).astype(np.float32)
            blended = crop.astype(np.float32) * (1.0 - soft_mask) + fill_rgb * soft_mask
            image[y1:y2, x1:x2] = np.clip(np.round(blended), 0, 255).astype(np.uint8)
            residual_mask[y1:y2, x1:x2][area] = 0
            filled += 1
            logger.info("Inpaint fast-fill: %s %s flat-filled (%s)", kind, (x, y, w, h), reason)
        return filled

    def _estimate_ring_fill_color(
        self,
        crop: np.ndarray,
        area: np.ndarray,
        residual_crop: np.ndarray,
        *,
        allow_outline_contact: bool = True,
    ) -> tuple[np.ndarray | None, str]:
        area_u8 = area.astype(np.uint8)

        def grow(mask_u8: np.ndarray, radius: int) -> np.ndarray:
            kernel = np.ones((2 * radius + 1, 2 * radius + 1), np.uint8)
            return imk.dilate(mask_u8, kernel, iterations=1) > 0

        outer = grow(area_u8, HUMAN_FILL_RING_GAP + HUMAN_FILL_RING_WIDTH)
        around = outer & ~area & ~residual_crop
        ring = around & ~grow(area_u8, HUMAN_FILL_RING_GAP)
        if np.count_nonzero(ring) < 64:
            return None, f"ring-too-small:{int(np.count_nonzero(ring))}"

        # "Dark" is relative to the background right next to the stroke.
        near = around & ~grow(area_u8, HUMAN_FILL_RING_GAP + 4)
        near = around & ~near
        near_pixels = crop[near, :3] if np.count_nonzero(near) >= 16 else crop[ring, :3]
        dark_threshold = float(np.median(near_pixels.astype(np.float32), axis=0).mean()) - HUMAN_FILL_DARK_DELTA
        dark = np.zeros(area.shape, dtype=bool)
        dark[around] = crop[around, :3].astype(np.float32).mean(axis=1) <= dark_threshold

        # Only sample the background reachable from the stroke without crossing
        # a dark line: a caption's border bounds it, so the artwork outside
        # the box doesn't count as texture around the text.
        num_open, open_labels = imk.connected_components((around & ~dark).astype(np.uint8), connectivity=4)
        area_edge = grow(area_u8, 1) & ~area
        touching = np.unique(open_labels[area_edge & (open_labels > 0)])
        reachable = np.isin(open_labels, touching[touching > 0])
        # Skip the antialiased fringe of outlines - grey, but not dark enough
        # to count as the line itself.
        sample = ring & reachable & ~grow(dark.astype(np.uint8), 2)
        sample_count = int(np.count_nonzero(sample))
        if sample_count < 64:
            return None, f"ring-too-small:{sample_count}"

        # Dark structure bordering that background: an outline (a few pieces)
        # is fine, hatching or screentone (many pieces / lots of ink) is not.
        boundary_dark = dark & grow(reachable.astype(np.uint8), 1) & ring
        dark_ratio = float(np.count_nonzero(boundary_dark)) / float(sample_count + np.count_nonzero(boundary_dark))
        if dark_ratio > HUMAN_FILL_MAX_DARK_RATIO:
            return None, f"dark-ratio={dark_ratio:.3f}"
        if np.any(boundary_dark):
            _num_dark, dark_labels = imk.connected_components(dark.astype(np.uint8), connectivity=8)
            outline_labels = np.unique(dark_labels[boundary_dark])
            outline_labels = outline_labels[outline_labels > 0]
            if outline_labels.size > HUMAN_FILL_MAX_DARK_COMPONENTS:
                return None, f"dark-components={outline_labels.size}"
            # Only the ink that borders the sampled background: dark artwork on
            # the far side of a caption border would otherwise join the line's
            # two ends into one apparent contact.
            contact = area_edge & np.isin(dark_labels, outline_labels) & grow(reachable.astype(np.uint8), 1)
            if np.any(contact):
                if not allow_outline_contact:
                    return None, "touches-outline"
                # A line that enters the area on one side and leaves on the
                # other is being painted over: a flat fill would cut it, the NN
                # can redraw it. A letter merely touching the border makes a
                # single contact and is fine.
                _num_contacts, contact_labels = imk.connected_components(contact.astype(np.uint8), connectivity=8)
                for outline_label in outline_labels:
                    pieces = np.unique(contact_labels[contact & (dark_labels == outline_label)])
                    if np.count_nonzero(pieces) >= 2:
                        return None, "outline-crosses-stroke"

        pixels = crop[sample, :3].astype(np.float32)
        median = np.median(pixels, axis=0)
        close = np.all(np.abs(pixels - median) <= FAST_FILL_UNIFORM_COLOR_TOLERANCE, axis=1)
        coverage = float(np.count_nonzero(close)) / float(sample_count)
        if coverage < FAST_FILL_MIN_UNIFORM_COVERAGE:
            loose = np.all(np.abs(pixels - median) <= FAST_FILL_LOOSE_COLOR_TOLERANCE, axis=1)
            loose_coverage = float(np.count_nonzero(loose)) / float(sample_count)
            if (
                loose_coverage < FAST_FILL_MIN_LOOSE_COVERAGE
                or float(np.min(median)) < FAST_FILL_LOOSE_MIN_BRIGHTNESS
            ):
                return None, f"ring-nonuniform:coverage={coverage:.3f},loose={loose_coverage:.3f}"

        # Gradients: every ninth of the sampled ring must agree on the colour.
        ys, xs = np.nonzero(sample)
        ry1, ry2, rx1, rx2 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
        for gy in range(3):
            for gx in range(3):
                cy1 = ry1 + round(gy * (ry2 - ry1) / 3)
                cy2 = ry1 + round((gy + 1) * (ry2 - ry1) / 3)
                cx1 = rx1 + round(gx * (rx2 - rx1) / 3)
                cx2 = rx1 + round((gx + 1) * (rx2 - rx1) / 3)
                cell = sample[cy1:cy2, cx1:cx2]
                if np.count_nonzero(cell) < 32:
                    continue
                cell_median = np.median(crop[cy1:cy2, cx1:cx2][cell, :3].astype(np.float32), axis=0)
                shift = float(np.max(np.abs(cell_median - median)))
                if shift > FAST_FILL_UNIFORM_COLOR_TOLERANCE:
                    return None, f"ring-color-shift={shift:.1f}"

        return median, f"ring-uniform:coverage={coverage:.3f},outline={dark_ratio:.3f}"

    def _fast_fill_block(
        self,
        cleaned_image: np.ndarray,
        residual_mask: np.ndarray,
        block,
        bounds: tuple[int, int, int, int],
        crop_mask: np.ndarray,
    ) -> tuple[bool, str]:
        x1, y1, x2, y2 = bounds
        crop = cleaned_image[y1:y2, x1:x2]
        residual_crop = residual_mask[y1:y2, x1:x2]
        masked_region, background_region = self._get_fast_fill_regions(block, bounds, crop_mask, residual_crop)
        if masked_region is None or background_region is None:
            return False, "invalid-regions"

        fill_color, color_reason = self._estimate_fast_fill_color(crop, masked_region, background_region)
        if fill_color is None:
            return False, color_reason

        fill_region = self._get_associated_residual_components(residual_crop, masked_region)
        applied_region = fill_region
        
        if getattr(block, "text_class", None) == "text_bubble" and getattr(block, "bubble_xyxy", None) is not None:
            bubble_mask = build_bubble_clip_mask(
                fill_region.shape[:2],
                bounds,
                block.bubble_xyxy,
                inset=FAST_FILL_BUBBLE_INSET,
                image=cleaned_image,
                seed_bbox=block.xyxy,
            )
            if bubble_mask is not None:
                num_labels, labeled_fill = imk.connected_components(fill_region, connectivity=4)
                overlapping_labels = np.unique(labeled_fill[bubble_mask])
                keep_labels = overlapping_labels[overlapping_labels > 0]
                if keep_labels.size > 0:
                    # Saved segmentation strokes can bridge into a neighbouring, overlapping
                    # bubble after dilation. Only paint the pixels inside this bubble; leave
                    # the rest in the residual for the neighbouring bubble's cleanup pass.
                    applied_region = np.isin(labeled_fill, keep_labels) & bubble_mask
                else:
                    applied_region = np.zeros_like(fill_region)
        else:
            bubble_mask = None

        soft_mask = imk.gaussian_blur(applied_region.astype(np.uint8) * 255, 1.0).astype(np.float32) / 255.0
        soft_mask = np.clip(soft_mask, 0.0, 1.0)[..., np.newaxis]
        
        if bubble_mask is not None:
            soft_mask = soft_mask * bubble_mask[..., np.newaxis]
        crop_f = crop.astype(np.float32)
        fill_rgb = np.broadcast_to(fill_color, crop.shape).astype(np.float32)
        blended = crop_f * (1.0 - soft_mask) + fill_rgb * soft_mask
        cleaned_image[y1:y2, x1:x2] = np.clip(np.round(blended), 0, 255).astype(np.uint8)
        residual_crop[applied_region] = 0
        return True, color_reason

    def _get_associated_residual_components(self, residual_crop: np.ndarray, masked_region: np.ndarray) -> np.ndarray:
        residual_binary = (residual_crop > 0).astype(np.uint8)
        if not np.any(residual_binary):
            return masked_region

        num_labels, labels, _stats, _centroids = imk.connected_components_with_stats(
            residual_binary,
            connectivity=8,
        )
        if num_labels <= 1:
            return residual_binary > 0

        near_mask = imk.dilate(
            masked_region.astype(np.uint8),
            np.ones((17, 17), np.uint8),
            iterations=1,
        ) > 0
        overlap = near_mask & (labels > 0)
        overlap_labels = np.unique(labels[overlap])
        if overlap_labels.size == 0:
            return masked_region

        return np.isin(labels, overlap_labels)

    def _drop_tiny_residual_components(self, mask: np.ndarray, max_component_area: int = 256) -> tuple[np.ndarray, int]:
        if mask is None or not np.any(mask):
            return mask, 0

        binary = (mask > 0).astype(np.uint8)
        num_labels, labels, stats, _centroids = imk.connected_components_with_stats(
            binary,
            connectivity=8,
        )
        if num_labels <= 1:
            return mask, 0

        pruned = mask.copy()
        dropped_pixels = 0
        for label in range(1, num_labels):
            area = int(stats[label, imk.CC_STAT_AREA])
            if area <= max_component_area:
                component = labels == label
                dropped_pixels += int(np.count_nonzero(pruned[component]))
                pruned[component] = 0

        return pruned, dropped_pixels

    def _residual_component_summary(self, mask: np.ndarray) -> tuple[int, int, tuple[int, int, int, int] | None]:
        if mask is None or not np.any(mask):
            return 0, 0, None

        binary = (mask > 0).astype(np.uint8)
        num_labels, _labels, stats, _centroids = imk.connected_components_with_stats(
            binary,
            connectivity=8,
        )
        if num_labels <= 1:
            return 0, 0, None

        areas = stats[1:, imk.CC_STAT_AREA]
        largest_offset = int(np.argmax(areas))
        label = largest_offset + 1
        x = int(stats[label, imk.CC_STAT_LEFT])
        y = int(stats[label, imk.CC_STAT_TOP])
        w = int(stats[label, imk.CC_STAT_WIDTH])
        h = int(stats[label, imk.CC_STAT_HEIGHT])
        return num_labels - 1, int(stats[label, imk.CC_STAT_AREA]), (x, y, w, h)

    def inpaint_image(
        self,
        image: np.ndarray,
        mask: np.ndarray,
        config,
        blk_list: list | None = None,
        human_mask: np.ndarray | None = None,
    ) -> np.ndarray:
        """
        Intelligently chooses between full-image and patch-based inpainting
        based on image size, number of text blocks, and total mask area.
        """
        if image is None:
            return None
        if mask is None or not np.any(mask):
            return image.copy()

        working_image, working_mask, cleaned_blocks = self._apply_fast_bubble_cleanup(
            image, mask, blk_list, human_mask=human_mask
        )
        if cleaned_blocks:
            logger.info("Inpaint hybrid: fast-cleaned %d bubble blocks", cleaned_blocks)
        if human_mask is not None and working_mask is not None and np.any(working_mask):
            human_filled = self._apply_ring_fill(working_image, working_mask, human_mask, "hand-painted area")
            if human_filled:
                logger.info("Inpaint hybrid: flat-filled %d hand-painted areas", human_filled)
        if working_mask is None or not np.any(working_mask):
            return working_image

        contours, _ = imk.find_contours(working_mask)
        if not contours:
            return working_image

        if cleaned_blocks:
            component_count, largest_area, largest_bbox = self._residual_component_summary(working_mask)
            overlap_summary = self._summarize_mask_overlap_with_blocks(working_mask, blk_list, image)
            logger.info(
                "Inpaint hybrid: residual mask remains after fast cleanup; running NN inpainting "
                "(components=%d, largest_area=%d, largest_bbox=%s, overlaps=%s)",
                component_count,
                largest_area,
                largest_bbox,
                overlap_summary[:5],
            )

        if getattr(self, "main_page", None) is not None or getattr(self, "inpainter_cache", None) is None:
            self._ensure_inpainter()

        if getattr(self.inpainter_cache, "force_full_image_inpainting", False):
            logger.info("Inpaint hybrid: FULL-IMAGE forced by inpainter")
            return self._inpaint_full_image(working_image, working_mask, config)

        boxes = [imk.bounding_rect(c) for c in contours]
        merged_boxes = merge_overlapping_padded_boxes(boxes, working_image.shape)
        num_patches = len(merged_boxes)

        if num_patches == 0:
            return working_image

        # Calculate mathematically modeled cost comparison:
        # 1. Calculate the scaled dimension of the full image based on actual configuration
        h_img, w_img = working_image.shape[:2]
        hd_strategy = getattr(config, "hd_strategy", "Resize")
        if hd_strategy == "Resize":
            max_size = getattr(config, "hd_strategy_resize_limit", 1024)
        elif hd_strategy == "Original":
            max_size = max(h_img, w_img)
        else:
            max_size = 1024  # Fallback 

        scale = min(1.0, max_size / max(h_img, w_img))
        full_w = max(1, int(round(w_img * scale)))
        full_h = max(1, int(round(h_img * scale)))
        full_pixels = full_w * full_h

        # 2. Calculate the cumulative effective pixels processed by all patches,
        # accounting for minimum dimensions (128px) and padding modules (8px).
        total_patch_pixels = 0
        for x1, y1, x2, y2 in merged_boxes:
            w = max(0, x2 - x1)
            h = max(0, y2 - y1)
            eff_w = max(128, int(np.ceil(w / 8.0) * 8))
            eff_h = max(128, int(np.ceil(h / 8.0) * 8))
            total_patch_pixels += eff_w * eff_h

        # 3. Add a cost penalty for session run overhead (approx. 50,000 pixels worth of CPU/GPU time per extra patch)
        session_overhead_penalty = (num_patches - 1) * 50000
        estimated_patch_cost = total_patch_pixels + session_overhead_penalty

        # Choose full-image if the estimated patch cost exceeds the full-image cost
        use_patches = estimated_patch_cost < full_pixels
        area_ratio = (sum((x2 - x1) * (y2 - y1) for x1, y1, x2, y2 in merged_boxes)) / (w_img * h_img)

        if use_patches:
            logger.info(
                "Inpaint hybrid: PATCH-BASED (patches=%d, ratio=%.2f%%, size=%dx%d)",
                num_patches, area_ratio * 100, w_img, h_img
            )
            return self._inpaint_by_patches(working_image, working_mask, config)
        else:
            logger.info(
                "Inpaint hybrid: FULL-IMAGE (patches=%d, ratio=%.2f%%, size=%dx%d)",
                num_patches, area_ratio * 100, w_img, h_img
            )
            return self._inpaint_full_image(working_image, working_mask, config)

    def inpaint_page_from_saved_strokes(self, image: np.ndarray, strokes: list[dict], blk_list: list | None = None):
        mask, human_mask = self._generate_mask_from_saved_strokes(strokes, image, return_human=True)
        if mask is None:
            return []
        config = get_config(self.main_page.settings_page)
        inpainted = self.inpaint_image(image, mask, config, blk_list=blk_list or None, human_mask=human_mask)
        inpainted = imk.convert_scale_abs(inpainted)
        return self._get_regular_patches(mask, inpainted)

    def inpaint_complete(self, patch_list):
        # Handle webtoon mode vs regular mode
        if self.main_page.webtoon_mode:
            # In webtoon mode, group patches by page and apply them
            patches_by_page = {}
            for patch in patch_list:
                if 'page_index' in patch and 'file_path' in patch:
                    file_path = patch['file_path']
                    
                    if file_path not in patches_by_page:
                        patches_by_page[file_path] = []
                    
                    # Remove page-specific keys for the patch command but keep scene_pos for webtoon mode
                    clean_patch = {
                        'bbox': patch['bbox'],
                        'image': patch['image']
                    }
                    # Add scene position info for webtoon mode positioning
                    if 'scene_pos' in patch:
                        clean_patch['scene_pos'] = patch['scene_pos']
                        clean_patch['page_index'] = patch['page_index']
                    patches_by_page[file_path].append(clean_patch)
            
            # Apply patches to each page
            for file_path, patches in patches_by_page.items():
                self.main_page.image_ctrl.on_inpaint_patches_processed(patches, file_path)
        else:
            # Regular mode - original behavior
            self.main_page.apply_inpaint_patches(patch_list)
        
        if self.main_page.webtoon_mode:
            # Lazy loading keeps paths from neighbouring pages in the scene.
            # Clearing them all also persists an empty state when those pages
            # unload, losing segmentation that was never inpainted.
            painted_rects = []
            for patch in patch_list:
                scene_pos = patch.get('scene_pos')
                bbox = patch.get('bbox')
                if scene_pos is not None and bbox is not None:
                    painted_rects.append(QRectF(scene_pos[0], scene_pos[1], bbox[2], bbox[3]))
            self.main_page.image_viewer.drawing_manager.clear_brush_strokes_in_scene_rects(painted_rects)
        else:
            self.main_page.image_viewer.clear_brush_strokes()
        self.main_page.undo_group.activeStack().endMacro()  
        # get_best_render_area(self.main_page.blk_list, original_image, inpainted)    

    def get_inpainted_patches(self, mask: np.ndarray, inpainted_image: np.ndarray):
        # slice mask into bounding boxes
        contours, _ = imk.find_contours(mask)
        patches = []
        # Handle webtoon mode vs regular mode
        if self.main_page.webtoon_mode:
            # In webtoon mode, we need to map patches back to their respective pages
            visible_image, mappings = self.main_page.image_viewer.get_visible_area_image()
            if visible_image is None or not mappings:
                return patches
                
            for i, c in enumerate(contours):
                x, y, w, h = imk.bounding_rect(c)
                patch_bottom = y + h

                # Find all pages that this patch overlaps with
                overlapping_mappings = []
                for mapping in mappings:
                    if (y < mapping['combined_y_end'] and patch_bottom > mapping['combined_y_start']):
                        overlapping_mappings.append(mapping)
                
                if not overlapping_mappings:
                    continue
                    
                # If patch spans multiple pages, clip and redistribute
                for mapping in overlapping_mappings:
                    # Calculate the intersection with this page
                    clip_top = max(y, mapping['combined_y_start'])
                    clip_bottom = min(patch_bottom, mapping['combined_y_end'])
                    
                    if clip_bottom <= clip_top:
                        continue
                        
                    # Extract the portion of the patch for this page
                    clipped_patch = inpainted_image[clip_top:clip_bottom, x:x+w]
                    
                    # Convert coordinates back to page-local coordinates
                    page_local_y = clip_top - mapping['combined_y_start'] + mapping['page_crop_top']
                    clipped_height = clip_bottom - clip_top
                    
                    # Calculate the correct scene position by converting from visible area coordinates to scene coordinates
                    scene_y = mapping['scene_y_start'] + (clip_top - mapping['combined_y_start'])
                    
                    patches.append({
                        'bbox': [x, int(page_local_y), w, clipped_height],
                        'image': clipped_patch.copy(),
                        'page_index': mapping['page_index'],
                        'file_path': self.main_page.image_files[mapping['page_index']],
                        'scene_pos': [x, scene_y]  # Store correct scene position for webtoon mode
                    })
        else:
            # Regular mode - original behavior
            boxes = [imk.bounding_rect(c) for c in contours]
            merged_boxes = merge_overlapping_padded_boxes(boxes, inpainted_image.shape, pad=8)
            if len(merged_boxes) < len(boxes):
                logger.info("Inpaint hybrid: merged %d mask contours into %d output patches", len(boxes), len(merged_boxes))
            for x1, y1, x2, y2 in merged_boxes:
                w = x2 - x1
                h = y2 - y1
                patch = inpainted_image[y1:y2, x1:x2]
                patches.append({
                    'bbox': [x1, y1, w, h],
                    'image': patch.copy(),
                })
                
        return patches
    
    def inpaint(self):
        mask = self.main_page.image_viewer.get_mask_for_inpainting()
        painted = self.manual_inpaint()              
        patches = self.get_inpainted_patches(mask, painted)
        return patches         
