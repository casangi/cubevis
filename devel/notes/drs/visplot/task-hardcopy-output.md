# The name

`hardcopy` — the tempting alternative is `plotfile`, since that's CASA's term, but
plotms's `plotfile` is a _filename string_ . Anyone with that habit will type
`plotfile='amp.png'` and get a type error from muscle memory. `hardcopy` carries no
such expectation, and it correctly signals "a bundle of output state" rather
than "a path".

One concession worth making: accept a **bare string** as shorthand for `{'file': ...}`.
It costs one line and rescues the most likely mistake, `hardcopy='amp.png'`.

# What it contains

```python
hardcopy = {
    # --- output ---------------------------------------------------
    'file':      'amp_vs_uvdist.png',   # required
    'dpi':       100,
    'overwrite': False,
    'theme':     'light',               # palettes AND chrome, per §8.19

    # --- geometry -------------------------------------------------
    'layout':    'side',                # 'one' | 'side' | 'over'
    'width':     1400,                  # per-panel canvas, in pixels
    'height':    700,

    # --- series ---------------------------------------------------
    'iteraxis':   'spw',                # spw|field|antenna|scan|corr
    'itervalues': None,                 # None = every value present
    'gridrows':   1,                    # pages of N iterations
    'gridcols':   1,
}
```

`width` / `height` **are the Datashader canvas**, not just display scale — they
set aggregation resolution. Worth saying so in the docstring, since a user raising
them for a publication figure gets genuinely more detail, not a bigger version of
the same image.

# Four decisions to make explicitly

**Iteration nests inside selection, it doesn't replace it**. `spw='0,1,2'` with
`iteraxis='spw'` writes three files; `iteraxis='spw'` alone writes one per window
in the MS. One selection language, plotms semantics.

**Unknown keys raise**. Same argument as the selection overrides — a silently
ignored `dpi` produces a file that looks right and isn't. This is the single biggest
advantage of a dict over scalars, and it's forfeited if the dict is read leniently.

# The filename question

For a series, `file` needs a placeholder: `'amp_{iter}.png'` → `amp_spw0.png`. I'd
support `{iter}` explicitly and, when it's absent from a series, insert `_<value>`
before the extension rather than erroring — but log it, so the naming is visible
rather than surprising.

Worth deciding now rather than later: whether `{iter}` gets the raw value or a
sanitised one. Spectral windows are identified by _name_ on your store, and
`ALMA_RB_07#BB_2#SW-01#FULL_RES` contains `#` — legal in a filename but awkward
in a shell. I'd sanitise to `[A-Za-z0-9._-]` and say so.
