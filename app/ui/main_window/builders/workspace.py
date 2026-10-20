from PySide6 import QtCore, QtWidgets
from PySide6.QtCore import QSettings
from PySide6.QtGui import QIntValidator

from app.ui.dayu_widgets import dayu_theme
from app.ui.dayu_widgets.browser import MDragFileButton
from app.ui.dayu_widgets.button_group import MPushButtonGroup, MToolButtonGroup
from app.ui.dayu_widgets.check_box import MCheckBox
from app.ui.dayu_widgets.combo_box import MComboBox, MFontComboBox
from app.ui.dayu_widgets.divider import MDivider
from app.ui.dayu_widgets.label import MLabel
from app.ui.dayu_widgets.line_edit import MLineEdit
from app.ui.dayu_widgets.loading import MLoading
from app.ui.dayu_widgets.progress_bar import MProgressBar
from app.ui.dayu_widgets.push_button import MPushButton
from app.ui.dayu_widgets.qt import MIcon
from app.ui.dayu_widgets.radio_button import MRadioButton
from app.ui.dayu_widgets.slider import MSlider
from app.ui.dayu_widgets.spin_box import MSpinBox
from app.ui.dayu_widgets.switch import MSwitch
from app.ui.dayu_widgets.text_edit import MTextEdit
from app.ui.dayu_widgets.tool_button import MToolButton
from app.ui.search_replace_panel import SearchReplacePanel
from app.ui.main_window.constants import supported_source_languages, supported_target_languages


class WorkspaceMixin:
    def _create_main_content(self):
        content_widget = QtWidgets.QWidget()

        self.undo_tool_group = MToolButtonGroup(orientation=QtCore.Qt.Horizontal, exclusive=True)
        undo_tools = [
            {"svg": "undo.svg", "checkable": False, "tooltip": self.tr("Undo")},
            {"svg": "redo.svg", "checkable": False, "tooltip": self.tr("Redo")},
        ]
        self.undo_tool_group.set_button_list(undo_tools)

        # Plain MToolButtons (not MPushButtonGroup's own MPushButton) so
        # they match the flat, borderless look of every other icon in
        # self.tool_rail_widget instead of standing out with their own
        # boxed/bordered chrome. self.hbutton_group is kept only as the
        # QButtonGroup holder every other reference wants via
        # .get_button_group().buttons()[i] - its own (unused) internal
        # layout never gets these buttons added to it; they're registered
        # directly on its QButtonGroup below and live in the rail instead.
        pipeline_configs = [
            ("pipeline_detect.svg", self.tr("Detect")),
            ("pipeline_recognize.svg", self.tr("Recognize")),
            ("pipeline_translate.svg", self.tr("Translate")),
            ("pipeline_segment.svg", self.tr("Segment")),
            ("pipeline_clean.svg", self.tr("Clean")),
            ("pipeline_render.svg", self.tr("Render")),
        ]
        self.hbutton_group = MPushButtonGroup()
        self.hbutton_group.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        pipeline_buttons = []
        for index, (svg, tooltip) in enumerate(pipeline_configs):
            button = self.create_tool_button(svg=svg, checkable=False)
            button.setToolTip(tooltip)
            button.setEnabled(False)
            button.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            self.hbutton_group.get_button_group().addButton(button, index)
            pipeline_buttons.append(button)

        self.progress_bar = MProgressBar().auto_color()
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)

        self.loading = MLoading().small()
        self.loading.setVisible(False)

        # Kept as the real state (and as what the rest of the app already
        # reads/sets: project load/save, _run_batch_for_paths, etc.) but no
        # longer shown directly - self.mode_switch below is the visible
        # control, kept in sync with these in _setup_connections.
        self.manual_radio = MRadioButton(self.tr("Manual"))
        self.manual_radio.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)

        self.automatic_radio = MRadioButton(self.tr("Automatic"))
        self.automatic_radio.setChecked(True)
        self.automatic_radio.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)

        self.mode_switch = MSwitch()
        self.mode_switch.small()
        self.mode_switch.setChecked(True)
        self.mode_switch.setToolTip(
            self.tr("Automatic Mode (Translate All in one go) - off for Manual Mode (step by step)")
        )
        self.mode_switch.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)

        self.webtoon_toggle = MToolButton()
        self.webtoon_toggle.set_dayu_svg("webtoon-toggle.svg")
        self.webtoon_toggle.small()
        self.webtoon_toggle.setCheckable(True)
        self.webtoon_toggle.setToolTip(
            self.tr("Toggle Webtoon Mode. " "For comics that are read in long vertical strips")
        )
        self.webtoon_toggle.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)

        # Kept as the real state/wiring (start_batch_process/cancel_current_task
        # stay connected to these) but no longer shown - self.play_stop_button
        # below is the single visible control, refreshed by _refresh_play_stop_button.
        self.translate_button = MPushButton(self.tr("Translate All"))
        self.translate_button.setEnabled(True)
        self.translate_button.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.cancel_button = MPushButton(self.tr("Cancel"))
        self.cancel_button.setEnabled(True)
        self.cancel_button.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)

        self.play_stop_button = MToolButton()
        self.play_stop_button.set_dayu_svg("play_fill.svg")
        self.play_stop_button.setToolTip(self.tr("Translate All"))
        self.play_stop_button.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)

        self.batch_report_button = MToolButton()
        self.batch_report_button.set_dayu_svg("mingcute--document-line.svg")
        self.batch_report_button.setToolTip(self.tr("Report"))
        self.batch_report_button.setEnabled(False)
        self.batch_report_button.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)

        # Pipeline buttons (+ play/report, Automatic's equivalent) and
        # webtoon/mode_switch moved into self.tool_rail_widget below -
        # self.loading is the only thing left in this row, instead of its
        # own row, so it doesn't add a row that appears/disappears and
        # shifts everything below it while a long-running step is active.
        mode_controls_row = QtWidgets.QHBoxLayout()
        mode_controls_row.addWidget(self.loading)
        mode_controls_row.addStretch()

        self.top_actions_layout = QtWidgets.QVBoxLayout()
        self.top_actions_layout.addLayout(mode_controls_row)

        # Kept fully wired (see comments above) but never actually shown -
        # parented here just so they have a home.
        self.top_actions_layout.addWidget(self.manual_radio)
        self.top_actions_layout.addWidget(self.automatic_radio)
        self.top_actions_layout.addWidget(self.translate_button)
        self.top_actions_layout.addWidget(self.cancel_button)
        self.manual_radio.hide()
        self.automatic_radio.hide()
        self.translate_button.hide()
        self.cancel_button.hide()

        self.search_panel = SearchReplacePanel(self)
        self.search_panel.setVisible(False)

        left_layout = QtWidgets.QVBoxLayout()
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        left_layout.addWidget(MDivider())

        self.image_card_layout = QtWidgets.QVBoxLayout()
        self.image_card_layout.addStretch(1)

        self.page_list.setLayout(self.image_card_layout)
        left_layout.addWidget(self.page_list)
        left_layout.addWidget(self.search_panel)
        left_widget = QtWidgets.QWidget()
        left_widget.setLayout(left_layout)
        self.left_widget = left_widget

        self.central_stack = QtWidgets.QStackedWidget()

        self.drag_browser = MDragFileButton(text=self.tr("Click or drag files here"), multiple=True)
        self.drag_browser.set_dayu_svg("attachment_line.svg")
        self.drag_browser.set_dayu_filters(
            [
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
                ".bmp",
                ".avif",
                ".zip",
                ".cbz",
                ".cbr",
                ".cb7",
                ".cbt",
                ".pdf",
                ".epub",
                ".ctpr",
                ".psd",
            ]
        )
        self.drag_browser.setToolTip(
            self.tr("Import Images, PDFs, Epubs or Comic Book Archive Files(cbr, cbz, etc)")
        )
        self.central_stack.addWidget(self.drag_browser)
        self.central_stack.addWidget(self.image_viewer)
        self._create_page_nav_overlay()

        central_widget = QtWidgets.QWidget()
        self.central_widget = central_widget
        central_layout = QtWidgets.QVBoxLayout(central_widget)
        central_layout.addWidget(self.central_stack)
        central_layout.setContentsMargins(10, 10, 10, 10)

        right_layout = QtWidgets.QVBoxLayout()
        right_layout.setContentsMargins(4, 0, 4, 4)
        right_layout.setSpacing(4)
        right_layout.addWidget(MDivider())
        right_layout.addLayout(self.top_actions_layout)
        right_layout.addWidget(MDivider())

        input_layout = QtWidgets.QHBoxLayout()

        s_combo_text_layout = QtWidgets.QVBoxLayout()
        self.s_combo = MComboBox().medium()
        self.s_combo.addItems([self.tr(lang) for lang in supported_source_languages])
        self.s_combo.setToolTip(self.tr("Source Language"))
        s_combo_text_layout.addWidget(self.s_combo)
        self.s_text_edit = MTextEdit()
        self.s_text_edit.setFixedHeight(120)
        s_combo_text_layout.addWidget(self.s_text_edit)
        input_layout.addLayout(s_combo_text_layout)

        t_combo_text_layout = QtWidgets.QVBoxLayout()
        self.t_combo = MComboBox().medium()
        self.t_combo.addItems([self.tr(lang) for lang in supported_target_languages])
        self.t_combo.setToolTip(self.tr("Target Language"))
        t_combo_text_layout.addWidget(self.t_combo)
        self.t_text_edit = MTextEdit()
        self.t_text_edit.setFixedHeight(120)
        t_combo_text_layout.addWidget(self.t_text_edit)
        input_layout.addLayout(t_combo_text_layout)

        text_render_layout = QtWidgets.QVBoxLayout()
        font_settings_layout = QtWidgets.QHBoxLayout()

        self.font_dropdown = MFontComboBox().small()
        self.font_dropdown.setToolTip(self.tr("Font"))
        self.font_size_dropdown = MComboBox().small()
        self.font_size_dropdown.setToolTip(self.tr("Font Size"))
        self.font_size_dropdown.addItems(
            ["4", "6", "8", "9", "10", "11", "12", "14", "16", "18", "20", "22", "24", "28", "32", "36", "48", "72"]
        )
        self.font_size_dropdown.setCurrentText("12")
        self.font_size_dropdown.setFixedWidth(60)
        self.font_size_dropdown.set_editable(True)

        self.line_spacing_dropdown = MComboBox().small()
        self.line_spacing_dropdown.setToolTip(self.tr("Line Spacing"))
        self.line_spacing_dropdown.addItems(["1.0", "1.1", "1.2", "1.3", "1.4", "1.5"])
        self.line_spacing_dropdown.setFixedWidth(60)
        self.line_spacing_dropdown.set_editable(True)

        font_settings_layout.addWidget(self.font_dropdown)
        font_settings_layout.addWidget(self.font_size_dropdown)
        font_settings_layout.addWidget(self.line_spacing_dropdown)
        font_settings_layout.addStretch()

        main_text_settings_layout = QtWidgets.QHBoxLayout()

        settings = QSettings("ComicLabs", "ComicTranslate")
        settings.beginGroup("text_rendering")
        dflt_clr = settings.value("color", "#000000")
        dflt_outline_check = settings.value("outline", True, type=bool)
        settings.endGroup()

        self.block_font_color_button = QtWidgets.QPushButton()
        self.block_font_color_button.setToolTip(self.tr("Font Color"))
        self.block_font_color_button.setFixedSize(30, 30)
        self.block_font_color_button.setStyleSheet(f"background-color: {dflt_clr}; border: none; border-radius: 5px;")
        self.block_font_color_button.setProperty("selected_color", dflt_clr)

        self.alignment_tool_group = MToolButtonGroup(orientation=QtCore.Qt.Horizontal, exclusive=True)
        alignment_tools = [
            {"svg": "tabler--align-left.svg", "checkable": True, "tooltip": "Align Left"},
            {"svg": "tabler--align-center.svg", "checkable": True, "tooltip": "Align Center"},
            {"svg": "tabler--align-right.svg", "checkable": True, "tooltip": "Align Right"},
            {"svg": "tabler--align-justified.svg", "checkable": True, "tooltip": "Justify (last line left-aligned)"},
        ]
        self.alignment_tool_group.set_button_list(alignment_tools)
        self.alignment_tool_group.set_dayu_checked(1)

        self.bold_button = self.create_tool_button(svg="bold.svg", checkable=True)
        self.bold_button.setToolTip(self.tr("Bold"))
        self.italic_button = self.create_tool_button(svg="italic.svg", checkable=True)
        self.italic_button.setToolTip(self.tr("Italic"))
        self.underline_button = self.create_tool_button(svg="underline.svg", checkable=True)
        self.underline_button.setToolTip(self.tr("Underline"))

        main_text_settings_layout.addWidget(self.block_font_color_button)
        main_text_settings_layout.addWidget(self.alignment_tool_group)
        main_text_settings_layout.addWidget(self.bold_button)
        main_text_settings_layout.addWidget(self.italic_button)
        main_text_settings_layout.addWidget(self.underline_button)
        main_text_settings_layout.addStretch()

        outline_settings_layout = QtWidgets.QHBoxLayout()

        self.outline_checkbox = MCheckBox(self.tr("Outline"))
        self.outline_checkbox.setChecked(dflt_outline_check)

        self.outline_font_color_button = QtWidgets.QPushButton()
        self.outline_font_color_button.setToolTip(self.tr("Outline Color"))
        self.outline_font_color_button.setFixedSize(30, 30)
        self.outline_font_color_button.setStyleSheet("background-color: white; border: none; border-radius: 5px;")
        self.outline_font_color_button.setProperty("selected_color", "#ffffff")

        self.outline_width_dropdown = MComboBox().small()
        self.outline_width_dropdown.setFixedWidth(60)
        self.outline_width_dropdown.setToolTip(self.tr("Outline Width"))
        self.outline_width_dropdown.addItems(["1.0", "1.15", "1.3", "1.4", "1.5"])
        self.outline_width_dropdown.set_editable(True)

        outline_settings_layout.addWidget(self.outline_checkbox)
        outline_settings_layout.addWidget(self.outline_font_color_button)
        outline_settings_layout.addWidget(self.outline_width_dropdown)
        outline_settings_layout.addStretch()

        rendering_divider_top = MDivider()
        rendering_divider_bottom = MDivider()
        text_render_layout.addWidget(rendering_divider_top)
        text_render_layout.addLayout(font_settings_layout)
        text_render_layout.addLayout(main_text_settings_layout)
        text_render_layout.addLayout(outline_settings_layout)
        text_render_layout.addWidget(rendering_divider_bottom)

        # Floats over the canvas, anchored to the right panel's left edge
        # (see _reposition_text_flyout below), instead of widening the right
        # panel itself - a narrow monitor can't spare that width just to
        # show two text boxes. Hidden on reference-workflow pages (toggled
        # from _sync_hbutton_group_for_reference, tools.py) - Recognize/
        # Translate are disabled there and Render reveals the reference's
        # own pixels instead of drawing styled text, so none of this
        # applies; also toggled manually from self.text_panel_toggle_button
        # in the tool rail.
        self.text_section_widget = QtWidgets.QWidget(self.central_widget)
        self.text_section_widget.setFixedWidth(320)
        self.text_section_widget.setStyleSheet(
            "QWidget#textFlyout { background-color: #2b2b2b; border: 1px solid #555555; border-radius: 4px; }"
        )
        self.text_section_widget.setObjectName("textFlyout")
        text_section_layout = QtWidgets.QVBoxLayout(self.text_section_widget)
        text_section_layout.setContentsMargins(8, 8, 8, 8)
        text_section_layout.addLayout(input_layout)
        text_section_layout.addLayout(text_render_layout)
        self.central_widget.installEventFilter(self)
        self._reposition_text_flyout()
        self.text_section_widget.raise_()

        # Pan is a general canvas tool (not tied to text/language), so it
        # lives with the other box/canvas tools below instead of its own
        # row - set_all_button, on the other hand, operates on the language
        # combos in self.text_section_widget, so it's added there instead
        # (see below) and hides along with them on reference-workflow pages.
        self.pan_button = self.create_tool_button(svg="pan_tool.svg", checkable=True)
        self.pan_button.setToolTip(self.tr("Pan Image"))
        self.pan_button.clicked.connect(self.toggle_pan_tool)
        self.tool_buttons["pan"] = self.pan_button

        self.set_all_button = MPushButton(self.tr("Set for all"))
        self.set_all_button.setToolTip(
            self.tr("Sets the Source and Target Language on the current page for all pages")
        )
        text_section_layout.addWidget(self.set_all_button)

        self.box_button = self.create_tool_button(svg="select.svg", checkable=True)
        self.box_button.setToolTip(self.tr("Draw or Select Text Boxes"))
        self.box_button.clicked.connect(self.toggle_box_tool)
        self.tool_buttons["box"] = self.box_button

        self.delete_button = self.create_tool_button(svg="trash_line.svg", checkable=False)
        self.delete_button.setToolTip(self.tr("Delete Selected Box"))

        self.clear_rectangles_button = self.create_tool_button(svg="clear-outlined.svg")
        self.clear_rectangles_button.setToolTip(self.tr("Remove all the Boxes on the Image"))

        self.draw_blklist_blks = self.create_tool_button(svg="gridicons--create.svg")
        self.draw_blklist_blks.setToolTip(
            self.tr(
                "Draws all the Text Blocks in the existing Text Block List\n"
                "back on the Image (for further editing)"
            )
        )

        # All three move to self.tool_rail_widget (Box group), stacked
        # vertically to fit its width - dec/inc grow or shrink every text
        # block's box by the amount in the field next to them (e.g. if
        # boxes are consistently a bit tight/loose across the whole page).
        self.change_all_blocks_size_dec = self.create_tool_button(svg="minus_line.svg")
        self.change_all_blocks_size_dec.setToolTip(
            self.tr("Shrink every text block's box by the amount below"))

        self.change_all_blocks_size_diff = MLineEdit()
        self.change_all_blocks_size_diff.setFixedWidth(30)
        self.change_all_blocks_size_diff.setText("3")
        self.change_all_blocks_size_diff.setToolTip(
            self.tr("Amount (px) the -/+ buttons grow or shrink every text block's box by"))

        int_validator = QIntValidator()
        self.change_all_blocks_size_diff.setValidator(int_validator)
        self.change_all_blocks_size_diff.setAlignment(QtCore.Qt.AlignCenter)

        self.change_all_blocks_size_inc = self.create_tool_button(svg="add_line.svg")
        self.change_all_blocks_size_inc.setToolTip(
            self.tr("Grow every text block's box by the amount below"))

        self.brush_button = self.create_tool_button(svg="brush-fill.svg", checkable=True)
        self.brush_button.setToolTip(self.tr("Draw Brush Strokes for Cleaning Image"))
        self.brush_button.clicked.connect(self.toggle_brush_tool)
        self.tool_buttons["brush"] = self.brush_button

        self.eraser_button = self.create_tool_button(svg="eraser_fill.svg", checkable=True)
        self.eraser_button.setToolTip(self.tr("Erase Brush Strokes"))
        self.eraser_button.clicked.connect(self.toggle_eraser_tool)
        self.tool_buttons["eraser"] = self.eraser_button

        self.clear_brush_strokes_button = self.create_tool_button(svg="clear-outlined.svg")
        self.clear_brush_strokes_button.setToolTip(self.tr("Remove all the brush strokes on the Image"))

        self.eyedropper_button = self.create_tool_button(svg="eyedropper.svg", checkable=True)
        self.eyedropper_button.setToolTip(self.tr("Pick a Color from the Image"))
        self.eyedropper_button.clicked.connect(self.toggle_eyedropper_tool)
        self.tool_buttons["eyedropper"] = self.eyedropper_button

        self.pencil_button = self.create_tool_button(svg="edit_fill.svg", checkable=True)
        self.pencil_button.setToolTip(self.tr("Draw with the Picked Color to Manually Clean the Image"))
        self.pencil_button.clicked.connect(self.toggle_pencil_tool)
        self.tool_buttons["pencil"] = self.pencil_button

        self.patch_eraser_button = self.create_tool_button(svg="patch-eraser.svg", checkable=True)
        self.patch_eraser_button.setToolTip(self.tr(
            "Erase Part of Any Patch (Auto-Clean, Pencil, or Reveal Pencil), Revealing What's Underneath"))
        self.patch_eraser_button.clicked.connect(self.toggle_patch_eraser_tool)
        self.tool_buttons["patch_eraser"] = self.patch_eraser_button

        # Lives here (not in the Reference Page section below) because it's a
        # paint tool sharing the same size slider as the others, even though
        # it only does anything once a page has an aligned reference.
        self.reveal_pencil_button = self.create_tool_button(svg="reveal-pencil.svg", checkable=True)
        self.reveal_pencil_button.setToolTip(self.tr(
            "Reveal Pencil: Paint to Bring Over the Aligned Reference Page's Pixels "
            "(e.g. Its Already-Translated Text) Instead of a Flat Color"))
        self.reveal_pencil_button.clicked.connect(self.toggle_reveal_pencil_tool)
        self.reveal_pencil_button.setEnabled(False)
        self.tool_buttons["reveal_pencil"] = self.reveal_pencil_button

        self.brush_size_label = MLabel(self.tr("Tool Size (Brush/Eraser/Pencil/Patch Eraser/Reveal Pencil)"))
        self.brush_eraser_slider = MSlider()
        self.brush_eraser_slider.setMinimum(1)
        self.brush_eraser_slider.setMaximum(100)
        self.brush_eraser_slider.setValue(10)
        self.brush_eraser_slider.setToolTip(self.tr("Brush/Eraser/Pencil/Patch Eraser/Reveal Pencil Size"))
        self.brush_eraser_slider.valueChanged.connect(self.set_brush_eraser_size)

        ref_book_lay = QtWidgets.QHBoxLayout()

        self.load_reference_book_button = MPushButton(text=self.tr("Load Reference Book")).small()
        self.load_reference_book_button.setToolTip(self.tr(
            "Load a Whole Second Edition (cbz/cbr/pdf/...) to Auto-Pair Its Pages by Index + Offset"))
        self.load_reference_book_button.clicked.connect(self.load_reference_book)

        self.reference_offset_spin = MSpinBox()
        self.reference_offset_spin.setRange(-999, 999)
        self.reference_offset_spin.setValue(0)
        self.reference_offset_spin.setToolTip(self.tr(
            "Page Offset: Reference Page = Current Page + Offset "
            "(e.g. set to 2 if the reference book has 2 extra pages before this one)"))
        self.reference_offset_spin.valueChanged.connect(self.set_reference_page_offset)

        ref_book_lay.addWidget(self.load_reference_book_button)
        ref_book_lay.addWidget(MLabel(self.tr("Offset:")))
        ref_book_lay.addWidget(self.reference_offset_spin)
        ref_book_lay.addStretch()

        ref_preview_lay = QtWidgets.QHBoxLayout()
        self.reference_preview_label = QtWidgets.QLabel(self.tr("(no preview)"))
        self.reference_preview_label.setFixedSize(140, 190)
        self.reference_preview_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.reference_preview_label.setStyleSheet(
            "QLabel { border: 1px solid #555; background-color: #222; color: #888; }")
        self.reference_offset_label = MLabel(self.tr("No reference book loaded"))
        self.reference_offset_label.setWordWrap(True)
        ref_preview_lay.addWidget(self.reference_preview_label)
        ref_preview_lay.addWidget(self.reference_offset_label, 1)

        self.reference_excluded_checkbox = MCheckBox(
            self.tr("This Page Has No Reference (Use Traditional Translation)"))
        self.reference_excluded_checkbox.setToolTip(self.tr(
            "For pages that exist only in this edition (e.g. bonus pages) and "
            "have no counterpart in the reference book at all"))
        self.reference_excluded_checkbox.stateChanged.connect(self.toggle_reference_page_excluded)

        self.load_reference_button = self.create_tool_button(svg="ion--image-outline.svg", checkable=True)
        self.load_reference_button.setToolTip(self.tr(
            "Overlay This Page's Reference (from the Book Above, or Pick One) and Drag its Corners to Align It"))
        self.load_reference_button.clicked.connect(self.toggle_reference_alignment)
        self.tool_buttons["align_reference"] = self.load_reference_button

        self.confirm_reference_button = self.create_tool_button(svg="check.svg")
        self.confirm_reference_button.setToolTip(self.tr("Confirm Alignment"))
        self.confirm_reference_button.clicked.connect(self.confirm_reference_alignment)
        self.confirm_reference_button.setEnabled(False)

        self.reset_reference_button = self.create_tool_button(svg="refresh_line.svg")
        self.reset_reference_button.setToolTip(self.tr(
            "Reset Alignment to the Default Full-Page Rectangle "
            "(Use This if the Automatic Starting Guess Is Way Off and Its Corners Are Unreachable)"))
        self.reset_reference_button.clicked.connect(self.reset_reference_alignment)
        self.reset_reference_button.setEnabled(False)

        self.reference_opacity_label = MLabel(self.tr("Reference Overlay Opacity (While Aligning Only)"))
        self.reference_opacity_slider = MSlider()
        self.reference_opacity_slider.setMinimum(0)
        self.reference_opacity_slider.setMaximum(100)
        self.reference_opacity_slider.setValue(50)
        self.reference_opacity_slider.setToolTip(self.tr(
            "Reference Overlay Opacity - Not the Reveal Pencil's Size, See 'Tool Size' Above"))
        self.reference_opacity_slider.valueChanged.connect(self.set_reference_opacity)

        # "Options bar" for the paint-family tools (brush/eraser/pencil/
        # patch eraser/reveal pencil) - shown only while one of them is the
        # active tool (toggled from set_tool() in tools.py), instead of its
        # own permanently-visible collapsible section now that the tool
        # buttons themselves moved to self.tool_rail_widget.
        self.paint_options_widget = QtWidgets.QWidget()
        paint_options_layout = QtWidgets.QVBoxLayout(self.paint_options_widget)
        paint_options_layout.setContentsMargins(0, 0, 0, 0)
        paint_options_layout.addWidget(self.brush_size_label)
        paint_options_layout.addWidget(self.brush_eraser_slider)
        self.paint_options_widget.setVisible(False)
        self.top_actions_layout.addWidget(self.paint_options_widget)

        # Floats over the canvas, same as self.text_section_widget above -
        # anchored to the right panel's left edge, below the Text panel if
        # both happen to be open. Hidden by default; shown via
        # self.reference_panel_toggle_button (tool rail, Ref group) or
        # automatically on reference-workflow pages
        # (_sync_hbutton_group_for_reference, tools.py).
        self.reference_section_widget = QtWidgets.QWidget(self.central_widget)
        self.reference_section_widget.setFixedWidth(320)
        self.reference_section_widget.setStyleSheet(
            "QWidget#referenceFlyout { background-color: #2b2b2b; border: 1px solid #555555; border-radius: 4px; }"
        )
        self.reference_section_widget.setObjectName("referenceFlyout")
        reference_section_layout = QtWidgets.QVBoxLayout(self.reference_section_widget)
        reference_section_layout.setContentsMargins(8, 8, 8, 8)
        reference_section_layout.addLayout(ref_book_lay)
        reference_section_layout.addLayout(ref_preview_lay)
        reference_section_layout.addWidget(self.reference_excluded_checkbox)
        reference_section_layout.addWidget(self.reference_opacity_label)
        reference_section_layout.addWidget(self.reference_opacity_slider)
        self.reference_section_widget.setVisible(False)
        self._reposition_reference_flyout()

        right_content_widget = QtWidgets.QWidget()
        right_content_widget.setLayout(right_layout)

        self.tool_rail_widget = self._create_tool_rail()

        right_outer_layout = QtWidgets.QHBoxLayout()
        right_outer_layout.setContentsMargins(0, 0, 0, 0)
        right_outer_layout.setSpacing(0)
        right_outer_layout.addWidget(self.tool_rail_widget)
        right_outer_layout.addWidget(right_content_widget, 1)

        right_widget = QtWidgets.QWidget()
        right_widget.setLayout(right_outer_layout)

        splitter = QtWidgets.QSplitter()
        splitter.addWidget(left_widget)
        splitter.addWidget(central_widget)
        splitter.addWidget(right_widget)

        splitter.setStretchFactor(0, 40)
        splitter.setStretchFactor(1, 80)
        splitter.setStretchFactor(2, 10)

        content_layout = QtWidgets.QVBoxLayout()
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self.progress_bar)
        content_layout.addWidget(splitter)

        content_layout.setStretchFactor(splitter, 1)

        content_widget.setLayout(content_layout)

        return content_widget

    def _create_tool_rail(self) -> QtWidgets.QWidget:
        """Narrow, always-visible column of the right panel's icon-only
        tools (pan/box/inpainting/reference-alignment) - keeps a portrait
        monitor's right panel from needing the width those used to take up
        as horizontal rows inside the Box Drawing/Inpainting/Reference Page
        sections. Sliders, combos and the reference preview thumbnail can't
        fit a column like this (a tooltip can't host a draggable control),
        so they're left where they already are for now."""
        rail_button_size = 24
        rail_button_spacing = 0

        rail_widget = QtWidgets.QWidget()
        rail_layout = QtWidgets.QVBoxLayout(rail_widget)
        rail_layout.setContentsMargins(2, 4, 2, 4)
        rail_layout.setSpacing(0)
        rail_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)

        def make_divider_line():
            # MDivider's HLine relies on Qt's native sunken-groove shadow
            # effect, which depends on light/dark palette roles that barely
            # differ from the background in this dark theme - practically
            # invisible here regardless of how many are stacked. An explicit
            # background color always shows up.
            line = QtWidgets.QFrame()
            line.setFixedHeight(1)
            line.setStyleSheet("background-color: #666666; border: none;")
            return line

        def make_group_separator():
            separator_widget = QtWidgets.QWidget()
            separator_layout = QtWidgets.QVBoxLayout(separator_widget)
            separator_layout.setContentsMargins(0, 0, 0, 0)
            separator_layout.setSpacing(3)
            separator_layout.addWidget(make_divider_line())
            separator_layout.addWidget(make_divider_line())
            return separator_widget

        def add_group(buttons, label=None, add_divider_after=True):
            if label:
                group_label = MLabel(label)
                group_label.setWordWrap(True)
                group_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                group_label.setStyleSheet("font-size: 9px; color: #888;")
                rail_layout.addWidget(group_label)
                rail_layout.addSpacing(2)
            for index, button in enumerate(buttons):
                if not isinstance(button, MSwitch):
                    button.setFixedSize(rail_button_size, rail_button_size)
                # Plain .setIconSize() gets silently overwritten back to the
                # theme's own size-tier default (qproperty-iconSize in the
                # global stylesheet) the first time the button is actually
                # shown - confirmed with a throwaway script, not a guess. A
                # per-widget stylesheet rule for the same property wins the
                # cascade and actually sticks. Close to the button's own
                # size, not just "big enough to see" - the real driver of
                # how separated icons look is this padding inside each
                # button, not the (already near-zero) layout spacing.
                if isinstance(button, MToolButton):
                    button.setStyleSheet("MToolButton { qproperty-iconSize: 19px 19px; }")
                if index > 0:
                    rail_layout.addSpacing(rail_button_spacing)
                rail_layout.addWidget(button)
            if add_divider_after:
                rail_layout.addSpacing(4)
                rail_layout.addWidget(make_group_separator())
                rail_layout.addSpacing(4)

        # Webtoon toggle + Manual/Automatic switch, always visible
        # regardless of mode - first thing in the rail since it governs
        # everything below it.
        add_group((self.webtoon_toggle, self.mode_switch), label=self.tr("Ctrl"))

        # Manual's 6 pipeline steps and Automatic's play/report share this
        # top slot under one label - only one set is visible at a time
        # (toggled by batch_mode_selected/manual_mode_selected in
        # controller.py, same widgets as before, just living here now
        # instead of a 2x3 grid).
        add_group(self.hbutton_group.get_button_group().buttons(), label=self.tr("Steps"), add_divider_after=False)
        add_group((self.play_stop_button, self.batch_report_button))

        # Manual show/hide for the floating Text panel (self.text_section_widget,
        # see its construction above) - independent of the automatic hide/show
        # that already happens on reference-workflow pages (tools.py).
        self.text_panel_toggle_button = self.create_tool_button(svg="text_panel.svg", checkable=True)
        self.text_panel_toggle_button.setToolTip(self.tr("Show/Hide Text Panel"))
        self.text_panel_toggle_button.setChecked(True)
        self.text_panel_toggle_button.clicked.connect(self.toggle_text_panel)
        add_group((self.text_panel_toggle_button,), label=self.tr("Text"))

        add_group((
            self.pan_button,
            self.box_button,
            self.delete_button,
            self.clear_rectangles_button,
            self.draw_blklist_blks,
            self.change_all_blocks_size_dec,
            self.change_all_blocks_size_diff,
            self.change_all_blocks_size_inc,
        ), label=self.tr("Box"))

        add_group((
            self.brush_button,
            self.eraser_button,
            self.clear_brush_strokes_button,
            self.eyedropper_button,
            self.pencil_button,
            self.patch_eraser_button,
            self.reveal_pencil_button,
        ), label=self.tr("Paint"))

        self.reference_panel_toggle_button = self.create_tool_button(svg="panel_toggle.svg", checkable=True)
        self.reference_panel_toggle_button.setToolTip(self.tr("Show/Hide Reference Page Panel"))
        self.reference_panel_toggle_button.clicked.connect(self.toggle_reference_panel)
        add_group((
            self.reference_panel_toggle_button,
            self.load_reference_button,
            self.confirm_reference_button,
            self.reset_reference_button,
        ), label=self.tr("Ref"), add_divider_after=False)

        # Scrollable so a future addition (or a short window on a landscape
        # monitor) gets its own scrollbar instead of overflowing the window
        # or pushing the rest of the right panel around.
        rail_scroll = QtWidgets.QScrollArea()
        rail_scroll.setWidgetResizable(True)
        rail_scroll.setWidget(rail_widget)
        rail_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rail_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        rail_scroll.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        rail_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        return rail_scroll

    def _create_page_nav_overlay(self):
        """Prev/Next page arrows that float over the image viewer instead of
        taking up permanent layout space, so they stay reachable even with
        the page list fully collapsed - shown only while hovering the viewer,
        and only on the side(s) where there's actually another page."""
        nav_button_style = """
            QToolButton {
                background-color: rgba(0, 0, 0, 110);
                border: none;
                border-radius: 18px;
            }
            QToolButton:hover {
                background-color: rgba(0, 0, 0, 160);
            }
        """

        self.prev_page_button = QtWidgets.QToolButton(self.image_viewer)
        self.prev_page_button.setIcon(MIcon("arrow-left.svg"))
        self.prev_page_button.setIconSize(QtCore.QSize(18, 18))
        self.prev_page_button.setFixedSize(36, 36)
        self.prev_page_button.setToolTip(self.tr("Previous Page"))
        self.prev_page_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.prev_page_button.setStyleSheet(nav_button_style)
        self.prev_page_button.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.prev_page_button.clicked.connect(lambda: self._navigate_page_via_overlay(-1))
        self.prev_page_button.hide()

        self.next_page_button = QtWidgets.QToolButton(self.image_viewer)
        self.next_page_button.setIcon(MIcon("arrow-right.svg"))
        self.next_page_button.setIconSize(QtCore.QSize(18, 18))
        self.next_page_button.setFixedSize(36, 36)
        self.next_page_button.setToolTip(self.tr("Next Page"))
        self.next_page_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.next_page_button.setStyleSheet(nav_button_style)
        self.next_page_button.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.next_page_button.clicked.connect(lambda: self._navigate_page_via_overlay(1))
        self.next_page_button.hide()

        self._page_nav_hovering = False
        self.image_viewer.installEventFilter(self)
        # Mouse moves over the viewport don't reach the outer QGraphicsView's
        # own event() (QAbstractScrollArea forwards them internally instead),
        # so the viewport needs its own filter - this is what re-checks page
        # bounds while hovering, since switching pages loads the new image on
        # a background thread and curr_img_idx only updates once that finishes,
        # a moment after the click (see _navigate_page_via_overlay).
        self.image_viewer.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        viewer = getattr(self, "image_viewer", None)
        if viewer is not None and obj is viewer.viewport():
            if event.type() == QtCore.QEvent.Type.MouseMove:
                self._update_page_nav_buttons_visibility()
        elif obj is viewer:
            event_type = event.type()
            if event_type == QtCore.QEvent.Type.Resize:
                self._reposition_page_nav_buttons()
            elif event_type == QtCore.QEvent.Type.Enter:
                self._page_nav_hovering = True
                self._update_page_nav_buttons_visibility()
            elif event_type == QtCore.QEvent.Type.Leave:
                self._page_nav_hovering = False
                self.prev_page_button.hide()
                self.next_page_button.hide()
        elif obj is getattr(self, "central_widget", None) and event.type() == QtCore.QEvent.Type.Resize:
            self._reposition_text_flyout()
            self._reposition_reference_flyout()
        return super().eventFilter(obj, event)

    def _reposition_text_flyout(self):
        """Keeps self.text_section_widget flush against the canvas's right
        edge (i.e. the right panel's left edge), near the top - sized to
        its own content instead of stretched to the canvas's full height,
        which left a mostly-empty panel below the actual controls."""
        if not hasattr(self, "text_section_widget"):
            return
        margin = 10
        self.text_section_widget.adjustSize()
        x = self.central_widget.width() - self.text_section_widget.width() - margin
        self.text_section_widget.move(max(margin, x), margin)

    def _reposition_reference_flyout(self):
        """Same idea as _reposition_text_flyout - flush against the right
        panel's left edge, stacked below the Text panel if that one is
        also currently visible, otherwise at the same top margin."""
        if not hasattr(self, "reference_section_widget"):
            return
        margin = 10
        self.reference_section_widget.adjustSize()
        x = self.central_widget.width() - self.reference_section_widget.width() - margin
        y = margin
        text_widget = getattr(self, "text_section_widget", None)
        if text_widget is not None and text_widget.isVisible():
            y = text_widget.y() + text_widget.height() + margin
        self.reference_section_widget.move(max(margin, x), y)

    def _reposition_page_nav_buttons(self):
        if not hasattr(self, "prev_page_button"):
            return
        margin = 12
        y = (self.image_viewer.height() - self.prev_page_button.height()) // 2
        self.prev_page_button.move(margin, y)
        self.next_page_button.move(
            self.image_viewer.width() - self.next_page_button.width() - margin, y
        )

    def _update_page_nav_buttons_visibility(self):
        if not hasattr(self, "prev_page_button"):
            return
        if not self._page_nav_hovering or self.webtoon_mode or len(self.image_files) < 2:
            self.prev_page_button.hide()
            self.next_page_button.hide()
            return
        self.prev_page_button.setVisible(self.curr_img_idx > 0)
        self.next_page_button.setVisible(self.curr_img_idx < len(self.image_files) - 1)

    def _navigate_page_via_overlay(self, direction: int):
        self.image_ctrl.navigate_images(direction)
        # curr_img_idx only updates once the new page's background load
        # finishes, a moment after this call returns - recheck a few times
        # so the buttons catch up even if the mouse stays still after the
        # click (MouseMove while hovering also triggers this, for the
        # common case of the user moving towards the other arrow).
        self._update_page_nav_buttons_visibility()
        for delay_ms in (120, 350, 700):
            QtCore.QTimer.singleShot(delay_ms, self._update_page_nav_buttons_visibility)

    def create_tool_button(self, text: str = "", svg: str = "", checkable: bool = False):
        if text:
            button = MToolButton().svg(svg).text_beside_icon()
            button.setText(text)
        else:
            button = MToolButton().svg(svg)

        button.setCheckable(True) if checkable else button.setCheckable(False)

        return button
