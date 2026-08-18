"""
palettes.py
===========
Named colormaps for raster and scatter panels, keyed by role and theme.

Why this exists
---------------
The palette is baked into the shaded image.  It is chosen before
``tf.shade()`` runs, lives in ``ColorBand.cmap``, and by the time
``png_export`` sees a ``RenderedPanel`` the pixels are already coloured.
So a palette is a **render-time** choice, not a chrome choice, and
``export_png(theme=...)`` cannot fix a mismatched one -- it would have to
re-shade, and it deliberately has no backend access.

The bug that motivated this
---------------------------
Scatter density layers looked washed out on a light background, in the
exported PNG *and* in the GUI's own Light mode.  The cause is alpha, not
hue: Datashader gives low-count pixels low alpha, so they blend toward
whatever is behind them.  A dark-to-bright ramp works on a dark ground
(sparse fades to black, dense is bright) and inverts badly on white
(sparse fades to white, and the bright end is *also* near-white, so the
whole panel collapses toward the background).

A light theme therefore needs ramps running **light to dark**: sparse
fades into the white background, dense goes dark.  ``_LIGHT_*`` entries
below are ordered that way, and ``check_luminance_ordering()`` asserts
it.

Rasters are much less affected -- a populated raster cell is opaque, so
only genuinely empty cells show the background -- which is why the light
GUI raster always looked acceptable while the scatter did not.  Light
raster defaults are still provided for consistency.

Scatter takes a *family*, not a single ramp
-------------------------------------------
``_LAYER_CMAPS`` is indexed by layer, so a two-polarisation scatter draws
two ramps that must stay distinguishable **from each other** as well as
from the background.  Scatter selection therefore names a set
(``scatter_cmap="polar"``) rather than one colormap.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/palettes.py``
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Raster colormaps — single sequential ramp per entry
# ---------------------------------------------------------------------------

_RASTER: dict[str, tuple[str, ...]] = {
    # Dark-ground defaults: low end is a saturated blue that stays legible
    # against a dark axes background.
    "plasma": (
        "#0d0887", "#46039f", "#7201a8", "#9c179e", "#bd3786",
        "#d8576b", "#ed7953", "#fb9f3a", "#fdcb26", "#f0f921",
    ),
    "viridis": (
        "#440154", "#482878", "#3e4989", "#31688e", "#26828e",
        "#1f9e89", "#35b779", "#6ece58", "#b5de2b", "#fde725",
    ),
    "inferno": (
        "#000004", "#1b0c41", "#4a0c6b", "#781c6d", "#a52c60",
        "#cf4446", "#ed6925", "#fb9b06", "#f7d13d", "#fcffa4",
    ),
    "cividis": (
        "#00224e", "#123570", "#3b496c", "#575d6d", "#707173",
        "#8a8678", "#a59c74", "#c3b369", "#e1cc55", "#fee838",
    ),
    # Light-ground: reversed, so sparse/empty regions fade into white and
    # dense regions go dark.
    "plasma_r": tuple(reversed((
        "#0d0887", "#46039f", "#7201a8", "#9c179e", "#bd3786",
        "#d8576b", "#ed7953", "#fb9f3a", "#fdcb26", "#f0f921",
    ))),
    "viridis_r": tuple(reversed((
        "#440154", "#482878", "#3e4989", "#31688e", "#26828e",
        "#1f9e89", "#35b779", "#6ece58", "#b5de2b", "#fde725",
    ))),
    "gray_r": (
        "#ffffff", "#e0e0e0", "#c0c0c0", "#a0a0a0", "#808080",
        "#606060", "#404040", "#282828", "#141414", "#000000",
    ),
}

# ---------------------------------------------------------------------------
# Scatter colormap families — one ramp per layer index
# ---------------------------------------------------------------------------

_SCATTER: dict[str, tuple[tuple[str, ...], ...]] = {
    # Dark ground: each layer runs near-black -> saturated -> pale, so a
    # sparse pixel fades into the dark background.
    "polar": (
        ("#08306b", "#2171b5", "#6baed6", "#c6dbef"),   # blues   (XX)
        ("#67000d", "#cb181d", "#fb6a4a", "#fcbba1"),   # reds    (YY)
        ("#00441b", "#238b45", "#74c476", "#c7e9c0"),   # greens  (XY)
        ("#3f007d", "#6a51a3", "#9e9ac8", "#dadaeb"),   # purples (YX)
    ),
    "warm": (
        ("#000000", "#7201a8", "#ed7953", "#fdcb26"),
        ("#000000", "#0b525b", "#1c7c7d", "#7ae7c7"),
        ("#000000", "#7f2704", "#e6550d", "#fdae6b"),
        ("#000000", "#3f007d", "#807dba", "#dadaeb"),
    ),
    # Light ground: reversed, so a sparse pixel fades into white.
    "polar_light": (
        ("#c6dbef", "#6baed6", "#2171b5", "#08306b"),
        ("#fcbba1", "#fb6a4a", "#cb181d", "#67000d"),
        ("#c7e9c0", "#74c476", "#238b45", "#00441b"),
        ("#dadaeb", "#9e9ac8", "#6a51a3", "#3f007d"),
    ),
    "gray_light": (
        ("#d9d9d9", "#969696", "#525252", "#000000"),
        ("#fcbba1", "#fb6a4a", "#cb181d", "#67000d"),
        ("#c7e9c0", "#74c476", "#238b45", "#00441b"),
        ("#dadaeb", "#9e9ac8", "#6a51a3", "#3f007d"),
    ),
}

# Theme defaults.  ``theme`` selects these; an explicit ``raster_cmap`` or
# ``scatter_cmap`` overrides, and once overridden the theme stops driving
# that role for the session (see VisibilityPlotter's sticky-override
# handling).
_DEFAULTS = {
    # Raster default is the SAME ramp in both themes.  A raster cell is
    # opaque, so it needs only to avoid the background, which
    # ``condition()`` handles by trimming whichever end is at risk -- the
    # near-black low end on dark, the near-white high end on light.
    # Reversal was a mistake here: it only ever mattered for
    # alpha-blended scatter, where the *sparse* end must be the one that
    # fades into the background, and it made light-mode rasters read
    # inside-out for no benefit.
    "dark":  {"raster": "plasma", "scatter": "polar"},
    "light": {"raster": "plasma", "scatter": "polar_light"},
}


# ---------------------------------------------------------------------------
# Background conditioning
# ---------------------------------------------------------------------------

BACKGROUNDS = {"dark": "#181825", "light": "#ffffff"}
"""Axes background per theme — must track ``png_export.THEMES[...].axes``."""

RASTER_MIN_DIST = 70.0
"""Minimum RGB distance (0-441) between a raster ramp colour and the background.

**Distance, not luminance.**  An earlier version used luminance alone and
trimmed plasma's low end from ``#0d0887`` to ``#7804a6`` -- discarding the
deep blue that gives the ramp its depth, in both the GUI and the export.

That trim was unnecessary.  ``#0d0887`` differs from the dark axes ground
``#181825`` by only 0.026 in luminance but by **100 RGB units**: they are
deep blue versus dark navy-grey, obviously different to the eye.  For an
*opaque* raster cell, distinguishability is what matters and hue counts
as much as brightness, so luminance-only was measuring the wrong thing.
"""

RASTER_MIN_GAP = 0.06
"""Deprecated luminance form of RASTER_MIN_DIST; retained for reference.

A raster cell is drawn at full alpha, so it only needs to be
distinguishable from the background, not to survive blending.  Keeping
this small preserves most of the authored ramp: plasma trimmed at 0.06
starts at ``#7804a6`` and keeps its purple, where 0.16 would cut to
``#a21d9a`` and discard the whole deep-blue third.
"""

SCATTER_MIN_DIST = 150.0
"""Minimum RGB distance for scatter ramps — alpha-blended, so it needs more.

A sparse scatter pixel is drawn at Datashader's ``min_alpha`` (40/255 =
0.157), which scales its distance from the background *down* by that
factor: 150 units of ramp separation shows as ~24 on screen.  That is the
floor, not a comfortable margin, and it is why scatter needs roughly
double the raster threshold for a worse result.

Conditioning cannot lift this on its own -- ``tf.shade(min_alpha=...)``
is the complementary lever and the next thing to try if sparse data still
disappears.
"""

SCATTER_MIN_GAP = 0.16
"""Deprecated luminance form of SCATTER_MIN_DIST; retained for reference.

A sparse scatter pixel is drawn at low alpha, which scales its contrast
against the background *down*: a ramp end 0.16 from the background shows
only ~0.03 of separation at 20% alpha.  That is the floor, not a
comfortable margin, and it is why scatter needs roughly triple the raster
headroom for the same visible result.

Conditioning cannot fix this on its own -- ``tf.shade(min_alpha=...)``
raises the sparse end's alpha and is the complementary lever.  Datashader
defaults to ``min_alpha=40`` (of 255); raising it is the next thing to
try if the sparse end still disappears.
"""

MIN_GAP = SCATTER_MIN_GAP
"""Back-compat default for :func:`condition`; prefer the per-role values.

Both observed failures are this one constraint violated at opposite ends:

* **Light burnout** — a bright ramp end against white, so dense pixels
  wash out.
* **Dark vanish** — a near-black ramp end against the dark axes ground,
  so sparse pixels disappear.  Measured before conditioning, *every*
  dark-theme ramp started within 0.1 of the background, and
  ``polar[1]`` started at 0.09 against a background of 0.098.

It also matters for opaque rasters, not just alpha-blended scatter: a
plasma-minimum cell (0.07) against the dark ground (0.098) is
indistinguishable from an empty cell, so the lowest data value and "no
data" look the same.
"""


def _rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _hex(rgb) -> str:
    return "#" + "".join(f"{int(round(c * 255)):02x}" for c in rgb)


def _dist(a, b) -> float:
    """Euclidean RGB distance in 0-255 space (max 441).

    Used instead of a luminance difference because two colours of similar
    brightness but different hue are perfectly distinguishable, and a
    luminance-only rule discards them -- see ``RASTER_MIN_DIST``.
    """
    return (sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5) * 255.0


def _lum_rgb(rgb) -> float:
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def _sample(cmap, t: float):
    """Linear interpolation along *cmap* at position *t* in [0, 1]."""
    cols = [_rgb(c) for c in cmap]
    if len(cols) == 1:
        return cols[0]
    x = t * (len(cols) - 1)
    i = min(int(x), len(cols) - 2)
    f = x - i
    return tuple(cols[i][k] * (1 - f) + cols[i + 1][k] * f for k in range(3))


def condition(cmap, theme: str, n: Optional[int] = None,
              min_gap: float = SCATTER_MIN_DIST) -> tuple[str, ...]:
    """Trim *cmap* so no colour sits within *min_gap* of the background.

    Rather than hand-tuning every hex list per theme, the canonical ramps
    stay as authored and are re-sampled here over the longest contiguous
    stretch that clears the background.  That keeps one definition per
    ramp, adapts automatically when a background changes, and works for a
    user-supplied colormap the registry has never seen.

    Falls back to the untrimmed ramp if no stretch qualifies -- a ramp
    that is entirely too close to the background is a bad ramp, but
    silently returning nothing would be worse.
    """
    bg = _rgb(BACKGROUNDS.get(theme, BACKGROUNDS["dark"]))
    res = 256
    ok = [_dist(_sample(cmap, i / (res - 1)), bg) >= min_gap
          for i in range(res)]

    best = run = None
    for i, good in enumerate(ok + [False]):
        if good and run is None:
            run = i
        elif not good and run is not None:
            if best is None or (i - run) > (best[1] - best[0]):
                best = (run, i)
            run = None
    if best is None:
        return tuple(cmap)

    lo, hi = best[0] / (res - 1), (best[1] - 1) / (res - 1)
    n = len(cmap) if n is None else n
    if n == 1 or hi <= lo:
        return (_hex(_sample(cmap, (lo + hi) / 2.0)),)
    return tuple(_hex(_sample(cmap, lo + (hi - lo) * k / (n - 1)))
                 for k in range(n))


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def raster_names() -> list[str]:
    """Selectable raster colormap names, for the sidebar control."""
    return sorted(_RASTER)


def scatter_names() -> list[str]:
    """Selectable scatter colormap family names, for the sidebar control."""
    return sorted(_SCATTER)


def default_for(role: str, theme: str) -> str:
    """Default palette name for *role* (``"raster"``/``"scatter"``)."""
    return _DEFAULTS.get(theme, _DEFAULTS["dark"])[role]


def raster_cmap(name: Optional[str] = None, theme: str = "dark"
                ) -> tuple[str, ...]:
    """Ramp for a raster panel.  ``None`` takes the theme default.

    Unknown names fall back to the theme default rather than raising: a
    palette name is cosmetic, and failing a whole render over one is a
    worse outcome than drawing it in the default colours.
    """
    if name is None:
        name = default_for("raster", theme)
    cmap = _RASTER.get(name) or _RASTER[default_for("raster", theme)]
    return condition(cmap, theme, min_gap=RASTER_MIN_DIST)


def scatter_cmaps(name: Optional[str] = None, theme: str = "dark"
                  ) -> tuple[tuple[str, ...], ...]:
    """Ramp *family* for a scatter panel, indexed by layer.

    Callers index this modulo its length, as ``_LAYER_CMAPS`` was indexed
    before, so a scatter with more layers than the family has entries
    still works.
    """
    if name is None:
        name = default_for("scatter", theme)
    fam = _SCATTER.get(name) or _SCATTER[default_for("scatter", theme)]
    return tuple(condition(cm, theme, min_gap=SCATTER_MIN_DIST)
                 for cm in fam)


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

def _luminance(hex_colour: str) -> float:
    """Relative luminance in [0, 1] (Rec. 709 coefficients)."""
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def check_background_contrast() -> list[str]:
    """Return complaints about ramps that fight their theme background.

    Supersedes an earlier ``check_luminance_ordering``, which only
    asserted ramp *direction*.  Direction was too weak: it caught light
    burnout (a bright end on white) but not the mirror defect, a
    near-black end on the dark ground, where every dark-theme ramp was
    within 0.1 of the background and ``polar[1]`` was within 0.008.

    Checks what actually matters -- that after conditioning, every ramp
    colour clears the background by its role's margin, and that the ramp
    still runs the right way for its theme so the *sparse* end is the one
    that fades out.

    This defect is invisible in a swatch: the colours look fine on their
    own and only the composited image shows the problem, so it needs a
    programmatic check rather than review.
    """
    bad: list[str] = []

    def audit(label, cmap, theme, gap):
        bg = _rgb(BACKGROUNDS[theme])
        cond = condition(cmap, theme, min_gap=gap)
        worst = min(_dist(_rgb(c), bg) for c in cond)
        # Tolerance covers 8-bit quantisation: condition() re-samples in
        # float and _hex() rounds to a byte, shifting distance by ~1 unit.
        if worst < gap - 1.5:
            bad.append(f"{label}: closest colour is {worst:.1f} RGB units "
                       f"from the {theme} background, want >= {gap:.0f}")
        if label.startswith("raster"):
            # Direction is free for an opaque raster: nothing fades into
            # the background, so either orientation is legible once the
            # ramp clears it.
            return
        lo, hi = _lum_rgb(_rgb(cond[0])), _lum_rgb(_rgb(cond[-1]))
        if theme == "light" and lo <= hi:
            bad.append(f"{label}: light ramp must run light->dark so the "
                       f"sparse end fades into white")
        if theme == "dark" and lo >= hi:
            bad.append(f"{label}: dark ramp must run dark->light so the "
                       f"sparse end fades into black")

    # Raster ramps are offered in both themes -- conditioning adapts
    # them -- so audit every ramp against both backgrounds.
    for name, cmap in _RASTER.items():
        for theme in ("dark", "light"):
            audit(f"raster {name!r}", cmap, theme, RASTER_MIN_DIST)
    for fam, cmaps in _SCATTER.items():
        theme = "light" if fam.endswith("_light") else "dark"
        for i, cmap in enumerate(cmaps):
            audit(f"scatter {fam!r}[{i}]", cmap, theme, SCATTER_MIN_DIST)
    return bad
