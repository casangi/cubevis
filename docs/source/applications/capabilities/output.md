(cap-output)=

# Saving and describing plots

**Status:** available.

## What it is

- **PNG export** of what is shown, from the window, or from Python with
  no window at all.
- **A description of each plot.** For any plot, `visplot` can open a page
  listing everything that went into it:
  - the data set, and whether it was opened locally or on a remote
    kernel
  - the full selection
  - the axes and every setting that shaped the image
  - the color scaling, the displayed range and the zoom
  - the number of pending flags
  - the software versions
  - the `visplot(...)` call that reproduces the view, ready to copy

## What it is for

PNG export is for reports and logs, and for scripted batch plots. The
plot description is what to attach to a problem report: it makes a view
exactly reproducible, and it says which parts the reproducing call does
not cover (zoom, pending flags, some color settings, the theme).

## From Python

```python
visplot(vis="obs.ms", preset="vplot", plotfile="vplot.png")    # no window

visplot(vis="obs.ms", layout="grid", iterate_by="baseline",
        plotfile="bl_{page}.png", exprange="all")                # every page
```

With a grid, `exprange` chooses which pages are written: `"current"`
(the first page), `"all"`, or page numbers such as `"1-3,5"`.

---

Coming from plotms or AIPS? See {ref}`cap-from-plotms` and {ref}`cap-from-aips`.
