"""Camera capture thread lifecycle, with a scripted OpenCV capture (no real camera)."""

import threading
import time

import cv2
import numpy as np

from jarvis.camera import capture


class SlowCapture:
    """Each read takes a while, like a real camera waiting for its next frame."""

    opened = 0

    def __init__(self, *args, delay=0.3):
        SlowCapture.opened += 1
        self.delay = delay

    def isOpened(self):
        return True

    def set(self, *args):
        return True

    def read(self):
        time.sleep(self.delay)
        return True, np.random.default_rng().integers(0, 255, (8, 8, 3), dtype=np.uint8)

    def release(self):
        pass


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_start_right_after_a_background_stop_leaves_the_camera_running(monkeypatch):
    monkeypatch.setattr(cv2, "VideoCapture", SlowCapture)
    cam = capture.Camera()
    cam.start()
    assert _wait(lambda: cam.status == "active")
    # face once switches the camera off on a background thread; a setting change turns it
    # straight back on while that stop is still waiting for the frame being read
    stopper = threading.Thread(target=cam.stop)
    stopper.start()
    time.sleep(0.05)
    cam.start()
    stopper.join()
    time.sleep(0.8)  # the old thread has exited by now
    assert cam.status == "active"
    assert cam.latest() is not None
    cam.stop()


def test_no_frame_or_active_status_after_stop(monkeypatch):
    monkeypatch.setattr(cv2, "VideoCapture", SlowCapture)
    cam = capture.Camera()
    cam.start()
    assert _wait(lambda: cam.latest() is not None)
    cam.stop()
    time.sleep(0.5)  # a read that was in flight when stop() returned
    assert cam.status == "off" and cam.latest() is None
