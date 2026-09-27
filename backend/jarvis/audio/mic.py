"""Microphone capture in the backend process (16 kHz mono float32).

Like the camera, audio is captured here so the UI can't inject recordings into
the verification pipeline. Blocks are kept in a short in-memory queue and
discarded after processing; nothing is written to disk.

When the microphone doesn't work, ``cause`` says why so the owner gets a fix
that matches: macOS permission (denied, or only digital silence arriving —
what macOS delivers to an app it hasn't allowed), no input device, the chosen
device unplugged, or the device failing to open (``detail`` keeps the raw
error).
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BLOCK = 512  # 32 ms: the Silero VAD frame size at 16 kHz
SILENT_AFTER_S = 3.0  # this long of exact zeros means macOS is withholding the audio

# why the microphone isn't working -> what the owner reads
CAUSES = {
    "permission": "macOS hasn't allowed microphone access",
    "silent": "The microphone only sends silence — it may be muted, a virtual device, or blocked by macOS",
    "no_device": "No microphone found",
    "device_missing": "The chosen microphone isn't connected",
    "open_failed": "The microphone couldn't be opened",
}


def level_from_rms(rms: float) -> float:
    """Map RMS amplitude to a 0..1 display level (-60 dBFS .. -12 dBFS)."""
    db = 20 * np.log10(max(rms, 1e-9))
    return float(np.clip((db + 60) / 48, 0.0, 1.0))


def permission() -> str:
    """macOS microphone permission of this process (really: of the app that launched
    it): granted | denied | restricted | not_asked | unknown."""
    if sys.platform != "darwin":
        return "unknown"
    try:
        import AVFoundation as av

        st = int(av.AVCaptureDevice.authorizationStatusForMediaType_(av.AVMediaTypeAudio))
    except Exception:
        return "unknown"
    return {0: "not_asked", 1: "restricted", 2: "denied", 3: "granted"}.get(st, "unknown")


def input_devices() -> list[dict]:
    """Input devices as [{"name", "default"}]; empty when none (or no audio system)."""
    try:
        import sounddevice as sd

        default = sd.default.device[0]
        return [{"name": str(d["name"]), "default": i == default}
                for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
    except Exception as exc:
        log.warning("can't list audio devices: %s", exc)
        return []


def classify(exc: Exception, wanted: str | int | None) -> str:
    """The cause behind an error opening the input stream."""
    msg = str(exc).lower()
    if wanted is not None and ("no input device matching" in msg or "invalid device" in msg or "-9996" in msg):
        return "device_missing"
    if "no default input" in msg or "-9996" in msg:
        return "no_device"
    return "open_failed"


def request_permission() -> bool:
    """Trigger the macOS microphone prompt from the main thread at startup."""
    if permission() == "not_asked":
        try:
            import AVFoundation as av

            done = threading.Event()
            av.AVCaptureDevice.requestAccessForMediaType_completionHandler_(av.AVMediaTypeAudio,
                                                                            lambda ok: done.set())
            done.wait(60)  # the owner answers the macOS prompt
        except Exception:
            pass
    try:
        import sounddevice as sd

        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK):
            sd.sleep(50)
        return True
    except Exception:
        return False


class Microphone:
    def __init__(self, device: int | str | None = None):
        self.device = device or None
        self.status = "off"  # off | starting | active | error
        self.cause: str | None = None  # a CAUSES key while status == "error"
        self.error: str | None = None
        self.detail: str | None = None  # the raw error, for the curious
        self.device_name: str | None = None
        self.level = 0.0
        self._q: queue.Queue[np.ndarray] = queue.Queue(maxsize=400)  # ~13 s
        self._stop = threading.Event()
        self._reopen = threading.Event()
        self._thread: threading.Thread | None = None
        self._zeros_since: float | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.status = "starting"
        self._thread = threading.Thread(target=self._supervise, name="mic", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._reopen.set()  # wake the supervisor from its retry wait
        if self._thread:
            self._thread.join(timeout=3)
        self.status = "off"
        self.level = 0.0

    def use(self, device: str | None) -> None:
        """Switch to another input device (None: the system default) now."""
        self.device = device or None
        self.retry()

    def retry(self) -> None:
        """Reopen the stream right away (after granting permission, plugging a mic in…)."""
        self._reopen.set()

    def info(self) -> dict:
        chosen = self.device if isinstance(self.device, str) else None
        return {"status": self.status, "cause": self.cause, "error": self.error, "detail": self.detail,
                "device": self.device_name, "chosen": chosen,
                # the chosen mic is unplugged and the default is used meanwhile
                "fallback": bool(chosen and self.status == "active" and self.device_name != chosen)}

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

    def _fail(self, cause: str, detail: str | None = None) -> None:
        self.status, self.cause, self.error, self.detail = "error", cause, CAUSES[cause], detail
        self.level = 0.0

    def _track_silence(self, block: np.ndarray) -> None:
        """A real microphone always has some noise; a stream of exact zeros is macOS
        withholding the audio (permission) or a muted/virtual device."""
        if np.any(block):
            self._zeros_since = None
            if self.status == "error" and self.cause == "silent":
                self.status, self.cause, self.error, self.detail = "active", None, None, None
            return
        now = time.monotonic()
        if self._zeros_since is None:
            self._zeros_since = now
        elif now - self._zeros_since >= SILENT_AFTER_S and self.status == "active":
            p = permission()
            self._fail("permission" if p in ("denied", "restricted") else "silent", f"macOS permission: {p}")
            log.warning("microphone delivers only silence (permission: %s)", p)

    def _callback(self, indata, frames, time_info, status) -> None:
        block = indata[:, 0].copy()
        self._track_silence(block)
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

    def _open_once(self, sd) -> None:
        devices = input_devices()
        if not devices:
            self._fail("no_device")
            return
        if (p := permission()) in ("denied", "restricted"):
            self._fail("permission", f"macOS permission: {p}")
            return
        # a chosen mic that isn't plugged in: use the system default meanwhile
        device = self.device
        if isinstance(device, str) and device not in {d["name"] for d in devices}:
            device = None
        try:
            stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK,
                                    device=device, callback=self._callback)
        except Exception as exc:
            cause = classify(exc, device)
            log.warning("microphone unavailable (%s): %s", cause, exc)
            self._fail(cause, str(exc))
            return
        with stream:
            self.device_name = str(sd.query_devices(stream.device, "input")["name"])
            self.status, self.cause, self.error, self.detail = "active", None, None, None
            self._zeros_since = None
            while not self._stop.is_set() and not self._reopen.is_set() and stream.active:
                self._stop.wait(0.5)
            if not stream.active and not self._stop.is_set():
                log.warning("microphone stream stopped (device unplugged?)")

    def _supervise(self) -> None:
        """Keep a stream open; reopen after errors or device changes (e.g. AirPods)."""
        import sounddevice as sd

        while not self._stop.is_set():
            self._reopen.clear()
            try:
                self._open_once(sd)
            except Exception as exc:
                log.warning("microphone error: %s", exc)
                self._fail("open_failed", str(exc))
            if self._stop.is_set() or self._reopen.is_set():
                continue
            self._reopen.wait(2.0)  # a retry or device switch skips the wait
        self.status = "off"
