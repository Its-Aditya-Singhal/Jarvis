"""Math, unit conversions and date arithmetic, answered instantly without the model.

A small local language model gets arithmetic wrong often enough that sums,
percentages, conversions and "how many days till …" are worked out here
instead. ``answer`` returns the spoken reply when the WHOLE utterance is one
of these questions, and None otherwise (the request then goes on as before).
Expressions are evaluated from a parsed syntax tree with a fixed set of
operators; nothing is ever passed to ``eval``. No currencies: rates need the
internet.
"""

from __future__ import annotations

import ast
import calendar
import math
import operator
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta

from ..speech.text import has_devanagari, to_latin

# -- numbers --------------------------------------------------------------------------
_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_HI_UNITS = {
    "ek": 1, "do": 2, "teen": 3, "tin": 3, "char": 4, "chaar": 4, "panch": 5, "paanch": 5, "panc": 5, "paanc": 5,
    "chhe": 6, "chhah": 6, "chah": 6, "saat": 7, "sat": 7, "aath": 8, "ath": 8, "nau": 9, "das": 10,
    "gyarah": 11, "barah": 12, "baarah": 12, "terah": 13, "chaudah": 14, "pandrah": 15, "solah": 16,
    "satrah": 17, "atharah": 18, "unnis": 19, "bees": 20, "bis": 20, "pachchis": 25, "tees": 30, "tis": 30,
    "chalis": 40, "pachas": 50, "saath": 60, "sattar": 70, "assi": 80, "nabbe": 90,
}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 10**6, "billion": 10**9, "lakh": 10**5, "lakhs": 10**5,
           "crore": 10**7, "crores": 10**7}
_HI_SCALES = {"sau": 100, "hazaar": 1000, "hazar": 1000, "hajar": 1000, "lakh": 10**5, "crore": 10**7, "karod": 10**7}
_DIGIT_WORDS = {w: v for w, v in _UNITS.items() if v < 10} | {"oh": 0}


def _words_to_numbers(t: str, hi: bool) -> str:
    """'two hundred and fifty' -> '250', '3 point 5' -> '3.5', 'a thousand' -> '1000'."""
    units = _UNITS | (_HI_UNITS if hi else {})
    scales = _SCALES | (_HI_SCALES if hi else {})
    toks = t.split()
    out: list[str] = []
    i = 0
    while i < len(toks):
        w = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        starts = (w in units or re.fullmatch(r"\d+(\.\d+)?", w) is not None
                  or (w in ("a", "an") and nxt in scales))
        if not starts:
            out.append(w)
            i += 1
            continue
        total, current, seen = 0.0, 0.0, False
        while i < len(toks):
            w = toks[i]
            if w in ("a", "an") and not seen and i + 1 < len(toks) and toks[i + 1] in scales:
                current, seen = 1, True
            elif re.fullmatch(r"\d+(\.\d+)?", w) and not seen:
                current, seen = float(w), True
            elif w in units and (not seen or (current % 100 == 0 and units[w] < 100)
                                 or (current % 10 == 0 and current % 100 != 0 and units[w] < 10 and current >= 20)):
                current += units[w]
                seen = True
            elif w in scales and seen:
                if scales[w] == 100:
                    current = (current or 1) * 100
                else:
                    total += (current or 1) * scales[w]
                    current = 0
            elif w == "and" and seen and i + 1 < len(toks) and toks[i + 1] in units and (current % 100 == 0 or total):
                pass
            else:
                break
            i += 1
        value = total + current
        # "3 point 1 4" -> 3.14
        if i + 1 < len(toks) and toks[i] == "point" and (toks[i + 1] in _DIGIT_WORDS or toks[i + 1].isdigit()):
            frac = ""
            i += 1
            while i < len(toks) and (toks[i] in _DIGIT_WORDS or re.fullmatch(r"\d", toks[i]) or (toks[i].isdigit() and not frac)):
                frac += str(_DIGIT_WORDS[toks[i]]) if toks[i] in _DIGIT_WORDS else toks[i]
                i += 1
            value = float(f"{int(value)}.{frac}")
        out.append(_plain(value))
    return " ".join(out)


def _plain(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else repr(float(x))


_BIG_NAMES = [(10**30, "nonillion"), (10**27, "octillion"), (10**24, "septillion"), (10**21, "sextillion"),
           (10**18, "quintillion"), (10**15, "quadrillion")]


def _spoken_big(x: float) -> str:
    """1e20 -> '100 quintillion'; beyond the named scales, or tiny, '1.23 times 10 to the power of -5'
    (the speech engine read '1e+20' out letter by letter)."""
    for size, name in _BIG_NAMES:
        if abs(x) >= size and abs(x) < size * 1000:
            return f"{float(f'{x / size:.4g}'):g} {name}"
    mant, exp = f"{x:.2e}".split("e")
    return f"{float(mant):g} times 10 to the power of {int(exp)}"


def fmt(x: float, places: int = 4) -> tuple[str, bool]:
    """A number for speaking, and whether it was rounded."""
    if isinstance(x, float):
        x = float(f"{x:.12g}")  # 200 * 1.1 is 220, not 220.00000000000003
    if isinstance(x, int) or float(x).is_integer():
        if abs(x) < 1e15:
            return f"{int(x):,}", False
        return _spoken_big(x), True
    if abs(x) >= 1e15:
        return _spoken_big(x), True
    if abs(x) < 1e-4:
        short = f"{x:.10f}".rstrip("0")  # 0.000001 reads fine; 1.234567e-7 doesn't
        return (short, False) if abs(x) >= 1e-9 and float(short) == x else (_spoken_big(x), True)
    digits = 2 if abs(x) >= 100 else places if abs(x) >= 1 else 4
    s = f"{x:,.{digits}f}".rstrip("0").rstrip(".")
    return s, float(s.replace(",", "")) != x


# -- text clean-up --------------------------------------------------------------------
_HI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def normalize(text: str, hi: bool) -> str:
    t = text.translate(_HI_DIGITS)
    t = t.replace("×", " * ").replace("÷", " / ").replace("−", "-").replace("’", "'").replace("°", " degrees ")
    t = re.sub(r"[$€£₹]", "", t)
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)  # 1,000
    if has_devanagari(t):
        t = to_latin(t)
    t = t.lower()
    t = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", t)  # twenty-five
    t = re.sub(r"(\d)\s*%", r"\1 percent ", t)
    t = re.sub(r"(?<=\d)(?!(?:st|nd|rd|th)\b)(?=[a-z])", " ", t)  # 5km -> 5 km, but not 25th
    t = re.sub(r"\bper cent\b", "percent", t)
    t = re.sub(r"[?!]+$|\.+$", "", t.strip())
    t = re.sub(r"(?<![\d])\.(?!\d)", " ", t)
    t = re.sub(r"[^a-z0-9'+\-*/^().\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^(?:hey |ok |okay |so |jarvis |please |quick(?:ly)? |can you |could you |would you |will you |"
               r"tell me |do you know |i want to know |zara |jara )+", "", t)
    t = re.sub(r"(?: please| pls| jarvis| quickly| for me)+$", "", t)
    return _words_to_numbers(t, hi or re.search(_HI_MARK, t) is not None)


_HI_MARK = r"\b(?:kitna|kitana|kitne|kitni|guna|gunaa|jama|ghata|bata|bhag|hota|hote|hua|mein|ka|ke|ki|ko|din|baad|pehle)\b"
NUM = r"-?\d+(?:\.\d+)?"


# -- arithmetic ------------------------------------------------------------------------
_OPS: dict[type, Callable[..., float]] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow, ast.Mod: operator.mod,
}


class CalcError(ValueError):
    pass


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _eval(node.operand)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        a, b = _eval(node.left), _eval(node.right)
        if isinstance(node.op, (ast.Div, ast.Mod)) and b == 0:
            raise ZeroDivisionError
        if isinstance(node.op, ast.Pow) and (abs(b) > 1000 or (abs(a) > 1e6 and abs(b) > 50)):
            raise CalcError("too big")
        r = _OPS[type(node.op)](a, b)
        if isinstance(r, complex) or (isinstance(r, float) and not math.isfinite(r)) or (isinstance(r, int) and abs(r) > 10**30):
            raise CalcError("out of range")
        return r
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "sqrt" and len(node.args) == 1 and not node.keywords:
        v = _eval(node.args[0])
        if v < 0:
            raise CalcError("negative root")
        r = math.sqrt(v)
        return int(r) if r.is_integer() else r
    raise CalcError("unsupported")


def evaluate(expr: str) -> float:
    """Evaluate +, -, *, /, %, ** and sqrt() on numbers only."""
    if len(expr) > 200:
        raise CalcError("too long")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise CalcError("syntax") from exc
    r = _eval(tree)
    if isinstance(r, float) and r.is_integer() and abs(r) < 1e15:
        return int(r)
    return r


_WORD_OPS = [
    (r"\bmultiplied by\b|\btimes\b|\binto\b|\bguna\b|\bgunaa\b|\bgun\b|(?<=\d) ?x ?(?=[\d(-])", " * "),
    (r"\bdivided by\b|\bdivide by\b|\bover\b|\bbata\b|\bbate\b|\bbhag\b|\bbhaag\b", " / "),
    (r"\bplus\b|\bjama\b|\bjod\b|\band\b", " + "),
    (r"\bminus\b|\bghata\b|\bghatao\b|\bless\b", " - "),
    (r"\bto the power of\b|\braised to(?: the power of)?\b|\bpower\b|\^", " ** "),
    (r"\bmod(?:ulo)?\b", " % "),
    (r"\bsquared\b", " ** 2 "), (r"\bcubed\b", " ** 3 "),
    (r"\bopen (?:bracket|parenthesis)\b", " ( "), (r"\bclose (?:bracket|parenthesis)\b", " ) "),
]
_SAY = {"+": "plus", "-": "minus", "*": "times", "/": "divided by", "**": "to the power of", "%": "mod"}
_SAY_HI = {"+": "जमा", "-": "घटा", "*": "गुणा", "/": "भाग", "**": "की घात", "%": "mod"}

_ASK_EN = (r"(?:what(?:'s| is| s|s)|how much (?:is|are)|calculate|compute|work out|solve|figure out|find|what does|what do)")
_ASK_HI_TAIL = (r"(?: (?:kitna|kitana|kitne|kitane|kitni|kya))?(?: (?:hota hai|hote hain|hota h|hua|huaa|hue|hoga|hogi|hai|hain|aata hai|ata hai|aayega|batao|bolo|bata do))+")


def _strip_question(t: str) -> str:
    t = re.sub(rf"^{_ASK_EN} ", "", t)
    t = re.sub(r" (?:equals?|equal to|is equal to|make|makes|come to|comes to|give|gives)(?: what| how much)?$", "", t)
    t = re.sub(r"^(?:the )?(?:answer (?:to|of) |result of |value of )", "", t)
    t = re.sub(rf"{_ASK_HI_TAIL}$", "", t)
    return t.strip()


def _arithmetic(t: str, hi: bool) -> str | None:
    t = _strip_question(t)
    m = (re.fullmatch(rf"(?:add|sum of|the sum of) ({NUM}) (?:and|to|plus) ({NUM})", t))
    if m:
        expr = f"{m.group(1)} + {m.group(2)}"
    elif (m := re.fullmatch(rf"subtract ({NUM}) from ({NUM})|take (?:away )?({NUM}) from ({NUM})", t)):
        b, a = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        expr = f"{a} - {b}"
    elif (m := re.fullmatch(rf"(?:multiply ({NUM}) (?:by|and|with|times) ({NUM})|(?:the )?product of ({NUM}) and ({NUM}))", t)):
        a, b = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        expr = f"{a} * {b}"
    elif (m := re.fullmatch(rf"divide ({NUM}) (?:by|into) ({NUM})", t)):
        expr = f"{m.group(1)} / {m.group(2)}"
    else:
        e = re.sub(r"\b(?:the )?(?:square root|sqrt|root) of ", " sqrt ", t)
        e = re.sub(r"\bsqrt ?(\(|" + NUM + r")", lambda m_: " sqrt(" + ("" if m_.group(1) == "(" else m_.group(1) + ")"), e)
        for pat, rep in _WORD_OPS:
            e = re.sub(pat, rep, e)
        e = re.sub(r"\s+", " ", e).strip()
        if not re.fullmatch(r"[\d.\s+\-*/%()sqrt]+", e) or not re.search(r"[+\-*/%]|sqrt|\*\*", e.replace("sqrt", "s", 0)):
            return None
        if not re.search(r"\d\s*(?:[+\-*/%]|\*\*)\s*[\d(s-]|sqrt\(", e):
            return None
        expr = e
    try:
        v = evaluate(expr)
    except ZeroDivisionError:
        return "शून्य से भाग नहीं दिया जा सकता।" if hi else "You can't divide by zero."
    except CalcError:
        return None
    s, rounded = fmt(v)
    if hi:
        return f"{'लगभग ' if rounded else ''}{s} होता है।"
    if "(" in expr.replace("sqrt(", ""):
        return f"That's {'about ' if rounded else ''}{s}."  # read aloud, the brackets would be lost
    return f"{_speak_expr(expr)} is {'about ' if rounded else ''}{s}."


def _speak_expr(expr: str) -> str:
    toks = re.findall(r"\*\*|sqrt|" + r"\d+(?:\.\d+)?|[+\-*/%()]", expr)
    out: list[str] = []
    for i, k in enumerate(toks):
        if k == "sqrt":
            out.append("the square root of")
        elif k in _SAY and not (k == "-" and (i == 0 or toks[i - 1] in _SAY or toks[i - 1] == "(")):
            out.append(_SAY[k])
        elif k in "()":
            continue
        elif k == "-":
            out.append("minus")
        else:
            out.append(fmt(float(k) if "." in k else int(k))[0])
    s = " ".join(out)
    s = re.sub(r"to the power of 2$", "squared", s)
    return s[:1].upper() + s[1:]


# -- percentages -----------------------------------------------------------------------
PCT = r"(?:percent|percentage|pratishat|pratisat|pratishath)"


def _percent(t: str, hi: bool) -> str | None:
    t = _strip_question(t)

    def say(en: str, hi_text: str, v: float) -> str:
        s, rounded = fmt(v)
        about = ("लगभग " if hi else "about ") if rounded else ""
        return hi_text.format(v=about + s) if hi else en.format(v=about + s)

    def f(x: str) -> str:
        return fmt(float(x))[0]

    if m := re.fullmatch(rf"({NUM}) {PCT} of ({NUM})", t):
        p, y = float(m.group(1)), float(m.group(2))
        return say(f"{f(m.group(1))}% of {f(m.group(2))} is {{v}}.", f"{f(m.group(2))} का {f(m.group(1))} प्रतिशत {{v}} है।", p * y / 100)
    if m := re.fullmatch(rf"({NUM}) (?:ka|ki|ke) ({NUM}) {PCT}", t):
        y, p = float(m.group(1)), float(m.group(2))
        return say(f"{f(m.group(2))}% of {f(m.group(1))} is {{v}}.", f"{f(m.group(1))} का {f(m.group(2))} प्रतिशत {{v}} है।", p * y / 100)
    m = (re.fullmatch(rf"({NUM}) (?:is|as a) (?:what )?{PCT} of ({NUM})", t)
         or re.fullmatch(rf"what {PCT} of ({NUM}) is ({NUM})", t)
         or re.fullmatch(rf"({NUM}) (?:out of|of) ({NUM}) (?:is |as a |in )?(?:what )?{PCT}", t)
         or re.fullmatch(rf"what {PCT} is ({NUM}) (?:of|out of) ({NUM})", t))
    if m:
        a, b = (m.group(2), m.group(1)) if t.startswith(f"what {PCT}") and " of " in t and t.index(" of ") < t.index(" is ") else (m.group(1), m.group(2))
        if re.fullmatch(rf"what {PCT} of ({NUM}) is ({NUM})", t):
            a, b = m.group(2), m.group(1)
        if float(b) == 0:
            return None
        return say(f"{f(a)} is {{v}}% of {f(b)}.", f"{f(a)}, {f(b)} का {{v}} प्रतिशत है।", float(a) / float(b) * 100)
    m = (re.fullmatch(rf"(?:increase|raise|add) ({NUM}) by ({NUM}) {PCT}", t)
         or re.fullmatch(rf"({NUM}) (?:plus|\+|jama|with|and) ({NUM}) {PCT}(?: (?:tax|gst|vat|tip|interest|markup|extra))?", t))
    if m:
        y, p = float(m.group(1)), float(m.group(2))
        return say(f"{f(m.group(1))} plus {f(m.group(2))}% is {{v}}.", f"{f(m.group(1))} में {f(m.group(2))} प्रतिशत जोड़कर {{v}} होता है।", y * (1 + p / 100))
    m = (re.fullmatch(rf"(?:decrease|reduce|lower|cut) ({NUM}) by ({NUM}) {PCT}", t)
         or re.fullmatch(rf"({NUM}) (?:minus|-|less|ghata) ({NUM}) {PCT}", t)
         or re.fullmatch(rf"({NUM}) (?:with|after) (?:a )?({NUM}) {PCT} (?:discount|off)", t))
    if m:
        y, p = float(m.group(1)), float(m.group(2))
        return say(f"{f(m.group(1))} minus {f(m.group(2))}% is {{v}}.", f"{f(m.group(1))} में से {f(m.group(2))} प्रतिशत घटाकर {{v}} होता है।", y * (1 - p / 100))
    if m := re.fullmatch(rf"({NUM}) {PCT} (?:off|discount on|discount off) ({NUM})", t):
        p, y = float(m.group(1)), float(m.group(2))
        return say(f"{f(m.group(2))} with {f(m.group(1))}% off is {{v}}.", f"{f(m.group(2))} पर {f(m.group(1))} प्रतिशत छूट के बाद {{v}} होता है।", y * (1 - p / 100))
    m = (re.fullmatch(rf"(?:a |the )?({NUM}) {PCT} tip (?:on|for) (?:a )?({NUM})(?: bill)?", t)
         or re.fullmatch(rf"(?:a |the )?tip (?:of )?({NUM}) {PCT} (?:on|for) (?:a )?({NUM})(?: bill)?", t))
    if m:
        p, y = float(m.group(1)), float(m.group(2))
        tip = y * p / 100
        ts, total = fmt(tip)[0], fmt(y + tip)[0]
        return (f"{f(m.group(2))} पर {f(m.group(1))} प्रतिशत टिप {ts} है, कुल {total}।" if hi
                else f"A {f(m.group(1))}% tip on {f(m.group(2))} is {ts}, {total} in total.")
    if m := re.fullmatch(rf"(?:the )?{PCT} (?:change|increase|decrease|difference) (?:from|between) ({NUM}) (?:to|and) ({NUM})", t):
        a, b = float(m.group(1)), float(m.group(2))
        if a == 0:
            return None
        ch = (b - a) / a * 100
        s = fmt(abs(ch))[0]
        if hi:
            return f"{f(m.group(1))} से {f(m.group(2))} {s} प्रतिशत की {'बढ़त' if ch >= 0 else 'कमी'} है।"
        return f"From {f(m.group(1))} to {f(m.group(2))} is {'an increase' if ch >= 0 else 'a decrease'} of {s}%."
    m = re.fullmatch(rf"(half|a half|one half|a third|one third|a quarter|one quarter|quarter|double|twice|triple|thrice) (?:of )?({NUM})", t)
    if m:
        k = m.group(1).split()[-1]
        mult = {"half": 0.5, "third": 1 / 3, "quarter": 0.25, "double": 2, "twice": 2, "triple": 3, "thrice": 3}[k]
        v = float(m.group(2)) * mult
        s, rounded = fmt(v)
        return (f"{'लगभग ' if rounded else ''}{s} होता है।" if hi
                else f"{m.group(1)[:1].upper() + m.group(1)[1:]} {'of ' if mult < 1 else ''}{f(m.group(2))} is {'about ' if rounded else ''}{s}.")
    return None


# -- units ------------------------------------------------------------------------------
# kind, factor to the kind's base unit, English singular, plural, Hindi, spoken aliases
_U: list[tuple[str, float, str, str, str, tuple[str, ...]]] = [
    ("length", 0.001, "millimetre", "millimetres", "मिलीमीटर", ("mm", "millimeter", "millimeters", "millimetre", "millimetres")),
    ("length", 0.01, "centimetre", "centimetres", "सेंटीमीटर", ("cm", "cms", "centimeter", "centimeters", "centimetre", "centimetres")),
    ("length", 1, "metre", "metres", "मीटर", ("m", "meter", "meters", "metre", "metres", "mtr")),
    ("length", 1000, "kilometre", "kilometres", "किलोमीटर", ("km", "kms", "kilometer", "kilometers", "kilometre", "kilometres", "kilo meter", "kilo meters")),
    ("length", 0.0254, "inch", "inches", "इंच", ("inch", "inches")),
    ("length", 0.3048, "foot", "feet", "फ़ुट", ("ft", "foot", "feet", "feets")),
    ("length", 0.9144, "yard", "yards", "गज़", ("yd", "yds", "yard", "yards")),
    ("length", 1609.344, "mile", "miles", "मील", ("mi", "mile", "miles")),
    ("length", 1852, "nautical mile", "nautical miles", "नॉटिकल मील", ("nautical mile", "nautical miles")),
    ("mass", 1e-6, "milligram", "milligrams", "मिलीग्राम", ("mg", "milligram", "milligrams")),
    ("mass", 0.001, "gram", "grams", "ग्राम", ("g", "gm", "gms", "gram", "grams", "gramme", "grammes")),
    ("mass", 1, "kilogram", "kilograms", "किलो", ("kg", "kgs", "kilo", "kilos", "kilogram", "kilograms", "kilo gram", "kilo grams")),
    ("mass", 0.028349523125, "ounce", "ounces", "औंस", ("oz", "ounce", "ounces")),
    ("mass", 0.45359237, "pound", "pounds", "पाउंड", ("lb", "lbs", "pound", "pounds")),
    ("mass", 6.35029318, "stone", "stone", "स्टोन", ("stone", "stones")),
    ("mass", 1000, "tonne", "tonnes", "टन", ("t", "ton", "tons", "tonne", "tonnes", "metric ton", "metric tons")),
    ("volume", 0.001, "millilitre", "millilitres", "मिलीलीटर", ("ml", "milliliter", "milliliters", "millilitre", "millilitres")),
    ("volume", 1, "litre", "litres", "लीटर", ("l", "liter", "liters", "litre", "litres", "ltr")),
    ("volume", 0.00492892159375, "teaspoon", "teaspoons", "छोटा चम्मच", ("tsp", "teaspoon", "teaspoons")),
    ("volume", 0.01478676478125, "tablespoon", "tablespoons", "बड़ा चम्मच", ("tbsp", "tablespoon", "tablespoons")),
    ("volume", 0.2365882365, "cup", "cups", "कप", ("cup", "cups")),
    ("volume", 0.0295735295625, "fluid ounce", "fluid ounces", "फ़्लूइड औंस", ("fl oz", "fluid ounce", "fluid ounces")),
    ("volume", 0.473176473, "pint", "pints", "पिंट", ("pint", "pints")),
    ("volume", 0.946352946, "quart", "quarts", "क्वार्ट", ("quart", "quarts")),
    ("volume", 3.785411784, "gallon", "gallons", "गैलन", ("gallon", "gallons", "gal")),
    ("area", 1, "square metre", "square metres", "वर्ग मीटर", ("sq m", "square meter", "square meters", "square metre", "square metres", "sqm")),
    ("area", 0.09290304, "square foot", "square feet", "वर्ग फ़ुट", ("sq ft", "sqft", "square foot", "square feet")),
    ("area", 0.83612736, "square yard", "square yards", "वर्ग गज़", ("sq yd", "square yard", "square yards")),
    ("area", 1e6, "square kilometre", "square kilometres", "वर्ग किलोमीटर", ("sq km", "square kilometer", "square kilometers", "square kilometre", "square kilometres")),
    ("area", 2589988.110336, "square mile", "square miles", "वर्ग मील", ("sq mi", "square mile", "square miles")),
    ("area", 4046.8564224, "acre", "acres", "एकड़", ("acre", "acres")),
    ("area", 10000, "hectare", "hectares", "हेक्टेयर", ("hectare", "hectares", "ha")),
    ("time", 1, "second", "seconds", "सेकंड", ("s", "sec", "secs", "second", "seconds")),
    ("time", 60, "minute", "minutes", "मिनट", ("min", "mins", "minute", "minutes", "minat")),
    ("time", 3600, "hour", "hours", "घंटे", ("h", "hr", "hrs", "hour", "hours", "ghanta", "ghante")),
    ("time", 86400, "day", "days", "दिन", ("day", "days", "din")),
    ("time", 604800, "week", "weeks", "हफ़्ते", ("week", "weeks", "hafte", "hafta")),
    ("time", 31536000, "year", "years", "साल", ("year", "years", "yr", "yrs", "saal")),
    ("speed", 1, "metre per second", "metres per second", "मीटर प्रति सेकंड", ("m/s", "mps", "meter per second", "meters per second", "metre per second", "metres per second")),
    ("speed", 1000 / 3600, "kilometre per hour", "kilometres per hour", "किलोमीटर प्रति घंटा", ("km/h", "km/hr", "kmph", "kph", "km per hour", "kilometer per hour", "kilometers per hour", "kilometre per hour", "kilometres per hour", "kilometers an hour", "kilometres an hour")),
    ("speed", 0.44704, "mile per hour", "miles per hour", "मील प्रति घंटा", ("mph", "mile per hour", "miles per hour", "miles an hour")),
    ("speed", 1852 / 3600, "knot", "knots", "नॉट", ("knot", "knots", "kn")),
    ("data", 1, "byte", "bytes", "बाइट", ("byte", "bytes")),
    ("data", 1e3, "kilobyte", "kilobytes", "किलोबाइट", ("kb", "kilobyte", "kilobytes")),
    ("data", 1e6, "megabyte", "megabytes", "मेगाबाइट", ("mb", "megabyte", "megabytes", "mega byte", "mega bytes")),
    ("data", 1e9, "gigabyte", "gigabytes", "गीगाबाइट", ("gb", "gig", "gigs", "gigabyte", "gigabytes", "giga byte", "giga bytes")),
    ("data", 1e12, "terabyte", "terabytes", "टेराबाइट", ("tb", "terabyte", "terabytes")),
    ("temp", 0, "degree Celsius", "degrees Celsius", "डिग्री सेल्सियस", ("c", "celsius", "centigrade", "degree celsius", "degrees celsius", "degree c", "degrees c", "degree centigrade", "degrees centigrade")),
    ("temp", 1, "degree Fahrenheit", "degrees Fahrenheit", "डिग्री फ़ारेनहाइट", ("f", "fahrenheit", "farenheit", "fahrenhite", "degree fahrenheit", "degrees fahrenheit", "degree f", "degrees f", "degrees farenheit")),
    ("temp", 2, "kelvin", "kelvin", "केल्विन", ("k", "kelvin", "kelvins", "degrees kelvin")),
]
_ALIAS: dict[str, int] = {}
for _i, _u in enumerate(_U):
    for _a in _u[5]:
        _ALIAS[_a] = _i
_UNIT_RE = "(" + "|".join(re.escape(a) for a in sorted(_ALIAS, key=len, reverse=True)) + ")"
_QTY = rf"({NUM}|a|an|one)"


def _to_c(v: float, i: int) -> float:
    return [v, (v - 32) * 5 / 9, v - 273.15][int(_U[i][1])]


def _from_c(c: float, i: int) -> float:
    return [c, c * 9 / 5 + 32, c + 273.15][int(_U[i][1])]


def _unit_name(i: int, v: float, hi: bool) -> str:
    u = _U[i]
    return u[4] if hi else (u[2] if abs(v) == 1 else u[3])


def _convert(t: str, hi: bool) -> str | None:
    t = re.sub(r"\bdegrees? (?=celsius|centigrade|fahrenheit|farenheit|c\b|f\b)", "degrees ", t)
    t = _strip_question(t)
    q = src = dst = None
    pats = [
        (rf"(?:convert )?{_QTY} {_UNIT_RE} (?:to|in|into|in to|as|equals|is|are|=|in terms of) (?:how many |how much |kitne |kitna )?{_UNIT_RE}", (1, 2, 3)),
        (rf"how (?:many|much) {_UNIT_RE} (?:are |is )?(?:there )?(?:in|is|are|make|makes|equals?|to|per) {_QTY} {_UNIT_RE}", (2, 3, 1)),
        (rf"{_QTY} {_UNIT_RE} (?:is |are |equals |makes |make )?how (?:many|much) {_UNIT_RE}", (1, 2, 3)),
        (rf"{_QTY} {_UNIT_RE} (?:ko |me |mein |main )?(?:kitne |kitna |kitni )?{_UNIT_RE}(?: (?:mein|me|main))?(?: (?:badlo|convert karo|convert kar do|batao))?", (1, 2, 3)),
        (rf"{_QTY} {_UNIT_RE} (?:mein|me|main) (?:kitne|kitna|kitni) {_UNIT_RE}", (1, 2, 3)),
    ]
    how_many = False
    for n, (pat, (qi, si, di)) in enumerate(pats):
        m = re.fullmatch(pat, t)
        if m:
            q, src, dst = m.group(qi), m.group(si), m.group(di)
            how_many = n == 1
            break
    if q is None or src is None or dst is None:
        return None
    qty = 1.0 if q in ("a", "an", "one") else float(q)
    i, j = _ALIAS[src], _ALIAS[dst]
    if i == j or _U[i][0] != _U[j][0]:
        return None
    if _U[i][0] == "temp":
        v = _from_c(_to_c(qty, i), j)
    else:
        v = qty * _U[i][1] / _U[j][1]
    s, rounded = fmt(v, places=2)
    qs = fmt(qty)[0]
    if how_many and qty == 1 and not hi and _U[i][0] != "temp":
        return f"There {'is' if v == 1 else 'are'} {'about ' if rounded else ''}{s} {_unit_name(j, v, False)} in {'an' if _U[i][2][0] in 'aeiou' else 'a'} {_U[i][2]}."
    if hi:
        return f"{qs} {_unit_name(i, qty, True)} {'लगभग ' if rounded else ''}{s} {_unit_name(j, v, True)} होते हैं।"
    return f"{qs} {_unit_name(i, qty, False)} is {'about ' if rounded else ''}{s} {_unit_name(j, v, False)}."


# -- dates -------------------------------------------------------------------------------
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m} | {
    m.lower(): i for i, m in enumerate(calendar.month_abbr) if m} | {"sept": 9}
_MONTH_RE = "(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + ")"
_ORD = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9,
    "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15, "sixteenth": 16,
    "seventeenth": 17, "eighteenth": 18, "nineteenth": 19, "twentieth": 20, "thirtieth": 30,
}
_WEEKDAYS = {d.lower(): i for i, d in enumerate(calendar.day_name)}
_HOLIDAYS: dict[str, tuple[int, int, str, str]] = {
    "christmas": (12, 25, "Christmas", "क्रिसमस"), "christmas day": (12, 25, "Christmas", "क्रिसमस"),
    "christmas eve": (12, 24, "Christmas Eve", "क्रिसमस की पूर्व संध्या"),
    "new year": (1, 1, "New Year's Day", "नया साल"), "new years": (1, 1, "New Year's Day", "नया साल"),
    "new year's": (1, 1, "New Year's Day", "नया साल"), "new year's day": (1, 1, "New Year's Day", "नया साल"),
    "new years day": (1, 1, "New Year's Day", "नया साल"), "the new year": (1, 1, "New Year's Day", "नया साल"),
    "new year's eve": (12, 31, "New Year's Eve", "नए साल की पूर्व संध्या"), "new years eve": (12, 31, "New Year's Eve", "नए साल की पूर्व संध्या"),
    "valentine's day": (2, 14, "Valentine's Day", "वैलेंटाइन डे"), "valentines day": (2, 14, "Valentine's Day", "वैलेंटाइन डे"),
    "valentine's": (2, 14, "Valentine's Day", "वैलेंटाइन डे"), "valentines": (2, 14, "Valentine's Day", "वैलेंटाइन डे"),
    "halloween": (10, 31, "Halloween", "हैलोवीन"),
    "independence day": (8, 15, "Independence Day", "स्वतंत्रता दिवस"), "republic day": (1, 26, "Republic Day", "गणतंत्र दिवस"),
    "gandhi jayanti": (10, 2, "Gandhi Jayanti", "गांधी जयंती"),
}
_HOLIDAY_RE = "(" + "|".join(re.escape(h) for h in sorted(_HOLIDAYS, key=len, reverse=True)) + ")"
_HI_MONTHS = ["", "जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]
_HI_DAYS = ["सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"]


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _parse_day(s: str, today: date, after: bool | None) -> tuple[date, str | None] | None:
    """A date and its holiday key (if named). Without a year: the next one
    (after=True, today counts), the last one (after=False) or this year's (None)."""
    s = re.sub(r"^(?:the |this |this coming |coming )", "", s.strip())
    s = re.sub(r"'s$", "", s) if s.endswith("'s") and s[:-2] in _HOLIDAYS else s
    if s == "today":
        return today, None
    if s == "tomorrow":
        return today + timedelta(days=1), None
    if s == "yesterday":
        return today - timedelta(days=1), None
    if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s):
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return (d, None) if d else None
    if m := re.fullmatch(r"(next )?(" + "|".join(_WEEKDAYS) + ")", s):
        ahead = (_WEEKDAYS[m.group(2)] - today.weekday()) % 7 or 7
        return (today + timedelta(days=ahead) if after else today - timedelta(days=(today.weekday() - _WEEKDAYS[m.group(2)]) % 7 or 7)), None
    year = None
    if m := re.fullmatch(r"(.+?),? (\d{4})", s):
        s, year = m.group(1), int(m.group(2))
    key = None
    if s in _HOLIDAYS:
        mo, dy, _, _ = _HOLIDAYS[s]
        key = s
    else:
        day_re = r"(\d{1,2}|" + "|".join(sorted(_ORD, key=len, reverse=True)) + r")(?:st|nd|rd|th)?"
        m = (re.fullmatch(rf"{day_re} (?:of )?{_MONTH_RE}", s) or None)
        if m:
            dy_s, mo_s = m.group(1), m.group(2)
        elif m := re.fullmatch(rf"{_MONTH_RE} (?:the )?{day_re}", s):
            mo_s, dy_s = m.group(1), m.group(2)
        else:
            m2 = re.fullmatch(r"(twenty|thirty) (first|second|third|fourth|fifth|sixth|seventh|eighth|ninth) (?:of )?" + _MONTH_RE, s)
            if not m2:
                return None
            dy_s, mo_s = str(_UNITS[m2.group(1)] + _ORD[m2.group(2)]), m2.group(3)
        dy = int(dy_s) if dy_s.isdigit() else _ORD[dy_s]
        mo = _MONTHS[mo_s]
    if year is not None:
        d = _safe_date(year, mo, dy)
        return (d, key) if d else None
    if after is None:
        d = _safe_date(today.year, mo, dy)
        return (d, key) if d else None
    for y in (range(today.year, today.year + 9) if after else range(today.year, today.year - 9, -1)):
        d = _safe_date(y, mo, dy)
        if d and (d >= today if after else d <= today):
            return d, key
    return None


def _date_words(d: date, today: date, hi: bool) -> str:
    if hi:
        s = f"{_HI_DAYS[d.weekday()]}, {d.day} {_HI_MONTHS[d.month]}"
    else:
        s = f"{calendar.day_name[d.weekday()]}, {calendar.month_name[d.month]} {d.day}"
    return s + (f" {d.year}" if hi and d.year != today.year else f", {d.year}" if d.year != today.year else "")


def _label(d: date, key: str | None, today: date, hi: bool) -> str:
    if key:
        h = _HOLIDAYS[key]
        return h[3] if hi else h[2]
    return _date_words(d, today, hi)


def _span(days: int, weeks: bool, hi: bool) -> str:
    if weeks and days >= 7:
        w, r = divmod(days, 7)
        if hi:
            return f"{w} हफ़्ते" + (f" और {r} दिन" if r else "")
        return f"{w} week{'s' if w != 1 else ''}" + (f" and {r} day{'s' if r != 1 else ''}" if r else "")
    return f"{days:,} दिन" if hi else f"{days:,} day{'s' if days != 1 else ''}"


_DAYS_UNIT = r"(days?|weeks?|din|hafte|hafton)"


def _dates(t: str, now: datetime, hi: bool) -> str | None:
    today = now.date()
    raw = t  # the Hindi phrasings end in words the question-stripper would take
    t = _strip_question(t)
    t = re.sub(r"\b(?:till|til|untill|upto|up to)\b", "until", t)
    # how many days until X
    m = (re.fullmatch(rf"how many {_DAYS_UNIT} (?:are )?(?:left |remain(?:ing)? |are there |to go |do i have |do we have |away )?(?:until|to|before|for) (.+?)(?: left| to go| from (?:now|today))?", t)
         or re.fullmatch(r"how (?:long|far away) (?:is it )?(?:until|to|is) (.+?)(?: from (?:now|today))?()", t)
         or re.fullmatch(rf"(?:number of )?{_DAYS_UNIT} (?:left )?(?:until|to|before) (.+)", t)
         or re.fullmatch(rf"(.+?) (?:mein|me|main|aane mein|aane me|tak) (?:kitne|kitane) {_DAYS_UNIT} (?:hain|bache hain|baaki hain|bache|baki hain|reh gaye|rah gaye|hai)()", raw))
    if m:
        groups = [g for g in m.groups()]
        if re.fullmatch(_DAYS_UNIT, groups[0] or ""):
            unit, what = groups[0], groups[1]
        elif len(groups) > 1 and groups[1] and re.fullmatch(_DAYS_UNIT, groups[1]):
            what, unit = groups[0], groups[1]
        else:
            what, unit = groups[0], "days"
        r = _parse_day(what, today, after=True)
        if r is None:
            return None
        d, key = r
        n = (d - today).days
        name = _label(d, key, today, hi)
        weeks = unit.startswith(("week", "haft"))
        if n == 0:
            return f"{name} आज है!" if hi else f"{name} is today!"
        if n == 1:
            return f"{name} कल है।" if hi else f"{name} is tomorrow."
        when = "" if not key else (f" ({_date_words(d, today, hi)})")
        return (f"{name}{when} में {_span(n, weeks, True)} बाकी हैं।" if hi
                else f"{_span(n, weeks, False)[:1].upper()}{_span(n, weeks, False)[1:]} until {name}{when}.")
    # how many days since X
    m = (re.fullmatch(rf"how many {_DAYS_UNIT} (?:has it been |have passed |ago was it |is it )?(?:since|from) (.+?)(?: until (?:now|today)| to (?:now|today))?", t)
         or re.fullmatch(r"how long (?:has it been |ago was it |ago was )?(?:since )?(.+?) ago()", t)
         or re.fullmatch(r"how long (?:has it been|is it) since (.+?)()", t))
    if m:
        groups = list(m.groups())
        unit, what = (groups[0], groups[1]) if re.fullmatch(_DAYS_UNIT, groups[0] or "") else ("days", groups[0])
        r = _parse_day(what, today, after=False)
        if r is None:
            return None
        d, key = r
        n = (today - d).days
        name = _label(d, key, today, hi)
        weeks = unit.startswith(("week", "haft"))
        return (f"{name} को {_span(n, weeks, True)} हो गए।" if hi
                else f"It's been {_span(n, weeks, False)} since {name}.")
    # between two dates
    m = (re.fullmatch(rf"how many {_DAYS_UNIT} (?:are there )?(?:between|from) (.+?) (?:and|to|until) (.+)", t)
         or re.fullmatch(rf"{_DAYS_UNIT} (?:between|from) (.+?) (?:and|to|until) (.+)", t))
    if m:
        unit = m.group(1)
        r1 = _parse_day(m.group(2), today, after=None)
        r2 = _parse_day(m.group(3), today, after=None)
        if r1 is None or r2 is None:
            return None
        a, b = r1[0], r2[0]
        # without years, "between March 3 and January 5" means the next January 5
        if b < a and not re.search(r"\d{4}", m.group(3)):
            b = _safe_date(b.year + 1, b.month, b.day) or b
        n = abs((b - a).days)
        weeks = unit.startswith(("week", "haft"))
        return (f"{_label(a, r1[1], today, True)} और {_label(b, r2[1], today, True)} के बीच {_span(n, weeks, True)} हैं।" if hi
                else f"There are {_span(n, weeks, False)} between {_label(a, r1[1], today, False)} and {_label(b, r2[1], today, False)}.")
    # what day of the week is X
    m = (re.fullmatch(r"what day (?:of the week )?(?:is|was|will be|will it be on|falls on|does) (.+?)(?: (?:fall on|on|be|this year|next year))?", t)
         or re.fullmatch(r"(?:on )?what day (?:of the week )?(?:is|does) (.+?) (?:fall on|on|this year)", t)
         or re.fullmatch(r"(.+?) (?:kis din|kaun se din|kaunse din) (?:hai|padega|padta hai|tha|pada tha|hoga)()", raw))
    # ("what day is it" is the date reply; "what day was it 2 years ago" is below)
    if m and m.group(1).strip() not in ("it", "today", "it today") and (r := _parse_day(m.group(1), today, after=True)):
        d, key = r
        was = d < today
        if hi:
            return f"{_label(d, key, today, True)} {_date_words(d, today, True)} को {'था' if was else 'है'}।" if key else \
                f"{_date_words(d, today, True)} {'था' if was else 'है'}।"
        if key:
            return f"{_label(d, key, today, False)} {'was' if was else 'is'} on {_date_words(d, today, False)}."
        return f"It {'was' if was else 'is'} {_date_words(d, today, False)}."
    # the date N days from now / ago
    m = (re.fullmatch(rf"(?:what(?:'s| is| s| will be| was)? )?(?:the )?(?:date|day) (?:(?:will it be|is it|was it) )?(in|after) ({NUM}) (days?|weeks?|months?|years?)(?: from (?:now|today))?", t)
         or re.fullmatch(rf"(?:what(?:'s| is| s)? )?(?:the )?(?:date|day)? ?(?:is |was )?({NUM}) (days?|weeks?|months?|years?) (from now|from today|later|ago|before today|back)()", t)
         or re.fullmatch(rf"(?:what )?(?:date|day) (?:was it|is it|will it be) ({NUM}) (days?|weeks?|months?|years?) (from now|from today|later|ago)()", t)
         or re.fullmatch(rf"({NUM}) (din|hafte|mahine|saal) (baad|pehle|pahle)(?: (?:kya|kaun si|konsi) (?:date|tareekh|tarikh) (?:hogi|hoga|thi|tha))?(?: (?:ko )?(?:kaun sa|kya|konsa) din (?:hoga|tha))?()", raw))
    if m:
        g = [x for x in m.groups()]
        if g[0] in ("in", "after"):
            n_s, unit, ago = g[1], g[2], False
        else:
            n_s, unit, ago = g[0], g[1], g[2] in ("ago", "before today", "back", "pehle", "pahle")
        count = float(n_s)
        if not count.is_integer() or not 0 <= count <= 10000:
            return None
        k = int(count) * (-1 if ago else 1)
        if unit.startswith(("day", "din")):
            d = today + timedelta(days=k)
        elif unit.startswith(("week", "haft")):
            d = today + timedelta(weeks=k)
        else:
            months = k * (12 if unit.startswith(("year", "saal")) else 1)
            y, mo = divmod(today.month - 1 + months, 12)
            y += today.year
            mo += 1
            if not 1 <= y <= 9999:
                return None
            d = date(y, mo, min(today.day, calendar.monthrange(y, mo)[1]))
        if not 1 <= d.year <= 9999:
            return None
        word = _date_words(d, today, hi)
        if hi:
            return f"{word} {'था' if ago else 'होगा'}।"
        unit = {"din": "day", "hafte": "week", "mahine": "month", "saal": "year"}.get(unit, unit.rstrip("s"))
        span = f"{int(count):,} {unit}{'s' if count != 1 else ''}"
        return f"{span[:1].upper()}{span[1:]} ago was {word}." if ago else f"In {span} it'll be {word}."
    return None


# -- entry point ------------------------------------------------------------------------------
def answer(text: str, now: datetime, hi: bool) -> str | None:
    """The spoken answer when the whole request is math, a conversion or date math."""
    if len(text) > 200:
        return None
    try:
        t = normalize(text, hi)
    except (ValueError, KeyError):
        return None
    if not t or not re.search(r"\d|\bhow (?:many|much)\b|" + _HOLIDAY_RE + "|" + _MONTH_RE + r"|\bdays?\b|\bweeks?\b|\bdin\b", t):
        return None
    return _dates(t, now, hi) or _convert(t, hi) or _percent(t, hi) or _arithmetic(t, hi)
