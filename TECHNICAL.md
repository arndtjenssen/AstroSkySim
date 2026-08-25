# AstroSkySim — technical notes

This is the *why* behind the design: what each decision buys, what it costs, and where
the simulator deliberately stops. Every measured number here was taken against the
shipped `examples/sim.toml` on the g14 star catalogue, so a different rig or catalogue
moves them.

Looking for something else? [README.md](README.md) covers install, usage and
configuration. `CLAUDE.md` is the short version of this file, written for an agent
working on the code.

## Contents

- [The star catalogue](#the-star-catalogue)
- [Image sources](#image-sources)
- [Satellite trails](#satellite-trails)
- [Wind and gusts](#wind-and-gusts)
- [Temperature and focus drift](#temperature-and-focus-drift)
- [Design notes](#design-notes)
- [Known limitations](#known-limitations)

## The star catalogue

`artificial` and `composite` render stars from a HNSKY `.290` database, which is
too large to keep in the repository and is fetched separately:

```bash
uv run astroskysim fetch-catalog                       # -> ./catalog
```

It verifies a pinned SHA-256, unpacks the cells flat into the target directory,
and does nothing at all if they are already there — so it is safe in a setup
script. `--force` re-downloads, `-d` picks a directory, `--url`/`--sha256` point
it at a different archive.

**Without a catalogue the simulator still runs**, on a synthetic star field, and
warns once at startup. That is usable for focus, guiding and framing, but the
stars are not at real positions, so **plate solving against it will fail**.

### Why this is a mirror and not a link

The g14 set (GAIA eDR3 to BP 14.0, epoch 2025, 290 cells, 11,290,236 stars) is
**no longer distributed upstream**. HNSKY's and ASTAP's
[star database directories](https://sourceforge.net/projects/hnsky/files/star_databases/)
now carry g17, g18, v16, v17, u16 and the ASTAP d/v/g tiers — everything between
100 MB and 1.3 GB, with nothing at magnitude 14. So there is no upstream URL to
link to for g14, and a pinned mirror is the only way `catalog = "g14"` stays
reproducible.

At 56 MB for 11.3M stars it is also simply the right size for a simulator: deep
enough that a guide chip has real stars in every field and plate solving works,
small enough to download without thinking about it.

### g05 is an alternative, not an upgrade

`fetch-catalog g05` pulls
[ASTAP's current database](https://sourceforge.net/projects/astap-program/files/star_databases/)
from upstream at 102 MB, 20.66M stars. The existing decoder reads it unchanged —
same 290-cell layout, same record size 5, same epoch 2025, same BP magnitudes,
astrometry matching Gaia to 0.02" rms and the Tycho2 bright stars to 0.09".

But **ASTAP's suffix is star density, not magnitude**: g05 means ≤500 stars per
square degree, applied locally. So it is not a superset of g14. It goes far
deeper where the sky is empty and *discards stars g14 has* where the sky is full,
measured over a 0.5° radius field:

| Field | gal. b | g14 ≤BP14 | g05 ≤BP14 | g05 total | g05 limit |
|---|---:|---:|---:|---:|---:|
| Cygnus | +5.3° | 1073 | 491 | 491 | BP 13.4 |
| Sagittarius arm | −0.0° | 705 | 435 | 435 | BP 13.7 |
| M31 | −21.6° | 288 | 288 | 535 | BP 15.0 |
| Perseus | −18.0° | 62 | 62 | 413 | BP 18.7 |
| Coma (gal. pole) | +88.7° | 47 | 47 | 493 | BP 18.2 |

(stars per square degree)

That flattens the sky's real density contrast from 23:1 to about 1:1, which is
backwards for a simulator: a guide camera in Cygnus *should* be spoilt for choice
and one at the galactic pole *should* struggle. Guide-star selection and plate
solve success both depend on that contrast, so g14 stays the default.

g05 is the better choice for one specific job — a wide, sparse, high-latitude
field where g14 leaves a guide chip with almost nothing. Note that
`source.artificial.limiting_mag` then interacts with a spatially varying floor:
`limiting_mag = 16.0` really means 13.4 in Cygnus and 16.0 in Coma.

The mirrored archive includes Han Kleijn's `acknowledgement of databases.txt`,
which is the attribution the Gaia licence requires and travels with the data
rather than beside it. See [Acknowledgements](README.md#acknowledgements).

## Image sources

Three modes, selected by `source.mode`:

| Mode | Pixels come from |
|---|---|
| `artificial` | Stars rendered from the local HNSKY `.290` catalogue |
| `dss` | A real survey cutout (hips2fits, SkyView or ESO), reprojected |
| `composite` | Survey background **plus** rendered stars on top |

`composite` is the interesting one: it gives you real nebulosity together with
stars at catalogue-known positions, which is what makes a frame usable as ground
truth. By default the survey's own point sources are suppressed first
(`suppress_background_stars`), so a real star and its rendered counterpart do not
stack into a double-brightness artefact.

Surveys are requested as **FITS, not JPEG/GIF**, so a real WCS arrives with the
pixels and reprojection is exact.

### Survey back ends

`source.dss.survey` is `<backend>:<survey>`:

| Prefix | Service | Notes |
|---|---|---|
| `hips:` | CDS [hips2fits](https://aladin.cds.unistra.fr/hips/list) | Default. Any HiPS id, so narrowband and modern bands are reachable |
| `skyview:` | SkyView, via astroquery | Native plate pixels; names contain spaces (`skyview:DSS2 Red`) |
| `eso:` | archive.eso.org DSS CGI | Direct DSS cutouts; narrowest choice of the three |

Short names are accepted for both `hips:` and `skyview:` and translated, because
an unknown name otherwise fails on every exposure and the run silently falls back
to the artificial sky. Anything containing a `/` is taken as a full HiPS id and
passed through, so the entire CDS list works without being listed here:

```toml
survey = "hips:dss2r"                       # -> CDS/P/DSS2/red
survey = "hips:ha"                          # -> simg.de/P/NSNS/DR0_2/halpha
survey = "hips:CDS/P/Finkbeiner/Halpha"     # verbatim
```

**Only monochrome HiPS.** hips2fits answers `format=fits` for colour HiPS too,
but with a `(4, H, W)` uint8 RGBA cube — the JPEG tiles in a FITS wrapper, with
the bright cores already clipped flat. Those are rejected at decode with a message
naming the mono alternative, rather than imaged as an 8-bit colour channel. None
of the short names resolves to one.

**Coverage.** A partial-sky survey off its footprint, or one that masks its
saturated cores, reprojects to a frame full of NaN. Zero-filling that paints a
black hole exactly where the target is, so `source.dss.min_coverage` (default
0.5) refuses the frame and the fallback serves a usable one instead. PanSTARRS
over M42 is the worked example: the whole core is masked out. Smaller gaps are
filled as empty sky and warned about once.

**Two hips2fits hosts, tried in order.** CDS serves the same service from
`alaskybis.cds.unistra.fr` and `alasky.cds.unistra.fr`, and they fail
independently: With one host that is the whole survey path down for
however long the outage lasts, so `source.dss.hips_bases` is a failover chain and
the first host that answers is remembered for the rest of the process.

```toml
[source.dss]
hips_bases = ["https://alasky.cds.unistra.fr/hips-image-services/hips2fits"]
hips_probe_timeout_s = 15.0
```

hips2fits resamples onto whatever pixel grid it is asked for, so the request is
sized to the sensor's own plate scale across the downloaded footprint, clamped to
`[300, source.dss.max_download_px]`. The cutout is fetched north-up and rotated
locally — passing `rotation_angle` would make every rotator position a separate
download and a separate cache entry.

`source.dss.cache_dir` caches the downloaded cutouts. The cache key is the
pointing **snapped to a grid** of an eighth of the frame, not the raw centre:
`actual_pointing` carries tracking noise, so a raw centre never repeats and every
exposure re-downloaded the same field. The download is enlarged by the same
eighth so the frame stays covered wherever in the cell the true centre lies.

### The sensor is authoritative

Every source returns pixels on the sensor's own grid, and this is a load-bearing
rule rather than a convenience. The tempting shortcut is to let the survey drive
the frame: clamp the field of view to what the archive will serve, then derive
the pixel dimensions from the survey's plate scale. In a GUI that merely
surprises you. Headless it mutates `CCD_INFO` / `CCD_MAX_X` / `CCD_MAX_Y`
underneath a connected client, which read those once at connect and never look
again — so the client's frame maths quietly stops matching the frames arriving.

Here the geometry is immutable and the survey is resampled to fit.
`tests/test_sources.py` asserts this against surveys at 0.8", 1.7" and 5.0"/px.

### The guide camera is separate hardware

A guide camera is not a second copy of the imaging chip: it is small, uncooled
and coarse-pixelled, and it usually looks through a short guide scope. Three
optional config blocks say so, and each one falls back to the imaging value when
left out, so configs written before them behave exactly as they did:

| Config | Effect when set |
|---|---|
| `[sensor_guide_cam]` | The guider's own chip — `CCD_INFO`, `CCD_FRAME` bounds, frame size, well depth, read noise, hot pixels, Bayer pattern |
| `telescope.guide_focal_length_mm` / `guide_aperture_mm` | A separate guide scope. Sets the guide plate scale and fills `GUIDER_FOCAL_LENGTH`/`GUIDER_APERTURE` in `TELESCOPE_INFO`, which used to echo the main OTA |
| `optics.guide_hfd_px` | Fixed guide-star HFD, for a guide scope that holds its own focus |

`[sensor_guide_cam]` takes every key `[sensor]` does and inherits none of them —
spell out what you need. Omitting the section logs a warning at startup, since
two identical cameras is a config accident far more often than a real rig.

### Where the guide star's focus comes from

**Off-axis guider (the default, and what `examples/sim.toml` configures).** Leave
the last two rows above unset. The guide camera then looks through the imaging
OTA at the imaging focal length, and its focus tracks the imaging focuser —
correct, because the pickoff prism sits in the imaging train *downstream* of the
focuser. An autofocus sweep really does bloat the guide star, which is why Ekos
suspends guiding while it runs, and pinning the guide HFD would hide exactly that
(`test_an_off_axis_guider_follows_the_focuser`).

The prism is *upstream* of the filter wheel, though — it has to be, or a
narrowband filter would starve the guide camera. So `guide_hfd` deliberately
drops the per-filter focus offset that `current_hfd` applies: the offset that
brings the imaging chip into focus on Ha takes the guide star the same 120 steps
*out* of focus, and the guide star goes measurably soft (HFD 2.35 → 2.64 in the
example config). Computing both from one perfect-focus position loses that
(`test_a_filter_focus_offset_softens_the_oag_guide_star`).

**Separate guide scope.** Set all three, including `guide_hfd_px`: that scope is
focused once and left alone, so the imaging focuser must not touch it or an
autofocus run to HFD 10 loses the guide star mid-sequence
(`test_a_focus_run_does_not_blur_a_fixed_focus_guide_star`).

The geometry stays authoritative per camera: each one renders on its own grid at
its own plate scale, and `FOCALLEN`/`APTDIA`/`HFD` in the FITS header describe
the train that camera actually looks through — a plate solver takes `FOCALLEN` as
its scale hint, so the main scope's value in a guide frame sends it hunting at
the wrong field size.

## Satellite trails

A long sub taken in the 2020s has often satellites in it. AstroSkySim propagates real
orbital elements and draws the trails, so a client's rejection stack, its trail
detector, or a human looking at a sub has something real to work on.

```bash
uv run astroskysim fetch-satellites          # download the elements
uv run astroskysim fetch-satellites --list   # what is enabled, and how old it is
```

### The configuration is shared, not per rig

Which satellites are in orbit is a property of the machine and of the week, not
of a telescope. So the source list, the element cache and the photometry live in
their **own file**, found by search rather than named by each rig config:

1. `--satellites FILE`
2. `[satellites] config = "..."` in the rig config
3. `./satellites.toml`
4. `~/.config/astroskysim/satellites.toml`

One `fetch-satellites` then serves every `sim.toml` on the box. If no file is
found the built-in defaults apply, identical to the shipped template, so the
feature behaves the same with and without one. `fetch-satellites` writes the
template on first use — that is the copy to edit. `examples/satellites.toml` is
the same file, generated from the same defaults.

A rig config carries only a pointer and an off switch:

```toml
[satellites]
# config = "~/observatory/satellites.toml"
# enabled = false      # no trails for this rig
```

There is deliberately no `enabled = true` in a rig config. With nothing fetched
there is nothing to switch on, and "does this machine have satellites" is the
shared file's decision.

### Sources

[Celestrak](https://celestrak.org/NORAD/elements/) groups. Any URL serving
two-line elements works, not only Celestrak.

Downloads are idempotent: a list fetched within `refetch_after_hours` is left
alone, and **Celestrak answers "you already have the current data" with HTTP
403**, not 304. Read as a status code that is indistinguishable from a rate
limit, so the body decides — otherwise the message sends you off to wait out a
throttle that does not exist.

### Photometry

`std_mag` is per source list: the visual magnitude the satellite would have at
1000 km range and a 90° phase angle. Everything else follows from the geometry
and from the optics already in play — inverse square in range, a diffuse-sphere
phase function, then the same `magnitude_to_electrons` the stars go through. So
a trail dims through a narrowband filter, brightens with aperture and moves with
the zero point, together with everything else in the frame.

Two consequences worth knowing:

- **A trail's brightness is flux per dwell time, not flux per frame.** A pixel
  collects light only while the satellite is on it, so the level along a trail
  is the same in a 30 s sub and a 300 s one — the trail is just longer. That is
  why an ISS pass saturates and a Starlink pass at 1.1 °/s does not, from the
  same photometry.
- **A geostationary satellite still trails.** It sits still over the ground and
  therefore drifts at the sidereal rate against the stars, drawing a short
  bright trail in a long sub rather than a long faint one. Nothing special
  handles this; it falls out.

One number per list is coarse — real per-object standard magnitudes need a
magnitude database, which no Celestrak TLE carries — but it does put an ISS pass
and a Starlink pass four hundred times apart in brightness, which is the ordering
that matters.

### What the frame gets

Satellites reach **light frames only**: a trail in a master dark is a defect a
client cannot distinguish from a real one. The count of trails that reached the
sensor is written to the FITS header as `NSATS`, which is the only way a
rejection stack can check itself against ground truth.

The trail is convolved with the same PSF the stars get, so a trail through a
defocused frame is a defocused trail, and it is added *after* the composite
source's point-source suppression, which would otherwise erase it.

### Cost

Propagating every object at the sampling a sub-pixel trail needs would be
millions of SGP4 calls per frame, so the search is coarse and only the few
satellites that come near the field are propagated finely, over only the seconds
they are near it. Measured: **0.6 s** for a 300 s sub on a 3008² sensor against
12000 objects, in the readout thread, against ~3 s for a survey reprojection on
the same frame. A 2 s guide frame costs ~0.06 s. It scales with exposure, so an
hour-long sub against the same sky is several seconds — still off the event
loop, and still less than the frame it is drawn on.

The search cone is `2.5 °/s × coarse_step_s` wide and is derived rather than
configured, because a LEO satellite can cross the whole field between two coarse
samples: the step and the cone are one decision, and splitting them is how a
coarser search silently loses trails.

## Wind and gusts

Without a dome the rig is exposed. Wind pushes the tube — continuously, and in
gusts — and the mount deflects. Every other error term in this simulator is read
**once per frame**, at readout, so it produces a frame-to-frame centroid shift
and nothing else: a 300 s sub with 4″ of periodic error renders perfectly round
stars at a displaced centre. Wind is the one that happens *while the shutter is
open*, and it is what this section adds.

```toml
[server]
weather = true          # else nothing reports the wind to a client

[wind]
enabled = true
speed_kmh = 18.0                   # mean sustained speed while it is blowing
probability = 0.45                 # fraction of the session that is windy at all
duration_s = 180.0                 # mean length of a windy spell
gust_speed_kmh = 45.0              # peak in a gust; must be >= speed_kmh
gust_probability = 0.15            # fraction of a windy spell spent gusting
gust_duration_s = 2.5
response_arcsec_at_20kmh = 1.2     # the one calibration constant; scales as v^2
resonance_hz = 4.0                 # ring-down; this is what makes the V-shapes
damping = 0.12                     # must be < 1, or there is no ringing at all
axis_ratio_ra_dec = 1.5            # RA is the softer axis, so spikes land there
buffet_fraction = 0.25
history_s = 900.0                  # bounds the longest sub that can be smeared
```

Off by default. Every measured number in the test suite was taken against a
still sky, so a default of `true` would move all of them at once.

### Two effects, one state

The deflection is added to `actual_pointing`, in the same gap the other error
terms live in. That is most of the feature, and it is nearly free: the guide
camera images `actual_pointing`, so a gust throws the guide star, the client
corrects what it sees, and the pulse moves `mount.ra_deg` — shifting reported and
actual together. Sustained push therefore gets guided out with a lag and the
ring-down does not, because it is above any guider's correction bandwidth.
Nothing here computes an RMS; the client's RMS spike is the consequence.

The **mid-exposure smear** is the other half. `Rig.apply_wind_smear` slices the
deflection history over the exposure window, converts it to a pixel path, builds
a motion kernel and convolves the frame with it. One kernel for the whole frame,
which is exactly right for a translation: wind moves every star in the field
together, so stars, survey nebulosity and any satellite trail smear as one.

Two invariants carry it, and both are invisible in the pixels when broken:

- **Flux is conserved.** The kernel sums to 1. This is the opposite of a
  satellite trail, where brightness is flux per *dwell time* and a longer
  exposure lays down a longer streak at the same surface brightness — wind
  redistributes a star's fixed electron budget, so a streaked star is fainter per
  pixel and the same total.
- **The kernel is zero-mean, and the WCS carries the window mean.** So the smear
  spreads a star without moving it, and a wind-ruined sub still plate-solves to
  the true centre. `Rig.capture` takes the window **once** and hands the mean to
  `build_wcs` and the path to the kernel, because `capture` runs in the readout
  thread after the shutter closed — anything that re-reads the wind there gets a
  sample from outside the exposure entirely, and the frame translates by the
  difference. For a gust that is the whole amplitude.

### Focal length, without a focal-length parameter

The deflection is angular, so the plate scale does all of it. On 3.76 µm pixels
the same 1.2″ shake is 0.7 px at 432 mm and 3.1 px at 2000 mm — a factor of five,
exactly the focal-length ratio. `astroskysim -v` logs the pixel equivalent for
the rig you configured, at both plate scales, because arcsec-versus-pixels is the
unit trap in this section.

Whether the *guider* resolves the shake it is meant to correct depends on its own
plate scale, and both cameras are handled with no special case: each smear is
built from that camera's own WCS. An oversampled off-axis guider (2.9 µm through
the imaging OTA, 1.385″/px in `examples/sim.toml`) sees it slightly better than
the imaging chip; a 240 mm guide scope sees it far more coarsely and genuinely
under-resolves it.

### What the frame gets

**Light frames only**, like satellite trails: a wind smear on a flat is a no-op
except at the border, and on a bias there is nothing to smear. The header carries
`WINDKMH`, `GUSTKMH` and `SMEARPX` (peak-to-peak, in unbinned sensor pixels)
for the same reason `NSATS` exists — a client looking at streaked stars cannot
otherwise tell wind from a bad guide star or a slipped clutch.

The smear is applied after the satellite trails and **before** `apply_bayer`,
`add_sky_and_noise` and `add_hot_pixels`. Each of those three is deliberate: a
real CFA samples a smeared scene rather than smearing an already-attenuated
mosaic; convolving read noise would correlate it and leave the frame smoother
than the sensor is; and being downstream of the composite source's point-source
suppression keeps it from erasing the streak.

### Cost

The mount's ring-down is a few Hz, so the model integrates at a fixed 128 Hz
sub-step — sampling at the 10 Hz tick would alias the ringing into a slow wobble
— using a closed-form transition matrix for the damped oscillator. That is exact
for any step, which is what lets a stalled tick be caught up in one step instead
of spinning, with wind time still exactly equal to `elapsed_s`. Cost is tens of
microseconds per tick; 900 s of history is ~115k samples in two float32 columns,
under a megabyte.

The smear is one convolution, applied two ways. Measured on a 3008² frame: the
FFT pair is flat at **0.155 s** whatever the kernel, because the frame dominates,
while accumulating a shifted view costs ~7.6 ms per kernel tap. So small smears
take the taps (7 taps, 0.061 s) and large ones the FFT, crossing over around 16.
A 2 s guide frame costs ~0.015 s. Sub-pixel smears are skipped outright, so a
calm night is free.

## Temperature and focus drift

A night cools. Dusk to dawn is 5–15 K at a lowland site, with warm and cold air
masses passing over on top of that, and focus follows: the tube lengthens and
shortens and the glass changes index. That is 15–25 µm/K on an aluminium-tube
refractor and 150–350 µm/K on a Schmidt-Cassegrain, where the secondary amplifies
the primary-to-secondary spacing change by m² ≈ 25. So an autofocus run goes
stale, and the staler it gets the softer the subs.

```toml
[server]
weather = true          # else nothing reports the temperature to a client

[temperature]
enabled = true
start_c = 16.0                  # ambient when the session opens
night_drop_c = 10.0             # total fall toward the asymptote
tau_hours = 5.0                 # measured range is 3-8 h
hours_into_night = 0.0          # 3.0 opens most of the way down the curve
spell_amplitude_c = 2.5         # signed, so warm spells as well as cold
spell_probability = 0.25        # fraction of the session inside a spell
spell_duration_s = 900.0
spell_ramp_s = 180.0            # air does not step
sigma_c = 0.15                  # background wander
noise_tau_s = 300.0
optics_tau_s = 2400.0           # how far the optics lag the air
probe_tau_s = 300.0             # how far the focuser's probe lags it
focus_shift_um_per_c = 20.0     # signed; + means cooling racks out
# reference_c = 16.0            # where perfect_focus really is perfect
```

Off by default. Every measured HFD in the test suite was taken at a fixed focus,
so a default of `true` would drift all of them at once.

### Three temperatures, and the gap between them is the feature

This is the thermal analogue of `mount.ra_deg` versus `actual_pointing`. Three
numbers, and only one of them sets focus:

| number | lag | who sees it |
| --- | --- | --- |
| `air_c` | none | `WEATHER_TEMPERATURE` |
| `probe_c` | short (`probe_tau_s`) | `FOCUS_TEMPERATURE` — a sensor on the focuser body |
| `optics_c` | long (`optics_tau_s`) | nothing, except the FITS header |

A client that calibrates `focuser.temp_coeff` against the probe and compensates
perfectly *still* drifts, because focus follows the optics. That is not a
simplification artefact — it is the real failure, and the most commonly
misdiagnosed one: a ZWO EAF's sensor sits at the focuser, in the airflow, which
is why reported coefficients for nominally identical rigs scatter from 20 to over
100 steps/K, while an Optec TCF-SI probe strapped mid-tube calibrates
reproducibly. Set `probe_tau_s = optics_tau_s` to model that second rig.

Driving focus from `air_c` instead collapses all three into one, and a correct
coefficient then works perfectly — leaving the feature with nothing to test a
client against.

`focuser.temp_coeff` and `temperature.focus_shift_um_per_c` are deliberately two
numbers. One is the correction the client applies, the other is the error the
telescope actually commits. `focus_shift_um_per_c / focuser.step_size_um` is the
perfectly calibrated coefficient, and it is worth setting it wrong on purpose.

### How much focus a degree costs

`focus_shift_um_per_c` is **one lumped constant**, standing in for tube CTE,
glass dn/dT and secondary amplification together — the same choice as
`wind.response_arcsec_at_20kmh`, and for the same reason. It is deliberately not
derived from a tube-material setting: the Cassegrain amplification is m², and m
runs 1.5–5 across designs, so a single "cassegrain" constant would be wrong for
most rigs.

| tube | µm/K | basis |
| --- | --- | --- |
| Refractor, aluminium | 15–25 | measured; CTE 23 ppm/K × focal length |
| Refractor, carbon | 5–12 | inference — the tube term vanishes, the glass remains |
| Newtonian, aluminium | 20–25 | CTE × focal length, no amplification |
| Newtonian, steel / carbon | 10–13 / 2–6 | CTE 12 / ~0–1 ppm/K |
| Maksutov | 150–300 | **inference only** — Gregory m ≈ 6, so ~36× |
| SCT, C8 f/10 | 150–260 | three independent routes agree within ~30% |
| SCT, C11 / C14 | ~300 / ~350 | a C14 measures 161 steps/K on a 2.16 µm step |

The C8 figure is the best-supported number here: Optec's default coefficient
(86 steps × 2.16 µm = 186), a published estimate (254), and 23 ppm/K over a
400 mm tube amplified by 25 (230) all land in the same place. The Maksutov row is
m² inference with no measurement behind it. The sign convention is that
**positive means cooling racks the focuser out**, to a higher step number, which
is the direction every measured position-versus-temperature slope reports.

The unit trap is µm/K against *this rig's* `focuser.focus_range`, not against a
real critical focus zone. The shipped `focus_range = 1000` with `step_size_um =
1.0` is a much looser focus tolerance than a real f/5 system has, so 20 µm/K over
a 10 K night takes HFD from 2.35 to only ~3.09 — while 200 µm/K reaches ~20.
`astroskysim -v` logs the drift and the resulting HFD for the rig you actually
configured, because that is the number that says whether the config does
anything. Tighten `focus_range` for a rig that should need refocusing every half
degree.

### What the frame gets

The header carries `AMBTEMP`, `OPTTEMP` and `FOCDRIFT` (steps from the reference
position), for the same reason `NSATS` and `WINDKMH` exist — a client looking at
a soft sub cannot otherwise tell thermal drift from bad seeing or a missed focus
run. `OPTTEMP` is there specifically because no property publishes it.

Both chips drift. The tube expands upstream of the whole train, so an off-axis
guider's star bloats with the imaging one, by the same route that makes an
autofocus run suspend guiding. A separate guide scope pinned with
`optics.guide_hfd_px` holds its own focus and is correctly immune.

### Cost

A handful of floats per tick and no arrays at all — there is no history ring,
unlike wind. That asymmetry is deliberate rather than an omission: wind has to be
resolved *within* an exposure because it smears a star, but temperature moves at
~0.3 K/h, so even an SCT at 200 µm/K drifts ~5 µm across a 300 s sub. So this is
read once per frame at readout, like every other error term in the simulator.

The cooling curve is closed-form in elapsed time and the lags use
`1 - exp(-dt/tau)` rather than `dt/tau`, which saturates instead of overshooting.
A stalled tick therefore lands *on* the target rather than sailing past a
40-minute time constant and ringing.

## Design notes

**Shared devices, per-client sessions.** One `Rig` owns all physical state;
per-connection state (announced properties, BLOB policy, output queue) lives in
the session. The alternative — a device instance per connection — pushes the
physical state into globals to keep the instances agreeing, and every gap in that
bookkeeping surfaces as two clients disagreeing about where the telescope points.
Here two clients provably see one telescope: `test_two_clients_see_the_same_mount`.

**Coalescing output.** A `set*Vector` carries a property's *current* value, so a
newer one supersedes a queued older one. Without coalescing, a bounded queue
drops the `state="Ok"` that tells a client a slew finished, and the client hangs
forever. Definitions, messages and BLOBs are discrete events and are never
merged.

**BLOBs require `enableBLOB`.** A client that never sends it receives no frames.
This is correct INDI behaviour and a classic way to see "no images" —
`test_blobs_are_withheld_until_enabled` pins it.

**Closed-form astronomy on the tick.** The loop runs at 10 Hz over six devices.
Building an astropy `Time` per access and running `sidereal_time` or an `AltAz`
transform on it costs ~300 ms per tick, which starved the loop badly enough that
slews crawled. Sidereal time and alt/az are computed in closed form and pinned
against astropy in `tests/test_fast_astronomy.py`; astropy is retained for
one-off precision work like precession.

**Coordinates are equinox of date.** INDI's property is literally
`EQUATORIAL_EOD_COORD`, so the astropy reference implementations use FK5 at the
epoch of observation. Labelling them ICRS silently adds the full J2000-to-now
precession — about 20 arcminutes today.

**Reported vs actual pointing.** The mount reports the commanded position; the
camera images the actual one. Polar misalignment, periodic error, tracking noise
and wind live in the gap, so guiding and plate-solve-and-centre loops have
something real to correct.

**Only wind is resolved *within* an exposure.** The other error terms are read
once per frame, at readout, so they displace a round star; a 300 s sub with 4″ of
periodic error has perfectly round stars in the wrong place. Wind is recorded as
a path while the shutter is open and convolved into the frame, which is what
turns it into a streak, a smear or a V. That asymmetry is a deliberate limit and
not an oversight — trailing from *any* of these terms would need the same history
machinery, and wind is the one where a client is expected to lose the frame.
`tests/test_wind.py` pins the two invariants that make it safe: flux is conserved
and the smear does not move the star.

**`GUIDER_INTERFACE` is a promise, not a label.** Bit 2 of `DRIVER_INTERFACE`
means "this device has an ST4 port and accepts `TELESCOPE_TIMED_GUIDE_*`".
Clients build their guide-pulse device list from it, so the bit and the two
properties must always travel together — the reference drivers agree
(`indi_simulator_telescope` reports 5 = TELESCOPE|GUIDER, `indi_simulator_ccd`
reports 22 = CCD|GUIDER|FILTER, and both implement the pulse properties). The
mount and the guide camera both set the bit here and both route pulses to the
one shared `rig.mount`, so guiding works whichever device the client picks.
`test_every_guider_interface_device_accepts_timed_pulses` pins the invariant.

**One photometric scale for stars, sky and nebulosity.** A magnitude, a sky
brightness and a survey cutout all reach electrons through the same zero point,
aperture, throughput and plate scale — `magnitude_to_electrons` for point
sources, `surface_brightness_to_electrons` for extended ones. That is what makes
exposure time mean something: a 1 s R sub of IC 1805 on the 90 mm in
`examples/sim.toml` sits a couple of ADU above the offset, and a 20 s one is
faintly there, which is what the real sub does.

The survey path used to bypass all of it. `DssSource` percentile-stretched each
cutout to `[0, 1]` and multiplied by a fixed 400 e-/px/s, which had three
consequences: 5% of every frame came out at *zero* flux (darker than the sky, and
impossible); the object-to-sky contrast came from the plate's density curve
rather than from photometry; and the telescope never entered the calculation at
all, so a 1 s sub arrived with 30–150 e-/px of "nebula" over the sky and its
histogram filled the whole range in one second. Now the survey's own sky level is
estimated and subtracted, and what remains is anchored — `ref_percentile` of it
stands for a surface brightness of `ref_mag_arcsec2` — so aperture, plate scale,
throughput, filter and exposure all bite. `tests/test_photometry.py` pins each of
those dependencies.

**Sky brightness is an SQM reading.** `optics.sky_mag_arcsec2` is in
mag/arcsec² and converts through the optics, so a coarser pixel or a bigger
aperture moves the background the way it does on the sky, and the two cameras get
different rates. `optics.sky_background` still overrides it with a raw e-/px/s
figure, and the unit is a genuine trap: `sky_background = 21.0` reads like an SQM
21 sky and is in fact about SQM 17 on a small refractor. Startup logs which of
the two is in use, and what the override works out to in magnitudes.

**Filters attenuate the whole beam.** `filter_wheel.transmission` scales stars,
nebulosity *and* sky together, as a real filter does, so a narrowband sub is
genuinely starved and needs the exposure to match. It is folded into the
throughput of the imaging camera only — the OAG pickoff prism is upstream of the
wheel, the same reason `guide_hfd` drops the per-filter focus offset. Leave the
key out and every filter passes everything, which is what configs before this
got.

**One survey per filter.** `[source.dss.per_filter.<name>]` attaches a survey to
a filter, so Ha, OIII and SII show different *structure* rather than the same
picture at different brightness. The guide camera never dispatches — its
`filter_name` is `None`, so a narrowband layer can never starve it. A typo in a
filter name is a startup error, because the silent alternative is that filter
quietly keeping the broadband default.

Two rules make the mapping mean something, and both matter more than the mapping
itself:

*An in-band survey is exempt from the filter's transmission.* `transmission` is a
broadband fraction — the share of a wide band a filter passes. That is right for
a star and for the sky, whose light the filter throws away, and wrong for an
image already taken in that band: a 3 nm Ha filter selects the Ha line, it does
not dim an Ha map by fifty. So `SurveyLayer.in_band` (default true for a mapped
filter, false for the shared default layer) takes the factor back out of the
survey path while leaving stars and sky attenuated. That asymmetry *is* the
narrowband win, and `test_narrowband_keeps_the_nebula_and_loses_the_sky` pins it
as a ratio: signal-to-sky improves by exactly `1/transmission`.

*An absolute anchor beats a per-frame percentile on a linear survey.*
`ref_value` names a raw survey level above the survey's own sky, instead of
letting `ref_percentile` normalise each cutout against itself. The percentile
throws away two real things — the ratio between a survey's bands, and the
difference between a bright target and an empty field. The three NSNS line maps
share one calibrated scale, so one `ref_value` across Ha, OIII and SII
reproduces the real ratios with nothing in the config saying so: IC 1805 comes
out Ha-dominated (OIII/Ha ≈ 0.2 at p99), M27 comes out OIII-dominated (≈ 2.2),
and a blank field in Coma comes out blank. DSS2 is photographic and per-plate
scaled, so it keeps the percentile.

**Hot pixels are dark current.** Their charge scales with exposure time
(`sensor.hot_pixel_e_s`). A fixed fraction of full well regardless of exposure
makes every hot pixel saturate even in a 0.5 s guide frame, so the immobile
fixed-pattern pixels outshine every real star — and a guider that locks onto one
measures no drift under calibration pulses.

**The `.290` decoder is validated against the real sky, not just a round trip.**
A round-trip fixture shares its encoder with the decoder, so it cannot catch a
misreading of the format that both sides make. The real `catalog/` g14 set
(GAIA eDR3 to BP 14.0, epoch 2025, 290 cells, 11,290,236 stars) is checked three
ways in `tests/test_catalog.py`, and all three passed unchanged — no decoder fix
was needed:

- **Every star lands in the cell its own filename claims.** The cell assignment
  was made by whoever built the database, so this tests the RA and Dec scales,
  the two's-complement Dec sign, the running `dec9`/magnitude state carried by
  the header records and the ring indexing at once. 7 stars out of 11.29M fall
  the other side of a boundary, and each of those 7 is within one storage
  quantum of it — rounding ties, and the test asserts they are.
- **A real field reproduces Gaia eDR3 star for star.** Nine sources within 216"
  of 300.0 +40.0, propagated from Gaia's Ep=2016.0 to the epoch 2025 the file
  header states: nine matched one to one, worst residual 0.04", no systematic
  offset in either axis (−0.0006" ± 0.019" in RA, −0.00005" ± 0.012" in Dec
  over eight fields). That is the 0.077"/0.039" storage quantisation floor and
  nothing else. Magnitudes reproduce BP to half of the 0.1 mag bin. The frame is
  therefore ICRS/J2000 equinox — reading it as equinox of date would displace
  everything by ~20 arcminutes.
- **The brightest stars sit where proper motion puts them.** Nine stars from
  Sirius down, J2000 position plus Hipparcos proper motion over 25 years, all
  within 0.09". The residual *is* the proper motion: ignore it and Arcturus is
  57" out, Sirius 33", Rigel 0.05". This is also the only coverage of the 82
  Tycho2 bright-star additions, since Gaia saturates above about magnitude 3.

`StarCatalog.query` is separately checked against a brute-force scan of all
11.29M stars at six pointings (RA wrap, both poles, ring boundaries, a field
spanning several cells), so `areas_covering` cannot quietly drop an edge cell.

One thing the cross-match turned up is a property of Gaia, not of this code: in
the Orion Nebula, VizieR returns 287 sources with BP < 14 where g14 has 39. All
248 extras have *negative* BP−G — BP brighter than G, which no real star is —
and G of 13.5 to 15.4. Their BP photometry is contaminated by the nebula's blue
background, and the database builder was right to drop them.

## Known limitations

- **Sidereal time is mean, and treats UTC as UT1.** Disagreement with astropy
  reaches ~9" (ΔUT1 is up to ±0.9 s and needs IERS data we do not fetch). It
  feeds only `HORIZONTAL_COORD`, `TIME_LST` and pier-side selection, never the
  imaging path, so no frame is displaced by it.
- **The `.290` reader handles record sizes 5 and 6 only** — the same limit as the
  HNSKY reader it is ported from. Other sizes raise a clear error.
- **Without a `.290` database a synthetic star field is used.** Fine for focus,
  guiding and framing; plate solving against it will fail, and the simulator
  warns on startup.
- **An unterminated XML element blocks that connection.** By definition the
  bytes after it are that element's content, so no resynchronisation is
  possible. Other clients and the server are unaffected.
- **Colour is crude.** `apply_bayer` attenuates per CFA site so clients exercise
  their debayer path; it does not model per-star SEDs.
- **Absolute photometry is approximate.** `optics.zero_point_e_s_m2` defaults to
  1e10 e-/s/m² for a magnitude 0 source, which is order-of-magnitude correct for
  a broad visual band, not calibrated. Everything scales off it coherently, so
  moving it moves stars, sky and nebulosity together.
- **`ref_percentile` is still per frame.** Where no `ref_value` is set the
  percentile of a cutout is *defined* to be `ref_mag_arcsec2`, so the absolute
  level depends on what is in the field: a frame with no bright object gets its
  faint structure pulled up towards the reference, and an empty field renders as
  brightly as M42. That is why `examples/sim.toml` anchors every layer with
  `ref_value` instead. The percentile remains the default because it needs to
  know nothing about the survey, which is what an unfamiliar HiPS requires.
- **The background estimate is still per frame even under `ref_value`.** An
  object filling the whole sensor has some of itself subtracted as sky, so a
  very wide field and a tight crop of the same target do not land at exactly the
  same level. Taking the median of the lower half rather than a plain median
  limits it, but does not remove it.
- **Luminance has no survey.** DSS2 red is the stand-in in `examples/sim.toml`:
  all-sky, sharp, and its passband contains Ha so emission nebulae actually
  show. It is an R-band image wearing an L label, one magnitude brighter.
- **There is no green plate.** DSS2 runs blue (468–491 nm) then jumps to red
  (640–658), so `examples/sim.toml` gives G and B the same image and only
  different anchors. PanSTARRS `g` is a genuine green-blue band, but its 99th
  percentile is stellar peaks even in an empty field, so anchoring nebulosity on
  it yields sharp stars over nothing. Neither is a fix; the survey does not
  exist.
- **NSNS is coarse and northern.** 6.4"/px against DSS2's 0.8", so narrowband
  frames are soft and the survey's own stars are blobs — pair it with
  `mode = "composite"` to get catalogue stars instead. It covers ~65% of the
  sky; below its footprint the fetch fails and `fallback_to_artificial` serves a
  star field with no nebulosity.
- **Satellite magnitudes are one number per source list.** Real per-object
  standard magnitudes need a magnitude database (Mike McCants' `qs.mag`), which
  no Celestrak TLE carries. The values shipped are estimates from published
  observing campaigns, not measurements made here; they are an ordering, not
  photometry. Nothing models tumbling, flares, or the brightness difference
  between a Starlink v1.5 and a v2 mini.
- **The Earth's shadow is a cylinder.** The real one is a cone with a penumbra a
  few hundred km deep, so a satellite entering eclipse fades over a second or
  two where this cuts sharply. The useful behaviour — a trail that stops
  mid-frame — is there either way.
- **Satellite positions are TEME treated as equinox of date.** The two differ by
  the equation of the equinoxes, up to ~1.1″, which is three orders of magnitude
  below the arcminute-scale error a TLE a few days old already carries.
- **The wind smear is spatially invariant.** One kernel for the whole frame is
  exactly right for a translation, which is what a mount deflection is. It does
  not model the position-dependent part: field rotation from polar misalignment,
  and differential flexure. A corner star and a centre star smear identically
  here, where a real badly-aligned mount elongates them differently.
- **Wind compliance is lumped, not aerodynamic.** `response_arcsec_at_20kmh` is
  one number standing in for sail area, drag coefficient, lever arm and
  torsional stiffness, and `axis_ratio_ra_dec` is one number for the mount's
  two-axis compliance. There is no wind azimuth, so pointing into the wind is no
  worse than pointing across it, and a gust's direction is a coin toss rather
  than the weather's. Calibrate the constant against your own rig; it is not
  predictive from a specification.
- **Both cameras see the same deflection.** Correct for an off-axis guider, which
  shares the tube. Wrong for a separate guide scope, which flexes on its own
  rings — differential flexure under gusts is a well-known way to lose a night
  and is not modelled, so guiding here can always in principle chase the shake
  the imaging chip actually sees.
- **The smear cannot recover flux from outside the sensor.** Wind pulls light in
  from beyond the frame edge, and a convolution over the rendered array has
  nothing there to pull; the frame is edge-replicated before convolving, which
  avoids a dark border but does not invent the stars that should have drifted in.
- **Thermal focus drift is linear in temperature.** One coefficient, applied to
  one temperature. Some real rigs measure a quadratic relation with a stable
  slope and a drifting zero offset, and a carbon-tube SCT has been reported
  drifting non-monotonically with outright direction reversals. Nothing here
  reverses direction: cool and the focus point moves one way, monotonically.
- **The optics never sit *below* ambient.** They lag it and nothing more. Real
  exposed surfaces radiate to a sky 25–50 K colder than the air and settle
  *under* it, by an amount that shrinks as the wind picks up — so a *rise* in
  ambient makes the error worse, which is the mechanism that best explains those
  reported reversals. Radiative subcooling is not modelled, and neither is any
  coupling between `[wind]` and `[temperature]`.
- **The cooling curve has no dewpoint floor.** Real nocturnal cooling stalls near
  the dewpoint as condensation releases latent heat; here it decays toward
  `start_c - night_drop_c` whatever the humidity, because there is no humidity.
  There is also no dew, and so nothing for a dew heater to be needed for.
- **A separate guide scope shares the imaging tube's coefficient.** Its own tube
  would expand at its own rate on its own rings. `optics.guide_hfd_px` pins the
  guide HFD outright, which is the only alternative offered — there is no second
  `focus_shift_um_per_c` for the guide train.
- **The night is anchored on session start, not on the sun.** `hours_into_night`
  says where in the curve to begin; nothing derives dusk from `[site]` and the
  date. So a session left running past dawn keeps cooling, and two sessions
  started at different clock times behave identically. This keeps the astropy
  ephemeris out of the tick, which is the same constraint `fast_lst_deg` exists
  for.
