"""Downloading and parsing two-line element sets.

Celestrak serves every group from one endpoint, so a source stores the *group
name* and the URL is derived — an entry keeps working when the query string
changes, and it cannot be half-hand-edited into a request for the wrong format.
An explicit ``url`` is still accepted for the lists Celestrak does not carry.

Two things this module refuses to do quietly, both because the failure is
invisible in the frames rather than at the download:

* **Overwrite good elements with an error page.** Celestrak answers a bad group
  with ``200 text/plain`` reading "No GP data found", and rate-limits with an
  HTML page. Both parse to zero satellites, so the response is parsed *before*
  it replaces the cached file and a failure leaves last week's elements in
  place — stale beats empty.
* **Re-download on every run.** Elements are reissued a few times a day at most
  and Celestrak asks clients not to poll harder, so a file younger than
  ``refetch_after_hours`` is left alone unless ``--force`` says otherwise.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import SatellitesConfig, SatelliteSource

log = logging.getLogger("astroskysim.satellites.tle")

USER_AGENT = "astroskysim (+https://github.com/arndtjenssen/AstroSkySim)"

#: Celestrak's general-perturbations endpoint. ``FORMAT=tle`` gives the
#: three-line form (name, line 1, line 2), which is what carries the satellite
#: names; ``FORMAT=2le`` drops them and makes every log line say "unknown".
CELESTRAK_GP = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"


@dataclass(frozen=True, slots=True)
class TleRecord:
    """One satellite: the display name and its two element lines."""

    name: str
    line1: str
    line2: str


@dataclass(frozen=True, slots=True)
class FetchResult:
    source: str
    status: str  # "fetched" | "fresh" | "failed"
    count: int = 0
    detail: str = ""


def source_url(src: SatelliteSource) -> str:
    """The URL a source is fetched from."""
    if src.url:
        return src.url
    return CELESTRAK_GP.format(group=urllib.parse.quote(src.group or "", safe=""))


def tle_path(src: SatelliteSource, tle_dir: Path) -> Path:
    """Where this source's elements are cached.

    The stem is the source key, sanitised: a group name reaches this from a
    config file, and ``group = "../../.ssh/config"`` is a valid TOML string.
    """
    stem = "".join(c if c.isalnum() or c in "-_." else "_" for c in src.key).strip("._") or "source"
    return Path(tle_dir) / f"{stem}.txt"


def parse_tle_text(text: str) -> list[TleRecord]:
    """Every element set in a TLE file, ignoring whatever else is in there.

    Driven off the element lines rather than off a three-line rhythm: files in
    the wild carry blank lines, a header, or the two-line form with no names at
    all, and a rhythm-based reader silently pairs the wrong lines when any of
    those appear. A line 1 followed by a line 2 is an element set; the
    non-element line before it, if there is one, is the name.
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[TleRecord] = []
    i = 0
    while i < len(lines) - 1:
        first, second = lines[i], lines[i + 1]
        if _is_element_line(first, "1") and _is_element_line(second, "2"):
            name = ""
            if i > 0:
                previous = lines[i - 1].strip()
                if previous and not _is_element_line(lines[i - 1], "1") and not _is_element_line(
                    lines[i - 1], "2"
                ):
                    name = previous.removeprefix("0 ").strip()
            out.append(TleRecord(name or f"NORAD {first[2:7].strip()}", first, second))
            i += 2
            continue
        i += 1
    return out


def _is_element_line(line: str, number: str) -> bool:
    # 69 characters by spec; some sources trim the trailing checksum column, so
    # the length test is a floor rather than an equality.
    return len(line) >= 68 and line.startswith(f"{number} ") and line[2:7].strip().isdigit()


class NotModified(RuntimeError):
    """Celestrak already served these elements and has nothing newer.

    It says so with **HTTP 403** and a plain-text body, not a 304, so the status
    code alone cannot be distinguished from being rate-limited or blocked. Read
    literally it sends the user hunting for a throttle that is not there, and
    ``--force`` cannot help because the refusal is on the server's side of the
    conversation. The body is unambiguous, so it is what decides.
    """


#: Celestrak's wording for the above, matched loosely enough to survive
#: punctuation changes.
_NOT_MODIFIED = "has not updated since your last successful"


def _download(url: str, timeout_s: float) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - http(s) only
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        body = ""
        with contextlib.suppress(Exception):
            body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 403 and _NOT_MODIFIED in body:
            raise NotModified(" ".join(body.split())) from exc
        hint = (
            " - Celestrak rate-limits by address; wait and retry rather than looping"
            if exc.code in (403, 429)
            else ""
        )
        raise RuntimeError(f"{url}: HTTP {exc.code} {exc.reason}{hint}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{url}: {exc.reason}") from exc


def _age_hours(path: Path) -> float:
    try:
        return (time.time() - path.stat().st_mtime) / 3600.0
    except OSError:
        return float("inf")


def fetch_source(
    src: SatelliteSource,
    tle_dir: Path,
    *,
    timeout_s: float = 60.0,
    force: bool = False,
    refetch_after_hours: float = 12.0,
) -> FetchResult:
    """Download one source into ``tle_dir``. Idempotent within the age window."""
    dest = tle_path(src, tle_dir)
    if not force and dest.is_file() and _age_hours(dest) < refetch_after_hours:
        records = parse_tle_text(dest.read_text(encoding="utf-8", errors="replace"))
        log.info("%s: %d objects, %.1f h old - left alone", src.key, len(records), _age_hours(dest))
        return FetchResult(src.key, "fresh", len(records))

    url = source_url(src)
    log.info("fetching %s from %s", src.key, url)
    try:
        text = _download(url, timeout_s)
    except NotModified as exc:
        if dest.is_file():
            records = parse_tle_text(dest.read_text(encoding="utf-8", errors="replace"))
            log.info("%s: %d objects, already current upstream", src.key, len(records))
            return FetchResult(src.key, "fresh", len(records))
        # Nothing cached and nothing offered: the last successful download went
        # somewhere else, or the cache was cleared. Celestrak reissues on its
        # own two-hourly cycle, and no flag on this side shortens that.
        return FetchResult(
            src.key,
            "failed",
            detail=f"{exc} - nothing is cached for this source, so wait for the next "
            "upstream update (2 hours) rather than retrying",
        )
    except RuntimeError as exc:
        return FetchResult(src.key, "failed", detail=str(exc))

    records = parse_tle_text(text)
    if not records:
        # A 200 carrying "No GP data found" or a rate-limit page. Writing it
        # would delete working elements in exchange for nothing.
        head = " ".join(text.split())[:80]
        return FetchResult(
            src.key,
            "failed",
            detail=f"no element sets in the response (starts {head!r})"
            + (" - the cached file is kept" if dest.is_file() else ""),
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, dest)
    log.info("%s: %d objects -> %s", src.key, len(records), dest)
    return FetchResult(src.key, "fetched", len(records))


def fetch_sources(
    cfg: SatellitesConfig, *, force: bool = False, sources: list[SatelliteSource] | None = None
) -> list[FetchResult]:
    """Download every enabled source. Never raises for one bad list."""
    chosen = sources if sources is not None else cfg.active_sources
    if not chosen:
        log.warning("no sources are enabled - nothing to fetch")
        return []
    results = []
    for src in chosen:
        results.append(
            fetch_source(
                src,
                cfg.tle_dir,
                timeout_s=cfg.timeout_s,
                force=force,
                refetch_after_hours=cfg.refetch_after_hours,
            )
        )
    return results


def load_source(src: SatelliteSource, tle_dir: Path) -> list[TleRecord]:
    """Cached elements for one source, or an empty list if it has none yet."""
    path = tle_path(src, tle_dir)
    if not path.is_file():
        return []
    try:
        return parse_tle_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        log.warning("cannot read %s: %s", path, exc)
        return []
