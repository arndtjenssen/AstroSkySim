"""Incremental splitter for the INDI wire format.

The INDI stream is *not* a well formed XML document: it is an endless sequence of
top level elements with no enclosing root. Handing it to a normal parser blocks
forever waiting for a root close tag, so we cut the stream into complete top
level elements here and parse each one on its own.

The subtle part is quoting: attribute values may legally contain ``<``, ``>``
and ``/``, so depth may only be adjusted while outside a quoted attribute value.
A splitter that counts those characters unconditionally desynchronises on the
first attribute that holds one, and never recovers.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

#: A single element larger than this is refused rather than buffered forever.
#: BLOBs (a full frame, base64 encoded) are the reason this is generous.
MAX_ELEMENT_BYTES = 256 * 1024 * 1024


class XmlStreamError(Exception):
    """The peer sent something we cannot resynchronise from."""


class XmlStreamSplitter:
    """Feed bytes/str in, get complete top level element strings out.

    >>> s = XmlStreamSplitter()
    >>> s.feed('<getProperties version="1.7"/>')
    ['<getProperties version="1.7"/>']
    >>> s.feed('<defTextVector><oneText>a')      # partial - nothing emitted yet
    []
    >>> s.feed('</oneText></defTextVector>')
    ['<defTextVector><oneText>a</oneText></defTextVector>']
    """

    def __init__(self, max_element_bytes: int = MAX_ELEMENT_BYTES) -> None:
        self._buf: list[str] = []
        self._size = 0
        self._depth = 0
        self._max = max_element_bytes
        # Tag scanner state
        self._in_tag = False
        self._quote: str | None = None
        self._tag: list[str] = []

    def reset(self) -> None:
        self._buf.clear()
        self._size = 0
        self._depth = 0
        self._in_tag = False
        self._quote = None
        self._tag.clear()

    def feed(self, data: str | bytes) -> list[str]:
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")

        out: list[str] = []
        for ch in data:
            if not self._in_tag:
                if ch == "<":
                    self._in_tag = True
                    self._tag = ["<"]
                    self._append(ch)
                elif self._depth > 0:
                    # Character data inside an element - keep it.
                    self._append(ch)
                # else: whitespace between top level elements, discard.
                continue

            # --- inside a tag ---
            self._append(ch)
            self._tag.append(ch)

            if self._quote is not None:
                if ch == self._quote:
                    self._quote = None
                continue
            if ch in ("'", '"'):
                self._quote = ch
                continue
            if ch != ">":
                continue

            # Tag complete.
            tag = "".join(self._tag)
            self._in_tag = False
            self._tag = []

            if tag.startswith("<?") or tag.startswith("<!"):
                # Declaration, comment or CDATA marker: not a container.
                # A comment containing '>' would end early, but INDI does not
                # emit comments and mis-splitting one only affects that element.
                if self._depth == 0:
                    self._discard()
                continue
            if tag.startswith("</"):
                self._depth -= 1
                if self._depth <= 0:
                    out.append(self._take())
                continue
            if tag.endswith("/>"):
                if self._depth == 0:
                    out.append(self._take())
                continue
            self._depth += 1

        return out

    # -- buffer helpers ----------------------------------------------------
    def _append(self, ch: str) -> None:
        self._size += 1
        if self._size > self._max:
            self.reset()
            raise XmlStreamError(f"element exceeded {self._max} bytes")
        self._buf.append(ch)

    def _take(self) -> str:
        s = "".join(self._buf)
        self._buf.clear()
        self._size = 0
        self._depth = 0
        return s

    def _discard(self) -> None:
        self._buf.clear()
        self._size = 0


@dataclass(slots=True)
class Element:
    """One parsed top level INDI element."""

    tag: str
    attrib: dict[str, str] = field(default_factory=dict)
    text: str = ""
    children: list[Element] = field(default_factory=list)

    def get(self, key: str, default: str = "") -> str:
        return self.attrib.get(key, default)

    @property
    def device(self) -> str:
        return self.attrib.get("device", "")

    @property
    def name(self) -> str:
        return self.attrib.get("name", "")

    def child_values(self) -> dict[str, str]:
        """``{name: text}`` over children - the usual shape of a new*Vector."""
        return {c.name: c.text for c in self.children if c.name}


def parse_element(raw: str) -> Element:
    """Parse one complete top level element. Raises ``XmlStreamError``."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise XmlStreamError(f"malformed element: {exc}") from exc
    return _convert(root)


def _convert(node: ET.Element) -> Element:
    return Element(
        tag=node.tag,
        attrib=dict(node.attrib),
        text=(node.text or "").strip(),
        children=[_convert(c) for c in node],
    )
