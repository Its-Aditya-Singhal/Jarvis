"""Audio quality scoring for an utterance (0..1). Pure functions."""

from __future__ import annotations

import numpy as np


def _ramp(x: float, lo: float, hi: float) -> float:
    return float(np.clip((x - lo) / (hi - lo), 0.0, 1.0))


def audio_quality(audio: np.ndarray, speech_s: float, noise_rms: float) -> dict:
    rms = float(np.sqrt(np.mean(audio**2))) if audio.size else 0.0
    level_db = 20 * np.log10(max(rms, 1e-9))
    snr_db = 20 * np.log10(max(rms, 1e-9) / max(noise_rms, 1e-6))
    clipped = float(np.mean(np.abs(audio) > 0.98)) if audio.size else 0.0

    parts = {
        "duration": _ramp(speech_s, 0.6, 2.2),
        "snr": _ramp(snr_db, 4.0, 20.0),
        "level": _ramp(level_db, -52.0, -34.0),
        "clipping": 1.0 - _ramp(clipped, 0.001, 0.02),
    }
    vals = np.array(list(parts.values())) + 1e-6
    return {
        "score": float(np.exp(np.log(vals).mean())),
        "speech_s": round(speech_s, 2),
        "snr_db": round(snr_db, 1),
        "parts": parts,
    }
