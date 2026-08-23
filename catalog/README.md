# Star catalogue directory

The `.290` star database goes here. It is not in the repository — 56 MB of binary
cells that never change do not belong in git history.

```bash
uv run astroskysim fetch-catalog
```

That downloads, checksums and unpacks the g14 set (GAIA eDR3 to BP 14.0, epoch
2025, 290 cells, 11,290,236 stars) into this directory, and does nothing if the
cells are already present. Everything here except this file is gitignored.

Without it the simulator falls back to a synthetic star field and warns at
startup: fine for focus, guiding and framing, useless for plate solving.

## Publishing the mirror

g14 is retired upstream, so the archive is a release asset on this repository.
To rebuild and republish it from a populated `catalog/`:

```bash
cd catalog
zip -r -9 /tmp/g14_star_database_mag14.zip *.290 "acknowledgement of databases.txt"
shasum -a 256 /tmp/g14_star_database_mag14.zip     # pin this in src/astroskysim/sky/fetch.py

gh release create catalog-g14-v1 /tmp/g14_star_database_mag14.zip \
  --prerelease \
  --title "g14 star database (mirror)" \
  --notes "GAIA eDR3 to BP 14.0, epoch 2025. Mirror of the retired HNSKY g14 set,
Han Kleijn's acknowledgement of databases.txt included."
```

`--prerelease` is deliberate: a plain release becomes GitHub's "latest", so a
data asset would be what anyone looking for the software lands on. Nothing
resolves the download through `/releases/latest/`; the URL in `sky/fetch.py` is
pinned to the tag.

Zip output is not reproducible across tools, so re-zipping with anything other
than the line above changes the digest and needs `sha256=` in `sky/fetch.py`
updated to match.
