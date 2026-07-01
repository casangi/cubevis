# cubevis Application Startup Architecture

This document describes how a `cubevis` application integrates with the `cubevis.exe`
concurrency layer and `Showable` to support both Python CLI and Jupyter notebook
execution.  `iclean` (interactive clean) is used as the reference implementation
throughout; the name `VisibilityPlotter` is used wherever the document describes what
a *new* application must supply.

---

## 1. What the developer must implement

A `cubevis` application is a class with two required elements: an `__init__` that
constructs the backend and builds all Bokeh widgets, and a `__call__` that wires them
together and returns a `(app_context, task)` tuple to the adapter layer.

### 1.1 `__init__(self, *app_args)`

Initialize everything needed to display and run the application:

- Instantiate and configure the application's backend computation object.
- Create a `BokehAppContext`, which owns a `CommMgr` and becomes the active context
  singleton via `BokehInit.set_app_context()`.  The `CommMgr` is retrieved from it
  for opening communication channels.
- Allocate UUIDs for each named message type that will be exchanged with the front-end.
- Build all Bokeh widgets, figures, and layouts — but do not display anything yet.
  Assign the completed inner layout to `self._app_context.ui` so that `BokehAppContext`
  becomes the document root (see §1.3).

```python
from uuid import uuid4
import asyncio
import websockets
from cubevis.bokeh import BokehInit
from cubevis.bokeh.transport import CommMgr
from cubevis.bokeh.models import BokehAppContext
from cubevis import exe

class VisibilityPlotter:

    def __init__(self, *app_args):
        # --- backend setup (application-specific) ---
        self._backend = ...

        # --- communication infrastructure ---
        def shutdown_handler(reason, description):
            self._stop()
            BokehInit.clear_app_context(self._app_context)

        self._app_context = BokehAppContext(
            comm_mgr=CommMgr(on_shutdown=shutdown_handler),
            app_state={'name': 'my app', 'initialized': True},
            title='My App'
        )
        self._comm_mgr = self._app_context.comm_mgr

        # --- asyncio Future that carries the result out of the server loop ---
        self._result_future = None

        # --- UUIDs for named message types (see §2) ---
        self._ids = {
            'some-action': str(uuid4()),
            'done':        str(uuid4()),
        }

        # --- pipe handles, opened later in _init_pipes() ---
        self._pipe = {'control': None}

        # --- build Bokeh widgets and layout (application-specific) ---
        # Assign to self._app_context.ui so BokehAppContext is the document root.
        inner_layout = ...
        self._app_context.ui = inner_layout
```

> **`app_state` and JavaScript `appstate`.**  The `app_state` dict passed to
> `BokehAppContext` is the Python-side initialization of the shared JavaScript data
> store that is accessed in any `CustomJS` callback via
> `Bokeh.find.appState(any_model)` (see §3.1).  Keys set here are available in
> JavaScript from session start.

> **Accessing the current context from helper classes.**  Any helper class that needs
> the `CommMgr` without holding a direct reference can call
> `BokehInit.get_app_context().comm_mgr`.  `BokehInit` maintains a stack of active
> contexts; `get_app_context()` returns the most recently registered one.  This is
> how `CubeMask` obtains the `CommMgr` without being passed one explicitly:
>
> ```python
> self._comm_mgr = BokehInit.get_app_context().comm_mgr
> ```

### 1.2 Opening communication channels

Before `_task_server` runs, any `Comm` channels needed by the application must be
opened and their Python callbacks registered.  The only architectural requirement is
that this happens inside `__call__` before the `exe.Task` is handed to the adapter.
How it is organized internally — whether inlined, delegated to a helper method, or
split across several methods — is up to the developer.  In `iclean` this is done in a
method called `_init_pipes`; the name carries no special meaning to the framework.

The guard against double-initialization (`if self._pipe['control'] is None`) is worth
including if `__call__` may be invoked more than once — for example when a user
re-runs a notebook cell — since pipes must not be opened twice.

```python
    # Example: opening channels inline in __call__, or in a helper method
    def _open_channels(self):
        if self._pipe['control'] is None:
            self._pipe['control'] = self._comm_mgr.open(
                squash_queue=True,
                description='my app control'
            )
            self._pipe['control'].register(self._ids['some-action'], self._handle_some_action)
            self._pipe['control'].register(self._ids['done'],        self._handle_done)
```

### 1.3 `__call__(self, exec_context, task_id=None) → (BokehAppContext, exe.Task)`

Called by the adapter (§4).  Opens communication channels and returns the `BokehAppContext`
together with an `exe.Task` wrapping the async server coroutine.

```python
    def __call__(self, exec_context, task_id=None):
        self._open_channels()         # or inline — whatever the developer chooses
        return self._app_context, exe.Task(self._task_server)
```

> **The `BokehAppContext` must be the returned layout.**  The first element of the
> tuple must be `self._app_context`, not the inner Bokeh layout assigned to its `ui`
> property.  `BokehAppContext` is added as the root of the Bokeh document, which is
> what allows JavaScript to discover shared application state from any widget in the
> tree — either by walking up the parent tree or by searching across document roots
> (see §3.1).  Returning the plain inner layout instead would break this discovery
> mechanism.

`exec_context` (`exe.Context`) and `task_id` are passed in by the adapter and do not
need to be used directly.  For non-synchronous contexts a stop condition can be
obtained if needed:

```python
        self._stop_condition, _ = exec_context.create_stop_condition(task_id)
```

### 1.4 The async server coroutine

The second element of the tuple returned from `__call__` is `exe.Task(coroutine)`,
where `coroutine` is an `async def` method on the class.  `exe.Task` accepts either a
sync or async callable and handles the difference internally, but the coroutine must be
`async def` here because it uses `asyncio.Future` and `websockets.serve`.  The name
`_task_server` is used in `iclean` but carries no special meaning to the framework.

The coroutine creates the future that will carry the result, runs the websocket server,
and blocks until the result future is resolved:

```python
    async def _task_server(self):    # name is the developer's choice
        self._result_future = asyncio.Future()

        if self._comm_mgr.address:
            async with websockets.serve(self._comm_mgr.process_messages,
                                        self._comm_mgr.address[0],
                                        self._comm_mgr.address[1]):
                await self._result_future
        else:
            await self._comm_mgr.process_messages()

        return self.result()
```

### 1.5 Resolving the result future

There are two paths by which shutdown can be triggered, and the method that resolves
the result future must handle both.  The name `_stop` is used in `iclean` but is not
required by the framework — it is simply called from the developer's own message
handler and `shutdown_handler`:

- **Normal shutdown** — the user clicks Stop; JavaScript sends a "done" message
  through a `Comm`; the registered Python async callback resolves the future and
  returns a confirmation to JavaScript (see §3.2).
- **Session disconnect** — the browser tab is closed without pressing Stop; the
  `on_shutdown` callback passed to `CommMgr` at construction fires and resolves the
  future directly.

In both cases the future must be resolved exactly once, hence the `.done()` guard:

```python
    def _stop(self, _=None):    # name is the developer's choice
        if not self._result_future.done():
            self._result_future.set_result(self._compute_result())
```

### 1.6 Returning the computed result

The server coroutine calls `self.result()` before returning, and `Showable.get_result()`
calls it from outside.  Again the name is the developer's choice; what matters is that
the same callable is used in both places:

```python
    def result(self):    # name is the developer's choice
        if self._result_future is None or not self._result_future.done():
            raise RuntimeError('no result available yet')
        return self._result_future.result()
```

### 1.7 Summary of required interface

| Element | Type | Purpose |
|---|---|---|
| `__init__(self, *args)` | constructor | build backend + Bokeh layout + `BokehAppContext` |
| open channels + register callbacks | inside `__call__` | open `CommMgr` channels before `exe.Task` is returned |
| `__call__(self, exec_context, task_id)` | callable | return `(self._app_context, exe.Task(async_coroutine))` |
| async server coroutine | `async def` | websocket server loop; creates and awaits the result future |
| resolve result future | method or callback | called by message handler and `shutdown_handler`; sets future exactly once |
| return result | method | returns computed result; called by server coroutine and `Showable.get_result()` |

---

## 2. Python↔JavaScript communication via `CommMgr` and `Comm`

### 2.1 Overview

`CommMgr` (from `cubevis.bokeh.transport`) manages a set of named websocket channels,
each called a `Comm` (opened by `CommMgr.open()`).  As many `Comm` objects as needed
can be opened — for example, `iclean` opens one for interactive clean control and a
separate one for convergence plot updates.  A UUID is used as the *message tag* to
route messages between a specific JavaScript sender and a specific Python callback.
The same UUID dictionary is passed to both Python (to call `register`) and to
JavaScript (via `CustomJS` `args`) so that both sides agree on which tag corresponds
to which action.

Message payloads are dictionaries.  The format is open — the developer chooses whatever
keys are useful — with the only constraint that all values must be serializable, which
includes `numpy` arrays.

### 2.2 Python side

**Open a channel** before returning from `__call__`:

```python
self._pipe['control'] = self._comm_mgr.open(
    squash_queue=True,
    description='my app control'
)
```

`squash_queue=True` means that if multiple messages with the same UUID tag arrive
while the Python handler for that tag is still running, all but the last are dropped
from the queue.  This is intended for high-frequency GUI events such as mouse
movements or slider drags, where only the final position matters and processing every
intermediate event would cause the handler to fall further and further behind.

**Register a callback** for a UUID tag.  The callback is an `async` function that
receives the message dict sent from JavaScript and returns a response dict:

```python
self._pipe['control'].register(self._ids['some-action'], self._handle_some_action)

async def _handle_some_action(msg, context, self=self):
    value = msg.get('value')
    # ... do work ...
    return {'result': computed_value}
```

The returned dict is forwarded back to the JavaScript callback supplied as the third
argument to `ctrl.send(...)` (see §2.3).

**Attaching JavaScript initialization code to a `Comm`** is done with
`Comm.add_init_script()`:

```python
self._pipe['control'].add_init_script(
    code='''cb_obj._freeze_cursor_update = false''',
    description='initialize control pipe state'
)
```

The `code` string is JavaScript that runs when the `Comm` is first opened in the
browser.  `cb_obj` refers to the `Comm` model itself, so this is the right place to
initialize JavaScript-side state that needs to live on the pipe object.  An optional
`args` dict can pass Bokeh models into the script by name, exactly as `CustomJS` uses
`args`.

**Attaching JavaScript initialization code to the application** is done with
`BokehAppContext.add_init_script()`:

```python
self._app_context.add_init_script(
    code='''appstate.my_flag = false''',
    description='application startup',
    args={'some_model': self._some_bokeh_model}
)
```

These scripts run during application initialization in JavaScript, before other
scripts execute, making them the right place to set up global application state that
widgets will depend on from the start.

### 2.3 JavaScript side

The `Comm` object (referenced as `ctrl` below) and the `ids` dict are passed into
`CustomJS` via its `args` parameter.

**Send a message and handle the response:**

```javascript
// Inside a CustomJS callback:
ctrl.send(
    ids['some-action'],                       // UUID tag — routes to registered Python callback
    { action: 'some-action', value: this.item },  // payload (any serializable dict)
    function receive(msg) {                   // called with Python callback's return value
        if ('result' in msg && msg.result != null) {
            // update Bokeh models with msg.result
        }
    }
)
```

### 2.4 Complete round-trip example — palette selection

This example is taken directly from `CubeMask.palette()` in `_cube.py`.

**Python setup** (channel opening and callback registration):

```python
self._ids = {'palette': str(uuid4()), ...}
self._pipe['control'] = self._comm_mgr.open(squash_queue=True,
                                             description='cube mask control')

async def fetch_palette(msg, context, self=self):
    if 'value' in msg:
        return {'result': find_palette(msg['value']), 'value': msg['value']}
    return {'result': None, 'value': None}

self._pipe['control'].register(self._ids['palette'], fetch_palette)
```

**JavaScript** (attached to a `Dropdown` widget via `js_on_click`):

```javascript
// args: image=self._chan_image, ids=self._ids, ctrl=self._pipe['control']
function receive_palette(msg) {
    if ('result' in msg && msg.result != null) {
        let cm = image.glyph.color_mapper
        cm.palette = msg.result
        cm.change.emit()
        cb_obj.origin.label = msg.value
    }
}
ctrl.send(ids['palette'],
          { action: 'palette', value: this.item },
          receive_palette)
```

When the user selects a palette from the dropdown, JavaScript sends the selection to
Python via the `'palette'` UUID tag.  Python looks up the palette data and returns it;
JavaScript receives the result in `receive_palette` and updates the color mapper.

---

## 3. JavaScript-side architecture

### 3.1 `Bokeh.find` and `appstate`

From any `CustomJS` callback, three application-level objects can be retrieved by
passing any Bokeh `Model` that is part of the application GUI:

```javascript
const appstate    = Bokeh.find.appState(image)    // persistent, modifiable data store
const showable    = Bokeh.find.showable(image)     // the Showable root (null in CLI)
const app_context = Bokeh.find.app_context(image)  // the BokehAppContext model
```

`appstate` is the primary shared data store for JavaScript-side state.  It is a plain
object that persists across callbacks for the lifetime of the session and is accessible
from any widget in the application.  It is seeded from the `app_state` dict passed to
`BokehAppContext` at construction (see §1.1) and is the right place to store flags,
cached values, or functions that multiple parts of the GUI need to share — for example,
`iclean` stores `appstate.image_name` (the currently active field) and
`appstate.window_closed` there so that disparate callbacks can read the same
authoritative value without coupling to each other directly.

`showable` is `null` when running from the CLI (browser tab) and a `Showable` model
reference when running in a notebook.  This is the standard way to distinguish the two
environments in JavaScript and choose the appropriate shutdown behaviour (see §3.2).

### 3.2 Shutdown protocol

The shutdown sequence is left to the developer, but the pattern used in `iclean` /
`CubeMask` is the recommended approach:

1. **JavaScript initiates shutdown** — a Stop button (or equivalent) calls a function
   stored on `appstate` (e.g. `appstate.shutdown()`).  Storing the shutdown function
   on `appstate` rather than closing over it in a single widget's callback allows any
   part of the GUI to trigger the same coordinated shutdown.

2. **A "done" message is sent to Python** — using whichever UUID tag the developer has
   chosen for shutdown signalling.  The JavaScript side passes a callback that will run
   once Python replies.

3. **Python's handler resolves the result future** — allowing the server coroutine to
   return.

4. **Python replies, JavaScript closes** — inside the JavaScript reply callback,
   the code checks `showable`:
   - If `showable` is non-null (notebook): set `showable.disabled = true` to grey out
     the GUI and signal completion to the user.
   - If `showable` is null (CLI / browser tab): call `window.close()`.

The sketch below shows the structure, condensed from `CubeMask` and `InteractiveCleanUI`:

```javascript
// Typically set up in a Comm.add_init_script() or BokehAppContext.add_init_script():
// args include: ctrl (Comm), ids (UUID dict), image (any model for Bokeh.find lookups)
const appstate = Bokeh.find.appState(image)
const showable = Bokeh.find.showable(image)
appstate.already_shutdown = false

appstate.shutdown = (cb=null) => {
    if (appstate.already_shutdown) return
    appstate.already_shutdown = true

    function done_close(msg) {
        if (msg.result === 'stopped') {
            if (showable) {
                showable.disabled = true          // notebook: grey out the GUI
            } else {
                window.close()                    // CLI: close the browser tab
            }
        }
    }

    ctrl.send(ids['done'],
              { action: 'done', value: {} },
              (msg) => { if (!cb || cb(msg)) done_close(msg) })
}

// Later, in a Stop button callback:
appstate.shutdown()
```

---

## 4. The adapter layer

The adapter is a thin class that wraps the developer-implemented class and provides
the user-facing API.  It handles the two execution environments and insulates the
caller from the `exe` machinery.

A minimal adapter looks like this:

```python
from uuid import uuid4
from cubevis.bokeh.models import Showable
from cubevis import exe

class MyApp:

    def __init__(self, *app_args):
        self._ui = VisibilityPlotter(*app_args)
        self._future = None

    # ------------------------------------------------------------------
    # CLI entry point — blocks until the GUI is closed, returns result
    # ------------------------------------------------------------------
    def __call__(self):
        app_id = uuid4()
        context = exe.Context(exe.Mode.SYNC)
        app_context, task = self._ui(context, app_id)
        app_context.show()                             # opens the Bokeh tab
        return context.execute(task, app_id)           # blocks; returns result

    # ------------------------------------------------------------------
    # Notebook entry point — returns a Showable immediately
    # ------------------------------------------------------------------
    def notebook(self):
        from bokeh.io.state import curstate
        if not curstate().notebook:
            from bokeh.io import output_notebook
            output_notebook()

        app_id = uuid4()
        context = exe.Context(exe.Mode.THREAD)
        app_context, task = self._ui(context, app_id)

        def startup():
            self._future = context.execute(task, app_id)

        def get_future():
            if self._future is None:
                raise RuntimeError("app has not been launched yet")
            return self._future

        return Showable(
            app_context,   # BokehAppContext is the layout root
            startup,       # called when .show() is invoked
            get_future,    # called by .get_result()
            name="my-app-jpy",
        )
```

The user-facing calls are then:

```python
# Python CLI
result = MyApp(*args)()

# Jupyter notebook
app = MyApp(*args).notebook()
app.show()
...
result = app.get_result()
```

---

## 5. Architecture summary

```
User (CLI)                          User (Notebook)
    │                                       │
    ▼                                       ▼
MyApp.__call__()               MyApp.notebook() → Showable
    │                                       │
    │  exe.Context(SYNC)        exe.Context(THREAD)
    │                                       │
    └──────────┬────────────────────────────┘
               │
               ▼
      VisibilityPlotter.__call__(context, task_id)
               │
               ├─→  BokehAppContext (wrapping inner layout)  ───────────────┐
               │                                                            │
               └─→  exe.Task(_task_server)                                  │
                         │                                                  │
    CLI path:            │  context.execute(task)        Notebook path:     │
    app_context.show()   │  (blocks in SYNC mode)        Showable.show()    │
    immediately ◄────────┤                               triggers startup() │
    opens browser        │                               which calls        │
                         │                               context.execute()  │
                         │                               in thread pool ────┘
                         │
                         ▼
              _task_server() runs websocket server
              awaiting _result_future
                         │
              user clicks Stop in GUI
                         │
                         ▼
              _stop() sets _result_future
              _task_server() returns result()
                         │
         ┌───────────────┴──────────────────┐
         ▼                                  ▼
  returned directly                Showable.get_result()
  to CLI caller                    polls future.result()
```

### 5.1 `cubevis.exe` roles

| Class | Role |
|---|---|
| `exe.Mode` | Enum: `SYNC`, `ASYNC_RUN`, `ASYNC_TASK`, `THREAD` |
| `exe.Task` | Wraps a sync or async callable; bridges across all four modes |
| `exe.Context` | Holds a `Mode` + thread pool; `execute(task)` dispatches correctly |

For CLI use, `Mode.SYNC` is sufficient.  For notebooks, `Mode.THREAD` runs the server
coroutine in a thread pool so the Jupyter kernel event loop remains free to service
comm messages.

### 5.2 `Showable` roles

`Showable` (`cubevis.bokeh.models.Showable`) is a Bokeh `LayoutDOM` subclass.
Its constructor accepts:

| Parameter | Type | Purpose |
|---|---|---|
| `ui_element` | Bokeh `UIElement` | the `BokehAppContext` returned by `VisibilityPlotter.__call__`; `BokehAppContext` is a `LayoutDOM` subclass and therefore satisfies the `UIElement` type |
| `backend_func` | `Callable[[], None]` | called on `.show()`; invokes `context.execute(task)` |
| `result_retrieval` | `Callable[[], Future]` | called by `.get_result()`; returns the `concurrent.futures.Future` holding the result |
| `name` | `str` | used in display summaries |

Key behaviour:

- `.show()` renders the layout in the notebook cell and calls `backend_func` to start
  the server thread.
- `.get_result()` calls `result_retrieval()` to obtain the future; returns its value
  if done, `None` if still running.
- The `document` property setter intercepts `bokeh.plotting.show(showable)` and also
  starts the backend via that path, so either display mechanism works.
- `Showable` supports three display paths in a notebook — `bokeh.plotting.show(app)`,
  `app.show()`, and bare evaluation of `app` as the last expression in a cell — and
  prevents mixing them within a session via a class-level `_usage_mode` flag.

### 5.3 `BokehAppContext` and `CommMgr`

`BokehAppContext` (a Bokeh `LayoutDOM` model) is the communication hub and the root
of the Bokeh document tree for `cubevis` apps.  It:

- Wraps the application GUI via its `ui` property and is returned as the first element
  of the tuple from `__call__`.  It must be the document root — not the inner layout
  — so that JavaScript can discover the shared application state from any widget either
  by walking up the parent tree or by searching across document roots (see §3.1).
- Seeds the JavaScript `appstate` object via its `app_state` constructor parameter.
- Owns a `CommMgr` that manages named websocket channels.
- Registers itself as the active singleton on construction via `BokehInit.set_app_context()`.
- Is retrievable from anywhere in Python via `BokehInit.get_app_context()`, which
  returns the most recently registered context from an internal stack.
- For CLI display, `BokehAppContext.show()` saves the document to a temporary HTML
  file and opens it in the browser.

`CommMgr`:

- `CommMgr.open(description=..., squash_queue=...)` opens a channel and returns a
  `Comm` object.
- `Comm.register(uuid, async_callback)` binds a UUID tag to a Python async handler.
- `CommMgr.process_messages()` is the async coroutine dispatching incoming messages
  to registered handlers; it is passed directly to `websockets.serve()`.
- The `on_shutdown` callback passed to `CommMgr` is invoked when the Bokeh session
  closes (e.g. browser tab closed without pressing Stop), giving the application a
  chance to resolve the result future and clean up.

---

## 6. What `iclean` adds on top of this pattern

The reference implementation (`iclean` / `InteractiveCleanUI`) adds several concerns
that are *not* required by the general pattern:

- **`gclean` backend**: the async generator that drives `tclean` major cycles.  Any
  new app replaces this with its own computation backend.
- **Mustache-generated wrappers** (`iclean.py`, `iclean_notebook.py`,
  `_interactiveclean.py`, `_interactivecleannotebook.py`): carry the 80+ `tclean`
  parameter signatures, type validation, and CASA task logging through all layers.
  Apps that do not wrap a CASA task do not need this layer.
- **`_setup()` / `__reset()`**: bookkeeping for re-entrant calls and Bokeh output
  reset between uses.  Worth considering for any app that might be invoked multiple
  times in the same notebook session.
