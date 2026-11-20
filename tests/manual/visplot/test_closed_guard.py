"""After close(), the data-reading request handlers answer with one clear
message instead of reaching the closed reader ("MSv2Backend is not open").

The guard is VisibilityPlotter._while_open; the handlers it wraps are the
ones registered in _build_comm (Plot, export, summary, rows, time labels,
flag actions).  Done and the theme are not wrapped.
"""
import asyncio
import inspect
import types

from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter as VP


def _stub(closed=False):
    s = types.SimpleNamespace(_closed=closed)
    s.CLOSED_TEXT = VP.CLOSED_TEXT
    s._closed_reply = types.MethodType(VP._closed_reply, s)
    return s


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_open_plotter_calls_the_handler():
    calls = []

    async def handler(msg, context=None):
        calls.append((msg, context))
        return {"status": "ok"}

    g = VP._while_open(_stub(), handler)
    assert _run(g({"a": 1}, context="ctx")) == {"status": "ok"}
    assert calls == [({"a": 1}, "ctx")]


def test_sync_handler_result_is_returned():
    g = VP._while_open(_stub(), lambda msg, context=None: {"n": msg["n"] + 1})
    assert _run(g({"n": 1})) == {"n": 2}


def test_closed_plotter_answers_without_calling_the_handler():
    calls = []

    async def handler(msg, context=None):
        calls.append(msg)
        raise RuntimeError("MSv2Backend is not open.")

    s = _stub(closed=True)
    g = VP._while_open(s, handler)
    r = _run(g({}))
    assert calls == []
    assert r["closed"] is True and r["status"] == "error"
    # every reply shape the page reads carries the same sentence
    for key in ("message", "error", "status_text", "notify_text"):
        assert r[key] == VP.CLOSED_TEXT
    assert "closed" in VP.CLOSED_TEXT and "Run" in VP.CLOSED_TEXT
    # a second request is answered the same way (logged only once)
    assert _run(g({})) == r and s._closed_logged


def test_comm_mgr_still_passes_the_context():
    # CommMgr._handle_request passes context= only when the handler has
    # two or more parameters; the wrapper must keep that true.
    async def handler(msg, context=None):
        return None

    g = VP._while_open(_stub(), handler)
    assert len(inspect.signature(g).parameters) >= 2
    assert g.__name__ == "handler"
