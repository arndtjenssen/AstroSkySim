# AstroSkySim

Headless INDI simulator for a full astrophotography rig: mount, imaging camera,
guide camera, focuser, rotator and filter wheel. Serves INDI on port 7624 — no
GUI, no desktop application.

AstroSkySim follows in the footsteps of Han Kleijn's
[Sky Simulator for Ascom and Alpaca](https://sourceforge.net/projects/sky-simulator),
which established the idea of a synthetic sky driven by a simulated mount and
focuser. This is an independent implementation in Python, INDI only, built around
one shared rig rather than a GUI application. See
[Acknowledgements](#acknowledgements) and [Licence](#licence).

## Quick start

Download the gaia g14 database here and extract it into the catalog directory.

```bash
uv sync --all-extras --group dev
uv run astroskysim -c examples/sim.toml -v
```

Then point KStars/Ekos, CCDciel or any INDI client at `localhost:7624`.

Run tests with:

```bash
uv run pytest && uv run ruff check src tests
```

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

## Devices

INDI only. The property set is deliberately wide — a device that answers just
its headline property lets a client exercise almost nothing, so anything the rig
simulates is exposed:

- **Mount** — `TELESCOPE_MOTION_NS`/`_WE`, `TELESCOPE_SLEW_RATE`,
  `TELESCOPE_TRACK_RATE`, `GUIDE_RATE`, `TELESCOPE_OFFSET_RATES`,
  `HORIZONTAL_COORD`, `TIME_LST`, `TELESCOPE_HOME`,
  `TELESCOPE_PARK_POSITION`/`_OPTION`, `CONFIG_*`
- **Filter wheel** — `FILTER_FOCUS_OFFSET` (per-filter focus offsets)
- **Rotator** — `REL_ROTATOR_ANGLE`, `ROTATOR_MECHANICAL_ANGLE`
- **Focuser** — `FOCUS_TEMPERATURE_COMPENSATION`, `FOCUS_BACKLASH_*`
  (backlash is simulated physically, so a client that compensates is really tested)
- **Camera** — `CCD_COOLER_POWER`, `CCD_READOUT_MODE`, `CCD_STOP_EXPOSURE`
  (graceful stop that keeps the frame, as distinct from abort)

The guide camera has its own sensor spec (above) rather than a copy of the
imaging chip's, and `TELESCOPE_INFO`'s guider fields carry the guide scope rather
than echoing the main OTA.

`TELESCOPE_OFFSET_RATES` is a documented extension: INDI has no standard
property for RA/Dec offset rates, so the name is ours.

Not implemented, by choice: dome, switch, safety monitor, observing conditions.

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
camera images the actual one. Polar misalignment, periodic error and tracking
noise live in the gap, so guiding and plate-solve-and-centre loops have
something real to correct.

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
- **Luminance has no survey.** DSS2 red is the stand-in in `examples/sim.toml`:
  all-sky, sharp, and its passband contains Ha so emission nebulae actually
  show. It is an R-band image wearing an L label, one magnitude brighter.

## Layout

```
src/astroskysim/
  indi/      xml_stream.py  rootless-stream splitter (+ element parser)
             protocol.py    Text/Number/Switch/Light/BLOB vectors, sexagesimal
             device.py      Device base, DRIVER_INTERFACE bitmask
             server.py      asyncio server, sessions, coalescing queue, tick
  sky/       catalog.py     .290 reader, area indexing, synthetic fallback
             render.py      PSF, defocus hyperbola, noise, bayer, binning
             wcs.py         sensor WCS, closed-form LST/altaz, precession
  sources/   artificial | dss | composite, behind one interface
  rig.py     all physical state and the simulation step
  devices/   mount, camera, focuser, rotator, filterwheel
             pulse.py       TELESCOPE_TIMED_GUIDE_*, shared by every ST4 device
  cli.py
```

## Acknowledgements

**Han Kleijn's [Sky Simulator for Ascom and Alpaca](https://sourceforge.net/projects/sky-simulator)**
(hnsky.org) is the origin of the idea this project builds on: a simulated rig
whose camera returns a synthetic sky rendered from the mount's own pointing and
blurred by the focuser's own position, so an imaging client can be driven end to
end with no hardware and no clear night. AstroSkySim is an independent Python
implementation of that concept for INDI. The desktop application, the ASCOM and
Alpaca layers, and the sky map GUI are not part of it.

**Star data.** `artificial` and `composite` modes read the HNSKY `.290` star
databases, an extract of ESA's Gaia catalogue in the compact format developed for
the HNSKY and ASTAP programs. Those files are not produced here — see
`catalog/acknowledgement of databases.txt` for the full provenance and terms of
every catalogue involved.

> This work has made use of data from the European Space Agency (ESA) mission
> Gaia (https://www.cosmos.esa.int/gaia), processed by the Gaia Data Processing
> and Analysis Consortium (DPAC,
> https://www.cosmos.esa.int/web/gaia/dpac/consortium). Funding for the DPAC has
> been provided by national institutions, in particular the institutions
> participating in the Gaia Multilateral Agreement.

**Survey imagery.** `dss` and `composite` modes fetch cutouts at runtime from
[CDS hips2fits](https://alasky.cds.unistra.fr/hips-image-services/hips2fits)
(Centre de Données astronomiques de Strasbourg), NASA/GSFC SkyView, or the ESO
archive. Each service sets its own terms and its own acknowledgement
requirements for the surveys it serves; if you publish anything derived from a
fetched image, credit the survey, not this simulator.

## Licence

AstroSkySim is free software under the **GNU General Public License, version 3 or
(at your option) any later version** — see [LICENSE](LICENSE). It comes with no
warranty; see sections 15 and 16.

GPLv3-or-later is chosen deliberately rather than permissively. Sky Simulator for
Ascom and Alpaca carries GPLv3-or-later headers on its source units, and this
project was written with knowledge of how that program works. Nothing here is
translated from it, but a licence that is compatible with it either way removes
the question rather than arguing it.

The catalogue and survey data described under
[Acknowledgements](#acknowledgements) are **not** covered by this licence. They
carry their own terms, some of which are more restrictive than the GPL — notably
`catalog/deep_sky.csv`, which is licensed for non-commercial use only.
