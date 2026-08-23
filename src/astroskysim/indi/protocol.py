"""INDI property vectors and their wire representation.

An INDI device is entirely described by its properties. Each property is a
*vector* of one or more named items, of exactly one of five types: Text, Number,
Switch, Light or BLOB. The device announces them with ``def*Vector``, pushes
value changes with ``set*Vector``, and the client writes with ``new*Vector``.

Only the device may send ``def``/``set``; only the client may send ``new``.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from xml.sax.saxutils import escape, quoteattr


def utc_now() -> str:
    """INDI timestamp: ISO-8601 UTC, no offset suffix, millisecond precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


class PropState(str, Enum):
    IDLE = "Idle"
    OK = "Ok"
    BUSY = "Busy"
    ALERT = "Alert"


class Perm(str, Enum):
    RO = "ro"
    WO = "wo"
    RW = "rw"


class SwitchRule(str, Enum):
    ONE_OF_MANY = "OneOfMany"
    AT_MOST_ONE = "AtMostOne"
    ANY_OF_MANY = "AnyOfMany"


def parse_number(text: str) -> float:
    """Parse an INDI number, accepting sexagesimal as ``f_scansexa`` does.

    Clients are free to send ``12:30:45``, ``12 30 45`` or ``12.5125``, and the
    sign on the first component applies to the whole value.

    >>> parse_number("-12:30")
    -12.5
    """
    s = text.strip()
    if not s:
        raise ValueError("empty number")
    sep = ":" if ":" in s else (" " if " " in s else None)
    if sep is None:
        return float(s)
    parts = [p for p in s.split(sep) if p]
    negative = parts[0].lstrip().startswith("-")
    total = 0.0
    for i, p in enumerate(parts[:3]):
        total += abs(float(p)) / (60.0**i)
    return -total if negative else total


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------
@dataclass(slots=True)
class TextItem:
    name: str
    label: str = ""
    value: str = ""

    def def_xml(self) -> str:
        return (
            f"  <defText name={quoteattr(self.name)} label={quoteattr(self.label or self.name)}>"
            f"{escape(self.value)}</defText>\n"
        )

    def one_xml(self) -> str:
        return f"  <oneText name={quoteattr(self.name)}>{escape(self.value)}</oneText>\n"


@dataclass(slots=True)
class NumberItem:
    name: str
    label: str = ""
    value: float = 0.0
    fmt: str = "%.4f"
    min: float = 0.0
    max: float = 0.0
    step: float = 0.0

    def _fmt(self) -> str:
        # "%m" is a display hint for sexagesimal; the wire value stays decimal.
        f = self.fmt.replace("m", "f") if self.fmt.endswith("m") else self.fmt
        try:
            return f % self.value
        except (TypeError, ValueError):
            return repr(self.value)

    def def_xml(self) -> str:
        return (
            f"  <defNumber name={quoteattr(self.name)} label={quoteattr(self.label or self.name)}"
            f" format={quoteattr(self.fmt)} min={quoteattr(repr(self.min))}"
            f" max={quoteattr(repr(self.max))} step={quoteattr(repr(self.step))}>"
            f"{self._fmt()}</defNumber>\n"
        )

    def one_xml(self) -> str:
        return f"  <oneNumber name={quoteattr(self.name)}>{self._fmt()}</oneNumber>\n"


@dataclass(slots=True)
class SwitchItem:
    name: str
    label: str = ""
    value: bool = False

    @property
    def state_str(self) -> str:
        return "On" if self.value else "Off"

    def def_xml(self) -> str:
        return (
            f"  <defSwitch name={quoteattr(self.name)} label={quoteattr(self.label or self.name)}>"
            f"{self.state_str}</defSwitch>\n"
        )

    def one_xml(self) -> str:
        return f"  <oneSwitch name={quoteattr(self.name)}>{self.state_str}</oneSwitch>\n"


@dataclass(slots=True)
class LightItem:
    name: str
    label: str = ""
    value: PropState = PropState.IDLE

    def def_xml(self) -> str:
        return (
            f"  <defLight name={quoteattr(self.name)} label={quoteattr(self.label or self.name)}>"
            f"{self.value.value}</defLight>\n"
        )

    def one_xml(self) -> str:
        return f"  <oneLight name={quoteattr(self.name)}>{self.value.value}</oneLight>\n"


@dataclass(slots=True)
class BlobItem:
    name: str
    label: str = ""
    #: Raw payload. Encoded to base64 only at send time.
    data: bytes = b""
    #: File-extension style hint, e.g. ".fits" or ".fits.z" when deflated.
    fmt: str = ".fits"

    def def_xml(self) -> str:
        return (
            f"  <defBLOB name={quoteattr(self.name)} "
            f"label={quoteattr(self.label or self.name)}/>\n"
        )

    def one_xml(self) -> str:
        payload = base64.b64encode(self.data).decode("ascii")
        return (
            f"  <oneBLOB name={quoteattr(self.name)} size={quoteattr(str(len(self.data)))}"
            f" format={quoteattr(self.fmt)}>\n{payload}\n  </oneBLOB>\n"
        )


Item = TextItem | NumberItem | SwitchItem | LightItem | BlobItem


# --------------------------------------------------------------------------
# Vectors
# --------------------------------------------------------------------------
@dataclass
class Vector:
    """One INDI property. Subclasses only pick the wire type name."""

    name: str
    label: str = ""
    group: str = "Main Control"
    state: PropState = PropState.IDLE
    perm: Perm = Perm.RW
    timeout: int = 60
    items: list[Item] = field(default_factory=list)
    #: Set False to keep a property out of the announced set (capability gating).
    enabled: bool = True

    #: Wire type, e.g. "Text" -> defTextVector / setTextVector.
    kind: str = ""

    def __getitem__(self, name: str) -> Item:
        for it in self.items:
            if it.name == name:
                return it
        raise KeyError(f"{self.name} has no item {name!r}")

    def __contains__(self, name: str) -> bool:
        return any(it.name == name for it in self.items)

    def get(self, name: str, default: Item | None = None) -> Item | None:
        try:
            return self[name]
        except KeyError:
            return default

    def _attrs(self, device: str, *, with_perm: bool, message: str) -> str:
        a = f" device={quoteattr(device)} name={quoteattr(self.name)}"
        if with_perm:
            a += f" label={quoteattr(self.label or self.name)} group={quoteattr(self.group)}"
        a += f" state={quoteattr(self.state.value)}"
        if with_perm:
            a += f" perm={quoteattr(self.perm.value)} timeout={quoteattr(str(self.timeout))}"
        a += f" timestamp={quoteattr(utc_now())}"
        if message:
            a += f" message={quoteattr(message)}"
        return a

    def def_xml(self, device: str, message: str = "") -> str:
        body = "".join(it.def_xml() for it in self.items)
        a = self._attrs(device, with_perm=True, message=message)
        return f"<def{self.kind}Vector{a}>\n{body}</def{self.kind}Vector>\n"

    def set_xml(self, device: str, message: str = "", only: list[str] | None = None) -> str:
        items = [it for it in self.items if only is None or it.name in only]
        body = "".join(it.one_xml() for it in items)
        a = self._attrs(device, with_perm=False, message=message)
        return f"<set{self.kind}Vector{a}>\n{body}</set{self.kind}Vector>\n"


@dataclass
class TextVector(Vector):
    kind: str = "Text"


@dataclass
class NumberVector(Vector):
    kind: str = "Number"


@dataclass
class LightVector(Vector):
    kind: str = "Light"
    perm: Perm = Perm.RO


@dataclass
class BlobVector(Vector):
    kind: str = "BLOB"
    perm: Perm = Perm.RO


@dataclass
class SwitchVector(Vector):
    kind: str = "Switch"
    rule: SwitchRule = SwitchRule.ONE_OF_MANY

    def def_xml(self, device: str, message: str = "") -> str:
        body = "".join(it.def_xml() for it in self.items)
        a = self._attrs(device, with_perm=True, message=message)
        a += f" rule={quoteattr(self.rule.value)}"
        return f"<defSwitchVector{a}>\n{body}</defSwitchVector>\n"

    def apply(self, changes: dict[str, str]) -> None:
        """Apply a client write, honouring the switch rule."""
        wanted = {k: v.strip().lower() == "on" for k, v in changes.items()}
        if self.rule is SwitchRule.ANY_OF_MANY:
            for k, on in wanted.items():
                if (it := self.get(k)) is not None:
                    it.value = on  # type: ignore[union-attr]
            return
        # OneOfMany / AtMostOne: at most one On, and turning one on clears
        # the rest. Clients routinely send only the item they want set.
        turned_on = [k for k, on in wanted.items() if on]
        if turned_on:
            for it in self.items:
                it.value = it.name == turned_on[-1]  # type: ignore[union-attr]
        elif self.rule is SwitchRule.AT_MOST_ONE:
            for k in wanted:
                if (it := self.get(k)) is not None:
                    it.value = False  # type: ignore[union-attr]
        # OneOfMany with an all-Off write is invalid; keep the previous state.

    @property
    def selected(self) -> str | None:
        for it in self.items:
            if it.value:  # type: ignore[union-attr]
                return it.name
        return None


def message_xml(device: str, text: str) -> str:
    return (
        f"<message device={quoteattr(device)} timestamp={quoteattr(utc_now())} "
        f"message={quoteattr(text)}/>\n"
    )


def del_property_xml(device: str, name: str = "") -> str:
    a = f" device={quoteattr(device)}"
    if name:
        a += f" name={quoteattr(name)}"
    return f"<delProperty{a} timestamp={quoteattr(utc_now())}/>\n"


# Convenience constructors for the two switch vectors every device needs.
def connection_vector() -> SwitchVector:
    return SwitchVector(
        name="CONNECTION",
        label="Connection",
        group="Main Control",
        rule=SwitchRule.ONE_OF_MANY,
        items=[
            SwitchItem("CONNECT", "Connect", False),
            SwitchItem("DISCONNECT", "Disconnect", True),
        ],
    )


def config_process_vector() -> SwitchVector:
    return SwitchVector(
        name="CONFIG_PROCESS",
        label="Configuration",
        group="Options",
        rule=SwitchRule.AT_MOST_ONE,
        items=[
            SwitchItem("CONFIG_LOAD", "Load"),
            SwitchItem("CONFIG_SAVE", "Save"),
            SwitchItem("CONFIG_DEFAULT", "Default"),
            SwitchItem("CONFIG_PURGE", "Purge"),
        ],
    )
