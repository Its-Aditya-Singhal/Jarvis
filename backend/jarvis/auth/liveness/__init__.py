"""Liveness: is the face in front of the camera a live person?

Identity (who) is decided by the face matcher. This package decides whether
that face is live rather than a photo, a screen or a replayed video:

* ``passive``    — texture anti-spoof CNN on every frame
* ``blink``      — eye-openness tracking from the 106-point landmarks
* ``challenges`` — randomised challenge-response (blink / turn / move closer)
* ``replay``     — frozen-feed detection for looped or virtual video sources
* ``gate``       — combines the above into the session's liveness state
"""
