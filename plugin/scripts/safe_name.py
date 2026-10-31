"""One cleaning rule for a skill or agent-type name read from a capture.

A name is written by whoever authored the skill or agent file, and an old capture holds it as written. Every
report that prints one and every runner that hands a report to the model reads it through `clean_name`, so a
name can neither move the cursor (escape sequences, carriage return), start a line of its own (newline,
Unicode line and paragraph separators), reorder the text next to it (bidi controls) nor take a row's width.
The plugin applies the same rule when it records a name (`cleanName` in `hooks/records.ts`); the capture
check counts the names this function would change.

The rule is by general category, not `str.isprintable`: unassigned code points (Cn) are kept, so Python's and
JavaScript's Unicode versions cannot disagree about a name the plugin just recorded. What is
dropped (Cc, Cf, Cs, Co, Zl, Zp and every Zs but U+0020) is fixed in practice. Both sides scan the first
`4 * MAX_NAME_CHARS` code points.
"""

from __future__ import annotations

import unicodedata

MAX_NAME_CHARS = 64  # same cap as the plugin's `MAX_TYPE_CHARS`
_SCAN_CHARS = 4 * MAX_NAME_CHARS  # only this much of an input is looked at, so a megabyte name costs the same
_DROPPED = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp", "Zs"})


def _keeps(char: str) -> bool:
    """Whether `char` can stay in a name: a space only as U+0020, and none of the control, format, surrogate,
    private-use or separator categories."""
    return char == " " or unicodedata.category(char) not in _DROPPED


def clean_name(value: object) -> str | None:
    """`value` as a printable one-line name of at most `MAX_NAME_CHARS` characters, or None when it is not a
    string or nothing printable is left. Characters that `_keeps` refuses (controls, format characters
    such as bidi overrides, line and paragraph separators, unpaired surrogates, private use, every space but
    U+0020) are dropped, not replaced, so a name cannot be spelled with them."""
    if not isinstance(value, str):
        return None
    kept = "".join(c for c in value[:_SCAN_CHARS] if _keeps(c)).strip()[:MAX_NAME_CHARS].strip()
    return kept or None


def clean_name_or(value: object, fallback: str = "?") -> str:
    """`clean_name`, or `fallback` for a value that has no printable name."""
    return clean_name(value) or fallback


def agent_label(value: object, default: str) -> str:
    """The label of an agent in a view: `default` when `value` is missing or empty (the main loop), else the
    cleaned name, or `?` when nothing printable is left. A number is read as its text, like `str(value)`."""
    if value is None or value == "":
        return default
    return clean_name_or(value if isinstance(value, str) else str(value))


SUBAGENT_NAMED_MAIN = "main (subagent)"  # what a custom agent called `main` is shown as


def subagent_label(value: object, default: str) -> str:
    """`agent_label` for a subagent. A custom agent may be named `main` (any case, or spelled with characters the
    cleaning drops): shown as itself it would merge into a report's main row, so it gets `SUBAGENT_NAMED_MAIN`.
    A name equal to a built-in type stays: that is the type's own name."""
    label = agent_label(value, default)
    return SUBAGENT_NAMED_MAIN if label.casefold() == "main" else label


def name_is_clean(value: object) -> bool:
    """Whether `value` is a string `clean_name` would leave as it is (a non-string or an empty string is not
    a name to print: clean)."""
    return not isinstance(value, str) or value == "" or clean_name(value) == value


def printable_line(text: str) -> str:
    """`text` with every character `_keeps` refuses replaced by `?`, newline and tab included: one line of
    printable characters of the same length. Unassigned code points (Cn) stay, as in a name."""
    return "".join(c if _keeps(c) else "?" for c in text)


def printable_text(text: str) -> str:
    """`text` with every character a terminal or a model could read as a command replaced by `?`: all but
    newline, tab and the ones a name may keep. It cannot stop a newline: a name is cleaned at its source. The last line of defence for output handed on (the usage-reflection
    runner), after the views have cleaned the names they print."""
    return "".join(c if c in "\n\t" or _keeps(c) else "?" for c in text)
