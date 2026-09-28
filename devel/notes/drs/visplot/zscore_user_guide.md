# Z-Score in visplot: finding unusual data at a glance

The Z-Score views help you spot data that behaves differently from the rest of the
same baseline: interference spikes, a bad antenna, a stretch of time where something
went wrong. They are a screening aid: they show you where to look, and you decide
what the data actually is.

## In one minute

- Click the **Z-Score** button in the toolbar. You get two panels: a raster of
  **Baseline vs Time** colored by Z-Score, and an **Amplitude vs Time** scatter.
- In the raster, **blue means ordinary, yellow means unusual**. In the scatter,
  the same colors mean the same thing.
- A yellow **dot** is an isolated odd sample. A yellow **horizontal stripe** is one
  baseline that is odd for a long time (often an antenna problem). A yellow
  **vertical band** is a time when many baselines are odd at once.
- Then confirm what it is: step through antennas with the **Antenna** Prev/Next
  buttons, or look at the scatter, before deciding to flag anything.

## What a Z-Score is

For each **baseline** (and polarization) visplot works out the baseline's *typical*
visibility: the median of the real parts and the median of the imaginary parts over
everything in your current selection. Each sample is then scored by how far it lies
from that typical value, compared with how far samples *usually* lie from it.

The score is scaled so it reads like a familiar "number of sigmas" for noise:

- A Z-Score of about **1** is an ordinary sample.
- A Z-Score of **3.5 or more** is unusual: for pure noise this happens to only about
  **0.2 %** of samples (2 in 1000).
- A Z-Score of **10** is a very strong outlier.

Important properties:

- **Every baseline is compared with itself.** A baseline that is simply brighter or
  fainter than the others is *not* flagged just for that. Only samples that stand
  out from their own baseline's behavior are.
- **Flagged data is ignored.** Samples you (or the pipeline) already flagged do not
  count. After flagging more data, replot to update the scores.
- A baseline with too few samples cannot be scored and shows no color.

(It is a robust score based on medians, not the textbook mean-and-standard-deviation
z-score, so a few bad samples do not distort the reference they are judged against.)

## Where to find it

| What | Where |
|---|---|
| Everything together | **Z-Score** preset button (toolbar) |
| Just the raster | **Raster quantity** dropdown, choose **Z-Score** |
| Z-Score as a plotted value | Scatter **Y axis**, choose **Z-Score** |
| Color a scatter by Z-Score | Scatter layer **Colorize** selector, choose **Statistical** |
| Change the cutoff | **Color scaling** (gear tab): scaling **threshold**, edit the **min** box |

## Reading the raster

Each raster cell shows the **highest** Z-Score among the samples folded into that
cell. For a Baseline vs Time raster that means the highest score across all channels
at that time. The maximum (not the average) is used on purpose: one bad channel
among hundreds would vanish in an average.

**The cutoff adjusts itself.** The more samples a cell covers, the more likely at
least one of them exceeds any fixed number by chance. With 384 channels, a plain 3.5
cutoff would light up more than half of the cells even for perfectly clean data. So
the raster raises its cutoff to keep the chance of a false alarm per cell about the
same as the 0.2 % per sample: roughly **4.9 for 384 channels**, and exactly 3.5 for a
single sample. You do not need to do anything; this is why the raster is mostly
blue with sparse yellow rather than a wash of color.

Colors: cells below the cutoff are deep blue; cells above are bright yellow.

## Reading the scatter

Colored by **Statistical**, each pixel is colored by the Z-Score of the samples in it,
using the same blue and yellow, with the same cutoff (3.5 per sample). Unusual samples
are drawn opaque and on top; ordinary ones are drawn fainter. Where the two
polarizations overlap, unusual pixels stay visible.

Two things to keep in mind:

- In an **Amplitude vs Time** plot, the color is partly redundant with the height on
  the plot (a sample far from its baseline's typical amplitude is both high and
  yellow). The scatter is best used to **confirm** what the raster found, and to see
  what the flagged samples actually look like.
- Each pixel shows the average score of its samples, so a few unusual samples inside a
  crowded pixel can be diluted. The raster is the more sensitive view.

## Checking one antenna

Use the **Antenna** box with the **Prev/Next** buttons to step through antennas one at
a time. When exactly one antenna is selected and a Z-Score view is showing, a line
under the colorbar summarizes it, for example:

`DA41: N=4800  median=1.18  2.34% > 3.5`

- **N**: how many samples were scored.
- **median**: the middle Z-Score. For clean, noise-like data it is about **1.18**; a
  clearly larger value means the antenna is unusual across the board.
- **% > 3.5**: the fraction of samples above the cutoff. For clean data expect about
  **0.2 %**; several percent deserves a look.

## Adjusting the cutoff

- In the **Color scaling** controls, the **min** box is the cutoff when scaling is
  **threshold**. Type a value to override the automatic one; the reset button returns
  to automatic.
- Lower cutoff = more sensitive but more false alarms; higher = fewer, but faint
  problems disappear.
- Your color scaling settings are **remembered for each quantity**: change to Phase and
  back, and Z-Score keeps what you set.

## A typical workflow

1. Press **Z-Score** and look at the raster.
2. Yellow stripe on one baseline? Select an antenna involved and step through with
   Prev/Next, watching the readout and the scatter.
3. Yellow vertical band? Note the time range, then check the scatter and try selecting
   one **Field** or **Scan** to see whether the band is a real problem or a change in
   observing target (see below).
4. Isolated yellow dots? These are usually interference spikes on single samples or
   channels; zoom in and confirm.
5. Decide what to flag. Replot afterwards to see the effect.

## Things to know (limitations)

- **The reference is the whole selection.** Each baseline's "typical" value is taken
  over everything you have selected. If your selection covers several scans or fields
  with genuinely different brightness (for example a bright calibrator and a faint
  target), a whole stretch of time can look "unusual" just because it is a different
  source. If you see a band across nearly all baselines, **select a single Field or
  Scan** and check whether it disappears.
- **Smooth spectral shape is not modeled.** A strongly sloped bandpass can raise scores
  at the band edges.
- **It says "different", not "bad".** Real signal can be unusual too. Treat a high
  score as a reason to look, not a verdict.
- **Z-Score is slower than other plots**, because it must find medians over each
  baseline's data before it can score anything. Expect it to take noticeably longer
  than an Amplitude raster on large selections; narrowing the selection helps.
- Very few samples per baseline give unreliable references.

## Glossary

- **Baseline**: the pair of antennas a visibility comes from.
- **Median**: the middle value; unlike an average, it is not thrown off by a few
  extreme values.
- **Cutoff (threshold)**: the Z-Score above which something is drawn as unusual.
- **False alarm**: clean data crossing the cutoff by chance.
- **Sigma**: the typical size of the noise; a Z-Score of 5 is about five times the
  typical noise-level distance.
