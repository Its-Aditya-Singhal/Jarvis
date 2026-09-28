"""Math, conversions and date math answered without the model (jarvis/tools/calc.py).

The routing corpus covers everyday phrasings; this file covers the edges."""

from datetime import datetime

import pytest

from jarvis.tools.calc import CalcError, answer, evaluate, fmt

NOW = datetime(2026, 9, 27, 16, 30)  # Sunday


def ask(text: str, hi: bool = False) -> str | None:
    return answer(text, NOW, hi)


@pytest.mark.parametrize("expr", [
    "__import__('os').system('ls')", "open('x')", "(1).__class__", "a + 1", "[1, 2]", "lambda: 1", "2 ** 100000",
    "10 ** 10 ** 10", "sqrt(-1)", "True + 1", "'a' * 3",
])
def test_only_plain_arithmetic_is_evaluated(expr):
    with pytest.raises(CalcError):
        evaluate(expr)


@pytest.mark.parametrize("text", [
    "what's 2 ** 100000", "what is 10 to the power of 5000", "open('x') plus 1", "what is 2026", "what's 5",
    "set a timer for 5 minutes", "add milk to my list", "how many people live in delhi", "convert 5 miles to kilograms",
    "how many days until my birthday", "what day is it", "how do i get to the station", "call mom in 5 minutes",
])
def test_not_math_or_out_of_range_is_left_alone(text):
    assert ask(text) is None


@pytest.mark.parametrize("text,expected", [
    ("what is 0.1 plus 0.2", "0.1 plus 0.2 is 0.3."),                   # no floating-point noise
    ("increase 200 by 10 percent", "200 plus 10% is 220."),
    ("what's 1,000,000 times 3", "1,000,000 times 3 is 3,000,000."),
    ("what's minus 5 plus 3", "Minus 5 plus 3 is -2."),
    ("(3 + 4) * 2", "That's 14."),                                          # brackets aren't read back
    ("what is 2 to the power of 0.5", "2 to the power of 0.5 is about 1.4142."),
    ("what's 10 mod 0", "You can't divide by zero."),
])
def test_arithmetic(text, expected):
    assert ask(text) == expected


def test_hindi_answers_are_in_devanagari():
    assert ask("5 guna 3 kitna hota hai", hi=True) == "15 होता है।"
    assert ask("100 bata 0", hi=True) == "शून्य से भाग नहीं दिया जा सकता।"
    assert ask("2 kg ko gram mein badlo", hi=True) == "2 किलो 2,000 ग्राम होते हैं।"


@pytest.mark.parametrize("text,expected", [
    ("0 celsius to fahrenheit", "0 degrees Celsius is 32 degrees Fahrenheit."),
    ("-40 f to c", "-40 degrees Fahrenheit is -40 degrees Celsius."),
    ("300 kelvin in celsius", "300 kelvin is 26.85 degrees Celsius."),
    ("1 inch in feet", "1 inch is about 0.0833 feet."),
    ("how many ounces in a pound", "There are 16 ounces in a pound."),
    ("how many fluid ounces in a cup", "There are 8 fluid ounces in a cup."),
    ("10 kmph in mph", "10 kilometres per hour is about 6.21 miles per hour."),
])
def test_conversions(text, expected):
    assert ask(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("how many days until tomorrow", "Monday, September 28 is tomorrow."),
    ("how many days until new year's eve", "95 days until New Year's Eve (Thursday, December 31)."),
    ("how many days until 27 september", "Sunday, September 27 is today!"),
    ("how many days until 26 september", "364 days until Sunday, September 26, 2027."),   # already passed this year
    ("how many days until february 29", "520 days until Tuesday, February 29, 2028."),   # the next leap day
    ("how many days until next friday", "5 days until Friday, October 2."),
    ("how many days since christmas", "It's been 276 days since Christmas."),
    ("what's the date in 1 month", "In 1 month it'll be Tuesday, October 27."),
    ("what day was it 2 years ago", "2 years ago was Friday, September 27, 2024."),
    ("christmas kis din hai", "Christmas is on Friday, December 25."),
])
def test_date_math(text, expected):
    assert ask(text) == expected


def test_rounding_is_said():
    assert fmt(1 / 3) == ("0.3333", True)
    assert fmt(2.5) == ("2.5", False)
    assert fmt(1234567) == ("1,234,567", False)
    # huge and tiny numbers used to be spoken as "1e+20"
    assert fmt(99999999980000000001) == ("100 quintillion", True)
    assert fmt(1e40) == ("1 times 10 to the power of 40", True)
    assert fmt(0.000001) == ("0.000001", False)
    assert fmt(1.2345e-7) == ("1.23 times 10 to the power of -7", True)
