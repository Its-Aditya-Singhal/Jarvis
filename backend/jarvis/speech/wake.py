"""Wake word = the assistant's chosen name, found in the transcript.

No keyword model is trained per name: every utterance is transcribed locally
anyway, so the wake word is matched in text. The name is also given to
Whisper as a prompt so unusual names are spelled the way the user chose.
Matching tolerates small spelling/script differences ("Jarvis" / "जार्विस").
"""

from __future__ import annotations

from rapidfuzz import fuzz

from .text import skeleton_word, to_latin

FILLERS = {"hey", "hi", "ok", "okay", "hello", "oh", "arre", "are", "suno", "a"}


def find_wake(name: str, text: str, threshold: float = 80.0) -> tuple[bool, str]:
    """Returns (found, command): the command is what follows the name.

    The name must be among the first words (after fillers like "hey"), which
    keeps "I told Friday about it" in conversation from waking the assistant.
    """
    target = to_latin(name).replace(" ", "")
    if not target:
        return False, ""
    tokens = text.split()
    latin = [to_latin(t).replace(" ", "") for t in tokens]
    n_name = max(1, len(to_latin(name).split()))
    lead = 0
    while lead < len(latin) and latin[lead] in FILLERS:
        lead += 1
    for start in range(lead, min(lead + 2, len(tokens))):
        for width in {n_name, n_name + 1, 1}:
            cand = "".join(latin[start : start + width])
            if not cand:
                continue
            score = fuzz.ratio(cand, target)
            sk_a, sk_b = skeleton_word(cand), skeleton_word(target)
            if score >= threshold or (len(sk_b) >= 3 and sk_a == sk_b):
                rest = " ".join(tokens[start + width :]).strip(" ,.!?;:-—।")
                return True, rest
    return False, ""
