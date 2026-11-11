# Changelog — Lettering Bench

**Lettering Bench** (`ThePerramus/lettering-bench`, branch `v2.8.8-work`) is a fork of
[Comic Translate](https://github.com/ogkalu2/comic-translate), based on upstream `v2.8.9`. It adds
a workflow for pages that already have a translation to reuse instead of retranslating them, plus
several general-purpose editing and lettering improvements built along the way.

68 commits, 51 files touched, roughly +4,330/−350 lines relative to `v2.8.9`.

## Reference-page reveal workflow

For comics where a high-quality foreign-language scan and a separate, already-translated
low-quality scan both exist, re-running OCR and translation on the HQ page throws away a
translation that's already correct. This workflow cleans the HQ page's bubbles as usual, aligns
the LQ scan onto it, and reveals the LQ page's own pixels through the holes.

- **Manual corner alignment** — drag a semi-transparent overlay of the reference scan by its four
  corners until its art lines up with the page.
- **Automatic starting guess** — detects text blocks and panel boundaries on both scans and fits a
  starting perspective transform from the matches.
- **Reset Alignment** — one click back to the default full-page rectangle if the automatic guess
  goes wrong.
- **Bulk reference books** — load a whole cbz/cbr of the reference edition and pair pages to the
  working book by index plus an offset.
- **Piecewise offsets** — set a new offset partway through a book so inserted/divider pages don't
  throw off every page after them.
- **Per-page exclusion** — mark an HQ-only bonus page as having no reference counterpart at all.
- **Reveal Pencil** — an inpainting-style brush that paints with the aligned reference's own pixels
  instead of a flat color.
- **Content-aware auto-reveal** — reveals only the reference's actual detected text (dilated to
  keep its anti-aliased/halftone edges), not the whole cleaned rectangle, so an irregular bubble's
  corners keep Clean's own fill instead of the reference's off-tone scan background.
- **Pipeline integration** — on a reference page, Recognize/Translate disable themselves and
  Render reveals instead of drawing new text.
- **Re-letter mode** — a per-page "Translated Text" choice in the Reference Page panel: *Reveal
  Original* (the above) or *Re-letter*, which reads the reference's translated text and letters it
  as normal, editable text in the chosen font instead of bringing over the reference's pixels.
  - **Recognize** reads the aligned reference, not the working page, and puts the result straight
    into each box's translation. With a vision LLM as the translator it reads the whole page in
    one request (boxes given by position, including text a box cuts off); otherwise the OCR engine
    reads each box, in the target language.
  - **Translate** becomes a proofreading pass: the LLM gets the reference page plus each box's
    current text and returns what is actually printed, without translating or rewording.
  - Line breaks and end-of-line hyphenation from the reference's lettering are removed, so Render
    re-wraps and re-hyphenates the text to the box. Detect and the Text panel stay available.
  - The mode picked becomes the default for pages not worked on yet; pages already cleaned keep
    theirs.
- **Full persistence** — alignment, offsets, exclusions, re-letter modes, and the reference book's
  path all survive closing and reopening the project.

## Workspace

- **Redesigned top toolbar** — collapsible page list, floating page-navigation arrows, and a
  compact Automatic mode row (with the loading spinner in it).
- **Icon rail instead of the right panel** — the tool rows became a narrow icon rail with two
  on-demand flyout panels (Text and Reference Page); the left nav rail is grouped, and a panel
  closed by hand stays closed across page switches. The paint-tool size box sits under the
  flyouts, top right.
- **More room for the page** — the 10px gutter around the canvas is gone.
- **Scroll past a fully visible page** — the mouse wheel can move the page out from under the
  floating panels even when it fits on screen (useful on a vertical monitor), in smooth steps
  instead of big jumps. Ctrl+wheel still zooms.

## Precision editing

- **Shift-constrain** — hold Shift while drawing with the brush, eraser, pencil, patch eraser, or
  reveal pencil to lock the stroke to a straight horizontal or vertical line.
- **Arrow-key nudging** — 1px steps (10px with Shift) for reference-alignment corners and selected
  text boxes, coalesced into a single undo step.

## Lettering & text rendering

- **Format several boxes at once** — alignment, bold/italic/underline, outline and line spacing
  apply to every selected text box, not just the current one.
- **Justify alignment** — a fourth option beside left/center/right; the paragraph's final line
  keeps its natural width instead of being stretched.
- **Live word-wrap** — neither the manual Render step nor the automatic "Translate All"
  pipeline bake hard line breaks into the translation anymore; the text box reflows live
  when the font size or box size changes.
- **Syllable hyphenation** — long words that don't fit a narrow bubble break at a real syllable
  boundary instead of overflowing or splitting arbitrarily.

## Recognition

- **Custom OCR engine** — any OpenAI-compatible vision endpoint (URL, model, API key), mirroring
  the existing Custom translator; works with e.g. Gemini's OpenAI-compatible API.

## Clean (inpainting) quality

- **Flat fill for more bubbles** — text-dense bubbles and bubbles on grainy paper are filled with
  their flat background color instead of being sent to LaMa, which used to leave a faint texture.
  Bubble flatness is judged near the text, so artwork elsewhere in the bubble's box doesn't
  count against it, and the looser grain tolerance only applies to light backgrounds.
- **Hand-painted mask strokes are honored** — areas added with the brush are no longer dropped
  when they reach a bubble's edge. They're flat-filled when the surrounding background is flat
  (the usual case: text the detector missed inside a bubble), and only go to LaMa over texture or
  when an outline actually runs through the stroke.
- **Leftover letters next to bubble outlines** are flat-filled when only clean background
  surrounds them, instead of being left behind; anything touching the outline is still left
  alone so the outline itself is never smeared or thinned.

## Inpainting touch-ups

- **Eyedropper, pencil, patch eraser** — hand tools for touching up cleaned pages, each with its
  own independently remembered brush size.
- The patch eraser also fixes a reveal-pencil stroke that ran over the edge of a bubble.
- **Pencil/patch layering fixes** — a pencil correction used before a reference page's first
  reveal now merges directly into Clean's own patch(es) underneath (even when a stroke spans more
  than one, e.g. one Clean patch per text line) instead of sitting on top as a separate layer that
  blocked the real text; one used after a reveal still stays as a permanent top layer. Also fixed
  the patch eraser and reveal-pencil losing each other's work through the same "opaque rectangle
  instead of real stroke shape" root cause.

## Manual workflow fixes

- **Adding a missed bubble to a finished page keeps the finished work** — Segment no longer
  removes the page's rendered text and only masks boxes that don't have text rendered yet; on a
  partly done page, OCR only reads boxes without text and Translate only translates boxes without
  a translation, so text and translations fixed by hand aren't overwritten (with every box filled
  in, the whole page is redone as before); after translating, only text boxes whose translation
  changed are re-wrapped, keeping hand-set font sizes.
- **Re-translating keeps the live wrap** — after translating one box on a rendered page (or
  translating several pages at once), text boxes are refreshed the same way a fresh Render does:
  flowing text with automatic soft hyphens, instead of baked-in line breaks and no hyphens.
  Multi-page Render now uses the same live wrap too.
- **A newly drawn text box is selected right away**, so source/translation text typed next goes
  to it instead of to whichever box was selected before.
- **The eraser matches its preview circle** — it used to erase twice as wide as the circle shown.
- OCR, Translate and Render now log the cases where they used to stop silently, so a step that
  "does nothing" can be traced in `crash_logs`.

## Stability fixes

Assorted general-purpose fixes made along the way: a Windows heap-corruption crash, rectangular
text boxes collapsing into ellipses, erased inpaint patches reverting after a project reload, a
broken brush-size hover preview, an image-cache desync crash, a save/navigation race, and an
unnecessary forced-login requirement.

## On the next upstream release

This branch sits well ahead of the `v2.8.9` tag it's built on, across files that are central to
the app (canvas event handling, the text item class, the rendering pipeline, the main window's
tool wiring) rather than a separate plug-in. When a new upstream tag lands, a single
`git merge <new-tag>` into this branch is the practical move — it reconciles the two histories
once, at a single merge commit, instead of replaying every commit here one at a time the way a
rebase would.
