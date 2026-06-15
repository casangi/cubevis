"""conftest.py — pytest configuration for msvis xarray-ms tests."""

import os
import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--ms",
        action="store",
        default=None,
        help="Path to MSv2 Measurement Set for testing",
    )


def pytest_configure(config):
    ms = config.getoption("--ms", default=None)
    if ms is not None:
        os.environ["MS"] = ms


def pytest_collection_modifyitems(config, items):
    ms = os.environ.get("MS", "sis14_twhya_calibrated_flagged.ms")
    if not os.path.isdir(ms):
        skip_real = pytest.mark.skip(
            reason=f"Real MS not found at {ms!r}; tests will use simulated data"
        )
        # Don't skip — tests fall back to simulated data automatically.
        # This hook is here for future use (e.g. marking slow tests).
        _ = skip_real
