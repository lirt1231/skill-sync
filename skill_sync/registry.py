"""Portable registry.yaml storage for Skill sync repositories.

The registry intentionally supports only a small, owned YAML subset so the CLI
can remain dependency-free while still producing human-readable state.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


_INTEGER_RE = re.compile(r"-?(0|[1-9][0-9]*)\Z")
_NONCANONICAL_INTEGER_RE = re.compile(r"(?:\+[0-9]+|-0)\Z")
_FLOAT_RE = re.compile(
    r"[+-]?(?:(?:[0-9]+\.[0-9]*)|(?:\.[0-9]+)|(?:[0-9]+(?:\.[0-9]*)?[eE][+-]?[0-9]+))\Z"
)
_LEADING_ZERO_INTEGER_RE = re.compile(r"[+-]?0[0-9]+\Z")
_DATE_LIKE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_BASE_INTEGER_LIKE_RE = re.compile(
    r"[+-]?(?:0[xX][0-9A-Fa-f_]+|0[oO][0-7_]+|0[bB][01_]+)\Z"
)
_NUMERIC_SEPARATOR_LIKE_RE = re.compile(r"[+-]?[0-9]+(?:_[0-9]+)+\Z")
_WINDOWS_DRIVE_PATH_PREFIX_RE = re.compile(r"^[A-Za-z]:[\\/].*")
_BOOLEAN_LIKE_TOKENS = frozenset({"true", "false", "True", "False", "TRUE", "FALSE"})
_YAML_BOOLEAN_WORDS = frozenset({"yes", "no", "on", "off"})
_NULL_LIKE_TOKENS = frozenset({"null", "Null", "NULL", "~"})
_SPECIAL_FLOAT_LIKE_TOKENS = frozenset({".nan", ".inf", "+.inf", "-.inf"})


def empty_registry() -> dict[str, Any]:
    """Return a new empty registry using the initial schema."""

    return {"version": 1, "skills": {}}


def load_registry(path: str | Path) -> dict[str, Any]:
    """Load registry data from the constrained registry.yaml subset.

    Supported syntax:
    - two-space indentation
    - mappings only
    - string, boolean, and integer scalar values
    - comments and blank lines
    """

    registry_path = Path(path)
    root: dict[str, Any] = {}
    stack: list[dict[str, Any]] = [root]

    for line_number, raw_line in enumerate(
        registry_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = _strip_comment(raw_line)
        if not line.strip():
            continue

        indent = _indent_width(line, line_number)
        if indent % 2 != 0:
            raise ValueError(
                f"Invalid indentation on line {line_number}: use two-space indentation"
            )
        level = indent // 2
        if level >= len(stack):
            raise ValueError(
                f"Invalid indentation on line {line_number}: no parent mapping at this level"
            )

        content = line[indent:]
        key, value_text = _split_mapping_entry(content, line_number)
        parent = stack[level]
        if key in parent:
            raise ValueError(f"Duplicate key on line {line_number}: {key}")

        if value_text == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack = stack[: level + 1]
            stack.append(child)
        else:
            parent[key] = _parse_scalar(value_text, line_number)
            stack = stack[: level + 1]

    _reject_absolute_path_values(root)
    return root


def save_registry(path: str | Path, registry: dict[str, Any]) -> None:
    """Write registry data using the normalized constrained YAML subset."""

    if not isinstance(registry, dict):
        raise ValueError("Registry root must be a mapping")
    _reject_absolute_path_values(registry)

    lines: list[str] = []
    _append_mapping(lines, registry, level=0)
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _strip_comment(raw_line: str) -> str:
    for index, character in enumerate(raw_line):
        if character == "#" and (
            index == 0 or raw_line[index - 1] in {" ", "\t"}
        ):
            return raw_line[:index].rstrip()
    return raw_line


def _indent_width(line: str, line_number: int) -> int:
    indent = 0
    for character in line:
        if character == " ":
            indent += 1
            continue
        if character == "\t":
            raise ValueError(
                f"Invalid indentation on line {line_number}: tabs are not supported"
            )
        return indent
    return indent


def _split_mapping_entry(content: str, line_number: int) -> tuple[str, str]:
    if content.startswith("-"):
        raise ValueError(f"Unsupported sequence syntax on line {line_number}")
    if ":" not in content:
        raise ValueError(f"Invalid mapping entry on line {line_number}: missing ':'")
    if _WINDOWS_DRIVE_PATH_PREFIX_RE.match(content):
        raise ValueError(f"Registry mapping key must not be an absolute path on line {line_number}")
    if content.endswith(":") and _is_absolute_path_text(content[:-1].strip()):
        raise ValueError(f"Registry mapping key must not be an absolute path on line {line_number}")
    separator_index = content.find(":")
    if separator_index > 0 and content[separator_index - 1] == " ":
        raise ValueError(f"Unsupported mapping separator on line {line_number}")
    if not (content.endswith(":") or content.startswith(": ", separator_index)):
        raise ValueError(f"Unsupported mapping separator on line {line_number}")

    key, raw_value_text = content.split(":", 1)
    if key.strip() != key:
        raise ValueError(f"Unsupported mapping key whitespace on line {line_number}")
    if raw_value_text:
        if not raw_value_text.startswith(" "):
            raise ValueError(f"Unsupported mapping separator on line {line_number}")
        value_text = raw_value_text[1:]
        if not value_text or value_text.strip() != value_text:
            raise ValueError(f"Unsupported scalar whitespace on line {line_number}")
    else:
        value_text = ""
    if not key:
        raise ValueError(f"Invalid mapping entry on line {line_number}: empty key")
    if key.startswith(("'", '"')):
        raise ValueError(f"Unsupported quoted key on line {line_number}")
    if key.startswith(("{", "[", "|", ">", "?")):
        raise ValueError(f"Unsupported YAML feature in key on line {line_number}")
    if _is_absolute_path_text(key):
        raise ValueError(f"Registry mapping key must not be an absolute path on line {line_number}")
    if _is_ambiguous_plain_scalar_token(key):
        raise ValueError(f"Unsupported ambiguous key on line {line_number}")
    if _contains_unsupported_token(key):
        raise ValueError(f"Unsupported YAML feature in key on line {line_number}")
    if value_text.startswith("{") or value_text.startswith("["):
        raise ValueError(f"Unsupported flow style YAML on line {line_number}")
    return key, value_text


def _parse_scalar(value_text: str, line_number: int) -> str | bool | int:
    if value_text.startswith(("'", '"')):
        raise ValueError(f"Unsupported quoted scalar on line {line_number}")
    if value_text.startswith("|") or value_text.startswith(">"):
        raise ValueError(f"Unsupported multiline scalar on line {line_number}")
    if _contains_unsupported_token(value_text):
        raise ValueError(f"Unsupported YAML feature in scalar on line {line_number}")
    if ":" in value_text:
        if _is_absolute_path_text(value_text):
            raise ValueError("Registry values must not contain absolute paths")
        raise ValueError(f"Unsupported colon in scalar on line {line_number}")
    if _is_unsupported_plain_scalar_value_token(value_text):
        raise ValueError(f"Unsupported scalar on line {line_number}")
    if value_text == "true":
        return True
    if value_text == "false":
        return False
    if _INTEGER_RE.fullmatch(value_text):
        return int(value_text)
    return value_text


def _contains_unsupported_token(text: str) -> bool:
    return (
        text.startswith("&")
        or text.startswith("*")
        or text.startswith("!")
        or " &" in text
        or " *" in text
        or " !" in text
    )


def _append_mapping(lines: list[str], mapping: dict[str, Any], *, level: int) -> None:
    for key, value in mapping.items():
        if not isinstance(key, str) or not key:
            raise ValueError("Registry mapping keys must be non-empty strings")
        if _is_absolute_path_text(key):
            raise ValueError("Registry mapping keys must not contain absolute paths")
        if (
            "\n" in key
            or "\r" in key
            or key.strip() != key
            or _is_ambiguous_plain_scalar_token(key)
            or key.startswith(("#", "-", "{", "[", "|", ">", "?", "'", '"'))
            or _contains_comment_hazard(key)
            or _contains_unsupported_token(key)
            or ":" in key
        ):
            raise ValueError(f"Registry mapping key is not supported: {key!r}")

        prefix = "  " * level
        if isinstance(value, dict):
            lines.append(f"{prefix}{key}:")
            _append_mapping(lines, value, level=level + 1)
        else:
            lines.append(f"{prefix}{key}: {_format_scalar(value)}")


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if "\n" in value or "\r" in value:
            raise ValueError("Registry string values must be single-line scalars")
        if value == "" or value.strip() != value:
            raise ValueError("Registry string values cannot be empty or padded")
        if ":" in value:
            raise ValueError("Registry string values cannot contain colons")
        if _contains_comment_hazard(value):
            raise ValueError("Registry string values cannot contain YAML comments")
        if _is_ambiguous_plain_scalar_token(value):
            raise ValueError(f"Registry string value is ambiguous: {value!r}")
        if value.startswith(("{", "[", "|", ">", "'", '"')) or _contains_unsupported_token(value):
            raise ValueError(f"Registry string value is not supported: {value!r}")
        return value
    raise ValueError(f"Unsupported registry scalar type: {type(value).__name__}")


def _reject_absolute_path_values(value: Any) -> None:
    if isinstance(value, dict):
        for child in value.values():
            _reject_absolute_path_values(child)
        return
    if isinstance(value, str) and _is_absolute_path_text(value):
        raise ValueError("Registry values must not contain absolute paths")


def _contains_comment_hazard(text: str) -> bool:
    return text.startswith("#") or " #" in text or "\t#" in text


def _is_absolute_path_text(text: str) -> bool:
    return PurePosixPath(text).is_absolute() or PureWindowsPath(text).is_absolute()


def _is_null_like_token(text: str) -> bool:
    return text in _NULL_LIKE_TOKENS


def _is_float_like_token(text: str) -> bool:
    return bool(_FLOAT_RE.fullmatch(text)) or text.lower() in _SPECIAL_FLOAT_LIKE_TOKENS


def _is_leading_zero_integer_like_token(text: str) -> bool:
    return bool(_LEADING_ZERO_INTEGER_RE.fullmatch(text))


def _is_boolean_like_token(text: str) -> bool:
    return text in _BOOLEAN_LIKE_TOKENS


def _is_unsupported_plain_scalar_value_token(text: str) -> bool:
    return (
        (_is_boolean_like_token(text) and text not in {"true", "false"})
        or _is_null_like_token(text)
        or _is_float_like_token(text)
        or _is_leading_zero_integer_like_token(text)
        or _is_noncanonical_integer_like_token(text)
        or _is_other_yaml_implicit_scalar_token(text)
    )


def _is_ambiguous_plain_scalar_token(text: str) -> bool:
    return (
        _is_boolean_like_token(text)
        or bool(_INTEGER_RE.fullmatch(text))
        or _is_null_like_token(text)
        or _is_float_like_token(text)
        or _is_leading_zero_integer_like_token(text)
        or _is_noncanonical_integer_like_token(text)
        or _is_other_yaml_implicit_scalar_token(text)
    )


def _is_noncanonical_integer_like_token(text: str) -> bool:
    return bool(_NONCANONICAL_INTEGER_RE.fullmatch(text))


def _is_other_yaml_implicit_scalar_token(text: str) -> bool:
    return (
        text.lower() in _YAML_BOOLEAN_WORDS
        or bool(_DATE_LIKE_RE.fullmatch(text))
        or bool(_BASE_INTEGER_LIKE_RE.fullmatch(text))
        or bool(_NUMERIC_SEPARATOR_LIKE_RE.fullmatch(text))
    )
