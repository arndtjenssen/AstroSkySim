# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

AstroSkySim (`astroskysim`) — a headless INDI server simulating a full astrophotography rig
(mount, imaging camera, guide camera, focuser, rotator, filter wheel, and an optional weather
station reporting the simulated wind). **INDI only: no GUI, no
Alpaca, no ASCOM**, and none of those are wanted. The project descends conceptually from Han
Kleijn's "Sky Simulator for Ascom and Alpaca" — an independent implementation of the same idea,
not a translation of it. See the Acknowledgements section of `README.md`.

`README.md` records the design decisions and the reasoning behind each device's property set —
read it before adding a device property. This file is the short version. GPLv3-or-later; keep the
licence header in `src/astroskysim/__init__.py` intact.

## Commands

```bash
uv sync --all-extras --group dev
uv run astroskysim fetch-catalog                    # ~56 MB .290 database, once
uv run astroskysim fetch-satellites                 # orbital elements, occasionally
uv run astroskysim -c examples/sim.toml -v          # -v info, -vv debug
uv run pytest -q                               # ~20 s, no network needed
uv run pytest tests/test_end_to_end.py::test_slew_moves_and_settles
uv run pytest -k "device_filter or blob"
uv run ruff check src tests
```

`ruff` is the only static check configured. There is no mypy in the dev group — the `# type: ignore`
comments on `vec.apply(...)` / `vec.selected` are hand-placed for a checker that is not wired up, so
don't trust them as evidence a type is correct.

CLI flags override the TOML (`--port`, `--host`, `--mode`, `--survey`, `--catalog-dir`,
`--satellites`); everything else is config-only.

`catalog/` is **gitignored except its README** — the `.290` files are fetched, not committed. Without
them a synthetic star field is substituted and startup warns: usable for focus, guiding and framing,
but plate solving against it will fail. only stars are rendered, so there is no nebulosity in `artificial` mode
(that is what `composite` is for).

`sky/fetch.py` is the download, and the reason it is a mirror rather than a link is load-bearing:
**g14 is retired upstream.** HNSKY and ASTAP now ship g17/g18/v16/v17/u16 and the d/v/g tiers, 100 MB
to 1.3 GB, nothing at magnitude 14 — so no upstream URL exists for the database every test fixture
and every measured number in this file was taken against. The archive is a pre-release asset on this
repo, pinned by tag and SHA-256; `catalog/README.md` has the publish commands. Two traps the code
guards, both with tests:

- **The skip check may not go through `StarCatalog`'s prefix fallback.** `StarCatalog(dir, "g14")`
  resolves through `g14 → g16 → g17 → g18 → u16`, so a directory holding g17 answers yes to "do you
  have g14" and the download is skipped forever. `_has_cells` globs the exact prefix instead.
- **A zip member name is hostile data.** Only `*.290` and the acknowledgement are extracted, by
  basename, so `../../x` cannot escape and HNSKY's nested layout flattens. The acknowledgement ships
  *inside* the archive because separated from the data the attribution is lost on the first re-upload.

A checksum is never invented: an unpinned entry (`sha256=None`) downloads, warns, and prints the
digest it got, which is the step that turns a fresh upload into a pinned entry. Zip output is not
reproducible across tools, so re-zipping the same files needs the digest re-pinned.

**`fetch-catalog g05` is a tested alternative and not an upgrade, and the reason is a naming trap.**
ASTAP's suffix is *stars per square degree*, not a magnitude: g05 is 20.66M stars capped at ~500/sq°
locally. The existing decoder reads it with no changes (290 cells, record size 5, epoch 2025, BP
magnitudes, Gaia to 0.02" rms), so nothing in `catalog.py` needed touching — but it is **not a
superset of g14**. It reaches BP 18.7 at the galactic pole and only BP 13.4 in Cygnus, where it
*discards* half the stars g14 has, flattening the sky's real 23:1 density contrast to about 1:1.
Guide-star selection and plate solve success are exactly what that contrast drives, so g14 stays the
default; the table is in `README.md`. Every measured number in this file is g14's, and
`tests/test_catalog.py` hardcodes g14 expectations (`mag.max() == 14.0`, 11,290,236 stars, 7 boundary
strays), so switching the default would invalidate the fixtures rather than just change the pixels.

`StarCatalog.KNOWN` had to grow ASTAP's density tiers for g05 to be visible at all: with `catalog =
"g14"` configured, a directory of 290 readable g05 cells resolved to no prefix and the run used the
*synthetic* field — working software right up until a plate solve. The probe globs
`<prefix>_*.290`, so listing prefixes that ship in ASTAP's larger `.1476` format is harmless; both
halves are pinned by tests.

### Connecting a client

KStars/Ekos must use **Mode: Remote**, host `localhost`, port as configured, with no drivers
selected. Local mode with `dev@host:port` entries in the "Remote" field chains a second
`indiserver` in front of this one, opens one connection per entry, and broke a real setup. Device
names must match exactly (`AstroSkySim CCD`, not `AstroSkySim Camera`); a mismatch is logged as
a warning listing the real names.

`indi_getprop` / `indi_setprop` / `indiserver` from a KStars install
(`/Applications/KStars.app/Contents/MacOS/` on macOS) are the fastest way to check wire behaviour
without a GUI.

## Architecture

### One rig, many sessions

`Rig` (`rig.py`) owns **all** physical state — mount, focuser, rotator, filter, both cameras — as
plain dataclasses, plus `step(dt)` which advances the physics. Devices in `devices/` are thin
protocol adapters that read and write rig state; they hold no physics. The rejected alternative
is a device instance per connection, which forces the physical state into globals to keep the
copies consistent and turns every bookkeeping gap into two clients disagreeing about where the
telescope points. Here two clients provably see one telescope
(`test_two_clients_see_the_same_mount`).

Per-connection state lives in `ClientSession` (`indi/server.py`): subscriptions, which definitions
that client has been sent, BLOB policy, output queue.

**Reported vs actual pointing is the point of the simulator.** `rig.mount.ra_deg/dec_deg` is the
commanded position and is what the mount device reports; `rig.actual_pointing` adds polar
misalignment, periodic error, tracking noise and untracked drift, and is what `build_wcs` and
therefore the camera image. Guiding and plate-solve-and-centre loops live in that gap. Reporting
`actual_pointing` from the mount, or imaging `mount.ra_deg`, silently removes everything worth
testing.

### INDI layer

- `indi/xml_stream.py` — the stream is rootless: an endless sequence of top-level elements with no
  enclosing root, so a normal parser blocks forever. `XmlStreamSplitter` cuts it into complete
  elements; each is parsed alone. Depth may only be adjusted outside a quoted attribute value
  (attribute values legally contain `<`, `>`, `/`; a splitter that counts them unconditionally
  desynchronises on the first one and never recovers).
- `indi/protocol.py` — `Vector` and item types for the five INDI property types, `def_xml`/`set_xml`,
  `SwitchVector.apply` honouring the switch rule, and `parse_number` accepting sexagesimal.
- `indi/device.py` — `Device` base: `CONNECTION`, `DRIVER_INFO` (with the `DRIVER_INTERFACE`
  bitmask), `CONFIG_PROCESS`, plus `push` / `push_def` / `message` outbound and `handle_write`
  inbound.
- `indi/server.py` — asyncio server, sessions, routing, the coalescing output queue, and the tick.

Devices declare properties in `setup()` and register write handlers by property name via
`self.writer(name, fn)` — see the table in `Mount.setup`. A writable property with no handler is
accepted and echoed back `Ok` so the client's control doesn't hang in `Busy`.

`devices/pulse.py` — `GuidePulseMixin`, the `TELESCOPE_TIMED_GUIDE_NS`/`_WE` implementation shared
by `Mount` and `GuideCamera`. `GUIDER_INTERFACE` (bit 2) is INDI's **ST4 "I accept timed guide
pulses"** bit, not a "this is a guide camera" label; clients build their guide-pulse device list
from it. Setting the bit without the properties, or the properties without the bit, leaves a client
with no working pulse target and Ekos rejects calibration with "star drift is too short". Every
device that sets the bit must mix this in — `test_every_guider_interface_device_accepts_timed_pulses`
enforces it. Both implementations write to `rig.mount`, so the route the client picks is irrelevant.

### What a client sees

Four independent filters decide whether a message reaches a connection. All of them are load-bearing
and each has caused a "no images" or "device won't connect" report:

1. **Subscription.** `ClientSession.subscribed()` mirrors `indiserver`'s per-client filter: a bare
   `getProperties` sets `all_props`, otherwise `(device, property)` pairs accumulate, with an empty
   property meaning the whole device. A client receives only what it asked for. A client that never
   sends `getProperties` receives nothing.
2. **Definition before value.** A `set*Vector` for a property the client has no `def*Vector` for is
   unusable. `broadcast()` takes a lazy `def_xml` callable and emits the definition ahead of the
   value rather than dropping the update; `broadcast_def()` is the separate path for definitions and
   records them in `session.seen_defs`.
3. **Coalescing.** A `set*Vector` carries a property's *current* value, so a newer one supersedes a
   queued older one — `OutQueue` merges on `(device, property)`. Without this a bounded queue drops
   the `state="Ok"` ending a slew and the client hangs forever. Definitions, messages and BLOBs are
   discrete events and are never merged; pass `key=None` for those.
4. **BLOB policy.** A client that never sends `enableBLOB Also|Only` gets no frames. Correct INDI,
   and a classic cause of "connected but no images" (`test_blobs_are_withheld_until_enabled`).

### Image pipeline

`Rig.capture(cam)` is the whole path: build the WCS from `actual_pointing`, ask the source for
electrons, then Bayer attenuation → sky and shot noise → hot pixels → subframe → binning → ADU.
Frames are numpy arrays indexed **`[y, x]`** everywhere. Don't introduce an `[x, y]` buffer
anywhere in the path; the flip is invisible on a square sensor and silently transposes on any
other.

**The two cameras are different hardware.** `[sensor]` is the imaging chip; `[sensor_guide_cam]`
(optional, `cfg.sensor_guide_cam`) is the guider's. Nothing may read `cfg.sensor` for a frame — ask
`rig.sensor_cfg(cam)`, `rig.scale_arcsec_px(cam)`, `rig.sensor_model(cam)` or `rig.build_optics(cam)`,
all keyed on `cam is rig.guider`. `cfg.guide_sensor` falls back to `cfg.sensor` and
`telescope.guide_focal_length`/`guide_aperture` fall back to the main OTA, so an old config is
unchanged; the fallbacks are the only reason `cfg.sensor` and `cfg.guide_sensor` are ever the same
object. `Rig` has no `.sensor` attribute — a single `SensorModel` on the rig is exactly the bug this
replaced.

`rig.guide_hfd()` is the OAG model and is **not** `current_hfd()`: the pickoff prism is downstream of
the focuser (so the guide star does defocus with an autofocus run — don't "fix" that) but upstream of
the filter wheel, so it drops the per-filter focus offset that `current_hfd` adds. Focused for Ha,
the imaging chip is sharp and the guide star is 120 steps out, not the other way round.
`optics.guide_hfd_px` is the separate-guide-scope escape hatch — it pins the guide HFD outright, and
is unset by default.

`sources/` — `artificial` (rendered `.290` stars), `dss` (real survey cutout) and `composite`
(survey background plus rendered stars) behind the `ImageSource` Protocol in `sources/base.py`.
The contract is one line and non-negotiable: **a source returns electrons on the sensor's own pixel
grid**, shaped exactly `ctx.shape`. Sources reproject to fit; sensor geometry is immutable —
letting survey selection rewrite `CCD_INFO`/`CCD_MAX_X`/`CCD_MAX_Y` underneath a connected client
is the specific bug this rules out. `tests/test_sources.py` asserts this against 0.8", 1.7" and
5.0"/px surveys. Surveys are
fetched as FITS, never JPEG/GIF, so a real WCS arrives with the pixels.

`dss.py` has three back ends keyed by the `survey` prefix: `hips:` (CDS hips2fits, the default),
`skyview:` and `eso:`. Three hips2fits-specific traps, all guarded and all with tests:

- **CDS hosts may fail independently, so `HIPS_BASES` is a chain and not a preference.**
  `HipsEndpoint` walks the list and **pins the first host that answers for the process**;
  `sources/registry.py` builds *one*
  endpoint and hands it to every layer, because with a `DssSource` per filter a per-layer endpoint
  rediscovers the dead host once per filter. Two rules stop the failover being worse than none: a
  host with an alternate behind it gets `probe_timeout_s` (15 s) not `timeout_s` — safe because
  `urlopen`'s timeout is per socket operation, so only silence trips it and not a slow transfer —
  and a 4xx raises immediately instead of failing over, since an unknown HiPS id is the same 400 on
  every mirror and retrying it reports the last host's message instead of the real one. 408/429 are
  excluded from that: both are about *this* host right now.

- **Colour HiPS are not an image source.** `format=fits` on a `.../color` HiPS returns a
  `(4, H, W)` uint8 RGBA cube — the JPEG tiles in a FITS wrapper, bright cores clipped to 255.
  `_decode` rejects any cube with a non-degenerate leading axis. `(1, H, W)` is a wrapped 2-D
  plane and still reshapes away. No entry in `HIPS_ALIASES` may resolve to a colour HiPS.
- **NaN has two causes and the pixels can't distinguish them**: the sensor overhanging the survey
  footprint, and the survey masking its own saturated cores. `_check_coverage` refuses the frame
  below `min_coverage` (0.5) so `FallbackSource` serves the artificial sky. Zero-filling instead
  puts a black hole exactly where the target is — PanSTARRS over M42 is 0% usable at 20'.

hips2fits resamples onto the requested grid, so `_download_grid` sizes the request to the sensor's
plate scale rather than accepting whatever comes. The cutout is fetched north-up; `rotation_angle`
would make every rotator position its own download and its own cache entry.

`sources/filtered.py` — `FilterSurveySource` dispatches on `ctx.filter_name`, so
`[source.dss.per_filter.<name>]` gives each filter its own survey. `ctx.filter_name is None` means
the guide camera (prism upstream of the wheel) and always takes the default layer. A `per_filter`
key that names no filter is a `Config` validation error, not a silent fallback.

Two rules carry the physics, and both are easy to undo by accident:

- **`in_band` exempts a matched survey from `filter_transmission`.** `transmission` is a *broadband
  fraction* — correct for a star or the sky, whose light the filter discards, and wrong for an image
  already taken in that band. `DssSource.render` divides `ref_e_s` by `ctx.filter_transmission` when
  `in_band`, undoing the fold-in `build_optics` performed. Remove it and an Ha layer arrives 50x too
  faint, so narrowband loses to luminance and the whole mapping is pointless. Per-filter layers
  default `in_band=True`; the shared default layer defaults `False`, because it is a stand-in for
  whatever filter is in the beam rather than a match for it.
- **`ref_value` anchors absolutely; `ref_percentile` does not.** The percentile normalises every
  cutout against itself, which discards a survey's band ratios *and* its target-to-target contrast —
  an empty field renders as brightly as M42. The three NSNS line maps share one linear scale, so one
  `ref_value` across Ha/OIII/SII reproduces the real ratios unconfigured (measured at p99: IC 1805
  OIII/Ha 0.2, M27 2.2, blank Coma nothing). DSS2 is photographic and per-plate scaled, so it keeps
  the percentile. Reprojection preserves surface brightness, not flux, which is why one `ref_value`
  holds across plate scales; the *background* estimate is still per frame, so an object filling the
  sensor loses some of itself to the sky term.

`sky/` — `catalog.py` (`.290` reader and area indexing), `render.py` (PSF, defocus hyperbola, noise,
bayer, binning), `wcs.py` (sensor WCS plus closed-form LST and alt/az).

### Satellite trails

`satellites/` is `config.py` (the shared source list), `tle.py` (Celestrak download and element
parsing) and `trails.py` (SGP4, illumination, the swept PSF). Four decisions carry it:

- **The config is shared and found by search, not named by a rig config.** What is in orbit is a
  property of the machine and the week, so `--satellites` → `[satellites] config` →
  `./satellites.toml` → `~/.config/astroskysim/satellites.toml`, then built-in defaults identical
  to the shipped template (`default_config_text()` is *generated from* `DEFAULT_SOURCES`, so the
  file a user edits and the defaults they get without one cannot drift — a test parses it back).
  A rig config gets a pointer and an off switch only; there is no `enabled = true` there.
  **`tests/conftest.py` pins satellites off for every test**, because otherwise a machine that has
  run `fetch-satellites` renders trails into every end-to-end frame and CI does not.
- **A trail is not an `ImageSource`.** A source answers "what is on the sky in this direction" and
  reprojects a static scene; a trail is an integration over *when the shutter was open*, which is
  not in a `RenderContext`. So `Rig.add_satellite_trails` runs after `source.render` and before
  `apply_bayer` — also downstream of composite's `suppress_point_sources`, which would otherwise
  erase it. Light frames only (`frame_type == 0`), and the window is anchored on `cam.start_jd`,
  not on `rig.jd`: the readout runs in a thread and can start minutes after the shutter opened.
- **Brightness is flux per *dwell time*.** Sub-pixel points carry `rate x dt`, so the level along a
  trail is independent of how long the shutter stays open afterwards and a fast satellite lays down
  a fainter line than a slow one from the same magnitude. Multiplying the rate by the exposure
  instead is the hundred-fold error, and it looks entirely plausible in the frame
  (`test_the_trail_is_flux_per_dwell_time_not_flux_per_frame`).
- **The search cone is derived from the coarse step, not configured beside it.** Every object is
  propagated at `coarse_step_s` to find candidates; a LEO satellite covers up to 2.5 °/s, so the
  guard radius is `MAX_ANGULAR_RATE_DEG_S * coarse_step_s`. Configure them separately and a coarser
  search silently loses trails — a test renders the same field at 0.5 s and 30 s steps and demands
  the same sky. Measured cost: 0.6 s for a 300 s sub on a 3008² sensor against 12000 objects.

Traps the code guards, all with tests:

- **Celestrak answers "you already have this" with HTTP 403**, not 304, and only the body tells it
  apart from a rate limit. `NotModified` turns it into `status="fresh"` with the cached count;
  reported as a rate limit it sends the user off to wait out a throttle that does not exist, and
  `--force` cannot help because the refusal is server-side.
- **A 200 carrying "No GP data found" parses to zero satellites.** The response is parsed *before*
  it replaces the cached file, so a failure leaves last week's elements in place. Stale beats empty.
- **Element staleness is the *median* epoch, not the newest.** High orbits are routinely issued with
  epochs a day or two in the future, so `max()` sits ahead of now while 800 others are a fortnight
  old and the check never fires.
- **`get_sun` is geocentric GCRS.** Transforming it to FK5 *with its distance attached* routes
  through barycentric ICRS and puts the Sun 160° from where it is. `sun_teme_km` is the USNO
  low-precision series (0.01° against astropy, pinned like `fast_lst_deg`) and the test compares
  directions only.

`std_mag` is one number per source list, which is an ordering (ISS against Starlink) rather than
photometry: per-object standard magnitudes need a magnitude database no TLE carries. Positions are
TEME treated as equinox of date — a ~1.1″ difference, three orders of magnitude below the error a
TLE a few days old already carries.

### Wind and gusts

`wind.py` is `WindModel` (weather → deflection → history), and `Rig` consumes it in exactly two
places. Off by default: **every measured number in this file was taken against a still sky**, so
`wind.enabled = true` as a default would move all of them at once. Four decisions carry it:

- **Wind is the only error term resolved *within* an exposure.** Everything else in
  `actual_pointing` is read once per frame at readout, so it displaces a round star. Wind is
  recorded as a path at a fixed **128 Hz** sub-step into a ring buffer and convolved into the frame,
  which is what makes a streak, a smear or a V instead of a displaced disc. Sampling at the 10 Hz
  tick would alias a 4 Hz ring-down into a slow wobble, which is why the sub-step is fixed and not
  the tick.
- **`Rig.capture` takes the window once and hands the halves to different consumers.** `build_wcs`
  gets the window **mean**, the kernel gets the **zero-mean path** about that mean. Re-reading the
  wind for either one is the bug this shape exists to prevent: `capture` runs in the readout thread
  *after the shutter closed*, so an instantaneous read is a sample from outside the exposure
  entirely, and the frame translates by the difference — for a gust, the whole amplitude. The tick
  also keeps stepping the model during `capture`, so the two would not even read the same value.
  `pointing_at(wind_offset)` is the seam; `actual_pointing` is it with `None`.
- **`WindModel` owns its RNG, seeded `(cfg.seed, 0x77494E44)`.** `actual_pointing` draws
  `rig.rng.normal` on *every read*, so a wind path sharing that generator would depend on how many
  times unrelated code touched a property — reproducible in principle, not in practice. The distinct
  spawn key also stops wind and tracking noise sharing a phase every run.
- **The oscillator step is a closed-form 2×2 transition matrix**, like `fast_lst_deg` and for the
  same reason. Being exact for *any* dt is load-bearing, not elegance: it lets a stalled tick be
  caught up in one step rather than spinning through sub-steps, with wind time still exactly equal
  to `elapsed_s`. Dropping sub-steps instead would desynchronise the index→time mapping the window
  slice depends on. `[u, 0]` is a fixed point by construction, so DC gain is exactly 1 and a
  sustained wind settles precisely on `response_arcsec_at_20kmh · (v/20)²`.

Traps the code guards, all with tests:

- **A mirrored smear looks entirely correct.** `out(p) = Σ K(d)·scene(p−d)`; reverse the sign and the
  streak still has the right length and still lies along the wind. There are two kernel applications
  (shifted views under `SMEAR_TAP_LIMIT` taps, `_convolve_fft` above), so
  `test_the_two_smear_paths_agree` pins them against each other to 1e-9 on an *asymmetric* path — a
  symmetric one is its own mirror and proves nothing. The weight floor is applied **before**
  dispatch, or the branches disagree by the dropped weight and the test cannot check anything.
- **The kernel must be odd-sized.** `_convolve_fft` crops at `kernel.shape // 2`, so an even kernel
  injects a half-pixel translation — exactly the shift the zero-mean path prevents.
- **`damping ≥ 1` silently deletes the feature.** Critically damped means no overshoot, no ringing,
  no V-shapes, just a slow offset — hence `lt=1`. Likewise `probability = 1.0` divides by zero in
  the Markov off-rate, and `resonance_hz` is bounded in the *validator* rather than with a `Field`
  `le` so the error can say it is an aliasing limit rather than an arbitrary one.
- **`path_to_pixels` must not divide by `cos(dec)`.** `actual_pointing` does, to turn a great-circle
  offset into RA degrees; the CD matrix's first axis is already the projection plane's east
  coordinate. Doing it twice shrinks the smear away from the pole. It takes the **WCS**, never a
  plate scale, so a separate guide scope needs no `is_guider` branch — and it inverts
  `pixel_scale_matrix` rather than calling `wcs_world2pix`, because TAN is not affine and a path that
  is zero-mean in arcsec would only be *approximately* zero-mean in pixels.
- **The ring buffer is read across threads and must not take `capture_lock`.** That lock is held
  across a multi-second render, so taking it in the tick reintroduces the freeze `CameraBase.step`
  spawns a thread to avoid. `History.slice` snapshots the write count and copies its window instead.
- **An exposure longer than `history_s` warns.** A silently shortened window renders a shorter
  streak, which reads as "the wind died down" rather than as missing history — and the WCS mean must
  come from the *same* clamped window, or the frame translates.

Measured cost on a 3008²: the FFT pair is flat at 0.155 s (the frame dominates), a tap is ~7.6 ms,
so the crossover is ~16 taps and `SMEAR_TAP_LIMIT` is measured rather than guessed — at 75 taps the
tap branch is 0.573 s against 0.153 s. A guide frame is ~0.015 s; sub-pixel smears are skipped.

`devices/weather.py` is `WEATHER_INTERFACE` (**bit 7**, added to `indi/device.py`'s bitmask).
`server.weather` defaults **false**: a client's profile enumerates devices, so defaulting it on adds
a device to every existing Ekos profile. It is deliberately *not* derived from `wind.enabled` — two
sources of truth for one switch. `WEATHER_STATUS`'s vector *state* is what Ekos reads for "is it
safe", so a driver with readings and no status is decoration. It must not set `GUIDER_INTERFACE`, or
`test_every_guider_interface_device_accepts_timed_pulses` will demand pulse properties of a weather
station — the copy-paste-from-`mount.py` mistake.

### One photometric scale

Point sources go through `magnitude_to_electrons`, extended ones through
`surface_brightness_to_electrons` — same zero point, aperture, throughput and plate scale. The sky
(`rig.sky_e_s(cam)`, from `optics.sky_mag_arcsec2`) and the survey cutout are both extended sources,
so a telescope or pixel change moves stars, sky and nebulosity together. Nothing may reintroduce a
fixed electrons-per-second: `DssSource` had `scale_e_s = 400` behind a per-frame `[0, 1]` stretch,
which made a 1 s sub as bright as a 20 s one, zeroed the darkest 5% of every frame, and left the
telescope out of the survey path entirely. `calibrate_survey_image` now subtracts the survey's own
sky and normalises to `ref_percentile`; `render` scales that by
`surface_brightness_to_electrons(ref_mag_arcsec2, ctx.optics)`.

`optics.sky_background` is a raw e-/px/s override that still exists and is a unit trap —
`sky_background = 21.0` looks like an SQM reading and is ~SQM 17. `build_rig` warns when it is set
and logs the equivalent magnitude.

`filter_wheel.transmission` is folded into `Optics.throughput` inside `build_optics`, and only for
the imaging camera: the OAG prism is upstream of the wheel, the same reason `guide_hfd` drops the
focus offset. Because it rides on `throughput`, it attenuates stars, survey nebulosity and sky in
one place — don't apply it a second time downstream. The one exception is an `in_band` survey layer,
which divides it back out; see `sources/filtered.py` above for why.

### The tick, and why astropy is kept out of it

`IndiServer._tick` runs at `tick_hz` (default 10) calling `rig.step(dt)` then `device.step(dt)` for
each device. Exceptions are logged and never fatal. Constructing an astropy `Time` per access and
running `sidereal_time` or an `AltAz` transform on it costs ~300 ms per tick and starved the loop
badly enough that slews crawled, so `fast_lst_deg` and `fast_radec_to_altaz` are closed-form and
pinned against astropy in `tests/test_fast_astronomy.py`. The rig clock is a plain float JD
(`rig.jd`); `rig.now` builds an astropy `Time` for the rare one-off (FITS `DATE-OBS`). Don't
reintroduce astropy into anything reachable from `step()`.

Two rules follow from that, and breaking either one produced guiding that diverged rather than
anything that looked like a bug:

- **The tick steps by wall-clock time, not by its nominal period.** Whatever blocks the loop
  stretches the interval; stepping by `1/tick_hz` regardless silently deletes that time from the
  simulated clock. The client measures drift and guide-pulse response in real seconds, so a rig
  running at 40% of real time — and at a *varying* fraction, depending on what is rendering — makes
  every rate it derives wrong. One step is capped at `MAX_STEP_S` so recovering from a genuine stall
  cannot teleport a slew.
- **Nothing expensive may run inside the tick.** `rig.capture` is seconds of numpy and astropy
  (a survey reprojection is ~0.6 s on a guide chip, ~3 s on a 3008², measured), so `CameraBase.step`
  *spawns* the readout instead of awaiting it, and `_readout` runs in a thread via `asyncio.to_thread`.
  Awaiting it froze all six devices for the render: the mount stopped tracking, no property updates
  went out, and guide pulses sat unread in the socket for up to 3.3 s, so Ekos's corrections landed
  three frames late. numpy, scipy and astropy release the GIL, so the thread keeps loop latency in
  single-digit milliseconds. Both cameras' renders are serialised by `rig.capture_lock` because
  `rig.rng` is shared and not thread-safe — only the *loop* has to stay free, not the second camera.
  A camera reading out no longer stops the sky, which also means an abort can now arrive mid-readout;
  `_finish` checks `cam.aborted` before delivering the BLOB.
  `test_a_slow_readout_neither_stops_the_clock_nor_blocks_a_client` pins both rules.

Coordinates are **equinox of date** — the INDI property is literally `EQUATORIAL_EOD_COORD`, so
astropy reference implementations in tests use FK5 at the epoch of observation. Calling it ICRS
silently adds the full J2000-to-now precession, about 20 arcminutes today.

## Tests

`pytest-asyncio` in `asyncio_mode = "auto"`, so tests are plain `async def` with no decorator.
`tests/test_end_to_end.py` drives a real server over a real socket: `Harness` binds port 0 and runs
`server._tick(0.02)` itself instead of `serve_forever`, and `Client` is a minimal INDI client with
`pump(seconds)`, `until(predicate)` and `vectors(name)`.

Devices push continuously, so call `client.mark()` before sending the command under test — otherwise
`until` matches a periodic update that predates it.

## Gotchas

- **The mount's status is `EQUATORIAL_EOD_COORD`'s `state`, not `TELESCOPE_TRACK_STATE`.** Ekos maps
  that one attribute: Idle = parked/idle, Ok = tracking, Busy = slewing (parking when
  `TELESCOPE_PARK` is Busy too). `Mount._eq_state` derives it from the rig, mirroring
  `INDI::Telescope::NewRaDec`. Pushing `Ok` whenever the mount is not slewing is the natural-looking
  bug: it showed a parked mount as tracking and reverted the indicator a tick after **OFF** was
  pressed. `_push_track_state` reports the switch from `rig.mount.tracking` for the same reason —
  parking clears the flag that `slew_to` had just set.
- **`test_get_properties_returns_all_six_devices` asserts an exact set.** It still passes only
  because `server.weather` defaults off; a seventh device on by default breaks it, and the count is
  in the test's name. `test_driver_interface_bitmasks` is the opposite trap — it asserts per key, so
  a new device with a *wrong* bit would not fail it, which is why the weather bit is pinned
  deliberately in `test_the_weather_device_appears_and_reports_the_wind`.
- `server.device_prefix` in the config is **dead**. Device names are hardcoded class attributes
  (`Camera.device_name` etc.); renaming means editing those.
- `Vector.enabled=False` gates a property out of the announced set. Nothing currently flips it at
  runtime, and the late-appearing-property path exists only for that case
  (`test_a_property_that_appears_late_is_defined_before_its_value` forces it).
- `stop()` closes client sockets *before* awaiting `wait_closed()`. The reverse order deadlocks:
  each handler sits in `reader.read()` until its socket closes.
- An unterminated XML element blocks that one connection permanently — by definition the bytes after
  it are that element's content, so no resynchronisation exists. Other clients are unaffected, and
  a test pins that.
- `seed` in the config is a fixed RNG seed shared by pointing noise, sensor noise and hot pixels, so
  a run is reproducible; set it to `None` for OS entropy.
- Hot pixels are dark current: `add_hot_pixels` scales by exposure time via `sensor.hot_pixel_e_s`.
  Adding a fixed fraction of full well instead (the original) saturated all of them in a 0.5 s guide
  frame, so 20 immobile pixels outshone the brightest real star ~100x and a guider calibrated
  against fixed-pattern noise.
- `Rig.capture` stores the WCS it rendered on in `cam.last_wcs` and `_to_fits` reuses it. Calling
  `build_wcs` again there redraws the tracking noise, so the header would disagree with the pixels.
  `frame_wcs` then re-references it for the subframe origin and bin factor — the delivered array
  dimensions alone do not describe a binned subframe.
- `indi_setprop` opens a connection, waits for the definition, writes and closes; under rapid
  repeats it silently drops some writes. Real clients hold one connection and lose nothing (verified:
  8 x 120 ms pulses at 300 ms spacing all land). Don't diagnose a lost-command bug from it.
