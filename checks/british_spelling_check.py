from __future__ import annotations

from dataclasses import dataclass
import ast
import io
from pathlib import Path
import re
import subprocess
import tokenize
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]

_TEXT_FILE_NAMES = frozenset({"README.md", "changelog.txt", "env.example", "users.json.example"})
_TEXT_SUFFIXES = frozenset({".py"})

_AMERICAN_TO_BRITISH = {
    "analyze": "analyse",
    "analyzed": "analysed",
    "analyzes": "analyses",
    "analyzing": "analysing",
    "authorize": "authorise",
    "authorized": "authorised",
    "authorizes": "authorises",
    "authorizing": "authorising",
    "authorization": "authorisation",
    "behavior": "behaviour",
    "behaviors": "behaviours",
    "canceled": "cancelled",
    "canceling": "cancelling",
    "centered": "centred",
    "centering": "centring",
    "colored": "coloured",
    "coloring": "colouring",
    "color": "colour",
    "colors": "colours",
    "customize": "customise",
    "customized": "customised",
    "customizes": "customises",
    "customizing": "customising",
    "favorite": "favourite",
    "favorites": "favourites",
    "gray": "grey",
    "initialize": "initialise",
    "initialized": "initialised",
    "initializes": "initialises",
    "initializing": "initialising",
    "labeled": "labelled",
    "labeling": "labelling",
    "organize": "organise",
    "organized": "organised",
    "organizes": "organises",
    "organizing": "organising",
}
_AMERICAN_WORD_PATTERN = re.compile(r"\b(" + "|".join(sorted(_AMERICAN_TO_BRITISH)) + r")\b", re.IGNORECASE)

_ALLOWED_EXACT_VALUES = frozenset(
    {
        "Authorization",
        "authorization",
        "/oauth2/authorize",
    }
)
_ALLOWED_LINE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"https?://[^\s\"']*/oauth2/authorize\b",
        r"\bAccess-Control-[A-Za-z-]*Headers?\b",
        r"\bZombieHordeMeter\b",
        r"\bMSPointerCancel\b",
    )
)
_CSS_VALUE_CONTEXT_PATTERN = re.compile(
    r"\b(?:align-(?:items|content|self)|justify-content|text-align|transform-origin|overscroll-behavior|"
    r"scroll-behavior)\s*:\s*[^;{}]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SpellingFinding:
    path: Path
    line_number: int
    word: str
    replacement: str
    text: str

    def format(self) -> str:
        return f"{self.path}:{self.line_number}: use {self.replacement!r} instead of {self.word!r}: {self.text}"


def _tracked_text_paths() -> tuple[Path, ...]:
    try:
        result = subprocess.run(
            ("git", "ls-files"),
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        paths = (path.relative_to(REPO_ROOT) for path in REPO_ROOT.rglob("*") if path.is_file())
    else:
        paths = (Path(line) for line in result.stdout.splitlines() if line)
    return tuple(
        sorted(
            path
            for path in paths
            if path.name in _TEXT_FILE_NAMES or path.suffix in _TEXT_SUFFIXES
        )
    )


def _python_text_segments(path: Path) -> tuple[tuple[int, str], ...]:
    segments: list[tuple[int, str]] = []
    source = (REPO_ROOT / path).read_text(encoding="utf-8")
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for token in tokens:
        if token.type == tokenize.COMMENT:
            segments.append((token.start[0], token.string.lstrip("#").strip()))
            continue
        if token.type != tokenize.STRING:
            continue
        string_text = _decode_string_token(token.string)
        for offset, line in enumerate(string_text.splitlines() or (string_text,)):
            if line.strip():
                segments.append((token.start[0] + offset, line))
    return tuple(segments)


def _decode_string_token(token_text: str) -> str:
    try:
        value = ast.literal_eval(token_text)
    except (SyntaxError, ValueError):
        value = _strip_string_token_delimiters(token_text)
    if isinstance(value, bytes):
        return ""
    return str(value)


def _strip_string_token_delimiters(token_text: str) -> str:
    prefix_match = re.match(r"(?i)[rubf]*", token_text)
    start = prefix_match.end() if prefix_match is not None else 0
    quote = token_text[start : start + 3] if token_text[start : start + 3] in {"'''", '"""'} else token_text[start]
    end = -len(quote)
    return token_text[start + len(quote) : end]


def _plain_text_segments(path: Path) -> tuple[tuple[int, str], ...]:
    text = (REPO_ROOT / path).read_text(encoding="utf-8")
    return tuple((line_number, line) for line_number, line in enumerate(text.splitlines(), start=1))


def _find_spelling_issues(path: Path) -> tuple[SpellingFinding, ...]:
    segments = _python_text_segments(path) if path.suffix == ".py" else _plain_text_segments(path)
    findings: list[SpellingFinding] = []
    for line_number, text in segments:
        for match in _AMERICAN_WORD_PATTERN.finditer(text):
            word = match.group(0)
            replacement = _replacement_for(word)
            if _is_allowed_usage(text, match.start(), match.end()):
                continue
            findings.append(
                SpellingFinding(
                    path=path,
                    line_number=line_number,
                    word=word,
                    replacement=replacement,
                    text=text.strip(),
                )
            )
    return tuple(findings)


def _replacement_for(word: str) -> str:
    replacement = _AMERICAN_TO_BRITISH[word.casefold()]
    if word.isupper():
        return replacement.upper()
    if word[:1].isupper():
        return replacement.capitalize()
    return replacement


def _is_allowed_usage(text: str, start: int, end: int) -> bool:
    if text.strip() in _ALLOWED_EXACT_VALUES:
        return True
    if any(pattern.search(text) for pattern in _ALLOWED_LINE_PATTERNS):
        return True
    previous_char = text[start - 1] if start > 0 else ""
    next_char = text[end] if end < len(text) else ""
    if previous_char in "_-." or next_char in "_-.":
        return True
    if previous_char == "{" or next_char in {"!", "}", "("}:
        return True
    if next_char in {":", "="}:
        return True
    if previous_char in {"'", '"'} and _is_quoted_mapping_key(text, start, end):
        return True
    if _is_css_keyword_value(text, start, end):
        return True
    return False


def _is_quoted_mapping_key(text: str, start: int, end: int) -> bool:
    quote = text[start - 1]
    if end >= len(text) or text[end] != quote:
        return False
    tail = text[end + 1 :].lstrip()
    return tail.startswith((":=", ":"))


def _is_css_keyword_value(text: str, start: int, end: int) -> bool:
    if text[end : end + 1] not in {"", ";", " ", ")", ","}:
        return False
    prefix = text[:start]
    return _CSS_VALUE_CONTEXT_PATTERN.search(prefix) is not None


class BritishSpellingTests(unittest.TestCase):
    def test_repository_text_uses_british_spellings(self) -> None:
        findings = tuple(finding for path in _tracked_text_paths() for finding in _find_spelling_issues(path))
        if findings:
            formatted_findings = "\n".join(finding.format() for finding in findings)
            self.fail(f"American spellings found in prose-like text:\n{formatted_findings}")


if __name__ == "__main__":
    unittest.main()
