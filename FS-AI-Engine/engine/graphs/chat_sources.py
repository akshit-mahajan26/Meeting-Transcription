"""Parsing and streaming-time cleanup of the `SOURCES:` line and inline `[n]` citation markers."""

from __future__ import annotations

import re

_SOURCES_LINE = re.compile(r"^[\s*_`>#-]*sources?[\s*_`]*:[\s*_`]*(.*?)[\s*_`]*$", re.IGNORECASE | re.MULTILINE)


_INLINE_MARKER = re.compile(r"[ \t]*\[\s*\d+(?:\s*[,;\u2013-]\s*\d+)*\s*\]")

_SOURCES_START = re.compile(r"^sources?[\s*_`]*(:.*)?$", re.IGNORECASE)
_OPEN_MARKER = re.compile(
    r"[ \t]*(?:\[[\s\d,;\u2013-]*)?$"
)  # trailing spaces and/or an unfinished `[1, 2`: may belong to a marker
_LINE_LEAD = " \t*_`>#-"


def _could_be_sources_line(line: str) -> bool:
    """True while `line` is still compatible with a `SOURCES: ...` line (so it must not be shown yet)."""
    core = line.lstrip(_LINE_LEAD)
    return not core or "sources".startswith(core.lower()) or bool(_SOURCES_START.match(core))


class AnswerStreamFilter:
    """
    Cleans a streamed answer the way `split_sources_line` cleans the final one, so the reader never sees the
    `SOURCES:` line or `[3]` excerpt markers flash by: lines that might still turn out to be a SOURCES line are
    held back, and so are trailing spaces and an unfinished `[1, 2` marker. The final event carries the canonical text regardless.
    """

    def __init__(self) -> None:
        self._pending = ""  # text of the current line that has not been released yet
        self._live = False  # the current line cannot be a SOURCES line any more, so it is released as it arrives

    def feed(self, delta: str) -> str:
        """Takes the next text delta and returns the text that is safe to show now."""
        out: list[str] = []
        for ch in delta:
            if ch == "\n":
                out.append(self._finish_line())
                continue
            self._pending += ch
            if not self._live and not _could_be_sources_line(self._pending):
                self._live = True
        if self._live:
            cut = _OPEN_MARKER.search(self._pending)
            ready, self._pending = (
                (self._pending[: cut.start()], self._pending[cut.start() :]) if cut else (self._pending, "")
            )
            out.append(_INLINE_MARKER.sub("", ready))
        return "".join(out)

    def flush(self) -> str:
        """Called when the stream ends: releases whatever is still held."""
        line, live = self._pending, self._live
        self._pending, self._live = "", False
        if not live and _SOURCES_LINE.match(line):
            return ""
        return _INLINE_MARKER.sub("", line)

    def _finish_line(self) -> str:
        line, live = self._pending, self._live
        self._pending, self._live = "", False
        if not live and _SOURCES_LINE.match(line):
            return ""  # the SOURCES line and its newline are dropped
        return _INLINE_MARKER.sub("", line) + "\n"


def split_sources_line(answer: str, n_chunks: int) -> tuple[str, list[int] | None]:
    """
    Returns (clean answer, 1-based excerpt numbers the answer relied on).
    - Removes all `SOURCES: ...` lines the model appends (both per-section and trailing).
    - Removes inline `[11, 13]` excerpt markers the model may still write; they mean nothing to the user.
    - Numbers are limited to the excerpts provided. `none` gives [].
    - Aggregates reported source indices across all occurrences in the answer text.
    - With no usable SOURCES line, inline markers are used if present, else None so the caller
      can fall back to heuristic alignment.
    """

    def valid(nums: list[str]) -> list[int]:
        return list(dict.fromkeys(int(n) for n in nums if 1 <= int(n) <= n_chunks))

    inline = valid([n for m in _INLINE_MARKER.findall(answer) for n in re.findall(r"\d+", m)])
    matches = list(_SOURCES_LINE.finditer(answer))
    reported: list[int] | None = None
    if matches:
        all_nums: list[str] = []
        has_numbers = False
        all_none = True
        for m in matches:
            value = m.group(1).strip().lower()
            if value in {"none", "n/a", "no sources", "-", ""}:
                continue
            all_none = False
            nums = re.findall(r"\d+", value)
            if nums:
                has_numbers = True
                all_nums.extend(nums)

        if has_numbers:
            reported = valid(all_nums)
        elif all_none:
            reported = []
        else:
            reported = None

        # Globally remove all SOURCES lines so none leak into user-facing output
        answer = _SOURCES_LINE.sub("", answer)

    clean = _INLINE_MARKER.sub("", answer).strip()
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    if reported is None and inline:
        reported = inline
    return clean, reported
