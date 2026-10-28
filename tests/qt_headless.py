"""Run Qt multimedia headlessly and silently on macOS (AVFoundation backend).

* QT_QPA_PLATFORM=offscreen: no windows are shown.
* The CoreFoundation run loop is pumped, otherwise AVFoundation never delivers its callbacks under
  the offscreen platform plugin.
* Video goes to an offscreen surface: QVideoWidget needs a native window and crashes on play().
  The surface decodes the frame index that media_factory paints into every frame, so tests can
  check the picture itself (shown_frame), not only player positions.
* Every player is forced to muted / volume 0 whatever the app asks for, so nothing is ever audible.
"""
from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import os
from typing import Callable, Dict, Iterator, List, Optional, Tuple

_SURFACES: Dict[int, object] = {}  # id(QMediaPlayer) -> its offscreen surface
# Surfaces are never freed: a player that is only scheduled for deletion can still post frames to its
# surface, and presenting into a freed one corrupts memory (crashes in a later test).
_KEEP_ALIVE: List[object] = []


def shown_frame(player) -> Optional[int]:
    """Index of the frame the player last presented (media_factory clips only)."""
    surface = _SURFACES.get(id(player))
    return getattr(surface, "last", None)


@contextlib.contextmanager
def headless_qt() -> Iterator[object]:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PyQt5.QtCore import Qt, QTimer
    from PyQt5.QtMultimedia import QAbstractVideoBuffer, QAbstractVideoSurface, QMediaPlayer, QVideoFrame
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)  # no window is shown: closing a progress dialog must not quit
    from PyQt5.QtCore import QSettings
    import tempfile
    # remembered window layout, data root ...: never the user's own (also for scripts run outside pytest)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, tempfile.mkdtemp(prefix="video-annote-settings-"))
    cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    cf.CFRunLoopRunInMode.restype = ctypes.c_int32
    cf.CFRunLoopRunInMode.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]
    mode = ctypes.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")
    pump = QTimer()
    pump.setTimerType(Qt.PreciseTimer)
    pump.timeout.connect(lambda: cf.CFRunLoopRunInMode(mode, 0.0, True))
    pump.start(2)

    from media_factory import BITS

    class Surface(QAbstractVideoSurface):
        last: Optional[int] = None

        def supportedPixelFormats(self, handle=QAbstractVideoBuffer.NoHandle):
            return [QVideoFrame.Format_ARGB32] if handle == QAbstractVideoBuffer.NoHandle else []

        def present(self, frame):
            f = QVideoFrame(frame)
            if f.map(QAbstractVideoBuffer.ReadOnly):
                try:  # bit b is a white bar in column b of BITS equal columns
                    width, height, stride = f.width(), f.height(), f.bytesPerLine()
                    start = (height // 2) * stride
                    if start + width * 4 > f.mappedBytes():
                        return True
                    ptr = f.bits()
                    ptr.setsize(f.mappedBytes())
                    row = bytes(ptr[start:start + stride])
                    self.last = sum(1 << b for b in range(BITS)
                                    if max(row[int((b + 0.5) * width / BITS) * 4:][:3]) > 125)
                finally:
                    f.unmap()
            return True

    original = QMediaPlayer.setVideoOutput
    original_mute, original_volume = QMediaPlayer.setMuted, QMediaPlayer.setVolume

    def offscreen_output(player, _widget):
        surface = Surface()
        _SURFACES[id(player)] = surface
        _KEEP_ALIVE.append(surface)
        original(player, surface)

    QMediaPlayer.setVideoOutput = offscreen_output
    QMediaPlayer.setMuted = lambda player, _muted: original_mute(player, True)
    QMediaPlayer.setVolume = lambda player, _volume: original_volume(player, 0)
    try:
        yield app
    finally:
        from PyQt5.QtCore import QCoreApplication, QEvent

        for _ in range(20):  # let players scheduled for deletion go before the next test starts
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            app.processEvents()
            cf.CFRunLoopRunInMode(mode, 0.005, True)
        QMediaPlayer.setVideoOutput = original
        QMediaPlayer.setMuted, QMediaPlayer.setVolume = original_mute, original_volume
        _SURFACES.clear()
        pump.stop()


_CURRENT_RUN: List[object] = [None]


def run_steps(app, steps: List[Tuple[int, Callable[[], object]]], timeout_ms: int = 60000) -> None:
    """Run (delay_ms, fn) steps strictly in order inside the event loop, then quit."""
    from PyQt5.QtCore import Qt, QTimer

    queue = list(steps)
    errors: List[BaseException] = []

    def run_next() -> None:
        if not queue or errors:
            app.quit()
            return
        delay, fn = queue.pop(0)

        def fire() -> None:
            try:
                fn()
            except BaseException as e:  # surface assertion failures from inside the loop
                errors.append(e)
            run_next()

        QTimer.singleShot(delay, Qt.PreciseTimer, fire)

    run_next()
    _CURRENT_RUN[0] = run = object()  # a leftover timeout of an earlier run must not end this one's loop
    QTimer.singleShot(timeout_ms, Qt.PreciseTimer, lambda: _CURRENT_RUN[0] is run and app.quit())
    app.exec_()
    if errors:
        raise errors[0]
    if queue:
        raise TimeoutError(f"{len(queue)} step(s) did not run")
