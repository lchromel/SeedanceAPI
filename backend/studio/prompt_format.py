"""The eight photographic brief sections used by Image studio."""

import re
from pathlib import Path

HEADINGS = (
    "FORMAT + MEDIUM",
    "SUBJECT + AGE",
    "CAMERA + FRAMING",
    "SCENE + SETTING",
    "MICRO-DETAILS",
    "EXPRESSION + ACTION",
    "LIGHTING",
    "NEGATIVE CUES",
)
SYSTEM = (Path(__file__).with_name("prompts") / "image_prompt_format.md").read_text()
_HEADER = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*|__)?("
    + "|".join(re.escape(h) for h in HEADINGS)
    + r")[ \t]*:?[ \t]*(?:\*\*|__)?[ \t]*:?[ \t]*([^\n]*)$",
    re.M | re.I,
)


def parse(text):
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    matches = list(_HEADER.finditer(text))
    if len(matches) != len(HEADINGS) or {m[1].upper() for m in matches} != set(HEADINGS):
        return None
    if text[: matches[0].start()].strip():
        return None
    sections = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = (match[2] + "\n" + text[match.end() : end]).strip()
        if not body:
            return None
        sections[match[1].upper()] = body
    return sections


def render(sections):
    return "\n\n".join(f"{heading}\n{sections[heading].strip()}" for heading in HEADINGS)


def add(text, heading, content):
    sections = parse(text)
    if not sections:
        return content + "\n\n" + text
    sections[heading] = content + " " + sections[heading]
    return render(sections)
