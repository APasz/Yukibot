"""Read and edit Java properties while retaining comments and unmanaged entries."""

import re
from collections.abc import Mapping
from dataclasses import dataclass

_WHITESPACE = " \t\f"
_KEY_SEPARATORS = f"=:{_WHITESPACE}"
_LINE_ENDINGS = re.compile(r"\r\n|\r|\n")
_ESCAPES = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}
_ENCODED_ESCAPES = {value: f"\\{key}" for key, value in _ESCAPES.items()}
_ENCODED_ESCAPES["\\"] = "\\\\"


@dataclass(frozen=True, slots=True)
class PropertyEntry:
    key: str
    value: str
    start_line: int
    end_line: int


def _is_continued(line: str) -> bool:
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _decode_value(raw: str) -> str:
    characters: list[str] = []
    index = 0
    while index < len(raw):
        character = raw[index]
        index += 1
        if character != "\\":
            characters.append(character)
            continue
        if index == len(raw):
            break
        character = raw[index]
        index += 1
        if character == "u":
            hexadecimal = raw[index:index + 4]
            if len(hexadecimal) != 4 or re.fullmatch(r"[0-9a-fA-F]{4}", hexadecimal) is None:
                raise ValueError("Invalid Unicode escape in Java properties.")
            characters.append(chr(int(hexadecimal, 16)))
            index += 4
        else:
            characters.append(_ESCAPES.get(character, character))
    value = "".join(characters)
    return value.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le", errors="surrogatepass")


def _encode_text(value: str, *, is_key: bool = False) -> str:
    encoded: list[str] = []
    for index, character in enumerate(value):
        if character in _ENCODED_ESCAPES:
            encoded.append(_ENCODED_ESCAPES[character])
        elif (character == " " and (is_key or index == 0)) or (is_key and character in "=:#!"):
            encoded.append(f"\\{character}")
        elif not 0x20 <= ord(character) <= 0x7e:
            hexadecimal = character.encode("utf-16-be", errors="surrogatepass").hex()
            encoded.extend(f"\\u{hexadecimal[start:start + 4]}" for start in range(0, len(hexadecimal), 4))
        else:
            encoded.append(character)
    return "".join(encoded)


def parse_properties(source: str) -> tuple[PropertyEntry, ...]:
    """Decode Java escapes, separators, and continued lines, retaining their source locations."""
    lines = _LINE_ENDINGS.split(source)
    entries: list[PropertyEntry] = []
    index = 0
    while index < len(lines):
        start = index
        line = lines[index].lstrip(_WHITESPACE)
        index += 1
        if not line or line.startswith(("#", "!")):
            continue
        while _is_continued(line):
            line = line[:-1]
            if not line or index == len(lines):
                break
            line += lines[index].lstrip(_WHITESPACE)
            index += 1
        if not line:
            if index == len(lines) or (index == len(lines) - 1 and not lines[index]):
                entries.append(PropertyEntry("", "", start, len(lines)))
            continue

        key_end = 0
        escaped = False
        for key_end, character in enumerate(line):
            if not escaped and character in _KEY_SEPARATORS:
                break
            escaped = character == "\\" and not escaped
        else:
            key_end = len(line)
        value_start = key_end
        while value_start < len(line) and line[value_start] in _WHITESPACE:
            value_start += 1
        if value_start < len(line) and line[value_start] in "=:":
            value_start += 1
        while value_start < len(line) and line[value_start] in _WHITESPACE:
            value_start += 1
        entries.append(PropertyEntry(_decode_value(line[:key_end]), _decode_value(line[value_start:]), start, index))
    return tuple(entries)


def replace_properties(source: str, values: Mapping[str, str]) -> str:
    """Replace selected properties and append missing ones, rejecting duplicate managed keys."""
    lines = _LINE_ENDINGS.split(source)
    entries = parse_properties(source)
    tail_index = len(lines) - 1
    if tail_index > 0 and not lines[tail_index]:
        tail_index -= 1
    if entries and entries[-1].end_line > tail_index and _is_continued(lines[tail_index]):
        # Appending after an escaped EOF would otherwise continue the last unmanaged property.
        last = entries[-1]
        lines[last.start_line:last.end_line] = [f"{_encode_text(last.key, is_key=True)}={_encode_text(last.value)}"]
    seen: set[str] = set()
    for entry in reversed(entries):
        key = entry.key
        if key not in values:
            continue
        if key in seen:
            raise ValueError(f"Duplicate Minecraft server property: {key}")
        seen.add(key)
        lines[entry.start_line:entry.end_line] = [f"{_encode_text(key, is_key=True)}={_encode_text(values[key])}"]
    if lines and lines[-1] == "":
        lines.pop()
    lines.extend(
        f"{_encode_text(key, is_key=True)}={_encode_text(value)}"
        for key, value in values.items() if key not in seen
    )
    return "\n".join(lines) + "\n"
