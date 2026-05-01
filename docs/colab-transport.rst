Colab Python↔JavaScript Transport: Development History
=======================================================

This content was generated with assistance from `Claude.ai <https://claude.ai>`_.

Overview
--------

This document summarises the development of bidirectional Python↔JavaScript
communication for ``cubevis`` running inside Google Colab. The primary challenge
is that Colab isolates each cell's output in a separate ``<iframe>``, making
standard Jupyter comm channels and ``window`` object sharing unavailable across
cells.

The solution is implemented in ``_low_level_transport.py`` (Python),
``low_level_transport.ts`` (TypeScript, compiled to ``cubevisjs.min.js``),
and the anywidget bridge ESM embedded within ``_low_level_transport.py``.

Colab Internals and Execution Architecture
------------------------------------------

Understanding the following Colab internals was essential to arriving at a
working implementation.

Kernel type
~~~~~~~~~~~

The Colab kernel is ``google.colab._kernel.Kernel``, not the standard
``ipykernel.kernelbase.Kernel``. It shares some surface-level API but differs
in important internal details, particularly around output routing and message
parent tracking.

``kernel._parents``
~~~~~~~~~~~~~~~~~~~

The kernel maintains a plain mutable dict ``kernel._parents`` with (at minimum)
a ``'shell'`` key whose value is the parent message header of the most recently
dispatched comm message. Colab's output routing infrastructure reads this dict
to determine which cell's output context is currently "active" — and therefore
which ``<iframe>`` should receive output, ``eval_js()`` calls, and widget
``msg:custom`` events.

Unlike ``ipykernel``'s ``ContextVar``-based parent tracking, Colab's
``_parents`` is a simple shared mutable dict on the kernel object. It is
overwritten synchronously as each new message is dispatched. This means that
by the time a daemon thread calls ``eval_js()``, the kernel's main thread will
typically have already moved on to processing the next message, leaving
``_parents`` pointing at a different cell's context.

The implementation captures a snapshot of ``kernel._parents`` at
``display_bridge()`` time — the one moment when the kernel is guaranteed to be
executing in the bridge cell's context — and stores it in
``self._colab_bridge_parents``. Every daemon thread that calls ``eval_js()``
restores this snapshot via ``kernel._parents.update(_colab_bridge_parents)``
before making any ``eval_js()`` call, ensuring the JavaScript executes in the
bridge iframe rather than whatever cell most recently sent a message.

This manipulation is not thread-safe: if two delivery threads ran concurrently
they could interfere with each other's ``_parents`` restoration. In practice
only one delivery thread runs at a time (the poll handler pops the reply queue
and spawns at most one thread per poll), but this is a design constraint that
future refactors must preserve.

``google.colab.output.eval_js()``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``eval_js(js_string, ignore_result=True)`` is the only reliable mechanism for
Python to push JavaScript into a specific iframe. It sends the JS string to the
frontend, which evaluates it in the output context identified by the current
``kernel._parents['shell']`` header.

Despite the ``ignore_result=True`` flag, ``eval_js()`` still performs a
synchronous kernel round-trip — it blocks the calling thread until the frontend
acknowledges execution. This means it must never be called from the kernel's
main execution thread while that thread is occupied (e.g. inside an ``async``
coroutine that is itself being awaited), as this deadlocks the kernel. All
``eval_js()`` calls in this implementation are made exclusively from daemon
threads.

``eval_js()`` is an undocumented Colab internal API. Its per-call payload size
limit is not formally specified; empirically, 500 KB per call is conservative
and reliable.

Cell output isolation and ``BroadcastChannel``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Each Colab cell output runs in its own sandboxed ``<iframe>``. The ``window``
object is not shared between iframes. Any value written to ``window.foo`` in one
iframe is completely invisible to another iframe, regardless of same-origin
status. This rules out using ``window`` as a shared store for cross-iframe
data transfer.

The one cross-iframe mechanism that works reliably is
``BroadcastChannel``, which uses a named-channel abstraction rather than shared
object references. Messages posted via ``bc.postMessage(value)`` are
structured-cloned and delivered to all listeners on the same channel name
across all iframes in the same browsing context. This is the mechanism used
for both JS→Python (``cubevis_tx_<id>``) and Python→JS (``cubevis_rx_<id>``).

``msg:custom`` and CDN widget routing
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Anywidget's ``model.on("msg:custom", handler)`` fires when Python calls
``self._bridge.send(data)``. However, Colab's CDN widget manager routes
``msg:custom`` events based on which widget is in the currently-executing
cell's output — not based on ``kernel._parents``. This means ``bridge.send()``
only delivers reliably when the bridge cell is the active output context, which
is not the case during normal app interaction. All attempts to use
``bridge.send()`` from daemon threads or IOLoop callbacks resulted in silent
message drops. The sole exception is the external-channel path described in
the Working Solution section below.

Architecture
------------

Two separate mechanisms are used depending on direction:

**JavaScript → Python**
  JS posts to a ``BroadcastChannel`` named ``cubevis_tx_<comm_mgr_id>``. The
  anywidget bridge ESM listens on this channel and relays messages to Python via
  ``channel.send()``. Python's ``_recv()`` method handles all incoming messages.

**Python → JavaScript**
  Python serialises the reply envelope using Bokeh's ``Serializer`` (which
  converts numpy arrays to base64-encoded ndarray descriptors). The serialised
  JSON string is delivered to the bridge iframe via a sequence of
  ``eval_js()`` calls from a daemon thread. Each call appends one chunk to a
  JS-side accumulator array (``window._cubevis_ch_<token>``). A final
  ``eval_js()`` call joins the chunks, ``JSON.parse``s the result, and posts the
  reconstructed envelope object to ``BroadcastChannel cubevis_rx_<comm_mgr_id>``,
  which the Bokeh app iframe's ``bc_rx.onmessage`` handler receives.

Key Components
--------------

``CommsTransport`` (Python)
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Additional state for Colab:

- ``_colab_pending_replies: list`` — FIFO queue of serialised reply envelopes
- ``_colab_inflight: int`` — requests received but not yet replied to
- ``_colab_bridge_parents: dict`` — snapshot of ``kernel._parents`` taken at
  ``display_bridge()`` time; used to target ``eval_js()`` at the bridge iframe
- ``colab_chunk_size: int`` — max bytes per ``eval_js()`` call (default 500,000);
  lower this (e.g. to 5,000) to force chunked delivery for testing

Bridge ESM (JavaScript, embedded in Python string)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

- Opens a Colab kernel comm targeting ``comm_<comm_mgr_id>``
- Sets up ``bc_tx`` (outbound) and ``bc_rx`` (inbound) BroadcastChannels
- Hooks ``bc_tx.onmessage`` to relay JS→Python and call ``_startPoll()``
- Implements ``_startPoll()`` / ``_stopPoll()`` with exponential backoff
  (50 ms → 500 ms)

``CommsTransport`` (TypeScript / ``cubevisjs.min.js``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

- ``handleJupyterMessage()``: entry point for received envelopes; calls
  ``dispatchMessage()``
- ``dispatchMessage()``: calls Bokeh's ``deserialize()`` on the inner data
  string, then ``substituteBinary()`` on the result, then ``onMessageCallback``
- ``substituteBinary()``: recursively walks plain ``{}`` objects only. Skips
  class instances (``Object.getPrototypeOf(obj) !== Object.prototype``), typed
  arrays (``ArrayBuffer.isView``), and arrays — preserving Bokeh model
  instances and their ``.get()`` methods intact

Approaches Tried and Outcomes
------------------------------

Jupyter Comm / ``window`` Sharing
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Tried:** Standard Jupyter kernel comms or sharing ``window`` properties
across cells.

**Result:** Failed. Colab isolates each cell in its own ``<iframe>``; ``window``
is not shared and comm targets only reach the widget model's own output context.

``eval_js()`` from Background Threads / IOLoop Callbacks
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Tried:** Calling ``google.colab.output.eval_js()`` from Tornado IOLoop
``add_callback`` callbacks.

**Result:** Failed silently. ``eval_js()`` requires an active cell output
context. When called from a background callback the output is silently discarded.

``self._bridge.send()`` from Various Contexts
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Tried:** Using anywidget's ``model.send()`` (fires ``msg:custom`` in JS)
from background threads, IOLoop callbacks, and ``_recv()`` poll handlers with
``_parents`` manipulation.

**Result:** Failed silently except from the very first poll (before any
``bc_tx`` interaction). The Colab CDN widget manager routes ``msg:custom``
based on which widget is in the currently-executing cell's output context —
not based on ``_parents``. Once any ``bc_tx`` message updates ``_parents``,
subsequent ``bridge.send()`` calls target the wrong cell and are dropped.
``_parents`` manipulation has no effect on CDN manager routing.

Binary Token / Cross-iframe ``window`` Store
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Tried:** Extracting numpy arrays from the envelope before serialisation,
storing them in ``window._cubevis_bin_<token>`` via ``eval_js()`` in the bridge
iframe, and substituting them in ``handleJupyterMessage()`` in the Bokeh app
iframe.

**Result:** Failed. ``eval_js()`` runs in the bridge iframe's ``window``.
``handleJupyterMessage()`` runs in the Bokeh app iframe's ``window``. These are
completely separate ``window`` objects — iframe isolation means no property set
in one is visible in the other. ``BroadcastChannel`` crosses the iframe boundary
correctly because it uses a named-channel abstraction; raw ``window`` property
access does not.

``eval_js()`` Blocking the Kernel
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Tried:** Calling ``eval_js()`` synchronously from within ``_recv()`` or from
``send_message()`` while the kernel was actively awaiting.

**Result:** Deadlocked. ``eval_js()`` even with ``ignore_result=True`` performs
a kernel round-trip waiting for a frontend execution acknowledgement. When the
kernel is occupied (awaiting ``send_message()``, or processing another
coroutine), ``eval_js()`` blocks indefinitely.

**Fix:** All ``eval_js()`` calls are made from daemon threads spawned inside
``_recv()``'s poll handler. ``_recv()`` returns immediately; the daemon thread
runs ``eval_js()`` concurrently while the kernel is free to process the next
incoming message.

Poll Loop Idle Timeouts
~~~~~~~~~~~~~~~~~~~~~~~~

**Tried:** JS-side idle timers (2 s, then 3 min) to stop the poll loop when
idle, and an empty-poll counter.

**Result:** All failed for long-running ``finish`` actions (1–2 min of
computation). Any fixed timeout fires during computation, leaving the reply
undeliverable.

**Fix:** No idle timeout. The poll loop runs indefinitely until Python explicitly
calls ``_cubevis_stopPoll_()`` via ``eval_js()`` in the delivery thread. The
``_colab_inflight`` counter ensures ``_stopPoll`` is not called while any
request is still computing.

Working Solution
-----------------

The working solution combines:

1. **JS-controlled poll start** — ``bc_tx.onmessage`` calls ``_startPoll()``
   whenever JS sends any message to Python, before Python even begins
   processing the request.

2. **In-flight counter** — ``_colab_inflight`` is incremented on every incoming
   ``cubevis_message`` and decremented when ``send_message()`` is called.
   ``_stopPoll`` is only sent when both ``_colab_pending_replies`` is empty
   AND ``_colab_inflight == 0``.

3. **FIFO reply queue** — ``_colab_pending_replies`` is a list; ``send_message``
   appends and the poll handler pops from the front. Multiple in-flight replies
   coexist without overwriting each other.

4. **Bridge cell parent header** — Captured once at ``display_bridge()`` time
   and stored in ``_colab_bridge_parents``. All daemon-thread ``eval_js()``
   calls restore this header before executing so that JS runs in the bridge
   iframe context, from which ``BroadcastChannel`` can reach the Bokeh app
   iframe.

5. **Chunked ``eval_js()`` delivery** — The serialised envelope JSON string is
   split into ``colab_chunk_size``-byte pieces. Each piece is delivered via a
   separate ``eval_js()`` call that appends to
   ``window._cubevis_ch_<token>[]`` in the bridge iframe. A final ``eval_js()``
   call joins all pieces, calls ``JSON.parse()`` to reconstruct the envelope
   object, and posts it to ``bc_rx``. This approach:

   - Stays within ``eval_js()``'s practical per-call size limit
   - Scales to arbitrarily large images (4096×4096 and beyond) by increasing
     the number of ``eval_js()`` calls
   - Avoids all cross-iframe ``window`` issues because both accumulation and
     delivery happen entirely within the bridge iframe
   - Requires no changes to the Bokeh deserialisation path — the Bokeh app
     iframe receives a complete, valid envelope object via ``postMessage``

6. **Bokeh serialisation used throughout** — Numpy arrays are serialised by
   Bokeh's ``Serializer`` into ``{"type":"ndarray","array":{"type":"bytes",
   "data":"<base64>"},...}`` format. Bokeh's ``Deserializer`` in the app iframe
   reconstructs them into Bokeh's internal ndarray class. No custom binary
   protocol is needed.

7. **``substituteBinary()`` prototype guard** — Added to ``low_level_transport.ts``
   to prevent the recursive object reconstruction from stripping prototype chains
   off Bokeh model instances (which would destroy ``.get()`` and other methods).
   Only plain ``{}`` objects are reconstructed; all class instances are passed
   through unchanged.

8. **External-channel path** (``inflight == 0``) — When ``send_message()`` is
   called for a request that bypassed the normal ``bc_tx`` path (e.g. a direct
   kernel comm from a ``%%javascript`` cell), Python uses
   ``self._bridge.send()`` — which works in this specific context because
   ``send_message()`` IS the active kernel execution, making the bridge cell the
   current output context. The bridge ESM's ``msg:custom`` handler receives it
   and posts to ``bc_rx``.

Chunking Performance
--------------------

At 512×512 uint8 image size, the serialised envelope is ~80 KB and fits in a
single ``eval_js()`` call. At 4096×4096, two arrays (image + mask) serialise
to approximately 44 MB of base64, requiring ~90 ``eval_js()`` calls at the
500 KB default chunk size. Delivery time is proportional to the number of calls
and Colab's round-trip latency per ``eval_js()`` call.

To test chunking with small images, set ``transport.colab_chunk_size = 5000``
before triggering an image update. The ``debug.txt`` log will show
``delivered via N chunk(s) (M bytes)``.

Known Limitations
-----------------

- ``eval_js()`` round-trips are sequential — each chunk must complete before
  the next begins. For very large images this will be the dominant latency.

- The solution depends on ``google.colab.output.eval_js`` and
  ``kernel._parents``, both undocumented Colab internals. Changes to Colab's
  internals could break delivery without notice. A version check at
  ``display_bridge()`` time that fails loudly if the kernel type is unrecognised
  would improve robustness.

- The ``_parents`` restoration in delivery threads is not thread-safe. Only one
  delivery thread should run at a time; the current poll-handler design enforces
  this implicitly but it is not an explicit invariant.

- The poll loop may linger briefly after all work is complete (until
  ``_stopPoll`` fires), generating background kernel traffic at the
  exponentially-backed-off poll rate (max 500 ms interval).
