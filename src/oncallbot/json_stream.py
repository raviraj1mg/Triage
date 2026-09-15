"""Read a JSON object as it is being written.

The summarizer returns one JSON object, not prose, so "streaming a summary"
means surfacing each field as the model finishes writing it. This scanner is
fed raw model output and reports two things: a fragment of the value currently
being written, and a field that has just closed.

It is deliberately shallow. Only top-level string fields are reported, because
those are the ones a card shows while it waits (`summary`, then `issue`, then
`category` and `severity`). Numbers, arrays and nested objects are left to the
authoritative `json.loads` of the complete text at the end -- this scanner
never decides what the summary *is*, it only decides what can be shown early.
"""

from __future__ import annotations

_UNESCAPE = {
    '"': '"', "\\": "\\", "/": "/",
    "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
}


class JsonFieldStream:
    """Feed chunks, get ("delta", key, fragment) and ("field", key, value).

    A "delta" is part of a top-level string value, decoded, in order. A "field"
    is that value once its closing quote arrives. `text` is everything fed so
    far, for the final parse.
    """

    def __init__(self) -> None:
        self._chunks: list[str] = []
        self._depth = 0
        self._in_str = False
        self._esc = False
        self._u = ""            # a \uXXXX escape being collected
        self._cur: list[str] = []   # the string being read, decoded
        self._key = ""          # the most recent top-level key
        self._value = False     # is the current string a value, not a key?

    @property
    def text(self) -> str:
        return "".join(self._chunks)

    def feed(self, chunk: str) -> list[tuple[str, str, str]]:
        self._chunks.append(chunk)
        events: list[tuple[str, str, str]] = []
        # Decoded characters of the value being written, flushed as one delta
        # per chunk rather than one per character.
        out: list[str] = []

        for ch in chunk:
            if self._in_str:
                if self._u:
                    self._u += ch
                    if len(self._u) == 5:      # "u" plus four hex digits
                        try:
                            dec = chr(int(self._u[1:], 16))
                        except ValueError:
                            dec = ""
                        self._u = ""
                        self._emit(dec, out)
                    continue
                if self._esc:
                    self._esc = False
                    if ch == "u":
                        self._u = "u"
                        continue
                    self._emit(_UNESCAPE.get(ch, ch), out)
                    continue
                if ch == "\\":
                    self._esc = True
                    continue
                if ch == '"':
                    self._in_str = False
                    value = "".join(self._cur)
                    self._cur = []
                    if self._depth == 1 and self._value:
                        if out:
                            events.append(("delta", self._key, "".join(out)))
                            out = []
                        events.append(("field", self._key, value))
                        self._value = False
                    elif self._depth == 1:
                        self._key = value
                    continue
                self._emit(ch, out)
                continue

            if ch == '"':
                self._in_str = True
                self._cur = []
            elif ch in "{[":
                self._depth += 1
                # A fresh container expects a key, not a value -- otherwise a
                # colon in prose before the object ("Here you go:") would make
                # the first key look like a value.
                self._value = False
            elif ch in "}]":
                self._depth -= 1
                self._value = False
            elif ch == ":":
                self._value = True
            elif ch == ",":
                self._value = False

        if out:
            events.append(("delta", self._key, "".join(out)))
        return events

    def _emit(self, ch: str, out: list[str]) -> None:
        self._cur.append(ch)
        # Only a top-level value is worth showing early; a key, or anything
        # nested, is structure the reader does not need to watch arrive.
        if self._depth == 1 and self._value:
            out.append(ch)
