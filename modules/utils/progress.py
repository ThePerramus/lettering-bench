"""Progress and cancellation for long manual steps (OCR, translation, re-letter).

The step's worker thread runs inside a TaskProgress (see
ComicTranslate.run_task_threaded); the code doing the work reports to
current_task() - pages done, boxes done, what it's waiting on - and checks
whether the user pressed Cancel. The UI polls the same object on a timer to
draw the progress bar, so nothing here touches Qt.

current_task() returns a do-nothing stand-in when no step is being tracked
(Automatic mode, or any other caller), so the reporting calls never need a
None check.
"""

from __future__ import annotations

import threading
import time


class TaskCancelled(Exception):
    """The user cancelled the step while it was waiting (e.g. between retries)."""


class TaskProgress:
    def __init__(self, label: str):
        self.label = label
        self.started = time.monotonic()
        self.pages_total = 0
        self.pages_done = 0
        self.items_total = 0
        self.items_done = 0
        self.note = ""
        self._cancel = threading.Event()

    # --- reporting (worker thread) ---------------------------------------

    def set_pages(self, total: int):
        self.pages_total, self.pages_done = max(0, int(total)), 0

    def page_done(self):
        self.pages_done += 1
        self.items_total = self.items_done = 0

    def at_page(self, index: int):
        """Start of page number `index` (0-based) of the selection."""
        self.pages_done = index
        self.items_total = self.items_done = 0

    def set_items(self, total: int):
        self.items_total, self.items_done = max(0, int(total)), 0

    def item_done(self):
        self.items_done += 1

    def set_note(self, text: str = ""):
        self.note = text or ""

    # --- cancellation ------------------------------------------------------

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def sleep(self, seconds: float):
        """Waits like time.sleep, but returns at once when cancelled - then
        raises TaskCancelled."""
        if self._cancel.wait(max(0.0, seconds)):
            raise TaskCancelled()

    # --- reading (UI thread) -----------------------------------------------

    def fraction(self) -> float | None:
        """0..1, or None when there's nothing countable (a single request)."""
        item = self.items_done / self.items_total if self.items_total else None
        if self.pages_total:
            within = min(item or 0.0, 1.0)
            return min(1.0, (self.pages_done + within) / self.pages_total)
        if item is not None:
            return min(1.0, item)
        return None

    def status_text(self) -> str:
        parts = [self.label]
        if self.pages_total:
            parts.append(f"page {min(self.pages_done + 1, self.pages_total)} of {self.pages_total}")
        if self.items_total:
            parts.append(f"box {min(self.items_done + 1, self.items_total)} of {self.items_total}")
        text = " - ".join(parts)
        elapsed = int(time.monotonic() - self.started)
        text += f"  ({elapsed // 60}:{elapsed % 60:02d})"
        if self.note:
            text += f"\n{self.note}"
        return text


class _NoTask(TaskProgress):
    """Stand-in when nothing is tracked: accepts every call, never cancelled."""

    def __init__(self):
        super().__init__("")

    def cancel(self):
        pass

    def sleep(self, seconds: float):
        time.sleep(max(0.0, seconds))


_NO_TASK = _NoTask()
_current: TaskProgress | None = None
_lock = threading.Lock()


def current_task() -> TaskProgress:
    task = _current
    return task if task is not None else _NO_TASK


def active_task() -> TaskProgress | None:
    """The tracked task, or None (for the UI)."""
    return _current


def start_task(label: str) -> TaskProgress:
    global _current
    task = TaskProgress(label)
    with _lock:
        _current = task
    return task


def end_task(task: TaskProgress):
    global _current
    with _lock:
        if _current is task:
            _current = None
