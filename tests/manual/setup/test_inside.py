"""Tests for cubevis.setup.inside: is this computer inside a network?

The rule (2026-10-19, from Darrell's sample of NRAO addresses): inside when
the local address the system would use to reach a node is in the network's
ranges -- not when any local address is (Docker / libvirt bridges, home
routers).
"""
from __future__ import annotations

import time

import pytest

from cubevis.setup import inside as I
from cubevis.setup import state as S

SITE = ["10.7.0.0/16", "10.2.0.0/16"]


def _decide(addrs, src, ranges=SITE):
    return I.decide(["node140.cv.example.org"], ranges,
                    resolver=lambda h: addrs, router=lambda a: src)


def test_inside_by_route_not_by_any_address():
    ok, why = _decide(["10.7.7.199"], "10.7.7.228")       # zuul06 -> cvpost140
    assert ok and "inside" in why
    ok, _ = _decide(["10.7.7.199"], "10.2.103.201")       # ullr (another subnet)
    assert ok
    # A laptop at home with a docker bridge: the route to 10.7.x is the
    # default one, from the home address, so outside.
    ok, why = _decide(["10.7.7.199"], "192.168.1.23")
    assert not ok and "not in" in why
    ok, _ = _decide(["10.7.7.199"], "10.0.0.5")           # home router 10.0.0.x
    assert not ok


def test_names_that_do_not_resolve_mean_outside():
    ok, why = _decide([], None)
    assert not ok and "does not resolve" in why


def test_no_ranges_means_outside():
    ok, why = _decide(["10.7.7.199"], "10.7.7.228", ranges=[])
    assert not ok and "no inside ranges" in why


def test_no_route():
    ok, why = _decide(["10.7.7.199"], None)
    assert not ok and "no route" in why


def test_resolve_times_out():
    import socket
    real = socket.getaddrinfo
    try:
        socket.getaddrinfo = lambda *a, **k: time.sleep(5)
        t0 = time.time()
        assert I.resolve("slow.example.org", timeout=0.2) == []
        assert time.time() - t0 < 1.5
    finally:
        socket.getaddrinfo = real


def test_route_source_localhost():
    assert I.route_source("127.0.0.1") == "127.0.0.1"
    assert I.in_ranges("127.0.0.1", ["127.0.0.0/8"])
    assert not I.in_ranges("127.0.0.1", ["10.0.0.0/8", "bad"])


def test_main_reads_the_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    st = S.load()
    st.add_network("lo", login_host="gw.example.org", user="u", inside=["127.0.0.0/8"])
    st.add_node("n", network="lo", hostname="localhost")
    st.add_network("far", login_host="gw.example.org", user="u", login_alias="far-gw",
                   inside=["10.99.0.0/16"])
    st.add_node("m", network="far", hostname="localhost")
    S.save(st)
    assert I.main(["-v", "lo"]) == 0
    assert "lo: inside" in capsys.readouterr().err
    assert I.main(["far"]) == 1
    assert I.main(["-v", "nope"]) == 1              # unknown: outside, never an error
    assert "cannot tell" in capsys.readouterr().err
    assert I.main([]) == 2


def test_ranges_are_normalized():
    assert S.normalize_ranges(["10.7.7.199/16", " 10.2.0.0/16", "10.1.2.3"]) == \
        ["10.7.0.0/16", "10.2.0.0/16", "10.1.2.3/32"]
    with pytest.raises(S.StateError, match="not an address range"):
        S.normalize_ranges(["10.7/xx"])


def test_network_hosts_default_first():
    net = {"default_node": "b"}
    nodes = {n: {"hostname": f"{n}.x"} for n in "abcde"}
    assert I.network_hosts(net, nodes) == ["b.x", "a.x", "c.x"]
