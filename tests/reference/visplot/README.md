# `visplot` Reference Testing

This is a collection of reference tests comparing `visplot`
(`cubevis.toolbox.visplot`) against CASA's established visualization
tools — `plotms` for scatter-style plots, `msview` for raster-style
plots — run against a shared test measurement set
(`sis14_twhya_calibrated_flagged.ms`, ALMA Band 7 observations of TW
Hya). The goal: confirm `visplot` produces the same results as the
tools it's meant to eventually stand alongside, and catch real bugs
along the way rather than just eyeballing "does this look plausible."

This testing effort — including the tests themselves, the
investigations behind them, and the bug fixes that came out of
them — was carried out with the help of Claude (Anthropic).

## Summary

Six reference tests were scoped; four ran to a clear result, one was
closed without running once a fundamental tooling difference was
discovered while setting it up, and one is postponed pending a GUI
capability `visplot` doesn't have yet. Along the way, this effort
found and fixed several real, independently-verified bugs: a
color-scaling bug affecting `linear`/`log` scaling in global color
mode, a field-selection bug where numeric `field=` values resolved to
the wrong field entirely, a raster rendering bug where multiple data
partitions were combined out of chronological order, and a hover-probe
bug that miscounted data samples near pixel edges. It also surfaced a
real architectural difference between `visplot` and `msview` — one
averages over non-displayed axes, the other animates through them —
that limits how directly the two can be compared for certain views.

Methodology and full investigation details, including everything that
didn't fit in an individual test's own document, live in
[`visplot-reference-testing-sources.md`](visplot-reference-testing-sources.md).

## Tests

| Test | Kind | Summary | Status |
|---|---|---|---|
| [01 — UV coverage](reference-test-01-uv-coverage.md) | Scatter | u vs. v coverage shape/symmetry; found and fixed a missing axis capability along the way | PASS |
| [02 — Amp vs. UVdist, Ceres](reference-test-02-amp-vs-uvdist-ceres.md) | Scatter | Resolved-source amplitude falloff; led to a real, independently-confirmed field-selection bug fix | PASS |
| [03 — Amp vs. UVdist, J1037-295](reference-test-03-amp-vs-uvdist-j1037.md) | Scatter | Point-source contrast case against test 02, run alongside it for a physical-shape comparison | PASS |
| [04 — Time vs. Baseline raster](reference-test-04-time-vs-baseline.md) | Raster | Found and fixed a real partition-ordering bug; surfaced the animate-vs-average tooling difference | Bug fixed; not a strict tool-parity PASS |
| [05 — Time vs. Channel raster](reference-test-05-time-vs-channel.md) | Raster | Postponed — needs single-frame/iteration display support `visplot` doesn't have yet | Postponed |
| [06 — Real and Imaginary](reference-test-06-real-imaginary.md) | Scatter | Confirmed `Real² + Imaginary² = Amplitude²` against a known raw value; found a hover-probe bug (fixed) and two open findings along the way | Core check confirmed; two findings open |

## Where to look for what

- **Just want the bottom line on each test?** The table above, or each
  test's own `Result` section.
- **Want the reasoning, the code investigations, or the bug-fix
  details?** Each test document has that inline where it's specific to
  that test; findings that apply more broadly (methodology decisions,
  audits, bugs found in shared code) live in the numbered sections of
  [`visplot-reference-testing-sources.md`](visplot-reference-testing-sources.md).
- **Want to reproduce a test yourself?** Every test's `Commands`
  section has exact, runnable `plotms`/`msview`/`visplot` calls.
