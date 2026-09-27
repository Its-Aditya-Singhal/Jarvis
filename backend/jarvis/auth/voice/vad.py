"""Voice activity detection (Silero VAD v5, ONNX) and utterance segmentation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

SR = 16000
FRAME = 512
CONTEXT = 64  # Silero v5 expects the last 64 samples of the previous frame prepended


class SileroVAD:
    def __init__(self, model_path: str | None = None):
        import onnxruntime as ort

        if model_path is None:
            import silero_vad

            model_path = os.path.join(os.path.dirname(silero_vad.__file__), "data", "silero_vad.onnx")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self._sess = ort.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT), dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        """Speech probability for one 512-sample frame."""
        x = np.concatenate([self._context, frame.reshape(1, -1).astype(np.float32)], axis=1)
        out, self._state = self._sess.run(
            None, {"input": x, "state": self._state, "sr": np.array(SR, dtype=np.int64)}
        )
        self._context = x[:, -CONTEXT:]
        return float(out[0, 0])


@dataclass
class Utterance:
    audio: np.ndarray  # float32, 16 kHz, includes a short pre-roll
    speech_s: float  # seconds of frames classified as speech
    noise_rms: float  # background level measured before the utterance


@dataclass
class Segmenter:
    """Turns a stream of (frame, speech-probability) into complete utterances."""

    start_threshold: float = 0.5
    end_threshold: float = 0.35
    min_speech_s: float = 0.3  # drops clicks; callers apply their own minimums
    end_silence_s: float = 0.45  # short: commands are acted on as soon as you stop
    max_s: float = 12.0
    preroll_frames: int = 6

    _active: bool = False
    _frames: list[np.ndarray] = field(default_factory=list)
    _preroll: list[np.ndarray] = field(default_factory=list)
    _silence: int = 0
    _voiced: int = 0
    _noise_rms: float = 0.01

    @property
    def active(self) -> bool:
        return self._active

    def reset(self) -> None:
        self._active = False
        self._frames = []
        self._preroll = []
        self._silence = 0
        self._voiced = 0

    def push(self, frame: np.ndarray, prob: float) -> Utterance | None:
        frame_s = len(frame) / SR
        if not self._active:
            rms = float(np.sqrt(np.mean(frame**2)))
            if prob < self.end_threshold:
                # track the background noise floor between utterances
                self._noise_rms = 0.95 * self._noise_rms + 0.05 * rms
            self._preroll.append(frame)
            if len(self._preroll) > self.preroll_frames:
                self._preroll.pop(0)
            if prob >= self.start_threshold:
                self._active = True
                self._frames = list(self._preroll)
                self._voiced = 1
                self._silence = 0
            return None

        self._frames.append(frame)
        if prob >= self.end_threshold:
            self._voiced += 1
            self._silence = 0
        else:
            self._silence += 1

        total_s = len(self._frames) * frame_s
        if self._silence * frame_s >= self.end_silence_s or total_s >= self.max_s:
            speech_s = self._voiced * frame_s
            audio = np.concatenate(self._frames)
            noise = self._noise_rms
            self.reset()
            if speech_s >= self.min_speech_s:
                return Utterance(audio=audio, speech_s=speech_s, noise_rms=noise)
        return None
