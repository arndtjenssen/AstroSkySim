"""Frame synthesis, vectorised.

All stars in a frame share one PSF, so instead of stamping a kernel per star we
splat sub-pixel delta functions and convolve the whole plane once with an FFT.
That is what keeps a defocused frame — where the PSF is tens of pixels across —
from costing a per-star inner loop.

Units: the render works in **electrons** throughout and converts to ADU only at
the end, so gain, offset, well depth and read noise all mean what they say.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from astropy.wcs import WCS

#: HFD in pixels at perfect focus, and the hyperbola scale divisor. Together they
#: put the HFD at ~10 px once the focuser is ``focus_range`` steps from focus.
IN_FOCUS_HFD_PX = 2.35
_HFD_RANGE_DIVISOR = 10.0

#: Electrons per second per square metre for a magnitude 0 star, integrated over
#: a broad visual band. Order-of-magnitude correct; calibrate per setup if you
#: care about absolute photometry.
ZERO_POINT_E_S_M2 = 1.0e10


def hfd_from_focus(
    position: float,
    perfect_position: float,
    focus_range: float,
    in_focus_hfd: float = IN_FOCUS_HFD_PX,
) -> float:
    """HFD in pixels for a focuser position, as a hyperbola.

    The usual formulation ``a*cosh(arsinh(x/b))`` is identically
    ``a*sqrt(1 + (x/b)^2)``, which is what we compute. With ``focus_range=1000``
    the HFD reaches ~10 px at 1000 steps from focus.

    >>> round(hfd_from_focus(16000, 15000, 1000), 1)
    10.3
    """
    b = in_focus_hfd * focus_range / _HFD_RANGE_DIVISOR
    if b <= 0:
        return in_focus_hfd
    x = perfect_position - position
    return float(in_focus_hfd * np.hypot(1.0, x / b))


@dataclass(slots=True)
class Optics:
    """Everything the renderer needs about the light path."""

    aperture_mm: float
    scale_arcsec_px: float
    seeing_arcsec: float = 2.5
    hfd_px: float = IN_FOCUS_HFD_PX
    throughput: float = 0.5
    #: Secondary obstruction as a fraction of aperture diameter; makes the
    #: defocused PSF a donut rather than a disc, as a real reflector does.
    obstruction: float = 0.0
    zero_point: float = ZERO_POINT_E_S_M2

    @property
    def seeing_fwhm_px(self) -> float:
        return max(self.seeing_arcsec / self.scale_arcsec_px, 1e-3)

    @property
    def collecting_area_m2(self) -> float:
        r = self.aperture_mm / 2000.0  # mm -> m
        return float(np.pi * r * r * (1.0 - self.obstruction**2))


def magnitude_to_electrons(mag: np.ndarray, optics: Optics, exposure_s: float) -> np.ndarray:
    """Total electrons collected from each star over the exposure."""
    rate = (
        optics.zero_point
        * optics.collecting_area_m2
        * optics.throughput
        * np.power(10.0, -0.4 * np.asarray(mag, dtype=np.float64))
    )
    return rate * max(exposure_s, 0.0)


def surface_brightness_to_electrons(mag_arcsec2: float, optics: Optics) -> float:
    """e-/pixel/second for an extended source of the given surface brightness.

    A pixel subtends ``scale_arcsec_px**2`` square arcseconds, so the magnitude
    falling on it is the surface brightness dimmed by that solid angle. Beyond
    that this is the *same* zero point, aperture and throughput the star path
    uses, which is the whole point: the sky background, a survey cutout and a
    catalogue star are then all on one photometric scale, and changing the
    telescope changes all three together.

    >>> o = Optics(aperture_mm=90.0, scale_arcsec_px=1.795)
    >>> round(surface_brightness_to_electrons(21.0, o), 3)
    0.408
    """
    mag_px = mag_arcsec2 - 2.5 * np.log10(max(optics.scale_arcsec_px, 1e-9) ** 2)
    return float(magnitude_to_electrons(np.asarray(mag_px), optics, 1.0))


def make_psf(optics: Optics, max_radius_px: int = 128) -> np.ndarray:
    """Normalised PSF kernel: a defocus disc/annulus blurred by seeing.

    At focus the disc collapses and only the seeing Gaussian remains. A uniform
    disc of radius R has HFD = R*sqrt(2), which is how the defocus HFD maps to a
    radius here.
    """
    seeing_sigma = optics.seeing_fwhm_px / 2.3548200450309493
    defocus_hfd = max(optics.hfd_px**2 - IN_FOCUS_HFD_PX**2, 0.0) ** 0.5
    disc_r = min(defocus_hfd / np.sqrt(2.0), float(max_radius_px))

    half = int(np.ceil(disc_r + 4 * seeing_sigma)) + 1
    half = max(min(half, max_radius_px), 1)
    y, x = np.mgrid[-half : half + 1, -half : half + 1].astype(np.float64)
    r = np.hypot(x, y)

    if disc_r < 0.5:
        k = np.exp(-0.5 * (r / max(seeing_sigma, 1e-6)) ** 2)
    else:
        inner = disc_r * optics.obstruction
        disc = ((r <= disc_r) & (r >= inner)).astype(np.float64)
        if disc.sum() == 0:
            disc[half, half] = 1.0
        k = _gaussian_blur(disc, seeing_sigma)

    total = k.sum()
    return k / total if total > 0 else k


def _gaussian_blur(a: np.ndarray, sigma: float) -> np.ndarray:
    """Separable Gaussian blur without a scipy dependency."""
    if sigma <= 1e-6:
        return a
    n = int(np.ceil(4 * sigma))
    t = np.arange(-n, n + 1, dtype=np.float64)
    g = np.exp(-0.5 * (t / sigma) ** 2)
    g /= g.sum()
    out = np.apply_along_axis(lambda m: np.convolve(m, g, mode="same"), 0, a)
    return np.apply_along_axis(lambda m: np.convolve(m, g, mode="same"), 1, out)


def _splat(shape: tuple[int, int], x: np.ndarray, y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Bilinear sub-pixel accumulation of weights onto a plane."""
    h, wd = shape
    plane = np.zeros(shape, dtype=np.float64)
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    fx = x - x0
    fy = y - y0
    for dx, dy, wt in (
        (0, 0, (1 - fx) * (1 - fy)),
        (1, 0, fx * (1 - fy)),
        (0, 1, (1 - fx) * fy),
        (1, 1, fx * fy),
    ):
        xi = x0 + dx
        yi = y0 + dy
        m = (xi >= 0) & (xi < wd) & (yi >= 0) & (yi < h)
        if m.any():
            np.add.at(plane, (yi[m], xi[m]), w[m] * wt[m])
    return plane


def _next_fast_len(n: int) -> int:
    """Smallest 5-smooth integer >= n, for an efficient FFT length.

    ``scipy.fft`` has this, but keeping it local means the imaging core needs no
    scipy (only the optional DSS path does).
    """
    if n <= 1:
        return 1
    best = 1 << (n - 1).bit_length()  # a power of two is always a valid bound
    p5 = 1
    while p5 < best:
        p3 = p5
        while p3 < best:
            # Multiply by two until it reaches n; that candidate is 5-smooth.
            p2 = p3
            while p2 < n:
                p2 *= 2
            if p2 < best:
                best = p2
            p3 *= 3
        p5 *= 5
    return best


def _convolve_fft(plane: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Same-size FFT convolution, zero padded to avoid wraparound."""
    if kernel.shape == (1, 1):
        return plane * float(kernel[0, 0])
    sh = (plane.shape[0] + kernel.shape[0] - 1, plane.shape[1] + kernel.shape[1] - 1)
    fh = _next_fast_len(sh[0])
    fw = _next_fast_len(sh[1])
    out = np.fft.irfft2(np.fft.rfft2(plane, (fh, fw)) * np.fft.rfft2(kernel, (fh, fw)), (fh, fw))
    oy = kernel.shape[0] // 2
    ox = kernel.shape[1] // 2
    return out[oy : oy + plane.shape[0], ox : ox + plane.shape[1]]


def render_stars(
    ra_deg: np.ndarray,
    dec_deg: np.ndarray,
    mag: np.ndarray,
    wcs: WCS,
    shape: tuple[int, int],
    optics: Optics,
    exposure_s: float,
) -> np.ndarray:
    """Star field in electrons on the sensor grid defined by ``wcs``."""
    out = np.zeros(shape, dtype=np.float64)
    if ra_deg.size == 0:
        return out

    x, y = wcs.wcs_world2pix(ra_deg, dec_deg, 0)
    flux = magnitude_to_electrons(mag, optics, exposure_s)

    psf = make_psf(optics)
    pad = max(psf.shape) // 2 + 2
    keep = (x > -pad) & (x < shape[1] + pad) & (y > -pad) & (y < shape[0] + pad)
    if not keep.any():
        return out

    # Splat into a padded plane so stars just outside the frame still bleed in.
    padded_shape = (shape[0] + 2 * pad, shape[1] + 2 * pad)
    plane = _splat(padded_shape, x[keep] + pad, y[keep] + pad, flux[keep])
    conv = _convolve_fft(plane, psf)
    cropped = conv[pad : pad + shape[0], pad : pad + shape[1]]
    # FFT convolution rings slightly negative (order 1e-11 of the peak).
    # Negative flux is unphysical and would poison the Poisson draw downstream.
    return np.clip(cropped, 0.0, None)


#: Below this peak-to-peak pixel extent a smear is inside the PSF and the frame
#: is returned untouched. Keeps a calm night free.
MIN_SMEAR_PX = 0.25

#: Above this many nonzero kernel taps the FFT pair is cheaper than accumulating
#: shifted views.
#:
#: Measured on a 3008x3008 frame, which is where the choice matters: the FFT is
#: flat at ~0.155 s whatever the kernel, because ``_convolve_fft`` zero-pads the
#: *frame* to 3072 either way, while a tap costs ~7.6 ms. So the crossover sits
#: between 15 taps (0.127 s) and 23 (0.177 s). Guessing a larger number is the
#: expensive mistake: at 75 taps the tap branch takes 0.573 s against the FFT's
#: 0.153 s, and it is a long lightly-damped ring-down - exactly the case this
#: feature exists to render - that produces those tap counts.
SMEAR_TAP_LIMIT = 16

#: Taps below this fraction of the peak are dropped before the count is taken.
#: A long ring-down deposits a very faint halo of single samples that costs a tap
#: each and contributes nothing visible.
_SMEAR_WEIGHT_FLOOR = 1e-4


def smear_kernel(dx: np.ndarray, dy: np.ndarray) -> np.ndarray | None:
    """Motion kernel for a zero-mean pixel path, normalised to sum 1.

    ``None`` means the path is not worth convolving.

    Two properties are load-bearing and both are easy to lose:

    * **Odd size.** ``_convolve_fft`` crops at ``kernel.shape // 2``, so an even
      kernel injects a half-pixel translation - precisely the frame shift the
      zero-mean path exists to avoid, and invisible in the pixels.
    * **Renormalised after the splat.** ``_splat`` silently drops out-of-range
      samples, so a kernel whose path overran the half-width would lose weight
      and dim the frame. The half-width is derived from the path, so that should
      not happen; dividing by the sum means it cannot.

    Unlike a satellite trail, this conserves flux rather than depositing per
    dwell time: wind redistributes a star's fixed electron budget, so a streaked
    star is fainter per pixel and the same total. Weighting by dwell falls out of
    the samples being uniform in time - one sample, one unit of dwell.
    """
    if dx.size == 0:
        return None
    extent = max(float(np.ptp(dx)), float(np.ptp(dy)))
    if extent < MIN_SMEAR_PX:
        return None

    half = int(np.ceil(max(np.abs(dx).max(), np.abs(dy).max())))
    if half < 1:
        half = 1
    size = 2 * half + 1
    k = _splat((size, size), dx + half, dy + half, np.full(dx.size, 1.0 / dx.size))
    total = k.sum()
    if total <= 0.0:
        return None
    return k / total


def apply_smear(electrons: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Convolve a frame with a motion kernel, preserving its edges.

    ``_convolve_fft`` zero-pads, which would darken a border the width of the
    kernel's half-width, so the frame is padded by edge replication first. That
    padding is free in FFT terms up to a 16 px half-width: ``_next_fast_len``
    sends 3008 + 4*16 to the same 3072 as 3008 alone.

    Two applications of the *same* kernel, chosen on the tap count. Accumulating
    shifted views is memory-bandwidth bound at roughly a hundredth of a second
    per tap, so it wins by a wide margin for the small smears that dominate -
    and the tap count is bounded by the path's spatial extent, not by how many
    samples it holds. A 600 s sub ringing at 15 Hz revisits the same few dozen
    pixels tens of thousands of times.

    The sign is the trap: a source at ``s`` with the optics displaced by ``d``
    lands at ``s + d``, so ``out(p) = sum_d K(d) * scene(p - d)``. Reversed, the
    smear mirrors, which looks entirely plausible in a frame. Hence
    ``test_the_two_smear_paths_agree``, which pins the branches against each
    other rather than against an eyeball.
    """
    half = kernel.shape[0] // 2
    padded = np.pad(electrons, half, mode="edge")
    h, w = electrons.shape

    # Threshold *before* dispatching, not inside the tap branch. A long
    # ring-down leaves a halo of near-zero taps that costs a shifted view each
    # and contributes nothing; dropping them is worth it there and costs nothing
    # in the FFT, whose price is set by the frame. Doing it in one branch only
    # would leave the two disagreeing by the dropped weight - which is precisely
    # what ``test_the_two_smear_paths_agree`` would then be unable to check.
    strong = kernel > _SMEAR_WEIGHT_FLOOR * kernel.max()
    kernel = np.where(strong, kernel, 0.0)
    kernel = kernel / kernel.sum()

    if int(strong.sum()) > SMEAR_TAP_LIMIT:
        return _convolve_fft(padded, kernel)[half : half + h, half : half + w]

    ys, xs = np.nonzero(strong)
    out = np.zeros_like(electrons)
    for weight, ky, kx in zip(kernel[ys, xs], ys, xs, strict=True):
        # d = (ky - half, kx - half); the source window is p - d.
        oy = half - (int(ky) - half)
        ox = half - (int(kx) - half)
        out += weight * padded[oy : oy + h, ox : ox + w]
    return out


@dataclass(slots=True)
class SensorModel:
    """Detector characteristics used to turn electrons into ADU."""

    well_depth_e: float = 20000.0
    read_noise_e: float = 3.0
    dark_current_e_s: float = 0.02
    e_per_adu: float = 1.0
    bit_depth: int = 16
    offset_adu: int = 100
    #: gain 100 == unity; higher gain means fewer electrons per ADU.
    gain: int = 100
    hot_pixels: int = 0
    #: Dark current of a hot pixel, e-/s. 200 e-/s saturates a 20 ke- well in
    #: 100 s, and contributes ~100 e- to a 0.5 s guide frame.
    hot_pixel_e_s: float = 200.0

    @property
    def max_adu(self) -> int:
        return (1 << self.bit_depth) - 1

    def effective_e_per_adu(self) -> float:
        return max(self.e_per_adu * 100.0 / max(self.gain, 1), 1e-6)


def add_sky_and_noise(
    electrons: np.ndarray,
    sensor: SensorModel,
    exposure_s: float,
    sky_e_s: float,
    rng: np.random.Generator,
    *,
    dark_frame: bool = False,
) -> np.ndarray:
    """Sky background, dark current, shot noise and read noise, in electrons."""
    signal = np.zeros_like(electrons) if dark_frame else electrons.copy()
    signal += max(sky_e_s, 0.0) * max(exposure_s, 0.0) if not dark_frame else 0.0
    signal += max(sensor.dark_current_e_s, 0.0) * max(exposure_s, 0.0)

    # Poisson is only stable for modest means; Gaussian is exact enough above it.
    out = np.empty_like(signal)
    small = signal < 1e6
    out[small] = rng.poisson(np.clip(signal[small], 0, None)).astype(np.float64)
    big = ~small
    if big.any():
        out[big] = rng.normal(signal[big], np.sqrt(signal[big]))

    if sensor.read_noise_e > 0:
        out += rng.normal(0.0, sensor.read_noise_e, out.shape)
    return out


def add_hot_pixels(
    electrons: np.ndarray,
    count: int,
    sensor: SensorModel,
    seed: int | None,
    exposure_s: float = 1.0,
) -> np.ndarray:
    """Fixed-pattern hot pixels. Positions are stable across frames.

    A hot pixel is runaway dark current, so its charge scales with exposure
    time. Adding a fixed fraction of the well regardless of exposure made every
    hot pixel saturate even in a 0.5 s guide frame, so the 20 immobile hot
    pixels outshone the brightest real star by two orders of magnitude. A guider
    then locks onto fixed-pattern noise and reports no drift under calibration
    pulses, which is exactly the failure this scaling prevents.
    """
    if count <= 0:
        return electrons
    rng = np.random.default_rng(0 if seed is None else seed)
    h, w = electrons.shape
    n = min(count, h * w)
    idx = rng.choice(h * w, size=n, replace=False)
    out = electrons.copy()
    rate = rng.uniform(0.3, 1.0, n) * max(sensor.hot_pixel_e_s, 0.0)
    out.flat[idx] += rate * max(exposure_s, 0.0)
    return out


def to_adu(electrons: np.ndarray, sensor: SensorModel) -> np.ndarray:
    """Saturate at the well, convert to ADU, clip to the bit depth."""
    e = np.clip(electrons, 0.0, sensor.well_depth_e)
    adu = e / sensor.effective_e_per_adu() + sensor.offset_adu
    dtype = np.uint16 if sensor.bit_depth > 8 else np.uint8
    return np.clip(np.rint(adu), 0, sensor.max_adu).astype(dtype)


BAYER_OFFSETS = {
    "RGGB": ((0, 0), (1, 1)),
    "BGGR": ((1, 1), (0, 0)),
    "GRBG": ((0, 1), (1, 0)),
    "GBRG": ((1, 0), (0, 1)),
}


def apply_bayer(electrons: np.ndarray, pattern: str) -> np.ndarray:
    """Crude CFA response: attenuate per-channel so a mosaic is visible.

    Real colour would need per-star SEDs; this is enough for clients to exercise
    their debayer path and for BAYERPAT/XBAYROFF to mean something.
    """
    if pattern == "MONO" or pattern not in BAYER_OFFSETS:
        return electrons
    (ry, rx), (by, bx) = BAYER_OFFSETS[pattern]
    out = electrons * 0.6  # green sites
    out[ry::2, rx::2] = electrons[ry::2, rx::2] * 0.9
    out[by::2, bx::2] = electrons[by::2, bx::2] * 0.5
    return out


def bin_frame(a: np.ndarray, bx: int, by: int) -> np.ndarray:
    """Sum-bin. Sums (not averages) because binning collects charge."""
    if bx <= 1 and by <= 1:
        return a
    h = (a.shape[0] // by) * by
    w = (a.shape[1] // bx) * bx
    return a[:h, :w].reshape(h // by, by, w // bx, bx).sum(axis=(1, 3))


def subframe(a: np.ndarray, x: int, y: int, w: int, h: int) -> np.ndarray:
    """Clamp a requested subframe to the array and return that view."""
    x = max(0, min(x, a.shape[1] - 1))
    y = max(0, min(y, a.shape[0] - 1))
    w = max(1, min(w, a.shape[1] - x))
    h = max(1, min(h, a.shape[0] - y))
    return a[y : y + h, x : x + w]
