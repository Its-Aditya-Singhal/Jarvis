"""Measure the voice match: false accepts (other people let in) and false rejects (the owner
refused) at the Standard and Strict presets, with the same model, windows and top-k matching
JARVIS uses. Nothing is stored; recordings are read from the folders you give.

    backend/.venv/bin/python scripts/eval_voice.py --enroll ~/voice/enroll --owner ~/voice/owner \\
        --others ~/voice/others

--enroll   the owner's enrollment-style recordings (one phrase per .wav, ~20 of them)
--owner    other recordings of the owner (different days, rooms, volumes): these should be accepted
--others   anyone else (friends, family, or VoxCeleb clips): these should never be accepted

Any WAV works (mono or stereo, any sample rate; it is converted to 16 kHz mono). Clips can be
cut into 1-3 s pieces with --chunk to see how short commands fare. Recording the owner needs
the owner present and willing: never record anyone without asking.
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from jarvis.auth.matching import TemplateMatcher  # noqa: E402
from jarvis.auth.voice.engine import SpeakerEngine  # noqa: E402
from jarvis.auth.voice.verification import VoiceAuth  # noqa: E402
from jarvis.config import Settings  # noqa: E402
from jarvis.prefs import VOICE_PRESETS  # noqa: E402

SR = 16000


def load(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        n, ch, width, rate = w.getnframes(), w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(n)
    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}[width]
    x = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    if width == 1:
        x = x - 128.0
    x /= float(2 ** (8 * width - 1))
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if rate != SR:  # linear resampling is plenty for speaker embeddings
        t = np.arange(0, len(x) / rate, 1 / SR)
        x = np.interp(t, np.arange(len(x)) / rate, x).astype(np.float32)
    return x


def clips(folder: Path, chunk: float | None) -> list[tuple[str, np.ndarray]]:
    out = []
    for p in sorted(folder.rglob("*.wav")):
        x = load(p)
        if chunk:
            step = int(chunk * SR)
            out += [(f"{p.name}@{i // SR}s", x[i:i + step]) for i in range(0, len(x) - step + 1, step)]
        else:
            out.append((p.name, x))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--enroll", type=Path, required=True)
    ap.add_argument("--owner", type=Path, required=True)
    ap.add_argument("--others", type=Path, required=True)
    ap.add_argument("--chunk", type=float, default=None, help="cut test clips into pieces this many seconds long")
    args = ap.parse_args()

    engine = SpeakerEngine(Settings().models_dir)
    if not engine.load():
        print(engine.error)
        return 1
    template = [e for _, x in clips(args.enroll, None) for e in engine.embed_windows(x)]
    if not template:
        print("no enrollment recordings found")
        return 1
    matcher = TemplateMatcher(np.stack(template), top_k=Settings().voice_top_k)
    owner = [(n, matcher.similarity(engine.embed(x)), len(x) / SR) for n, x in clips(args.owner, args.chunk)]
    others = [(n, matcher.similarity(engine.embed(x)), len(x) / SR) for n, x in clips(args.others, args.chunk)]
    print(f"template: {len(template)} vectors from {args.enroll}; self-consistency {matcher.self_consistency():.3f}")
    print(f"owner clips: {len(owner)}, other clips: {len(others)}\n")
    print(f"{'preset':<10}{'accept':>8}{'reject':>8}  {'FRR (owner refused)':>20}  {'owner uncertain':>16}  "
          f"{'FAR (other accepted)':>21}  {'others rejected':>16}")
    for name, (acc, rej) in VOICE_PRESETS.items():
        auth = VoiceAuth(acc, rej)
        ov = [auth.judge(s, 0.9, 0.0, d).verdict for _, s, d in owner]
        xv = [auth.judge(s, 0.9, 0.0, d).verdict for _, s, d in others]
        pct = lambda v, k: 100.0 * v.count(k) / max(1, len(v))
        print(f"{name:<10}{acc:>8.2f}{rej:>8.2f}  {pct(ov, 'rejected'):>19.1f}%  {pct(ov, 'uncertain'):>15.1f}%  "
              f"{pct(xv, 'verified'):>20.1f}%  {pct(xv, 'rejected'):>15.1f}%")
    worst = sorted(others, key=lambda r: -r[1])[:5]
    print("\nclosest other voices:", ", ".join(f"{n} {s:.3f}" for n, s, _ in worst))
    weakest = sorted(owner, key=lambda r: r[1])[:5]
    print("weakest owner clips: ", ", ".join(f"{n} {s:.3f}" for n, s, _ in weakest))
    if others and owner:
        hi_other = max(s for _, s, _ in others)
        ok = sum(s > hi_other for _, s, _ in owner) / len(owner)
        print(f"\nan accept threshold just above the closest other voice ({hi_other:.3f}) would accept "
              f"{100 * ok:.0f}% of the owner's clips")
    return 0


if __name__ == "__main__":
    sys.exit(main())
