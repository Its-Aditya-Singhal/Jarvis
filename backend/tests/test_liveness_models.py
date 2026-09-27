"""Passive liveness and eye tracking with the real models on sample photos."""

import cv2
import numpy as np
import pytest

from conftest import SAMPLES

pytestmark = pytest.mark.models


@pytest.fixture(scope="module")
def engine(face_engine):
    if not face_engine.liveness.ready:
        pytest.skip("liveness model not downloaded")
    return face_engine


def test_direct_photos_score_live(engine):
    faces = engine.analyze(cv2.imread(str(SAMPLES / "t1.jpg")))
    scores = [f.live_score for f in faces if f.live_score is not None]
    assert len(scores) >= 5 and min(scores) > 0.6


def test_recaptured_face_scores_as_spoof(engine):
    # simulate a re-captured screen: resolution loss, scanlines, gamma/brightness shift
    img = cv2.imread(str(SAMPLES / "t1.jpg"))
    rec = cv2.resize(cv2.resize(img, None, fx=0.35, fy=0.35), (img.shape[1], img.shape[0]))
    rec = rec.astype(np.float32)
    rec[::3] *= 0.85
    rec = np.clip(rec * 1.1 + 15, 0, 255).astype(np.uint8)
    scores = [f.live_score for f in engine.analyze(rec) if f.live_score is not None]
    assert scores and np.median(scores) < 0.25


def test_eye_openness_drops_when_eyes_are_covered(engine):
    # webcam-sized face: upscale the bundled crop and give the detector context
    img = cv2.resize(cv2.imread(str(SAMPLES / "Tom_Hanks_54745.png")), (224, 224))
    img = cv2.copyMakeBorder(img, 120, 120, 160, 160, cv2.BORDER_REPLICATE)
    f = engine.analyze(img)[0]
    closed = img.copy()
    lm = f.landmarks
    for a, b in ((36, 42), (42, 48)):  # paint skin over each eye, draw a lid line
        x, y, w, h = cv2.boundingRect(lm[a:b].astype(np.int32))
        skin = img[y + h + 6 : y + h + 10, x : x + w].reshape(-1, 3).mean(axis=0)
        cv2.ellipse(closed, (x + w // 2, y + h // 2), (w // 2 + 3, h // 2 + 4), 0, 0, 360, skin.tolist(), -1)
        cv2.line(closed, (x, y + h // 2 + 1), (x + w, y + h // 2 + 1), (40, 40, 50), 1)
    g = engine.analyze(closed)[0]
    assert f.eye_open is not None and g.eye_open is not None
    assert g.eye_open < f.eye_open * 0.6  # well below the blink detector's 0.75 close ratio
