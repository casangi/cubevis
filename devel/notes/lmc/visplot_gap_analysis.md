Our Full Gap Analysis on Visplot will live here!

| Feature | MSview | **TVFLG** | Visplot |
| --- | --- | --- | --- |
| **View a MS** | Yes. Confirmed working for MSv2. Takes a while to load and progress is logged in the terminal CASA is running in, not in msview or CASA logger.| | Yes. Will support MSv2 and MSv4. Tested with MSv2. Have not found a file import option... |
| **Data Display Configurability**; Define one or multiple fields, spectral windows, time range, uv range, antennas, correlations, scans, arrays, and ms selection expressions. Define axes to be any of the following: Time, Baseline, Channel, Correlation, Spectral Window | Yes. Lots of different settings all lumped together in this menu. Scrolling is awful because some setting with dropdowns can potentially get altered.| | Yes. Lots of options. Left side panel is very easy to understand. |
| **Layout and Graphics Configurability** | Yes. Love the freedom with these settings. Can change the margins, background color, and number of displayed panels. Changes are applied realtime. The OK button just closes the window.| | Yes. Can change the number and orientation of plots and toggle Light and Dark mode. |
| **Flag Data** | Yes. Fine for small data, not recommended for large data sets. Can toggle whether your flag impacts all Times, Baselines, Channels, Correlations, Spectral Windows, and/or Antennas. Can undo One or All flags| | Yes. Can toggle whether your flag impacts all Baselines, Channels, Correlations, Spectral Windows, Fields, and/or the whole Scan. No option for Times or Antennas that I can see? The flas stores a Reason, and can Undo, Redo, or Clear flags. |
| **Draw Regions** | Yes. Can draw Rectangle, Point, Ellipse, Polygon, and Polyline.| | No (haven't found drawing tools yet) |
| **Plot Interactions** (Zoom and Pan) | Yes. These tools feel very clunky, as you do not pan in real time.| | Yes. Includes both Box Zoom and Wheel Zoom. |
| **Interactive Colormap Fiddling** | Yes. Can fiddle Shift/Slope as well as Brightness/Contrast. It is difficult to tell what each direction of the mouse will do to the colormap.| | Yes, much better. Accessed in the panels settings. |
| **Ruler Drawing** | Yes. Would like a way for the measurement to persist after unclicking the mouse (does this exist?)| | No. |
| **P/V Tool** | Yes. Have not been able to test.| | No. |
| **Animate the series of channels (or any of the axis options)** | Yes. Wish the Animator panel displayed more information, like units on whatever is being iterated through. I have to bring my cursor to the image to get any information.| | Yes. Navigate to the Grid layout option to hit Play on the Animation. |
| **Progress Loading Visibility** | Somewhat. The program appears to freeze with no indication of loading, but the terminal window shows the percentage of progress in real time.| | Somewhat. There is a loading wheel when axes or field views have to be changed, but no update on the percent to completion. No progress shown for when visplot is opening, so large MS will take forever to open visplot with no info being shown to users. |



### Help on **TVFLG** in AIPS version 31DEC26
**TVFLG**
Type:  Task
Use:   Grids UV data of the selected type to form an image.  **TVFLG** will
       work on single- and multi-source files in TB sort order.  It
       grids the data with Baseline on the X axis and Time on the Y
       axis.  All samples falling into a cell are averaged by vector
       averaging.  After making this "master" grid, **TVFLG** displays the
       grid on the TV smoothing the data in time and showing every n'th
       pixel in baseline as needed to fit the display.  Then, **TVFLG**
       displays its interactive options: fiddle colors and contrast,
       change zoom, set windows (and reload the new subimage with less
       time smoothing), and select cells to flag.  This last is, of
       course, the main function.  The grid file may be cataloged and
       kept for more than one session of interactive editing.  All
       commands to flag data are written to a special FC extension to
       the master grid file.  As a result, they are not lost when the TV
       hangs up, the power fails, the program aborts, or the user exits
       the program willingly.  Commands in the FC file may be deleted
       and their effects on the master grid file undone.  The actual
       flagging of the uv data set is done at the end of all interactive
       operations at the users' command.  Flag tables are written for
       multi-source files and, optionally, for single-source files.  The
       uv data may be flagged directly for single-source files that do
       not already have a flag table.  The latter can be very slow, but
       there are a few tasks that do not honor flag tables.  We
       recommend creating flag tables nonetheless and using SPLIT to
       apply the tables directly to the data when needed.  Note that the
       cataloging of the master grid with its FC file means that one can
       apply the editing commands to more than one UV file (with
       caution, of course)!

Pages 24-30 in the AIPS Cookbook describe how to use **TVFLG**.

### Help on FTFLG in AIPS version 31DEC26
FTFLG
Type:  Task
Use:   Grids UV data of the selected type to form an image.  FTFLG will
       work on single- and multi-source files in TB sort order.  It
       grids the data with channel # on the X axis and time on the Y
       axis.  All samples falling into a cell are averaged by vector
       averaging.  **SPFLG** puts each baseline in a separate plane of the
       master grid and displays them one at a time.  FTFLG on the
       other hand averages all included baselines into a single
       plane, with only polarizations separated into planes.  This
       does help find global and dominating issues quickly.

After making this "master" grid, FTFLG displays the grid on the
TV smoothing the data in time and showing every n'th pixel in
channel as needed to fit the display.  Then, FTFLG displays its
interactive options: fiddle colors and contrast, change zoom,
set windows (and reload the new subimage with less time
smoothing), and select cells to flag.  This last could be
considered the main function, but the combination of baselines
into only one plane limits its usefulness.  The grid file may
be cataloged and kept for more than one session of interactive
editing.  All commands to flag data are written to a special FC
extension to the master grid file.  As a result, they are not
lost when the TV hangs up, the power fails, the program aborts,
or the user exits the program willingly.  Commands in the FC
file may be deleted and their effects on the master grid file
undone.  The actual flagging of the uv data set is done at the
end of all interactive operations at the users' command.  Flag
tables are written for single- and multi-source files.  Note
that the cataloging of the master grid with its FC file means
that one can apply the editing commands to more than one UV
file (with caution, of course)!

WARNING: since all included baselines are lumped together, the
output flags will delete data from ALL baselines.  The
exception is when ANTENNAS and/or BASELINE specifies a single
antenna.  In that case, the flags will be for that antenna with
ALL antennas.  If both specify a single antenna, then the
specified single baseline will be flagged (but then you could
use **SPFLG**).

### Help on **SPFLG** in AIPS version 31DEC26
**SPFLG**
Type:  Task
Use:   Grids UV data of the selected type to form an image.  **SPFLG** will
       work on single- and multi-source files in TB sort order.  It
       grids the data with channel # on the X axis and Time on the Y
       axis.  All samples falling into a cell are averaged by vector
       averaging.  After making this "master" grid, **SPFLG** displays the
       grid on the TV smoothing the data in time and showing every n'th
       pixel in channel as needed to fit the display.  Then, **SPFLG**
       displays its interactive options: fiddle colors and contrast,
       change zoom, set windows (and reload the new subimage with less
       time smoothing), and select cells to flag.  This last is, of
       course, the main function.  The grid file may be cataloged and
       kept for more than one session of interactive editing.  All
       commands to flag data are written to a special FC extension to
       the master grid file.  As a result, they are not lost when the TV
       hangs up, the power fails, the program aborts, or the user exits
       the program willingly.  Commands in the FC file may be deleted
       and their effects on the master grid file undone.  The actual
       flagging of the uv data set is done at the end of all interactive
       operations at the users' command.  Flag tables are written for
       multi-source files and, optionally, for single-source files.  The
       uv data may be flagged directly for single-source files that do
       not already have a flag table.  The latter can be very slow, but
       there are a few tasks that do not honor flag tables.  We
       recommend creating flag tables nonetheless and using SPLIT to
       apply the tables directly to the data when needed.  Note that the
       cataloging of the master grid with its FC file means that one can
       apply the editing commands to more than one UV file (with
       caution, of course)!

### Other notes from the AIPS Cookbook
The third and
“modern” route uses interactive (“TV”-based) tasks to display the data in a variety of ways and to allow
you to delete sections of bad data simply by pointing at them with the TV cursor. These tasks are **TVFLG**
(§ 4.4.3) for all baselines and times (but only shows one IF, one Stokes, and one spectral channel at a time),
**SPFLG** (§ 10.2.2) for all spectral channels, IFs, and times (but only shows one baseline and one Stokes at a
time), EDITA (§ 4.4.2) for editing based on TY (Tant), SN or CL table values, EDITR (§ 5.5.2) for all times (but
only shows a single antenna (1–11 baselines) and one channel average at a time) and WIPER for all types
of data (but with the time, polarization, and sometimes antenna of the points not available while editing).
**TVFLG** is the one used for continuum and channel-0 data from the VLA, while **SPFLG** is only used to check
for channel-dependent interference. **SPFLG** is useful for spectral-line editing in smaller arrays, such as the
Australia Telescope and the VLBA. (The redundancy in the spectral domain on calibrator sources helps the
eyes to locate bad data.) 

Continued info on 4-19 and on.

Spectral-line observers should also use **SPFLG** (§ 10.2.2) to examine and, perhaps, to edit their data. This task
is very similar to **TVFLG** described in § 4.4.3, but **SPFLG** displays spectral channels for all IFs on the horizontal
axis, one baseline at a time. If you have a large number of baselines, as with the VLA, then you should
examine a few of the baselines to check for interference, absorption (or emission) in your calibrator sources,
and other frequency-dependent eﬀects. Use the ANTENNAS and BASELINE adverbs to limit the displays to a
few short spacings and one or two longer ones as well. If there are serious frequency-dependent eﬀects in your
calibrators, use **SPFLG** and UVFLG to delete them. (4-41)

You can use **SPFLG** to
edit all channels, or if the signal is strong in a few channels, you could run **TVFLG** on those channels from
the spectral-line data set or average those with the BCHAN, ECHAN, and NCHAV adverbs. (4-42)

**TVFLG**, **SPFLG**, WIPER, IBLED and EDITR are TV-based, interactive editors. **TVFLG** is most suitable for data sets
with large numbers of baselines, e.g., the VLA, but it can be used usefully for VLBI data experiments with 10
or more antennas. **TVFLG** allows you a global overview of your data and can display the data for all baselines
simultaneously as a function of time. This task is documented extensively in § 4.4.3 of the CookBook. **SPFLG**
is a very useful task for data with a significant number of spectral channels. It is eﬀective in examining data
for frequency-dependent errors and interference and can be an eﬀective data editor for interferometers with
a small number of baselines; see § 10.2.2 and § 8.1. IBLED has a diﬀerent philosophy; it plots one baseline at
a time in a graphical rather than gray-scale (image) fashion. It is able to average data over time, spectral
channels, and/or IFs to make a more manageable amount of data and to measure the “decorrelation index”
which is a measure of how variable the phase is over the averaging intervals. The capability of averaging
IFs and displaying decorrelation may be of special interest for VLBI data sets. Otherwise, IBLED has been replaced in 15APR98 by EDITR, which also uses the graphics planes rather than gray-scale images but which
can plot multiple baselines to a chosen antenna and can display two data sets at the same time. This is
obviously more useful for smaller arrays — e.g., VLBI, MERLIN, and the Australia Telescope. (5-31)

If your calibrator data were almost free of problems,
save for simple matters like dead antennas, then your source data are not likely to need much editing either.
If your calibrator data required detailed and erratic data flagging, then your source data will also need
attention. All of the considerations of § 4.4 apply here too. In either case, the first step is to check for
narrow-band interference which is a fairly common problem at some wavelengths. The best task for this is
**SPFLG** (§ 10.2.2), a spectral-line version of **TVFLG** (§ 4.4.3). **SPFLG** displays spectral channels for all IFs along
the horizontal axis and time along the vertical axis, one baseline at a time. This is tedious to use for editing
all of the data from a large interferometer. Instead, select a few of the shorter baselines (with the ANTENNAS
and BASELINE adverbs) and do a quick check for interference. If your data has suﬃcient signal-to-noise, then
this quick examination should let you check which channels have signal and whether the Galaxy, for example,
has contributed unwanted signal to your data. If you find significant terrestrial interference, you may flag
the bad data with **SPFLG** (consider using the “ALL-BL” option to avoid looking at hundreds of baselines)
or with UVFLG (entering “by hand” what you find with **SPFLG**). (8-2)

More details on SPFLG in Section 10.2.2 (Page 10-7).


--------
### FTFLG is NOT mentioned in the AIPS Cookbook. 
### EDITR is described as the third interactive tool, alongside TVFLG and SPFLG.

### Help on EDITR in AIPS version 31DEC26
EDITR
Type:  Task
Use:   To edit visibility data interactively using the TV graphics
       planes.  The visibility amplitude or phase or the amplitude of
       the visibility with a running average subtracted may be displayed
       in an edit window.  A second observable from the selected
       baseline is shown in a display adjacent to the edit window.
       Optionally, data in the primary observable for 1 to 10 other
       baselines to the selected antenna may be displayed over the same
       time range in areas above the second window.  The data are shown
       in a low-resolution, full time-range display or they may be shown
       with higher resolution by interactively selecting a "frame" or
       window of shorter time duration.  A second uv data set may also
       be displayed along with the first.  These data are not used for
       editing but may help you to select the data to be deleted.  A
       "normal" choice for the second data set would be the residuals
       after Cleaning or UVSUB.  A menu-like control interface is
       available to select the data, antenna, and time range to be
       edited and to select various forms of editing.

EDITR creates a Flag Command file attached to the input UV data
file.  If one already exists - due to a prior power failure or
program failure - it will be used.  This is a bit dangerous and
should be done only if the data selection adverbs have the values
they had when the FC file was created.  When EDITR finishes, the
FC table is translated into a standard Flag (FG) table.

EDITR is for editing continuum data from one or more IFs.
Multiple spectral channels may be averaged on input with the
vector average used for display and editing; multiple IFs are
kept separate.  The data may also be averaged over time as they
are read into memory.  This is useful for improved
signal-to-noise and to help squeeze the data into memory, but
will cause the data flags to be less selective in time.  The
program is more efficient if all data fit in memory, so EDITR
will try to read all data into memory if it can.  Failing that it
will try to read all data for one IF or all data for one antenna
for all or one IF.  It will fail if this last case does not fit
and you will need to use TIMERANGE or SOLINT to reduce the amount
of data.


