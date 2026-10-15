"""Cut, from the bytes of a NeTEx document, the elements no reading pass needs.

Most of a national NeTEx file is what the stop extract never reads: the
passing times of every journey, the calendars, the links, the scheduled
stop points. Handing those bytes to expat costs a Python call per element
(some 14 MB/s), while finding where they start and end in the raw bytes
costs a few searches in C. So, once the root element has begun,
`SkipFilter` streams the document and drops every element whose name is in
its set, start tag to end tag, before expat sees it; expat parses what is
left, with all its bounds.

The filter is exact on a well-formed document: a start tag is found only
outside comments, CDATA sections and processing instructions (which are
passed through, or dropped with the element around them), the end of a
start tag is found outside its quoted attribute values (a `>` may stand in
one), a self-closing element is dropped whole, and an element of the same
name nested in a dropped one is counted. It keeps no more than a few dozen
bytes between two chunks, whatever the input: nothing it drops is held. Its
work is linear in the input: inside a dropped element each search (the next
end tag, nested start tag, `<!`, `<?`) runs once over a stretch of a chunk,
its result kept until passed, so that many comments or nested tags cost no
more than their bytes. It takes over from expat at a token boundary (`prime`:
the bytes expat already parsed after the root's start tag are only read, to
know whether a comment, CDATA section or processing instruction is open). A
document it cuts badly (one that is not well-formed) is refused by expat,
as before. The names are compared with the prefix of the root element (or
with none): an element of the set written with another prefix is not cut,
only parsed (and then not built), as before.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

_TAG_END = re.compile(rb"[\"'>]")
_PREFIX = re.compile(rb"<([A-Za-z_][\w.\-]{0,63}):[A-Za-z_]", re.ASCII)
_DELIMITERS = frozenset(b" \t\r\n/>")
# What may open inside a dropped element and hide a tag: a comment, a CDATA
# section, a processing instruction; and what ends each.
_OPENERS = ((b"<!--", b"-->"), (b"<![CDATA[", b"]]>"), (b"<?", b"?>"))
_LONGEST_OPENER = 9


def root_prefix(window: bytes, index: int) -> bytes:
    """The namespace prefix of the start tag at `index` of `window` (b"" when
    it has none or when the tag is not there)."""
    match = _PREFIX.match(window, index) if 0 <= index < len(window) else None
    return match.group(1) if match else b""


def start_tag_end(window: bytes, index: int) -> int | None:
    """Where the start tag at `index` of `window` ends (just after its `>`,
    found outside quoted values), or None when it does not end there."""
    position = index
    while True:
        match = _TAG_END.search(window, position)
        if match is None:
            return None
        at = match.start()
        if window[at] == 0x3E:
            return at + 1
        close = window.find(window[at : at + 1], at + 1)
        if close < 0:
            return None
        position = close + 1


def _opener(buffer: bytes, index: int) -> tuple[int, bytes] | None:
    """The length and the terminator of what opens at `index`, or None."""
    for opener, terminator in _OPENERS:
        if buffer.startswith(opener, index):
            return len(opener), terminator
    return None


class SkipFilter:
    """Streams a document, dropping the elements named in `names`.

    `feed(data)` returns the bytes to parse; `finish()` the bytes left at the
    end. `on_cut(name)` is called with the local name of each element
    dropped (the reader records which members hold which elements)."""

    def __init__(self, names: Iterable[str], prefix: bytes, on_cut: Callable[[str], None]) -> None:
        encoded = sorted({name.encode("ascii") for name in names}, key=len, reverse=True)
        self._qualifier = prefix + b":" if prefix else b""
        alternatives = b"|".join(re.escape(name) for name in encoded)
        self._outside = re.compile(
            rb"<(?:(!--)|(!\[CDATA\[)|(\?)|"
            + re.escape(self._qualifier)
            + rb"("
            + alternatives
            + rb")(?=[\s/>]))"
        )
        # The longest match that can start near the end of a chunk.
        longest = max((len(n) for n in encoded), default=0)
        self._lookahead = max(_LONGEST_OPENER + 1, len(self._qualifier) + longest + 4)
        self._on_cut = on_cut
        self._carry = b""
        self._cutting = False  # inside an element being dropped
        self._open_tag = b""  # `<name` of the element being dropped
        self._close_tag = b""  # `</name`
        self._depth = 0  # open elements of the dropped name
        self._until: bytes | None = None  # the end being looked for: `-->`, `]]>`, `?>`, `>`
        self._in_tag = False  # inside a start tag of the dropped name
        self._quote: bytes | None = None
        self._previous = 0  # the byte before the next one, inside a start tag
        # The next bytes expat has already parsed: read for the state only.
        self._parsed = 0
        # Inside a dropped element, the searches already made in this chunk:
        # their results hold until the position passes them.
        self._found: dict[str, int] = {}
        self._searched: dict[str, int] = {}
        self._simple = False  # the first step of a dropped element

    def prime(self, parsed: bytes) -> None:
        """Read `parsed`, bytes expat has already parsed from a token
        boundary on, for the state only: nothing is given back or dropped
        from them, an element of the set begun there is not dropped."""
        self._parsed = len(parsed)
        self._run(parsed, final=False)

    def feed(self, data: bytes) -> bytes:
        return self._run(self._carry + data, final=False)

    def finish(self) -> bytes:
        return self._run(self._carry, final=True)

    def _keep(self, kept: list[bytes], buffer: bytes, start: int, stop: int) -> None:
        start = max(start, self._parsed)
        if start < stop:
            kept.append(buffer[start:stop])

    def _run(self, buffer: bytes, *, final: bool) -> bytes:
        kept: list[bytes] = []
        position = 0
        size = len(buffer)
        self._found = {}
        self._searched = {}
        while position < size:
            if self._until is not None:
                position, done = self._to_terminator(buffer, position, kept, final)
            elif self._in_tag:
                position, done = self._to_tag_end(buffer, position)
            elif self._cutting:
                position, done = self._in_cut(buffer, position, final)
            else:
                position, done = self._outside_cut(buffer, position, kept, final)
            if not done:
                break
        self._carry = buffer[position:]
        self._parsed = max(0, self._parsed - position)
        return b"".join(kept)

    def _to_terminator(
        self, buffer: bytes, position: int, kept: list[bytes], final: bool
    ) -> tuple[int, bool]:
        """Inside a comment, a CDATA section, a processing instruction (kept
        unless inside a dropped element) or an end tag (dropped)."""
        until = self._until or b">"
        end = buffer.find(until, position)
        if end < 0:
            stop = len(buffer) if final else max(position, len(buffer) - len(until) + 1)
            if not self._cutting:
                self._keep(kept, buffer, position, stop)
            return stop, False
        stop = end + len(until)
        if not self._cutting:
            self._keep(kept, buffer, position, stop)
        self._until = None
        if self._cutting and until == b">" and self._depth == 0:
            self._cutting = False  # the end tag of the dropped element
        return stop, True

    def _to_tag_end(self, buffer: bytes, position: int) -> tuple[int, bool]:
        """Inside a start tag of the dropped name: its end is the first `>`
        outside a quoted value; `/>` closes it at once."""
        while True:
            if self._quote is not None:
                end = buffer.find(self._quote, position)
                if end < 0:
                    break
                self._quote = None
                position = end + 1
                continue
            match = _TAG_END.search(buffer, position)
            if match is None:
                break
            index = match.start()
            if buffer[index] == 0x3E:
                return self._tag_closed(buffer, index), True
            self._quote = buffer[index : index + 1]  # a quoted value opens
            position = index + 1
        self._previous = buffer[-1]
        return len(buffer), False

    def _tag_closed(self, buffer: bytes, index: int) -> int:
        """The `>` at `index` ends the start tag: the element is open unless
        it was `/>`. Returns where to go on."""
        before = buffer[index - 1] if index > 0 else self._previous
        self._in_tag = False
        if before != 0x2F:  # not `/>`: the element is open
            self._depth += 1
        if self._depth == 0:
            self._cutting = False
        return index + 1

    @staticmethod
    def _tag(buffer: bytes, needle: bytes, start: int, stop: int) -> int:
        """The first `needle` in `buffer[start:stop]` followed by a delimiter
        (a blank, `/` or `>`), or -1; one whose next byte is not there yet is
        -1 too (the caller keeps the end of the chunk for the next one)."""
        while True:
            index = buffer.find(needle, start, stop)
            after = index + len(needle)
            if index < 0 or after >= len(buffer):
                return -1
            if buffer[after] in _DELIMITERS:
                return index
            start = index + 1

    @staticmethod
    def _markup(buffer: bytes, mark: int, start: int, stop: int) -> int:
        """The first `<!` (`mark` 0x21) or `<?` (0x3F) in `buffer[start:stop]`,
        or -1. Found by the rare byte, not by `<`, which opens every tag."""
        index = buffer.find(mark, start + 1, stop)
        while index >= 0 and buffer[index - 1] != 0x3C:
            index = buffer.find(mark, index + 1, stop)
        return index - 1 if index >= 0 else -1

    def _search(self, key: str, found: int, searched: int) -> int:
        """Keep and give the result of a search: `found` (or -1), and how
        far nothing was found (`searched`)."""
        self._found[key] = found
        self._searched[key] = searched if found < 0 else found
        return found

    def _known(self, key: str, position: int, limit: int) -> int | None:
        """What an earlier search of this chunk says of `[position, limit)`:
        the first match there, -1 for none, or None when it must be made."""
        found = self._found.get(key)
        if found is None:
            return None
        if found >= position:
            return found if found < limit else -1
        if found < 0 and self._searched[key] >= limit:
            return -1
        return None

    def _first(self, key: str, buffer: bytes, position: int, limit: int) -> int:
        """The first match of `key` in `buffer[position:limit]`, or -1: an
        earlier search of this chunk is used when it answers, so that each
        stretch of the chunk is searched once (linear, whatever the input)."""
        known = self._known(key, position, limit)
        if known is not None:
            return known
        if key == "end":
            found = self._tag(buffer, self._close_tag, position, len(buffer))
            return self._search(key, found, len(buffer))
        if key == "start":
            found = self._tag(buffer, self._open_tag, position, limit)
        else:
            found = self._markup(buffer, 0x21 if key == "!" else 0x3F, position, limit)
        return self._search(key, found, limit)

    def _in_cut(self, buffer: bytes, position: int, final: bool) -> tuple[int, bool]:
        """Inside a dropped element: the next end tag of its name, unless a
        comment, CDATA section, processing instruction or nested start tag of
        the same name comes first. Searched with `bytes.find`, in C, each
        search only up to the first candidate already known (`_first`)."""
        size = len(buffer)
        if self._simple:
            # The first step of a dropped element (the passing times of a
            # journey): when its end tag comes with nothing before it that
            # needs care, straight there. Tried once per element, and what it
            # searches is passed over: linear.
            self._simple = False
            end = self._tag(buffer, self._close_tag, position, size)
            if (
                end >= 0
                and self._tag(buffer, self._open_tag, position, end) < 0
                and self._markup(buffer, 0x21, position, end) < 0
                and self._markup(buffer, 0x3F, position, end) < 0
            ):
                self._depth -= 1
                self._until = b">"
                return end + len(self._close_tag), True
        end = self._first("end", buffer, position, size)
        limit = end if end >= 0 else size
        nested = self._first("start", buffer, position, limit)
        if nested >= 0:
            limit = nested
        bang = self._first("!", buffer, position, limit)
        if bang >= 0:
            limit = bang
        mark = self._first("?", buffer, position, limit)
        found = [at for at in (end, nested, bang, mark) if at >= 0]
        if not found:
            return (size if final else max(position, size - self._lookahead)), False
        first = min(found)
        if first in (bang, mark):
            return self._markup_in_cut(buffer, first, final)
        if first == nested:
            self._in_tag = True
            self._quote = None
            self._previous = buffer[nested + len(self._open_tag) - 1]
            return nested + len(self._open_tag), True
        self._depth -= 1
        self._until = b">"
        return end + len(self._close_tag), True

    def _markup_in_cut(self, buffer: bytes, special: int, final: bool) -> tuple[int, bool]:
        found = _opener(buffer, special)
        if found is None:
            if not final and special + _LONGEST_OPENER > len(buffer):
                return special, False  # the rest of it is in the next chunk
            return special + 2, True  # `<!` of nothing that hides a tag
        opened, terminator = found
        end = buffer.find(terminator, special + opened)
        if end >= 0:  # ended in this chunk: dropped at once
            return end + len(terminator), True
        self._until = terminator
        return special + opened, True

    def _outside_cut(
        self, buffer: bytes, position: int, kept: list[bytes], final: bool
    ) -> tuple[int, bool]:
        match = self._outside.search(buffer, position)
        if match is None:
            stop = len(buffer) if final else max(position, len(buffer) - self._lookahead)
            self._keep(kept, buffer, position, stop)
            return stop, False
        if match.group(4) is None:
            self._keep(kept, buffer, position, match.end())
            found = _opener(buffer, match.start())
            self._until = found[1] if found else b">"
            return match.end(), True
        if match.start() < self._parsed:
            # begun in what expat has parsed already: not dropped
            self._keep(kept, buffer, position, match.end())
            return match.end(), True
        self._keep(kept, buffer, position, match.start())
        name = match.group(4)
        self._on_cut(name.decode("ascii"))
        self._cutting = True
        self._depth = 0
        self._open_tag = b"<" + self._qualifier + name
        self._close_tag = b"</" + self._qualifier + name
        for key in ("end", "start"):
            self._found.pop(key, None)
        self._simple = True
        self._in_tag = True
        self._quote = None
        self._previous = buffer[match.end() - 1]
        return match.end(), True
