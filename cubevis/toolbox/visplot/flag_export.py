"""flag_export.py
=================
Export of pending flags: JSON Lines (``to_jsonl``).

JSON Lines is the one export format: a header line (source, data format,
spectral-window table, data column, cubevis version, time) followed by one
line per operation, in application order, with full provenance.  It is the
complete and exact record of what visplot will apply, and can be loaded
back as pending flags or used to build any other tooling.

History: CASA ``flagdata`` command lists / scripts were generated here
until 2026-09-30.  They were removed: written through CASA's selection
language, a 52,624-sample operation on TW Hya came out 2,689 samples short
(caught by the after-write verification), so an exported command list
could silently differ from what the display showed.  Flags are written
with the exact arcae (MSv2) / zarr (MSv4) paths in ``flag_commit``.
"""

from __future__ import annotations

import json
from typing import Iterable, Optional

from .flag_model import FlagDelta


def to_jsonl(deltas: Iterable[FlagDelta], header: Optional[dict] = None) -> str:
    from .flag_db import JSONL_FORMAT, JSONL_VERSION
    lines = [json.dumps({"format": JSONL_FORMAT, "version": JSONL_VERSION, **(header or {})})]
    lines += [json.dumps(d.to_dict(json_safe=True)) for d in deltas]
    return "\n".join(lines) + "\n"
