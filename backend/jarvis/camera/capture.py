"""Camera capture in the backend process.

Frames are captured here rather than in the UI so the authentication pipeline
cannot be fed frames by the frontend. Only the most recent frame is kept in
memory; nothing is written to disk.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

import cv2
import numpy as np

log = logging.getLogger(__name__)


def request_permission(index: int = 0) -> bool:
    """Trigger the macOS camera permission prompt from the main thread.

    AVFoundation can only show the prompt from the main run loop; the capture
    thread would otherwise fail silently on first launch. Blocks until the
    user answers. Returns whether a frame could be read.
    """
    if sys.platform != "darwin":
        return True
    cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
    try:
        return cap.isOpened() and cap.read()[0]
    finally:
        cap.release()


class Camera:
    def __init__(self, index: int = 0):
        self.index = index
        self.status = "off"  # off | starting | active | error
        self.error: str | None = None
        self._frame: np.ndarray | None = None
        self._frame_t = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._life = threading.Lock()  # start() vs stop()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._life:
            if self._thread and self._thread.is_alive():
                if not self._stop.is_set():
                    return  # already running
                # still shutting down (stop() timed out, or it runs on another thread):
                # wait for it, or the old thread would exit and leave the camera off
                self._thread.join(timeout=3)
                if self._thread.is_alive():
                    log.warning("previous camera thread still running; starting a new one")
            stop = self._stop = threading.Event()
            self.status = "starting"
            self._thread = threading.Thread(target=self._run, args=(stop,), name="camera", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._life:
            self._stop.set()
            thread = self._thread
        if thread:
            thread.join(timeout=2)
        with self._life:
            if self._thread is thread:  # not restarted meanwhile
                self.status = "off"
                with self._lock:
                    self._frame = None

    def latest(self, max_age_s: float = 1.0) -> np.ndarray | None:
        with self._lock:
            if self._frame is None or time.monotonic() - self._frame_t > max_age_s:
                return None
            return self._frame

    def _open(self) -> cv2.VideoCapture | None:
        backend = cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.index, backend)
        if not cap.isOpened():
            cap.release()
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        cap.set(cv2.CAP_PROP_FPS, 15)  # analysis runs at 4-12 fps; fewer frames = less CPU and power
        return cap

    def _run(self, stop: threading.Event) -> None:
        # each thread has its own stop event: a restart can't revive a thread being stopped
        cap = None
        failures = 0
        while not stop.is_set():
            if cap is None:
                cap = self._open()
                if cap is None:
                    self.status = "error"
                    self.error = (
                        "Camera unavailable. Check System Settings → Privacy & Security → Camera."
                    )
                    stop.wait(3.0)
                    continue
            ok, frame = cap.read()
            if not ok or frame is None:
                failures += 1
                if failures > 30:
                    cap.release()
                    cap = None
                    failures = 0
                time.sleep(0.03)
                continue
            failures = 0
            with self._lock:
                if stop.is_set():
                    break  # stopped while reading: this frame must not reappear after stop()
                self.status = "active"
                self.error = None
                self._frame = frame
                self._frame_t = time.monotonic()
        if cap is not None:
            cap.release()
