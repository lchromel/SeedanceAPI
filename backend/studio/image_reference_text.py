"""Read explicit image citations, including grouped numeric lists and ranges."""

import re

_CARDINALS = "one two three four five six seven eight nine ten".split()
_ORDINALS = "first second third fourth fifth sixth seventh eighth ninth tenth".split()
_WORDS = {word: i for words in (_CARDINALS, _ORDINALS) for i, word in enumerate(words, 1)}
_NOUN = r"(?:references?(?:\s+(?:images?|photographs?|photos?|pictures?))?|images?|photographs?|photos?|pictures?)"
_NUMBER = r"(?:\d+(?:st|nd|rd|th)?|" + "|".join(_WORDS) + r")"
_ORDINAL = r"(?:\d+(?:st|nd|rd|th)|" + "|".join(_ORDINALS) + r")"
_TAIL = rf"(?:\s*(?:[,/&+]|and)\s*#?\s*\d+(?:st|nd|rd|th)?\b|\s*(?:[-–—−]|to|through)\s*#?\s*{_NUMBER}\b)*"
_MENTION = re.compile(
    rf"\b{_NOUN}[\s#:_\[(*`]*(?P<after>{_NUMBER})\b(?P<tail>{_TAIL})"
    rf"|\b(?P<before>{_ORDINAL})\s+{_NOUN}\b",
    re.I,
)
_TOKEN = re.compile(rf"{_NUMBER}|[-–—−]|\b(?:to|through)\b", re.I)


def _number(value):
    value = value.lower()
    if value in _WORDS:
        return _WORDS[value]
    digits = re.match(r"\d+", value)[0]
    return int(digits) if len(digits) <= 6 else 1_000_000


def _numbers(match):
    if match.group("before"):
        return {_number(match.group("before"))}
    values = _TOKEN.findall(match.group("after") + match.group("tail"))
    numbers, previous, ranged = set(), None, False
    for value in values:
        if value.lower() in ("-", "–", "—", "−", "to", "through"):
            ranged = True
            continue
        number = _number(value)
        if ranged and previous is not None and 0 <= previous <= number <= 100:
            numbers.update(range(previous, number + 1))
        else:
            numbers.add(number)
        previous, ranged = number, False
    return numbers


def reference_numbers(text):
    numbers = set()
    for match in _MENTION.finditer(text):
        numbers.update(_numbers(match))
    return numbers


def rewrite_observation_references(text, mapping, *, unknown="an unverified reference"):
    """Rebind analysis citations before they enter generation-numbered writer context."""
    return _MENTION.sub(
        lambda match: " and ".join(
            dict.fromkeys(mapping.get(i, unknown) for i in sorted(_numbers(match)))
        ),
        text,
    )
