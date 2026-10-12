"""plot_summary.py
================
"Describe this plot" (HRS A3, 2026-10-11): one page with what a panel
shows and how it was made -- the data set, the selection in full, the
axes and every setting that shaped the image, the displayed range, the
pending flags, the software versions, and the ``visplot(...)`` call that
reproduces the view.  It is what a tester attaches to a problem report.

Built in Python, on request, from the panel's *render state* -- what was
drawn by the last Plot, not values waiting in a configuration tab
(``visplot_export_handoff.md`` 2.6).  It runs in the process that holds
the plotter: with a remote kernel that is still the local plotter, which
knows the remote endpoint and reports it; only the finished page crosses
to the browser, the same way the pending-flag report does.

Entry points: ``describe(plotter, slot_id, viewport)`` -> dict (sections
of ``(label, value)`` rows, plus ``call``), ``summary_html(d)`` -> a
self-contained page, ``reproducing_call(plotter)`` -> the call as text.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/plot_summary.py``
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import os
import platform
import subprocess
from typing import Any, Optional

# ----------------------------------------------------------------------
# Software versions
# ----------------------------------------------------------------------


def _git_commit(path: str) -> Optional[str]:
    """Short commit (with ``+dirty``) of the checkout *path* is in, or None."""
    try:
        root = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=5)
        if root.returncode != 0:
            return None
        h = subprocess.run(["git", "-C", path, "rev-parse", "--short=10", "HEAD"],
                           capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "-C", path, "status", "--porcelain",
                                "--untracked-files=no"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return h + ("+dirty" if dirty else "") if h else None
    except Exception:
        return None


def software_versions() -> list[tuple[str, str]]:
    """``[(package, version), ...]`` -- cubevis first, with the git commit
    when it runs from a checkout (the installed version string of an
    editable install is only as new as its last ``pip install``)."""
    from importlib import metadata
    out = []
    try:
        ver = metadata.version("cubevis")
    except Exception:
        ver = "unknown"
    here = os.path.dirname(os.path.abspath(__file__))
    commit = _git_commit(here)
    out.append(("cubevis", ver + (f"  (git {commit})" if commit else "")))
    for pkg in ("bokeh", "numpy", "xarray", "xarray-ms", "xradio", "datashader",
                "dask", "arcae"):
        try:
            out.append((pkg, metadata.version(pkg)))
        except Exception:
            continue
    out.append(("python", platform.python_version()))
    out.append(("platform", platform.platform(terse=True)))
    return out


# ----------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------


def _utc(t) -> str:
    try:
        d = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(seconds=float(t))
        return d.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except Exception:
        return str(t)


def _list(v, n=12) -> str:
    if v is None:
        return "all"
    v = list(v)
    if not v:
        return "none"
    s = ", ".join(str(x) for x in v[:n])
    return s + (f" … ({len(v)} in all)" if len(v) > n else "")


def _axis_name(a) -> str:
    return getattr(a, "label", None) or getattr(a, "name", None) or str(a)


def _coord_text(value, is_time: bool, unit: str = "") -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if is_time:
        return _utc(v) + " UTC"
    from .tick_format import si_scale
    s, u = si_scale(v, unit)
    return f"{s:.6g}" + (f" {u}" if u else "")


# ----------------------------------------------------------------------
# The reproducing call
# ----------------------------------------------------------------------


def _slot_settings(plotter, slot) -> dict:
    """The panels=[...] dict that reproduces *slot* as last drawn."""
    d: dict[str, Any] = {"kind": slot.kind}
    if slot.kind == "raster":
        r = slot.raster
        d.update(raster_y=r._y_dim.name, raster_x=r._x_dim.name,
                 raster_qty=r._quantity.name, averaging=r.averaging,
                 baseline_order=r.baseline_order,
                 baseline_combine=r.baseline_combine, detrend=bool(r.detrend),
                 stat_time_window=r.stat_time_window,
                 stat_chan_window=r.stat_chan_window)
        sc = getattr(r, "_scaling", None)
        if sc:
            d["scaling"] = sc
        for k, attr in (("vmin", "_scaling_vmin"), ("vmax", "_scaling_vmax")):
            v = getattr(r, attr, None)
            if v is not None:
                d[k] = float(v)
    else:
        s = slot.scatter
        d.update(scatter_x=s._x_dim.name,
                 scatter_y=(s.layers[0].y_axis.name if s.layers
                            else plotter._scatter_y.name),
                 scatter_avg_time=s._avg_time or "off",
                 scatter_avg_chan=s._avg_chan or "off",
                 averaging=s._averaging or plotter._averaging)
        if s._detrend is not None or s._stat_time_window is not None:
            d.update(detrend=bool(plotter._detrend if s._detrend is None else s._detrend),
                     stat_time_window=(plotter._stat_time_window
                                       if s._stat_time_window is None
                                       else s._stat_time_window),
                     stat_chan_window=(plotter._stat_chan_window
                                       if s._stat_chan_window is None
                                       else s._stat_chan_window))
    return d


def _antenna_text(plotter) -> str:
    try:
        names, baselines = plotter._antenna_baseline_selection()
    except Exception:
        return ""
    if baselines:
        return ",".join(f"{a}&{b}" for a, b in baselines)
    if names:
        return ",".join(names)
    return ""


def reproducing_call(plotter) -> str:
    """``visplot(...)`` text that reproduces both panels as last drawn."""
    args: list[tuple[str, Any]] = []
    if getattr(plotter, "_ms_path", None):
        args.append(("ms", plotter._ms_path))
    else:
        args.append(("ps", getattr(plotter, "_ps_path", None)))
    if getattr(plotter, "_field_str", ""):
        args.append(("field", plotter._field_str))
    spw_ids = getattr(plotter, "_spw_ids", None)
    if spw_ids:
        args.append(("spw", ",".join(str(s) for s in spw_ids)))
    elif getattr(plotter, "_spw_str", ""):
        args.append(("spw", plotter._spw_str))
    if getattr(plotter, "_corr_str", ""):
        args.append(("correlation", plotter._corr_str))
    ant = _antenna_text(plotter)
    if ant:
        args.append(("antenna", ant))
    for name, attr in (("scan", "_scan_str"), ("timerange", "_timerange_str"),
                       ("uvrange", "_uvrange_str")):
        v = (getattr(plotter, attr, "") or "").strip()
        if v:
            args.append((name, v))
    if getattr(plotter, "_autocorr", "exclude") != "exclude":
        args.append(("autocorr", plotter._autocorr))
    args.append(("datacolumn", getattr(plotter, "_datacolumn", "DATA").lower()))
    args.append(("layout", getattr(plotter, "_layout", "side")))
    if getattr(plotter, "_time_labels", "utc") != "utc":
        args.append(("time_labels", plotter._time_labels))
    ctx = getattr(plotter, "_context", None)
    if ctx is not None and type(ctx).__name__.startswith("Remote"):
        args.append(("backend", "remote"))
    order = list(getattr(plotter, "_slot_display_order", [0, 1]))
    slots = [plotter._slots[i] for i in order]
    panels = [_slot_settings(plotter, s) for s in slots]

    def lit(v):
        return repr(v)
    lines = ["visplot("]
    for k, v in args:
        lines.append(f"    {k}={lit(v)},")
    lines.append("    panels=[")
    for p in panels:
        # Wrapped at ~78 columns, continuation lines under the first key.
        items = [f"{k}={lit(v)}" for k, v in p.items()]
        cur, rows = "        dict(", []
        for i, it in enumerate(items):
            piece = it + ("," if i < len(items) - 1 else "),")
            if len(cur) + len(piece) + 1 > 78 and not cur.endswith("("):
                rows.append(cur.rstrip())
                cur = " " * 13
            cur += piece + " "
        rows.append(cur.rstrip())
        lines.extend(rows)
    lines.append("    ],")
    lines.append(")")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# The description
# ----------------------------------------------------------------------


def _data_name(plotter) -> str:
    """The data set's file name (``tl016b_sub.ms``)."""
    p = (getattr(plotter, "_source_path", None) or "").rstrip("/\\")
    return os.path.basename(p) or "visplot"


def describe(plotter, slot_id: str, viewport=None) -> dict:
    """Everything the summary page shows for panel *slot_id*.

    *viewport* is ``[x0, x1, y0, y1]`` as the browser shows it (the
    panel's current pan / zoom), or ``None``.
    """
    slot = next(s for s in plotter._slots if s.id == slot_id)
    panel = slot.raster if slot.kind == "raster" else slot.scatter
    try:
        spec = panel._panel_spec()
    except Exception:
        spec = None
    sel_hist = (plotter._last_raster_selection_by_slot if slot.kind == "raster"
                else plotter._last_scatter_selection_by_slot)
    sel = sel_hist.get(slot.id) or plotter._selection
    meta = plotter._meta
    sections: list[tuple[str, list]] = []

    # --- data set -------------------------------------------------------
    ctx = getattr(plotter, "_context", None)
    ctx_name = type(ctx).__name__ if ctx is not None else "local"
    rows = [
        ("Path", os.path.abspath(plotter._source_path) if plotter._source_path else "—"),
        ("Format", "MSv2 (measurement set)" if getattr(plotter, "_ms_path", None)
         else "MSv4 (processing set)"),
        ("Data column", getattr(sel, "data_column", plotter._datacolumn)),
        ("Execution", "remote kernel" if ctx_name.startswith("Remote")
         else "local"),
    ]
    if ctx_name.startswith("Remote"):
        for attr, label in (("_endpoint", "Remote endpoint"),
                            ("_kernel_name", "Kernel")):
            v = getattr(ctx, attr, None)
            if v:
                rows.append((label, str(v)))
    try:
        t0, t1 = meta.time_range
        rows.append(("Observation", f"{_utc(t0)} – {_utc(t1)} UTC"))
    except Exception:
        pass
    try:
        rows.append(("Contents", f"{len(meta.fields)} fields, {len(meta.spws)} "
                                 f"spectral windows, {len(meta.antennas)} antennas, "
                                 f"{len(meta.scans)} scans"))
    except Exception:
        pass
    sections.append(("Data set", rows))

    # --- selection ----------------------------------------------------
    spw_names = {}
    try:
        spw_names = {s.spw_id: (s.name or str(s.spw_id)) for s in meta.spws}
    except Exception:
        pass
    spws = getattr(sel, "spw", None)
    rows = [
        ("Field", _list(getattr(sel, "field_names", None))),
        ("Spectral windows", "all" if spws is None
         else _list([str(i) if str(spw_names.get(i, i)) == str(i)
                     else f"{i} ({spw_names[i]})" for i in spws])),
        ("Correlations", _list(getattr(sel, "correlation", None))),
        ("Scans", _list(getattr(sel, "scan", None), 30)),
    ]
    tr = getattr(sel, "time_range", None)
    rows.append(("Time range", "all" if tr is None
                 else f"{_utc(tr[0])} – {_utc(tr[1])} UTC"))
    rows.append(("Autocorrelations", getattr(sel, "autocorr", None) or "include"))
    uv = getattr(sel, "uv_range", None)
    from .selection_text import uv_range_text
    rows.append(("UV range", uv_range_text(uv) or "all"))
    if getattr(sel, "baselines", None):
        rows.append(("Baselines", _list([f"{a}&{b}" for a, b in sel.baselines], 20)))
    elif getattr(sel, "antenna_names", None):
        rows.append(("Antennas", _list(sel.antenna_names, 30)
                     + "  (their baselines to every antenna)"))
    else:
        rows.append(("Antennas / baselines", "all"))
    for label, attr in (("Scan box", "_scan_str"), ("Time range box", "_timerange_str"),
                        ("UV range box", "_uvrange_str")):
        v = (getattr(plotter, attr, "") or "").strip()
        if v:
            rows.append((label, v))
    sections.append(("Selection", rows))

    # --- the plot ---------------------------------------------------------
    rows = [("Panel", f"{slot.id} ({slot.kind})"),
            ("Title", getattr(spec, "title", "") if spec else "—")]
    if slot.kind == "raster":
        r = slot.raster
        rows += [
            ("Y axis", _axis_name(r._y_dim)), ("X axis", _axis_name(r._x_dim)),
            ("Quantity", _axis_name(r._quantity)),
            ("Correlation shown", getattr(r, "_polarization", "—")),
            ("Averaging (within a baseline)", r.averaging),
            ("Baselines combined", r.baseline_combine),
            ("Baseline order", r.baseline_order),
            ("Phase slope removed", "yes" if r.detrend else "no"),
            ("Statistic time window", str(r.stat_time_window)),
            ("Statistic channel window", str(r.stat_chan_window)),
            ("Colour map", getattr(plotter, "_raster_cmap_name", "—")),
            ("Colour scaling", str(getattr(r, "_scaling", "—"))),
            ("Colour range", "automatic" if (r._scaling_vmin is None and r._scaling_vmax is None)
             else f"{r._scaling_vmin} – {r._scaling_vmax}"),
            ("Colour mode", getattr(r, "_color_mode", "—")),
        ]
        agg = getattr(r, "_agg", None)
        if agg is not None:
            rows.append(("Image cells", " × ".join(str(n) for n in agg.shape)
                         + ("  (decimated: each cell aggregates several samples "
                            "along an axis)" if getattr(r, "_is_decimated", False)
                            else "")))
    else:
        s = slot.scatter
        rows += [
            ("X axis", _axis_name(s._x_dim)),
            ("Layers", _list([f"{_axis_name(l.y_axis)} {l.polarization}"
                              for l in s.layers])),
            ("Average over time", s._avg_time or "off"),
            ("Average over channels", s._avg_chan or "off"),
            ("Averaging (vector / scalar)", s._averaging or plotter._averaging),
            ("Phase slope removed", "as the plotter" if s._detrend is None
             else ("yes" if s._detrend else "no")),
            ("Colour map", getattr(plotter, "_scatter_cmap_name", "—")),
        ]
    if spec is not None:
        rows.append(("Y extent (data)",
                     f"{_coord_text(spec.y_range[0], spec.y_is_time, spec.y_unit)} – "
                     f"{_coord_text(spec.y_range[1], spec.y_is_time, spec.y_unit)}"))
        rows.append(("X extent (data)",
                     f"{_coord_text(spec.x_range[0], spec.x_is_time, spec.x_unit)} – "
                     f"{_coord_text(spec.x_range[1], spec.x_is_time, spec.x_unit)}"))
        if viewport and len(viewport) == 4 and None not in viewport:
            x0, x1, y0, y1 = viewport
            rows.append(("Shown (zoom / pan)",
                         f"X {_coord_text(x0, spec.x_is_time, spec.x_unit)} – "
                         f"{_coord_text(x1, spec.x_is_time, spec.x_unit)};  "
                         f"Y {_coord_text(y0, spec.y_is_time, spec.y_unit)} – "
                         f"{_coord_text(y1, spec.y_is_time, spec.y_unit)}"))
        if spec.status != "ok":
            rows.append(("Status", f"{spec.status}: {spec.note or ''}"))
    rows.append(("Time labels", getattr(plotter, "_time_labels", "utc")))
    rows.append(("Theme", getattr(plotter, "_theme", "dark")))
    sections.append(("Plot", rows))

    # --- flags ----------------------------------------------------------------
    rows = []
    try:
        rows.append(("Pending (not written) flag operations",
                     str(plotter._flag_db.pending_count)))
    except Exception:
        pass
    if rows:
        sections.append(("Flags", rows))

    sections.append(("Software", software_versions()))

    not_in_call = [
        "the zoom / pan shown (see Plot, above)",
        "pending flags (export them as JSON from the Flagging section)",
        "scatter layer colour scaling and colourize settings",
        "the theme and panel sizes",
    ]
    return {
        # The browser tab shows only the start of the title, so the data
        # set's name leads; "visplot" is in the page heading instead.
        "title": f"{_data_name(plotter)} · panel {slot.id}",
        "heading": f"visplot — {_data_name(plotter)}, panel {slot.id} ({slot.kind})",
        "generated": _utc(_dt.datetime.now(_dt.timezone.utc).timestamp()) + " UTC",
        "sections": sections,
        "call": reproducing_call(plotter),
        "not_in_call": not_in_call,
    }


def summary_html(d: dict) -> str:
    """A self-contained page for *d* (from ``describe``): plain tables that
    paste cleanly into a report, and a Copy button for the call."""
    e = _html.escape
    body = [f"<h2>{e(d.get('heading', d['title']))}</h2>",
            f"<p class='gen'>Generated {e(d['generated'])}</p>"]
    for title, rows in d["sections"]:
        body.append(f"<h3>{e(title)}</h3><table>")
        for k, v in rows:
            body.append(f"<tr><td class='k'>{e(str(k))}</td><td>{e(str(v))}</td></tr>")
        body.append("</table>")
    body.append("<h3>Reproduce this view</h3>"
                "<button id='copy' onclick='cvCopy()'>Copy</button> "
                "<span id='copied'></span>"
                f"<pre id='call'>{e(d['call'])}</pre>"
                "<p class='note'>Not reproduced by the call: "
                + e("; ".join(d["not_in_call"])) + ".</p>")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{e(d['title'])}</title>
<style>
  body {{ background:#ffffff; color:#1e1e2e; font-family: -apple-system,
          Helvetica, Arial, sans-serif; margin: 16px 24px; max-width: 1100px; }}
  h2 {{ border-bottom: 1px solid #ccd0da; padding-bottom: 6px; }}
  h3 {{ color:#1e66f5; margin: 22px 0 6px 0; }}
  table {{ border-collapse: collapse; }}
  td {{ padding: 3px 14px 3px 0; vertical-align: top; font-size: 13px; }}
  td.k {{ color:#5c5f77; white-space: nowrap; }}
  pre {{ background:#eff1f5; padding: 10px 12px; font-size: 13px;
         overflow-x: auto; white-space: pre; }}
  .gen, .note {{ color:#6c6f85; font-size: 12px; }}
  button {{ font-size: 13px; padding: 3px 12px; }}
</style>
<script>
function cvCopy() {{
  const t = document.getElementById('call').innerText;
  const done = () => {{ document.getElementById('copied').textContent = 'copied'; }};
  if (navigator.clipboard && window.isSecureContext) {{
    navigator.clipboard.writeText(t).then(done, fallback);
  }} else {{ fallback(); }}
  function fallback() {{
    const r = document.createRange(); r.selectNodeContents(document.getElementById('call'));
    const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
    try {{ document.execCommand('copy'); done(); }} catch (e) {{}}
  }}
}}
</script></head>
<body>{''.join(body)}</body></html>"""
