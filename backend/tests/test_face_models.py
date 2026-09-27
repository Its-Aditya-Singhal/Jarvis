"""Checks with the real InsightFace models on bundled sample photos."""

import cv2
import numpy as np
import pytest

from conftest import SAMPLES
from jarvis.auth.matching import TemplateMatcher

pytestmark = pytest.mark.models


def _portrait():
    # the bundled sample is a tight 112x112 crop; give the detector context
    img = cv2.imread(str(SAMPLES / "Tom_Hanks_54745.png"))
    img = cv2.resize(img, (224, 224))
    return cv2.copyMakeBorder(img, 120, 120, 160, 160, cv2.BORDER_REPLICATE)


def test_different_people_do_not_match(face_engine):
    img = cv2.imread(str(SAMPLES / "t1.jpg"))
    faces = face_engine.analyze(img)
    assert len(faces) >= 5
    embs = np.stack([f.embedding for f in faces])
    sims = embs @ embs.T
    np.fill_diagonal(sims, 0)
    assert sims.max() < 0.25  # every pair of different people is below the reject threshold


def test_same_person_matches_under_augmentation(face_engine):
    img = _portrait()
    base = face_engine.analyze(img)[0]
    variants = [
        cv2.flip(img, 1),
        cv2.convertScaleAbs(img, alpha=0.7, beta=-20),  # darker
        cv2.GaussianBlur(img, (5, 5), 0),
    ]
    template = np.stack([face_engine.analyze(v)[0].embedding for v in variants])
    v = TemplateMatcher(template, top_k=2)
    assert v.similarity(base.embedding) > 0.6

    others = face_engine.analyze(cv2.imread(str(SAMPLES / "t1.jpg")))
    assert max(v.similarity(o.embedding) for o in others) < 0.25


def test_pose_is_reported(face_engine):
    img = _portrait()
    f = face_engine.analyze(img)[0]
    assert -90 < f.yaw < 90 and -90 < f.pitch < 90
    assert f.landmarks is not None and f.landmarks.shape == (68, 2)
    assert 0 <= f.quality <= 1
