"""Microphone capture in the backend process (16 kHz mono float32).

Like the camera, audio is captured here so the UI can't inject recordings into
the verification pipeline. Blocks are kept in a short in-memory queue and
discarded after processing; nothing is written to disk.
"""

from __future__ import annotations

import logging
import queue
import threading

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BLOCK = 512  # 32 ms: the Silero VAD frame size at 16 kHz


def level_from_rms(rms: float) -> float:
    """Map RMS amplitude to a 0..1 display level (-60 dBFS .. -12 dBFS)."""
    db = 20 * np.log10(max(rms, 1e-9))
    return float(np.clip((db + 60) / 48, 0.0, 1.0))


def request_permission() -> bool:
    """Trigger the macOS microphone prompt from the main thread at startup."""
    try:
        import sounddevice as sd

        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK):
            sd.sleep(50)
        return True
    except Exception:
        return False


class Microphone:
    def __init__(self, device: int | str | None = None):
        self.device = device
        self.status = "off"  # off | starting | active | error
        self.error: str | None = None
        self.device_name: str | None = None
        self.level = 0.0
        self._q: queue.Queue[np.ndarray] = queue.Queue(maxsize=400)  # ~13 s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.status = "starting"
        self._thread = threading.Thread(target=self._supervise, name="mic", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self.status = "off"
        self.level = 0.0

    def read(self, timeout: float = 0.5) -> np.ndarray | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> None:
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def _callback(self, indata, frames, time_info, status) -> None:
        block = indata[:, 0].copy()
        self.level = level_from_rms(float(np.sqrt(np.mean(block**2))))
        try:
            self._q.put_nowait(block)
        except queue.Full:
            # consumer fell behind; drop the oldest audio rather than block the device
            try:
                self._q.get_nowait()
                self._q.put_nowait(block)
            except queue.Empty:
                pass

    def _supervise(self) -> None:
        """Keep a stream open; reopen after errors or device changes (e.g. AirPods)."""
        import sounddevice as sd

        while not self._stop.is_set():
            try:
                with sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=1,
                    dtype="float32",
                    blocksize=BLOCK,
                    device=self.device,
                    callback=self._callback,
                ) as stream:
                    info = sd.query_devices(stream.device, "input")
                    self.device_name = str(info["name"])
                    self.status = "active"
                    self.error = None
                    while not self._stop.is_set() and stream.active:
                        self._stop.wait(0.5)
            except Exception as exc:
                log.warning("microphone unavailable: %s", exc)
                self.status = "error"
                self.error = "Microphone unavailable. Check System Settings → Privacy & Security → Microphone."
                self.level = 0.0
            if not self._stop.is_set():
                self._stop.wait(2.0)
        self.status = "off"
