"""Shared fixtures.

The one thing here is load-bearing: **the tests must not read the developer's
own satellite configuration.** ``build_rig`` resolves the shared satellite
config by searching ``./satellites.toml`` and ``~/.config/astroskysim``, and its
element cache lives in ``~/.cache``. Both are deliberately outside any rig
config — that is the point of the feature — but it means a machine that has run
``fetch-satellites`` would render trails into every end-to-end frame while CI
renders none, and the difference would surface as a flaky photometry assertion
rather than as anything to do with satellites.

So every test runs with satellites off unless it asks for them, and
``test_satellites.py`` drives the real code paths directly with its own element
files under ``tmp_path``.
"""

from __future__ import annotations

import pytest

from astroskysim.satellites import config as satconfig


@pytest.fixture(autouse=True)
def no_ambient_satellites(monkeypatch):
    """Pin satellites off for every test that has not built its own sky."""
    monkeypatch.setattr(satconfig, "SEARCH_PATH", ())
    monkeypatch.setattr(
        satconfig,
        "load_satellites_config",
        lambda *a, **k: satconfig.SatellitesConfig(enabled=False, sources=[]),
    )
