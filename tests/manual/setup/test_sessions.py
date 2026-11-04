"""This computer's session records (``cubevis.setup.sessions``, notes
section 59): written when a remote session connects, removed when it
closes, listed by ``status`` -- running here, or left over (its Python
gone, the kernel perhaps still running).  No ssh needed."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import types

import pytest

from cubevis.setup import sessions as SS
from cubevis.setup import state as S

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


@pytest.fixture
def home(monkeypatch):
    d = tempfile.mkdtemp(prefix="cvs", dir="/tmp")
    monkeypatch.setenv("XDG_CONFIG_HOME", d)
    monkeypatch.setenv("CUBEVIS_HOST_ID", "gui1")
    return d


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_record_list_forget(home):
    sid = SS.record(app="visplot", jupyter_kernel="cubevis-zuul06", node="zuul06",
                    persistent_file="/x/sshpyk-kernel-1.json", remote_pid="4242",
                    data="tw.ms")
    assert sid and sid.startswith("visplot-")
    rec = S.load().section("sessions")[sid]
    assert set(rec) == set(SS.FIELDS)
    assert rec["kernel"] == "zuul06" and rec["jupyter_kernel"] == "cubevis-zuul06"
    assert rec["local_pid"] == os.getpid() and rec["remote_pid"] == 4242
    # Never in the shared file: this computer's own.
    assert "sessions" not in open(S.state_path()).read() if os.path.exists(S.state_path()) else True
    (lsid, lrec, live), = SS.listing()
    assert lsid == sid and live
    assert "running here" in SS.describe(lsid, lrec, live)
    SS.update(sid, context_id="c1", handle="h1", bogus=1)
    rec = S.load().section("sessions")[sid]
    assert (rec["context_id"], rec["handle"]) == ("c1", "h1") and "bogus" not in rec
    SS.update("visplot-nosuch", handle="x")         # nothing to change
    SS.forget(sid)
    assert S.load().section("sessions") == {}
    SS.forget(sid)                                   # twice: nothing to do
    SS.forget(None)


def test_left_over_and_status(home):
    sid = SS.record(app="visplot", jupyter_kernel="cubevis-k", node="cvpost106", remote_pid=7)
    st = S.load()
    st.section("sessions")[sid]["local_pid"] = _dead_pid()
    S.save(st)
    (_, rec, live), = SS.listing()
    assert not live
    text = SS.describe(sid, rec, live)
    assert "left over" in text and "may still run on cvpost106 (pid 7)" in text
    r = subprocess.run([sys.executable, "-m", "cubevis.setup", "status"], capture_output=True,
                       text=True, cwd=ROOT, env=dict(os.environ, PYTHONPATH=ROOT), timeout=120)
    assert r.returncode == 0, r.stderr
    out = " ".join(r.stdout.split())
    assert "remote sessions started on this computer" in out and "left over" in out


def test_kernel_facts():
    assert SS.kernel_facts(types.SimpleNamespace()) == {}                 # a local kernel
    assert SS.kernel_facts(types.SimpleNamespace(provisioner=object())) == {}
    km = types.SimpleNamespace(provisioner=types.SimpleNamespace(
        ssh_host_alias="zuul06", persistent_file="/r/sshpyk-kernel-9.json", rem_pid_k=99))
    assert SS.kernel_facts(km) == {"node": "zuul06", "persistent_file": "/r/sshpyk-kernel-9.json",
                                   "remote_pid": 99}
    assert SS.setup_kernel_name("cubevis-zuul06") == "zuul06"
    assert SS.setup_kernel_name("python3") == "python3"


def test_recording_never_fails(home, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(S, "save", boom)
    assert SS.record(app="visplot", jupyter_kernel="cubevis-k", node="n") is None
    SS.forget("visplot-12345678")                   # load works, nothing to remove


def test_remote_context_records_only_sshpyk_kernels(home):
    """``RemoteReductionContext._record_session``: a local kernel (no ssh
    alias) is not recorded; an sshpyk one is, and ``close`` forgets it."""
    pytest.importorskip("jupyter_client")
    from cubevis.toolbox.visplot.remote_reduction_context import RemoteReductionContext
    rrc = RemoteReductionContext.__new__(RemoteReductionContext)
    rrc._kernel_name, rrc._path = "cubevis-zuul06", "tw.ms"
    rrc._ctx, rrc._handle = types.SimpleNamespace(context_id="ctx-1"), "obj-1"
    rrc._km = types.SimpleNamespace()
    assert rrc._record_session() is None
    rrc._km = types.SimpleNamespace(provisioner=types.SimpleNamespace(
        ssh_host_alias="zuul06", persistent_file=None, rem_pid_k=None))
    sid = rrc._record_session()
    assert sid in S.load().section("sessions")
    rec = S.load().section("sessions")[sid]
    assert (rec["data"], rec["context_id"], rec["handle"]) == ("tw.ms", "ctx-1", "obj-1")
