"""Read lightweight metadata from a Skill's ``SKILL.md`` file."""

from __future__ import annotations

import json
from pathlib import Path


def read_skill_description(skill_path: str | Path) -> str:
    """Return the frontmatter ``description`` for a Skill, or an empty string.

    Skill metadata is intentionally parsed without a YAML dependency.  This
    reader handles the scalar forms commonly used by Skill descriptions while
    failing closed for missing or malformed frontmatter.
    """

    path = Path(skill_path)
    skill_file = path if path.name == "SKILL.md" else path / "SKILL.md"
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""

    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return ""

    closing_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing_index is None:
        return ""

    frontmatter = lines[1:closing_index]
    for index, line in enumerate(frontmatter):
        if line[:1].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() != "description":
            continue
        value = value.strip()
        if value.startswith((">", "|")):
            return _parse_block_scalar(frontmatter[index + 1 :], value)
        return _parse_inline_scalar(value)
    return ""


def _parse_inline_scalar(value: str) -> str:
    if not value:
        return ""
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return ""
        return parsed if isinstance(parsed, str) else ""
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            return ""
        return value[1:-1].replace("''", "'")
    if value[0] in "[{" or value in {"null", "Null", "NULL", "~"}:
        return ""
    return value.split(" #", 1)[0].rstrip()


def _parse_block_scalar(lines: list[str], indicator: str) -> str:
    content: list[str] = []
    indentation: int | None = None
    for line in lines:
        if not line.strip():
            content.append("")
            continue
        leading = len(line) - len(line.lstrip())
        if leading == 0:
            break
        if indentation is None:
            indentation = leading
        if leading < indentation:
            break
        content.append(line[indentation:])

    if not content or indentation is None:
        return ""
    while content and not content[-1]:
        content.pop()
    if indicator.startswith("|"):
        return "\n".join(content)

    paragraphs: list[str] = []
    paragraph: list[str] = []
    for line in content:
        if line:
            paragraph.append(line)
        elif paragraph:
            paragraphs.append(" ".join(paragraph))
            paragraph = []
    if paragraph:
        paragraphs.append(" ".join(paragraph))
    return "\n".join(paragraphs)
