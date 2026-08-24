"""DSS / survey cutouts, reprojected onto the sensor grid.

Three back ends, selected by the ``survey`` string:

* ``hips:<HiPS id>``  e.g. ``hips:CDS/P/DSS2/red`` - the CDS hips2fits service.
  The default, and the only one that reaches narrowband (Ha/OIII/SII) and the
  modern photometric bands, because every HiPS at
  https://aladin.cds.unistra.fr/hips/list is addressable by id.
* ``skyview:<Survey>``  e.g. ``skyview:DSS2 Red`` - via astroquery, which returns
  FITS with a real WCS. SkyView survey names contain spaces; the short forms
  (``DSS2R``) are accepted as aliases and translated.
* ``eso:<Sky-Survey>``  e.g. ``eso:DSS`` - the archive.eso.org DSS cutout CGI,
  asked for FITS rather than its default GIF.

Requesting **FITS, not JPEG/GIF**, is deliberate. A lossy 8-bit image forces you
to re-derive astrometry from the requested centre, which is only ever
approximate; a real WCS in the response means we can reproject exactly.

For hips2fits that rule needs stating more precisely, because the service answers
``format=fits`` for *colour* HiPS too - with an ``(4, H, W)`` uint8 RGBA cube,
which is the lossy JPEG tile set in a FITS wrapper (the core of M42 comes back a
flat 255). Only the monochrome HiPS carry real survey values, so ``_decode``
rejects cubes by name rather than letting them through as pixels.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import urllib.error
import urllib.parse
import urllib.request
import warnings
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.utils.exceptions import AstropyWarning
from astropy.wcs import WCS

from ..sky.render import surface_brightness_to_electrons
from .base import RenderContext, SourceError, calibrate_survey_image

log = logging.getLogger("astroskysim.sources.dss")

#: A response below this is an error page, not an image. The body text is the
#: actual diagnosis, so it goes into the raised error.
MIN_IMAGE_BYTES = 4096

ESO_BASE = "https://archive.eso.org/dss/dss"

#: hips2fits hosts, tried in order. Two CDS machines serve the same service on
#: two addresses (``alasky`` 130.79.128.175, ``alaskybis`` 130.79.128.179), and
#: they fail **independently**: measured 2026-08-24, ``alasky`` accepted the TCP
#: connection and then never answered - a 2.9 deg DSS2/red cutout, a 0.2 deg one
#: and the service root with no arguments at all each hung past 120 s - while
#: ``alaskybis`` returned the identical 3000x3000 request in 10 s. ``alasky`` is
#: the name in every CDS document, so it is not dropped; it is second because a
#: dead first host costs a probe timeout per cutout and this is the one we have
#: evidence about. Order is a snapshot, not a ranking - override with
#: ``[source.dss] hips_bases``.
HIPS_BASES = (
    "https://alaskybis.cds.unistra.fr/hips-image-services/hips2fits",
    "https://alasky.cds.unistra.fr/hips-image-services/hips2fits"
)

#: Socket timeout for a host that has an alternate behind it. This is not a
#: budget for the whole transfer: ``urlopen``'s timeout applies per socket
#: operation, so a slow-but-streaming 18 MB download never trips it and only a
#: genuine stall does. That is what makes a short value safe here - the failure
#: mode being probed for is silence, not slowness.
HIPS_PROBE_TIMEOUT_S = 15.0

#: Short names for the HiPS worth pointing a simulator at. Anything containing a
#: "/" is taken as a full HiPS id and passed through, so the whole CDS list stays
#: reachable without this table having to know about it.
#:
#: Every entry is a **monochrome** HiPS. The ``.../color`` variants are omitted on
#: purpose - see the module docstring. Keys are lowercased with spaces, dashes and
#: underscores stripped.
HIPS_ALIASES = {
    # DSS2 - all-sky, the closest HiPS equivalent of the ESO DSS cutouts.
    "dss": "CDS/P/DSS2/red",
    "dss2": "CDS/P/DSS2/red",
    "dss2r": "CDS/P/DSS2/red",
    "dss2red": "CDS/P/DSS2/red",
    "dss2b": "CDS/P/DSS2/blue",
    "dss2blue": "CDS/P/DSS2/blue",
    "dss2i": "CDS/P/DSS2/NIR",
    "dss2ir": "CDS/P/DSS2/NIR",
    "dss2nir": "CDS/P/DSS2/NIR",
    # PanSTARRS DR1 - deeper, but dec > -30 only and saturated cores are masked.
    "ps1g": "CDS/P/PanSTARRS/DR1/g",
    "ps1r": "CDS/P/PanSTARRS/DR1/r",
    "ps1i": "CDS/P/PanSTARRS/DR1/i",
    "ps1z": "CDS/P/PanSTARRS/DR1/z",
    "ps1y": "CDS/P/PanSTARRS/DR1/y",
    # SDSS9 - partial sky.
    "sdssu": "CDS/P/SDSS9/u",
    "sdssg": "CDS/P/SDSS9/g",
    "sdssr": "CDS/P/SDSS9/r",
    "sdssi": "CDS/P/SDSS9/i",
    "sdssz": "CDS/P/SDSS9/z",
    # Narrowband, from the North Sky Narrowband Survey. Nothing on SkyView
    # covers these, and they are the reason to prefer hips2fits: a per-filter
    # survey map needs Ha/OIII/SII to mean anything. Third-party host, northern
    # sky only (~65% coverage), and coarse at 6.4"/px - pair them with
    # ``mode = "composite"`` so the stars come from the catalogue instead.
    #
    # The three line maps share one linear scale, so one ``ref_value`` across
    # all of them reproduces the real line ratios. The ``*8`` variants are 8-bit
    # and the ``hbr8``/``ohs8``/``rgb8``/``tc8`` ones are colour, so both are
    # left out: see the module docstring on why colour HiPS are not usable.
    "ha": "simg.de/P/NSNS/DR0_2/halpha",
    "halpha": "simg.de/P/NSNS/DR0_2/halpha",
    "oiii": "simg.de/P/NSNS/DR0_2/oiii",
    "sii": "simg.de/P/NSNS/DR0_2/sii",
    # NSNS visual continuum, 440-700 nm: the one broad mono band here that is
    # linear rather than photographic. It is continuum *only* though, so an
    # emission nebula all but vanishes - not a luminance stand-in.
    "nsnsvc": "simg.de/P/NSNS/DR0_2/vc",
    "continuum": "simg.de/P/NSNS/DR0_2/vc",
    # Infrared / dust.
    "2massj": "CDS/P/2MASS/J",
    "2massh": "CDS/P/2MASS/H",
    "2massk": "CDS/P/2MASS/K",
    "wise12": "CDS/P/WISE/WSSA/12um",
    "wssa": "CDS/P/WISE/WSSA/12um",
}


def resolve_hips_id(survey: str) -> str:
    """Translate a short name into a full HiPS id.

    A string containing "/" is already an id and is returned untouched, so any
    HiPS on the CDS list works without being listed here.
    """
    s = survey.strip()
    if "/" in s:
        return s
    key = s.replace(" ", "").replace("-", "").replace("_", "").lower()
    if key not in HIPS_ALIASES:
        raise SourceError(
            f"unknown HiPS short name {survey!r}; use a full id such as "
            f"'CDS/P/DSS2/red' (see https://aladin.cds.unistra.fr/hips/list), "
            f"or one of: {', '.join(sorted(HIPS_ALIASES))}"
        )
    return HIPS_ALIASES[key]

#: SkyView's own survey names, which contain spaces ("DSS2 Red"). The short
#: forms are what everyone actually types - and what the ESO CGI uses - so map
#: them rather than let SkyView answer "Survey is not among the surveys hosted".
#: Keys are the survey string with spaces, dashes and underscores removed,
#: lowercased. Anything not listed is passed to SkyView verbatim.
SKYVIEW_ALIASES = {
    "dss": "DSS",
    "dss1": "DSS1 Red",
    "dss1b": "DSS1 Blue",
    "dss1blue": "DSS1 Blue",
    "dss1r": "DSS1 Red",
    "dss1red": "DSS1 Red",
    "dss2": "DSS2 Red",
    "dss2b": "DSS2 Blue",
    "dss2blue": "DSS2 Blue",
    "dss2r": "DSS2 Red",
    "dss2red": "DSS2 Red",
    "dss2i": "DSS2 IR",
    "dss2ir": "DSS2 IR",
    "dss2infrared": "DSS2 IR",
}

#: Listed in the error when SkyView rejects a name, so the fix is in the log.
SKYVIEW_OPTICAL_HINT = (
    "DSS, DSS1 Blue, DSS1 Red, DSS2 Blue, DSS2 Red, DSS2 IR, "
    "SDSSg, SDSSr, SDSSi, SDSSu, SDSSz"
)

#: Cache granularity, as a fraction of the frame footprint. Successive frames
#: differ by the tracking noise and drift baked into ``actual_pointing``, so an
#: unrounded centre never repeats and every exposure re-downloaded the same
#: field. Snapping the centre to a grid this size makes the key repeat; the
#: download is grown by the same amount so the frame is still covered wherever
#: inside the cell the true centre sits.
CACHE_CELL_FRACTION = 0.125


@contextlib.contextmanager
def quiet_archive_headers():
    """Silence astropy's complaints about the archives' own FITS headers.

    Real DSS plate headers are from the 1990s and violate the current standard
    in ways astropy repairs on its own: non-standard cards with the value and
    comment run together (``SKEW``), the deprecated fixed-width ``PC001001``
    spelling of ``PCi_ja``, and a two-digit ``DATE-OBS`` ("04/10/54"). Each one
    is reported twice - once on stderr, once through astropy's logger - for
    every cutout parsed, which is a dozen lines per download and per cache read.

    We do not own those headers and the fix-ups are exactly what we want, so the
    warnings are noise. Filter on ``AstropyWarning``, not ``AstropyUserWarning``:
    ``VerifyWarning`` sits under the latter but ``FITSFixedWarning`` does not, so
    the narrower filter silences the ``SKEW`` card and leaves every ``PCi_ja`` and
    ``datfix`` line in place. Scoped to third-party header parsing only - a
    genuinely broken response still fails in ``_decode``.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", AstropyWarning)
        yield


def resolve_skyview_survey(survey: str) -> str:
    """Translate a short survey name into SkyView's own spelling."""
    key = survey.replace(" ", "").replace("-", "").replace("_", "").lower()
    return SKYVIEW_ALIASES.get(key, survey)


def hips_error_text(exc: urllib.error.HTTPError) -> str:
    """Unwrap the service's JSON error body, which names the actual problem.

    An unknown id answers 400 with
    ``{"title": "Unknown HiPS", "description": "Could not find a HiPS ..."}``,
    which is far more use than the bare status line.
    """
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        return f"HTTP {exc.code}"
    with contextlib.suppress(Exception):
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            detail = parsed.get("description") or parsed.get("title")
            if detail:
                return f"HTTP {exc.code}: {detail}"
    return f"HTTP {exc.code}: {body[:200].strip()}"


def _blames_the_request(exc: urllib.error.HTTPError) -> bool:
    """Is this status the request's fault rather than the host's?

    A 400 "Unknown HiPS" is the same 400 on every mirror, so failing over would
    burn a full timeout per host and then report the *last* one's message. 408
    and 429 are excluded: both are about this host right now, and another one may
    well answer.
    """
    return 400 <= exc.code < 500 and exc.code not in (408, 429)


class HipsEndpoint:
    """Picks a live hips2fits host and remembers it.

    One instance is shared by every survey layer (``sources/registry.py``),
    which is the point: with a layer per filter, a per-layer endpoint would
    rediscover a dead host once per filter instead of once per process.

    Failover is warranted because the two CDS hosts fail independently - see
    ``HIPS_BASES``. Two rules keep it from being worse than no failover at all:
    a host with an alternate behind it gets ``probe_timeout_s`` rather than the
    full ``timeout_s``, and a request-shaped error (an unknown HiPS id) is
    raised immediately instead of being retried against hosts that will all
    answer the same way.
    """

    def __init__(
        self,
        bases: Sequence[str] | None = None,
        probe_timeout_s: float = HIPS_PROBE_TIMEOUT_S,
    ) -> None:
        # ``None`` means "use the built-in chain"; an explicitly empty list is a
        # config mistake, and silently substituting the default would hide it.
        self.bases = HIPS_BASES if bases is None else tuple(bases)
        if not self.bases:
            raise ValueError("hips_bases is empty: no hips2fits host to fetch from")
        self.probe_timeout_s = probe_timeout_s
        #: The host that last worked, tried first from then on. Cleared when it
        #: fails, so a host that dies mid-run does not pin the run to itself.
        self.pinned: str | None = None

    def _order(self) -> tuple[str, ...]:
        if self.pinned is None or self.pinned not in self.bases:
            return self.bases
        return (self.pinned, *(b for b in self.bases if b != self.pinned))

    def get(self, query: str, timeout_s: float) -> bytes:
        """The response body for ``query``, from the first host that answers."""
        order = self._order()
        failures: list[str] = []
        for i, base in enumerate(order):
            last = i == len(order) - 1
            budget = timeout_s if last else min(timeout_s, self.probe_timeout_s)
            try:
                raw = self._get_one(f"{base}?{query}", budget)
            except urllib.error.HTTPError as exc:
                detail = hips_error_text(exc)
                if _blames_the_request(exc):
                    raise SourceError(f"hips2fits rejected the request: {detail}") from exc
                failures.append(f"{_host(base)}: {detail}")
            except Exception as exc:
                failures.append(f"{_host(base)}: {exc}")
            else:
                if self.pinned != base:
                    log.info("hips2fits host %s answered; using it from now on", _host(base))
                    self.pinned = base
                return raw
            if not last:
                log.warning("hips2fits %s, trying the next host", failures[-1])
        self.pinned = None
        raise SourceError("hips2fits fetch failed - " + "; ".join(failures))

    @staticmethod
    def _get_one(url: str, timeout_s: float) -> bytes:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310 - https only
            raw = resp.read()
        if len(raw) < MIN_IMAGE_BYTES:
            # A short body is an error page. It may be this host's gateway
            # rather than the request, so it counts as a host failure and the
            # text goes into the summary either way.
            msg = raw[:256].decode("utf-8", errors="replace").strip()
            raise SourceError(f"returned {len(raw)} bytes, not an image: {msg}")
        return raw


def _host(base: str) -> str:
    """Just the hostname, so a log line is readable."""
    return urllib.parse.urlparse(base).netloc or base


class DssSource:
    """Fetch a survey cutout and resample it onto the sensor pixel grid."""

    name = "dss"

    def __init__(
        self,
        survey: str = "hips:CDS/P/DSS2/red",
        cache_dir: Path | None = None,
        timeout_s: float = 60.0,
        #: Surface brightness (mag/arcsec^2) that ``ref_percentile`` of the
        #: background-subtracted cutout represents. This is the whole
        #: calibration: everything else follows from the aperture and plate
        #: scale. 19.5 at the 99th percentile puts the bright filaments of a
        #: typical emission nebula near 21 mag/arcsec^2 on DSS2 red.
        ref_mag_arcsec2: float = 19.5,
        ref_percentile: float = 99.0,
        #: Absolute anchor in the survey's own units, above its own sky.
        #: Overrides ``ref_percentile``, and keeps a linear survey's real
        #: band ratios and target-to-target contrast. See
        #: ``calibrate_survey_image``.
        ref_value: float | None = None,
        #: This survey is the object as the filter in the beam sees it, so the
        #: filter's broadband transmission must not dim it a second time.
        in_band: bool = False,
        #: Decoded cutouts kept in RAM, so a run does not re-read and re-parse
        #: the same FITS once per exposure.
        mem_cache_size: int = 4,
        #: Reject a cutout whose reprojection leaves less than this fraction of
        #: the sensor covered, so a partial-sky survey falls back instead of
        #: serving a frame that is mostly hole.
        min_coverage: float = 0.5,
        #: Ceiling on the pixel grid asked of hips2fits. The service resamples to
        #: whatever is requested, so this is politeness towards a free service as
        #: much as a memory bound.
        max_download_px: int = 3000,
        #: Shared hips2fits host picker. Pass the *same* instance to every layer
        #: so a dead host is discovered once per process, not once per filter.
        hips: HipsEndpoint | None = None,
    ) -> None:
        self.survey = survey
        self.hips = hips or HipsEndpoint()
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_s = timeout_s
        self.ref_mag_arcsec2 = ref_mag_arcsec2
        self.ref_percentile = ref_percentile
        self.ref_value = ref_value
        self.in_band = in_band
        self.mem_cache_size = max(mem_cache_size, 0)
        self.min_coverage = min_coverage
        self.max_download_px = max_download_px
        self._mem: dict[str, tuple[np.ndarray, WCS]] = {}
        #: Last reported coverage shortfall, so a permanently gappy field warns
        #: once rather than once per exposure.
        self._last_coverage_warn: float | None = None

    # -- public ------------------------------------------------------------
    def render(self, ctx: RenderContext) -> np.ndarray:
        data, wcs = self.fetch(ctx)
        resampled = self._reproject(data, wcs, ctx)
        # Object signal above the survey's own sky, in units of the reference
        # level, times what that level is worth through *this* telescope. The
        # aperture, plate scale, throughput and filter therefore all reach the
        # survey path, which they never did while it had its own fixed e-/s.
        norm = calibrate_survey_image(resampled, self.ref_percentile, self.ref_value)
        ref_e_s = surface_brightness_to_electrons(self.ref_mag_arcsec2, ctx.optics)
        if self.in_band:
            # ``optics.throughput`` already carries the filter's broadband
            # transmission, which is right for a star and for the sky and wrong
            # here: this cutout was taken *through* this band, so the filter
            # selects its light rather than throwing it away. Take that factor
            # back out and let ``ref_mag_arcsec2`` be the in-band surface
            # brightness it looks like. Without this, an Ha layer would arrive
            # fifty times too faint and narrowband would never beat luminance -
            # which is the opposite of why anyone owns an Ha filter.
            ref_e_s /= max(ctx.filter_transmission, 1e-9)
        return norm * ref_e_s * max(ctx.exposure_s, 0.0)

    def fetch(self, ctx: RenderContext) -> tuple[np.ndarray, WCS]:
        """Survey pixels and their WCS, from cache when possible."""
        ra, dec = ctx.center
        # Ask for a little more than the frame so reprojection has margin.
        frame_deg = ctx.radius_deg * 2.4
        cell = frame_deg * CACHE_CELL_FRACTION
        ra_q, dec_q = self._snap(ra, dec, cell)
        # Snapping displaces the centre by up to half a cell on either axis, so
        # the download has to cover the frame plus that displacement.
        size_deg = frame_deg + cell
        grid = self._download_grid(size_deg, ctx)
        key = self._cache_key(ra_q, dec_q, size_deg, ctx.shape, grid)

        hit = self._mem.get(key)
        if hit is not None:
            return hit

        cached = self._read_cache(key)
        if cached is not None:
            return self._remember(key, cached)

        backend, _, survey = self.survey.partition(":")
        log.info(
            "fetching %s cutout %.3f deg at %.4f %+.4f (cache key %s)",
            self.survey,
            size_deg,
            ra_q,
            dec_q,
            key,
        )
        if backend == "hips":
            raw = self._fetch_hips(ra_q, dec_q, size_deg, survey or "CDS/P/DSS2/red", grid)
        elif backend == "skyview":
            raw = self._fetch_skyview(ra_q, dec_q, size_deg, survey or "DSS2 Red", ctx.shape)
        elif backend == "eso":
            raw = self._fetch_eso(ra_q, dec_q, size_deg, survey or "DSS")
        else:
            raise SourceError(
                f"unknown survey backend {backend!r}; expected 'hips:', "
                f"'skyview:' or 'eso:'"
            )

        decoded = self._decode(raw)
        self._write_cache(key, raw)
        return self._remember(key, decoded)

    # -- back ends ---------------------------------------------------------
    def _fetch_hips(
        self, ra: float, dec: float, size_deg: float, survey: str, grid: int
    ) -> bytes:
        """One square TAN cutout from the CDS hips2fits service.

        The server reprojects onto exactly the grid asked for, so ``CDELT`` is
        ``fov/width`` and the resolution is ours to choose - hence ``grid``.

        No ``rotation_angle``: the cutout is fetched north-up and rotated locally
        by ``_reproject``. Baking the rotator angle into the request would make
        every rotator position a separate download and a separate cache entry.

        Which host serves it is ``HipsEndpoint``'s problem, not this method's.
        """
        hips_id = resolve_hips_id(survey)
        query = urllib.parse.urlencode(
            {
                "hips": hips_id,
                "ra": f"{ra:.6f}",
                "dec": f"{dec:.6f}",
                "width": grid,
                "height": grid,
                "fov": f"{size_deg:.6f}",
                "projection": "TAN",
                "coordsys": "icrs",
                # FITS, never jpg: jpg is an 8-bit stretch of someone else's
                # choosing and throws away the dynamic range that makes a survey
                # cutout worth fetching.
                "format": "fits",
            }
        )
        try:
            return self.hips.get(query, self.timeout_s)
        except SourceError as exc:
            # The endpoint reports hosts and statuses; the id is this layer's
            # contribution to the diagnosis and is not in scope down there.
            raise SourceError(f"{exc} (hips {hips_id!r})") from exc

    def _fetch_skyview(
        self, ra: float, dec: float, size_deg: float, survey: str, shape: tuple[int, int]
    ) -> bytes:
        from astropy import units as u
        from astropy.coordinates import SkyCoord
        from astroquery.skyview import SkyView

        resolved = resolve_skyview_survey(survey)
        if resolved != survey:
            log.debug("survey %r resolved to SkyView name %r", survey, resolved)
        try:
            hdus = SkyView.get_images(
                position=SkyCoord(ra * u.deg, dec * u.deg, frame="icrs"),
                survey=[resolved],
                radius=size_deg / 2.0 * u.deg,
                # Ask for at least the sensor grid so we never upsample badly.
                pixels=f"{max(shape[1], 300)},{max(shape[0], 300)}",
                coordinates="J2000",
            )
        except Exception as exc:
            if "not among the surveys" in str(exc):
                raise SourceError(
                    f"SkyView rejected survey {resolved!r}; try one of: "
                    f"{SKYVIEW_OPTICAL_HINT}"
                ) from exc
            raise SourceError(f"SkyView fetch failed: {exc}") from exc
        if not hdus:
            raise SourceError(f"SkyView returned no image for survey {resolved!r}")
        out = fits.HDUList([h.copy() for h in hdus[0]])
        import io

        buf = io.BytesIO()
        # SkyView reflects the survey's own header cards, so serialising can trip
        # the same standard violations - fix them quietly on the way out.
        with quiet_archive_headers():
            out.writeto(buf, output_verify="silentfix")
        return buf.getvalue()

    def _fetch_eso(self, ra: float, dec: float, size_deg: float, survey: str) -> bytes:
        arcmin = max(min(size_deg * 60.0, 60.0), 0.3)  # the CGI's own limits
        query = urllib.parse.urlencode(
            {
                "mime-type": "download-fits",
                "equinox": "J2000",
                "Sky-Survey": survey,
                "ra": f"{ra:.5f}",
                "dec": f"{dec:.5f}",
                "x": f"{arcmin:.2f}",
                "y": f"{arcmin:.2f}",
            }
        )
        url = f"{ESO_BASE}?{query}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as resp:
                raw = resp.read()
        except Exception as exc:
            raise SourceError(f"ESO fetch failed: {exc}") from exc

        if len(raw) < MIN_IMAGE_BYTES:
            # A short body means an error page, and its text is the actual
            # diagnosis - so surface it rather than a byte count.
            msg = raw[:256].decode("utf-8", errors="replace").strip()
            raise SourceError(f"ESO returned {len(raw)} bytes, not an image: {msg}")
        return raw

    # -- helpers -----------------------------------------------------------
    def _download_grid(self, size_deg: float, ctx: RenderContext) -> int:
        """Side length, in pixels, to request for a ``size_deg`` cutout.

        hips2fits samples onto whatever grid it is given, so asking for too few
        pixels quietly discards survey detail the sensor could have resolved.
        Match the sensor's own plate scale across the downloaded footprint, then
        clamp: never below 300 (a tiny guide subframe should still get a usable
        cutout to reproject from) and never above ``max_download_px``.
        """
        scale = max(ctx.optics.scale_arcsec_px, 1e-6)
        want = int(math.ceil(size_deg * 3600.0 / scale))
        return max(300, min(want, self.max_download_px))

    def _decode(self, raw: bytes) -> tuple[np.ndarray, WCS]:
        import io

        try:
            with quiet_archive_headers(), fits.open(io.BytesIO(raw)) as hdul:
                hdu = next(
                    (h for h in hdul if getattr(h, "data", None) is not None), None
                )
                if hdu is None:
                    raise SourceError("FITS response contained no image data")
                data = np.asarray(hdu.data, dtype=np.float64)
                wcs = WCS(hdu.header)
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"could not decode FITS response: {exc}") from exc

        if data.ndim > 2:
            # A degenerate leading axis (1, H, W) is just a wrapped 2-D plane and
            # reshapes away. A real stack of planes is a colour HiPS: 8-bit RGBA
            # transcoded from JPEG tiles, with the bright cores already clipped.
            # Say so instead of silently imaging a saturated colour channel.
            extra = int(np.prod(data.shape[:-2]))
            if extra != 1:
                raise SourceError(
                    f"survey response is a {data.shape} cube, not a mono image - "
                    f"this is a colour HiPS, whose FITS is 8-bit RGBA transcoded "
                    f"from JPEG. Use a monochrome HiPS instead "
                    f"(e.g. 'CDS/P/DSS2/red' rather than 'CDS/P/DSS2/color')"
                )
            data = data.reshape(data.shape[-2:])
            wcs = wcs.celestial
        if not wcs.has_celestial:
            raise SourceError("survey response has no celestial WCS to reproject from")
        if not np.isfinite(data).any():
            raise SourceError(
                "survey response is entirely blank - the field is outside this "
                "survey's footprint"
            )
        return data, wcs

    def _reproject(self, data: np.ndarray, wcs: WCS, ctx: RenderContext) -> np.ndarray:
        from reproject import reproject_interp

        try:
            out, _ = reproject_interp(
                (data, wcs), ctx.wcs, shape_out=ctx.shape, order="bilinear"
            )
        except Exception as exc:
            raise SourceError(f"reprojection onto the sensor grid failed: {exc}") from exc
        return self._check_coverage(out)

    def _check_coverage(self, out: np.ndarray) -> np.ndarray:
        """Reject a frame the survey barely covers; zero the remaining gaps.

        NaN in a reprojected cutout has two unrelated causes and no way to tell
        them apart from the pixels alone: the sensor overhanging the survey's
        footprint, and the survey masking its own saturated pixels. PanSTARRS at
        M42 is 70% NaN for the second reason - filling that with zero paints a
        black hole exactly where the nebula is, which is worse than not using the
        survey at all. Below ``min_coverage`` we raise, so ``FallbackSource``
        serves the artificial sky instead of a frame that is mostly hole.
        """
        finite = np.isfinite(out)
        covered = float(finite.mean()) if finite.size else 0.0
        if covered < self.min_coverage:
            raise SourceError(
                f"survey covers only {covered:.0%} of the frame "
                f"(minimum {self.min_coverage:.0%}) - the field is off the "
                f"survey's footprint, or its bright cores are masked out"
            )
        if covered < 1.0:
            # Gappy but usable. Warn once per distinct shortfall: reprojection
            # runs every exposure, so an unconditional warning is per-frame spam.
            rounded = round(covered, 2)
            if self._last_coverage_warn != rounded:
                self._last_coverage_warn = rounded
                log.warning(
                    "survey covers %.0f%% of the frame; the gaps are rendered as "
                    "empty sky", covered * 100.0,
                )
        else:
            self._last_coverage_warn = None
        return np.nan_to_num(out, nan=0.0)

    @staticmethod
    def _snap(ra: float, dec: float, cell: float) -> tuple[float, float]:
        """Round a pointing to the cache grid, in degrees of great circle.

        The RA step is widened by 1/cos(dec) so a cell stays roughly square on
        the sky; near the pole it would otherwise be a sliver and never repeat.
        """
        if cell <= 0:
            return ra % 360.0, dec
        dec_q = min(90.0, max(-90.0, round(dec / cell) * cell))
        ra_cell = cell / max(math.cos(math.radians(dec_q)), 1e-3)
        return (round(ra / ra_cell) * ra_cell) % 360.0, dec_q

    def _cache_key(
        self,
        ra: float,
        dec: float,
        size_deg: float,
        shape: tuple[int, int],
        grid: int = 0,
    ) -> str:
        # The requested pixel count is part of the response, so it is part of
        # the key: the guide camera must not be served the main camera's cutout.
        # ``grid`` is the hips2fits resolution, which shape alone does not pin.
        h = hashlib.sha256(
            f"{self.survey}|{ra:.5f}|{dec:.5f}|{size_deg:.5f}"
            f"|{shape[0]}x{shape[1]}|{grid}".encode()
        ).hexdigest()[:20]
        return f"{h}.fits"

    def _remember(self, key: str, value: tuple[np.ndarray, WCS]) -> tuple[np.ndarray, WCS]:
        if self.mem_cache_size:
            self._mem[key] = value
            while len(self._mem) > self.mem_cache_size:
                self._mem.pop(next(iter(self._mem)))
        return value

    def _read_cache(self, key: str) -> tuple[np.ndarray, WCS] | None:
        if not self.cache_dir:
            return None
        p = self.cache_dir / key
        if not p.exists():
            return None
        try:
            decoded = self._decode(p.read_bytes())
            log.debug("survey cutout served from cache: %s", p.name)
            return decoded
        except SourceError as exc:
            log.warning("discarding bad cache entry %s: %s", p.name, exc)
            p.unlink(missing_ok=True)
            return None

    def _write_cache(self, key: str, raw: bytes) -> None:
        if not self.cache_dir:
            return
        try:
            (self.cache_dir / key).write_bytes(raw)
        except OSError as exc:
            log.warning("could not write cache entry: %s", exc)


class FallbackSource:
    """Try one source, fall back to another on failure.

    Used so a network hiccup degrades a DSS run to the artificial sky instead of
    failing the exposure - a headless simulator should keep serving frames.
    """

    def __init__(self, primary, fallback) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = f"{primary.name}+fallback"
        self._last_error = ""

    def render(self, ctx: RenderContext) -> np.ndarray:
        try:
            out = self.primary.render(ctx)
        except SourceError as exc:
            # A misconfigured survey fails on every single exposure, so only the
            # first occurrence of a given message is worth a warning.
            if str(exc) != self._last_error:
                self._last_error = str(exc)
                log.warning(
                    "%s failed (%s), falling back to %s",
                    self.primary.name,
                    exc,
                    self.fallback.name,
                )
            else:
                log.debug("%s still failing, using %s", self.primary.name, self.fallback.name)
            return self.fallback.render(ctx)
        self._last_error = ""
        return out
