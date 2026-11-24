Our Full Gap Analysis on Visplot will live here!

| Feature | MSview | TVFLG | Visplot |
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



### Help on TVFLG in AIPS version 31DEC26
TVFLG
Type:  Task
Use:   Grids UV data of the selected type to form an image.  TVFLG will
       work on single- and multi-source files in TB sort order.  It
       grids the data with Baseline on the X axis and Time on the Y
       axis.  All samples falling into a cell are averaged by vector
       averaging.  After making this "master" grid, TVFLG displays the
       grid on the TV smoothing the data in time and showing every n'th
       pixel in baseline as needed to fit the display.  Then, TVFLG
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

### Help on FTFLG in AIPS version 31DEC26
FTFLG
Type:  Task
Use:   Grids UV data of the selected type to form an image.  FTFLG will
       work on single- and multi-source files in TB sort order.  It
       grids the data with channel # on the X axis and time on the Y
       axis.  All samples falling into a cell are averaged by vector
       averaging.  SPFLG puts each baseline in a separate plane of the
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
use SPFLG).

### Help on SPFLG in AIPS version 31DEC26
SPFLG
Type:  Task
Use:   Grids UV data of the selected type to form an image.  SPFLG will
       work on single- and multi-source files in TB sort order.  It
       grids the data with channel # on the X axis and Time on the Y
       axis.  All samples falling into a cell are averaged by vector
       averaging.  After making this "master" grid, SPFLG displays the
       grid on the TV smoothing the data in time and showing every n'th
       pixel in channel as needed to fit the display.  Then, SPFLG
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

