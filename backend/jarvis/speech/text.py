"""Script-independent text matching for English, Hindi and Hinglish.

Whisper may write the same Hindi/Hinglish words in Devanagari or in Latin
letters, and romanisations vary ("jagana" / "jagAnA", "subah" / "subaha").
Comparisons therefore go through:
  * ``to_latin``  — Devanagari transliterated to plain lowercase Latin
  * ``skeleton``  — consonant skeleton of each word, which erases the
                    vowel-length and schwa differences between romanisations
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_SENTENCE = re.compile(r"(?<=[.!?।])\s+")
# romanisation variants folded together before building skeletons
_FOLD = [("ph", "f"), ("bh", "b"), ("dh", "d"), ("th", "t"), ("kh", "k"), ("gh", "g"),
         ("jh", "j"), ("ch", "c"), ("sh", "s"), ("q", "k"), ("w", "v"), ("z", "j"),
         ("c", "k"), ("x", "ks")]


def has_devanagari(text: str) -> bool:
    return bool(_DEVANAGARI.search(text))


def to_latin(text: str) -> str:
    if has_devanagari(text):
        from indic_transliteration import sanscript
        from indic_transliteration.sanscript import transliterate

        text = transliterate(text, sanscript.DEVANAGARI, sanscript.ITRANS)
        # ITRANS marks: long vowels as capitals/doubles, nasalisation, visarga
        text = text.replace(".N", "n").replace(".n", "n").replace("M", "n").replace("H", "h")
        text = text.replace("~N", "n").replace("~n", "n").replace(".D", "d").replace(".a", "")
        # schwa deletion: word-final inherent 'a' is not pronounced in Hindi
        text = re.sub(r"(?<=[^aeiouAEIOU\s])a\b", "", text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower()
    return re.sub(r"[^a-z0-9' ]+", " ", text).strip()


def words(text: str) -> list[str]:
    return [w for w in to_latin(text).replace("'", "").split() if w]


def skeleton_word(word: str) -> str:
    for a, b in _FOLD:
        word = word.replace(a, b)
    if not word:
        return ""
    head, rest = word[0], re.sub(r"[aeiouy]", "", word[1:])
    s = head + rest
    return re.sub(r"(.)\1+", r"\1", s)  # collapse doubled letters


def skeleton(text: str) -> str:
    return " ".join(skeleton_word(w) for w in words(text))


def phrase_match(expected: str, heard: str) -> float:
    """0..1 similarity of what was heard to what should have been said."""
    if not heard.strip():
        return 0.0
    a, b = skeleton(expected), skeleton(heard)
    return max(fuzz.token_set_ratio(a, b), fuzz.ratio(a, b)) / 100.0


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text.strip()) if s.strip()]
