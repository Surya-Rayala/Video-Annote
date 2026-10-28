# video_annote/ui/workers.py
"""Run long media jobs (imports, exports) off the GUI thread behind a cancellable progress dialog."""
from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Optional

from PyQt5.QtCore import QEventLoop, QObject, Qt, QThread, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import QProgressDialog, QWidget

from ..media.ffmpeg import CancelledError, FFmpegError, FFmpegNotFoundError

# fn(report_progress(fraction, message), is_cancelled) -> result
JobFn = Callable[[Callable[[float, str], None], Callable[[], bool]], Any]


@dataclass
class JobOutcome:
    status: str  # "ok" | "error" | "cancelled"
    value: Any = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class _Job(QThread):
    progress = pyqtSignal(float, str)
    done = pyqtSignal(object)

    def __init__(self, fn: JobFn, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._fn = fn
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            value = self._fn(lambda f, msg="": self.progress.emit(float(f), str(msg)), self._cancel.is_set)
        except CancelledError:
            self.done.emit(JobOutcome("cancelled"))
        except (FFmpegError, FFmpegNotFoundError, OSError, ValueError, RuntimeError) as e:
            self.done.emit(JobOutcome("error", error=str(e)))
        except Exception:  # unexpected: keep the traceback for the bug report
            self.done.emit(JobOutcome("error", error=traceback.format_exc()))
        else:
            self.done.emit(JobOutcome("ok", value))


class _Bridge(QObject):
    """Lives on the GUI thread so worker signals are delivered there (queued)."""

    def __init__(self, dialog: QProgressDialog, loop: QEventLoop) -> None:
        super().__init__(dialog)
        self._dialog = dialog
        self._loop = loop
        self.outcome = JobOutcome("cancelled")

    @pyqtSlot(float, str)
    def on_progress(self, fraction: float, message: str) -> None:
        self._dialog.setValue(int(max(0.0, min(1.0, fraction)) * 1000))
        if message:
            self._dialog.setLabelText(message)

    @pyqtSlot(object)
    def on_done(self, outcome: JobOutcome) -> None:
        self.outcome = outcome

    @pyqtSlot()
    def on_finished(self) -> None:
        self._loop.quit()


def run_with_progress(parent: Optional[QWidget], title: str, label: str, fn: JobFn) -> JobOutcome:
    """Run ``fn`` in a worker thread while a modal progress dialog keeps the UI responsive.
    Blocks (in a local event loop) until the job finishes, fails or is cancelled."""
    dialog = QProgressDialog(label, "Cancel", 0, 1000, parent)
    dialog.setWindowTitle(title)
    dialog.setWindowModality(Qt.WindowModal)
    dialog.setMinimumDuration(0)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    dialog.setMinimumWidth(460)
    dialog.setValue(0)

    loop = QEventLoop()
    bridge = _Bridge(dialog, loop)
    job = _Job(fn)
    job.progress.connect(bridge.on_progress)
    job.done.connect(bridge.on_done)
    job.finished.connect(bridge.on_finished)

    def request_cancel() -> None:
        # QProgressDialog hides itself on cancel; keep it up (and the window blocked) until the job
        # has actually stopped
        dialog.setLabelText("Cancelling…")
        dialog.setCancelButton(None)
        dialog.show()
        job.cancel()

    dialog.canceled.connect(request_cancel)
    job.start()
    loop.exec_()
    job.wait()
    dialog.close()
    dialog.deleteLater()
    return bridge.outcome
