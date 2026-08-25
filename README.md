# AstroSkySim

A headless INDI server that pretends to be a complete astrophotography rig: mount, imaging
camera, guide camera, focuser, rotator, filter wheel and an optional weather station. Point
KStars/Ekos, CCDciel or any other INDI client at it and run a whole imaging session —
slew, plate solve, autofocus, guide, sequence — without hardware and without a clear night.
Something to play with when the weather is bad.

**INDI only.** No GUI, no desktop application, no Alpaca or ASCOM layer. It serves INDI on
port 7625 and that is all it does.

AstroSkySim follows in the footsteps of Han Kleijn's
[Sky Simulator for Ascom and Alpaca](https://sourceforge.net/projects/sky-simulator), which
established the idea of a synthetic sky driven by a simulated mount and focuser. This is an
independent Python implementation of that idea for INDI — see
[Acknowledgements](#acknowledgements) and [Licence](#licence).

> Design decisions, measured numbers and known limitations live in
> **[TECHNICAL.md](TECHNICAL.md)**. Read that before changing how a device behaves.

## What it simulates

- **A real sky.** Stars from a local Gaia-derived catalogue, real survey imagery fetched at
  runtime, or both composited — so frames plate solve against the actual sky.
- **A mount that points *almost* where it says it does.** Polar misalignment, periodic
  error, tracking noise and drift live in the gap between the commanded position and the
  real one, which is what gives guiding and plate-solve-and-centre something to correct.
- **A focuser with a personality.** Backlash, a physical focus curve, per-filter focus
  offsets and optional thermal drift, so a client's autofocus routine is genuinely tested.
- **Two different cameras.** The guide camera has its own chip, its own plate scale and its
  own focus behaviour — off-axis guider by default, or a separate guide scope if you say so.
- **One photometric scale.** Aperture, plate scale, throughput, filter and exposure time all
  bite, on stars, sky and nebulosity together. A 1 s sub really does look like a 1 s sub.
- **Satellite trails** from real orbital elements, **wind and wind gusts** including the streaked
  stars they leave mid-exposure, and **a night's temperature drop** with the focus drift it
  causes. All three are optional and off by default.
- **FITS frames with a real WCS** and ground-truth header cards — `NSATS`, `WINDKMH`,
  `SMEARPX`, `AMBTEMP`, `OPTTEMP`, `FOCDRIFT` — so a rejection stack or a trail detector can
  check itself against what actually happened.

## Devices

Seven devices, all sharing one simulated rig. The property set is deliberately wide: a
device that answers only its headline property lets a client exercise almost nothing.

| Device | INDI name | Highlights |
|---|---|---|
| Mount | `AstroSkySim Telescope` | `EQUATORIAL_EOD_COORD`, `TELESCOPE_MOTION_NS`/`_WE`, `TELESCOPE_SLEW_RATE`, `TELESCOPE_TRACK_RATE`, `TELESCOPE_PARK`/`_POSITION`/`_OPTION`, `TELESCOPE_HOME`, `TELESCOPE_PIER_SIDE`, `HORIZONTAL_COORD`, `TIME_LST`, ST4 guide pulses |
| Imaging camera | `AstroSkySim CCD` | `CCD_EXPOSURE`, `CCD_FRAME` (subframe), `CCD_BINNING`, `CCD_GAIN`/`CCD_OFFSET`, `CCD_FRAME_TYPE`, `CCD_COOLER`/`CCD_COOLER_POWER`, `CCD_READOUT_MODE`, `CCD_STOP_EXPOSURE` (graceful stop that keeps the frame, unlike abort) |
| Guide camera | `AstroSkySim Guider` | The same camera interface on its own sensor, plus ST4 guide pulses |
| Focuser | `AstroSkySim Focuser` | `FOCUS_ABSOLUTE_POSITION`, `FOCUS_RELATIVE_POSITION`, `FOCUS_BACKLASH_*`, `FOCUS_TEMPERATURE`, `FOCUS_TEMPERATURE_COMPENSATION`, `FOCUS_REVERSE_MOTION`, `FOCUS_SYNC` |
| Rotator | `AstroSkySim Rotator` | `ABS_ROTATOR_ANGLE`, `REL_ROTATOR_ANGLE`, `ROTATOR_MECHANICAL_ANGLE`, `ROTATOR_REVERSE` |
| Filter wheel | `AstroSkySim Filter Wheel` | `FILTER_SLOT`, `FILTER_NAME`, `FILTER_FOCUS_OFFSET` |
| Weather | `AstroSkySim Weather` | `WEATHER_PARAMETERS` (wind, gust, temperature), `WEATHER_STATUS`, per-parameter `MIN_OK`/`MAX_OK` thresholds, `WEATHER_UPDATE`. **Off by default** — set `server.weather = true` |

A few things worth knowing:

- The **guide camera is separate hardware**, not a copy of the imaging chip, and
  `TELESCOPE_INFO`'s `GUIDER_FOCAL_LENGTH`/`GUIDER_APERTURE` describe the guide train.
- Both the mount and the guide camera accept `TELESCOPE_TIMED_GUIDE_NS`/`_WE`, so guiding
  works whichever device your client picks.
- `TELESCOPE_OFFSET_RATES` is our own extension — INDI has no standard property for RA/Dec
  offset rates.
- Not implemented, by choice: dome, switch, safety monitor.

Why each property is there, and what the guide camera's focus does: see
[TECHNICAL.md](TECHNICAL.md#the-guide-camera-is-separate-hardware).

## Install

You need Python 3.10 or newer and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this repo> && cd astroskysim
uv sync --all-extras --group dev
```

Two extras are optional, so the core simulator has no network dependencies at all:

| Extra | Brings | Without it |
|---|---|---|
| `dss` | `reproject`, `astroquery` — survey cutouts for `dss` and `composite` modes | The artificial sky still works; the survey path logs that the extra is missing |
| `satellites` | `sgp4` — orbital propagation for trails | The simulator runs and draws no trails |

## Quick start

```bash
uv run astroskysim fetch-catalog        # ~56 MB Gaia-derived star database, once
uv run astroskysim fetch-satellites     # orbital elements, occasionally (optional)
uv run astroskysim -c examples/sim.toml -v
```

Then point [KStars/Ekos](https://kstars.kde.org/),
[CCDciel](https://sourceforge.net/projects/ccdciel/) or any INDI client at
`localhost:7625`.

`fetch-catalog` is idempotent — it verifies a pinned SHA-256, unpacks the cells and does
nothing at all if they are already there, so it is safe in a setup script.

## Connecting Ekos

**In KStars/Ekos, use Mode: Remote.** Host `localhost`, port `7625`, and **no drivers
selected**. Local mode with `dev@host:port` entries in the "Remote" field chains a second
`indiserver` in front of this one and opens a connection per entry — that breaks real setups.

### Ekos Profile
![Ekos Profile](/screenshots/ekos-config.jpg?raw=true)

### Ekos Optical Train definition
![Ekos Optical Train Primary](/screenshots/ekos-optical-train-primary.jpg?raw=true)
![Ekos Optical Train Primary](/screenshots/ekos-optical-train-secondary.jpg?raw=true)

### Ekos Scope definition
![Ekos Optical Train Primary](/screenshots/ekos-scope-definition.jpg?raw=true)

Three things that look like bugs and are not:

1. **Device names must match exactly.** `AstroSkySim CCD`, not `AstroSkySim Camera`. A
   mismatch is logged as a warning that lists the real names.
2. **No images until the client enables BLOBs.** A client that never sends
   `enableBLOB Also|Only` receives no frames. That is correct INDI, and the classic cause of
   "connected but nothing arrives".
3. **A client that never sends `getProperties` sees nothing**, for the same reason — the
   server mirrors `indiserver`'s per-client subscription filter.

To poke the server without a GUI, `indi_getprop` and `indi_setprop` from any KStars install
(`/Applications/KStars.app/Contents/MacOS/` on macOS) are the fastest route.

## Configuring it

Everything lives in one TOML file, passed with `-c`. Start from `examples/sim.toml` and
copy or edit it — every section is commented.

### Command line

| Flag | Does |
|---|---|
| `-c`, `--config FILE` | The TOML config to run |
| `-p`, `--port N` | Override the listen port |
| `--host ADDR` | Override the listen address |
| `-m`, `--mode MODE` | `artificial`, `dss` or `composite` |
| `--survey NAME` | Survey for `dss`/`composite`, e.g. `hips:dss2r` |
| `--catalog-dir DIR` | Where the `.290` star database lives |
| `--satellites FILE` | The shared satellite config |
| `-v`, `-vv` | Info and debug logging |

Flags override the TOML; everything else is config-only. Two subcommands download data:
`fetch-catalog` and `fetch-satellites` (`--list` shows what is enabled and how old the
elements are).

### Config sections

| Section | Sets |
|---|---|
| `[server]` | Host, port, tick rate, and which devices to advertise |
| `[site]` | Latitude, longitude, elevation |
| `[telescope]` | Focal length and aperture, plus optional separate guide-scope values |
| `[sensor]` | The imaging chip: size, pixel pitch, well depth, read noise, Bayer pattern, hot pixels |
| `[sensor_guide_cam]` | The guider's own chip. Takes every `[sensor]` key and inherits none of them |
| `[optics]` | Seeing, sky brightness (as an SQM reading), throughput, zero point |
| `[focuser]` | Travel, perfect-focus position, focus range, backlash, step size, temperature coefficient |
| `[rotator]` | Speed and mechanical offset |
| `[filter_wheel]` | Filter names, per-filter focus offsets and transmissions, change time |
| `[mount]` | Slew and guide rates, periodic error, tracking noise, polar misalignment |
| `[wind]` | Wind, gusts and mid-exposure smear. Off by default |
| `[temperature]` | The night's cooling curve and the focus drift it causes. Off by default |
| `[satellites]` | A pointer to the shared satellite config, and an off switch |
| `[source]`, `[source.artificial]`, `[source.dss]`, `[source.composite]` | Where pixels come from |
| `seed` (top level) | Fixed RNG seed, so a run is reproducible. `None` seeds from the OS |

### Shipped examples

| File | Rig |
|---|---|
| `examples/sim.toml` | The documented reference config — 90 mm f/4.8 apo, IMX533, IMX678 off-axis guider, composite sky, LRGB + Ha/OIII/SII |
| `examples/satellites.toml` | The shared satellite source list |

Run with `-v` the first time. Startup logs the derived plate scales, the sky brightness in
electrons, the focus drift a night produces and the HFD that results — which is where you
check that a config does what you meant it to.

## The sky

Pick where pixels come from with `source.mode`:

| Mode | Pixels come from | Good for |
|---|---|---|
| `artificial` | Stars rendered from the local `.290` catalogue | Focus, guiding, framing, plate solving. No nebulosity |
| `dss` | A real survey cutout, reprojected onto your sensor | Real targets that look like the real thing |
| `composite` | Survey background **plus** rendered stars | Real nebulosity *and* stars at catalogue-known positions — usable as ground truth |

Surveys come from CDS hips2fits (default), SkyView or the ESO archive, always as FITS so a
real WCS arrives with the pixels. Cutouts are cached, and if a survey has no coverage where
you are pointing the simulator falls back to the artificial sky rather than serving a black
hole.

The star catalogue is fetched separately because it is too big for the repository:

```bash
uv run astroskysim fetch-catalog        # -> ./catalog
```

**Without a catalogue the simulator still runs**, on a synthetic star field, and warns once
at startup. That is fine for focus, guiding and framing — but the stars are not at real
positions, so **plate solving against it will fail**.

Why the default catalogue is g14, why it is a pinned mirror, and how the three survey back
ends differ: [TECHNICAL.md](TECHNICAL.md#the-star-catalogue).

## Optional extras

All three are off by default, because every measured number in the test suite was taken
against a still, warm, satellite-free sky.

### Satellite trails

Real orbital elements, propagated with SGP4, drawn as trails in light frames only. An ISS
pass saturates and a Starlink pass does not, from the same photometry, and a trail through
a defocused frame is a defocused trail. The count that reached the sensor lands in the FITS
header as `NSATS`, so a rejection stack can grade itself.

```bash
uv run astroskysim fetch-satellites     # download the elements
uv run astroskysim fetch-satellites -l  # what is enabled, and how old it is
```

The satellite config is **shared between rigs** — what is in orbit is a property of the
machine and the week, not of a telescope — so it lives in its own file, found by search.
A rig config only points at it and can switch it off. Details:
[TECHNICAL.md](TECHNICAL.md#satellite-trails).

### Wind and gusts

Wind pushes the tube and the mount deflects. The guide star jumps, so a client's guider
fights it — and because the shift happens *while the shutter is open*, stars come out as
streaks, smears and V-shapes rather than displaced discs. Set `wind.enabled = true`, and
`server.weather = true` if you want a client to be able to read the wind.

The header carries `WINDKMH`, `GUSTKMH` and `SMEARPX`. Details:
[TECHNICAL.md](TECHNICAL.md#wind-and-gusts).

### Temperature and focus drift

A night cools 5–15 K, the tube changes length and focus goes stale. Three temperatures are
tracked — ambient, the focuser's probe and the optics — and only the optics set focus, so a
client that calibrates its temperature compensation against the probe still drifts, exactly
as on a real rig. Set `temperature.enabled = true` (and `server.weather = true` to report
it).

The header carries `AMBTEMP`, `OPTTEMP` and `FOCDRIFT`. Details:
[TECHNICAL.md](TECHNICAL.md#temperature-and-focus-drift).

## Layout

```
README.md          this file
TECHNICAL.md       design decisions, measured numbers, known limitations
examples/          reference config, three real rigs, satellite source list
tests/             pytest suite; test_end_to_end.py drives a real socket
src/astroskysim/
  indi/      xml_stream.py  rootless-stream splitter (+ element parser)
             protocol.py    Text/Number/Switch/Light/BLOB vectors, sexagesimal
             device.py      Device base, DRIVER_INTERFACE bitmask
             server.py      asyncio server, sessions, coalescing queue, tick
  sky/       catalog.py     .290 reader, area indexing, synthetic fallback
             render.py      PSF, defocus hyperbola, noise, bayer, binning
             wcs.py         sensor WCS, closed-form LST/altaz, precession
  satellites/ config.py     the shared source list, found by search
             tle.py         Celestrak groups, download, element parsing
             trails.py      SGP4, illumination, the swept PSF
  sources/   artificial | dss | composite, behind one interface
  rig.py     all physical state and the simulation step
  wind.py    gust weather, the mount's ring-down, the deflection history
  temperature.py  the night's cooling, the optics lag, the focus drift
  devices/   mount, camera, focuser, rotator, filterwheel, weather
             pulse.py       TELESCOPE_TIMED_GUIDE_*, shared by every ST4 device
  cli.py
```

## Contributing

Contributions are welcome — bug reports, new devices, better physics, or just a config for
a rig that is not represented yet.

```bash
uv sync --all-extras --group dev
uv run pytest -q                  # ~1 min, no network needed
uv run ruff check src tests
```

Both must pass before a pull request. `ruff` is the only static check configured — there is
no mypy, so don't read a `# type: ignore` as evidence that a type was checked.

A few house rules that will save you time:

- **Read the matching section of [TECHNICAL.md](TECHNICAL.md) before changing a device's
  behaviour.** Most of the choices that look odd are load-bearing, and nearly all of them
  are pinned by a test that explains itself in its name.
- **Prefer a test in `tests/test_end_to_end.py`** for anything a client can observe. It
  drives a real server over a real socket, which is where protocol mistakes actually show
  up.
- **Physics belongs in `Rig`**, not in a device. Devices in `devices/` are thin adapters
  that read and write rig state.
- New INDI properties should exist on real hardware, or be documented as an extension the
  way `TELESCOPE_OFFSET_RATES` is.

Contributions are accepted under GPLv3-or-later, like the rest of the project.

## Acknowledgements

**Han Kleijn's [Sky Simulator for Ascom and Alpaca](https://sourceforge.net/projects/sky-simulator)**
(hnsky.org) is the origin of the idea this project builds on: a simulated rig whose camera
returns a synthetic sky rendered from the mount's own pointing and blurred by the focuser's
own position, so an imaging client can be driven end to end with no hardware and no clear
night. AstroSkySim is an independent Python implementation of that concept for INDI. The
desktop application, the ASCOM and Alpaca layers, and the sky map GUI are not part of it.

**Star data.** `artificial` and `composite` modes read the HNSKY `.290` star databases, an
extract of ESA's Gaia catalogue in the compact format developed for the HNSKY and ASTAP
programs. Those files are not produced here — see
`catalog/acknowledgement of databases.txt` for the full provenance and terms of every
catalogue involved.

> This work has made use of data from the European Space Agency (ESA) mission
> Gaia (https://www.cosmos.esa.int/gaia), processed by the Gaia Data Processing
> and Analysis Consortium (DPAC,
> https://www.cosmos.esa.int/web/gaia/dpac/consortium). Funding for the DPAC has
> been provided by national institutions, in particular the institutions
> participating in the Gaia Multilateral Agreement.

**Survey imagery.** `dss` and `composite` modes fetch cutouts at runtime from
[CDS hips2fits](https://alasky.cds.unistra.fr/hips-image-services/hips2fits)
(Centre de Données astronomiques de Strasbourg), NASA/GSFC SkyView, or the ESO archive.
Each service sets its own terms and its own acknowledgement requirements for the surveys it
serves; if you publish anything derived from a fetched image, credit the survey, not this
simulator.

## Licence

AstroSkySim is free software under the **GNU General Public License, version 3 or (at your
option) any later version** — see [LICENSE](LICENSE). It comes with no warranty; see
sections 15 and 16.

GPLv3-or-later is chosen deliberately rather than permissively. Sky Simulator for Ascom and
Alpaca carries GPLv3-or-later headers on its source units, and this project was written
with knowledge of how that program works. Nothing here is translated from it, but a licence
that is compatible with it either way removes the question rather than arguing it.

The catalogue and survey data described under [Acknowledgements](#acknowledgements) are
**not** covered by this licence. They carry their own terms, some of which are more
restrictive than the GPL.
