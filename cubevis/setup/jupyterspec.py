"""Jupyter kernel definitions for setup's kernels: what today's
``visplot(..., backend="remote", kernel_name=...)`` starts.

A setup kernel (``state.json``: an environment, a working directory,
variables, a launcher on the node) becomes a Jupyter kernel named
``cubevis-KERNEL`` -- on the node it was last checked on -- written with
sshpyk's ``add``: the ssh wrapper, the generated ssh configuration, the
node's alias, the kernel's launcher as the remote python.  ``setup HOST``
writes it when a kernel is made or checked; ``setup jupyter KERNEL NODE``
adds ``cubevis-KERNEL-NODE`` for another node of a cluster, alongside.

A bridge: stage 3's Session builds the same definition in memory and takes
the node as an argument, so no file per node is needed then.

sshpyk (the ``remote`` extra) must be in the Python the plots come from:
``ensure_sshpyk`` offers to install it.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from . import sshgen
from .ui import tilde


def jupyter_name(kernel: str, node: Optional[str] = None) -> str:
    """``cubevis-KERNEL`` (its usual node), or ``cubevis-KERNEL-NODE`` for a
    node named explicitly -- one Jupyter kernel per node, so a kernel of a
    cluster can be used on several nodes at once."""
    return f"cubevis-{kernel}" + (f"-{node}" if node else "")


def have_sshpyk() -> bool:
    import importlib.util
    return (importlib.util.find_spec("sshpyk") is not None
            and importlib.util.find_spec("jupyter_client") is not None)


def ensure_sshpyk(c) -> bool:
    """sshpyk in this Python, installed with pip if the user agrees."""
    ui = c.ui
    if have_sshpyk():
        return True
    ui.say("sshpyk starts kernels on other computers over ssh; visplot needs it in the "
           f"Python you plot from, this one ({sys.executable}). (pip install "
           "\"cubevis[visplot]\" brings it.)")
    cmd = [sys.executable, "-m", "pip", "install", "sshpyk>=1.23"]
    ui.changes_list("runs: " + " ".join(cmd[:4]) + " 'sshpyk>=1.23'")
    if ui.yes and "install_sshpyk" not in ui.answers:
        ui.note("not installed (unattended run): install it, then run this again")
        return False
    if not ui.ask_yn("Install it?", True, key="install_sshpyk"):
        return False
    r = ui.run(cmd)
    import importlib
    importlib.invalidate_caches()
    if r.returncode != 0 or not have_sshpyk():
        ui.fail("installing sshpyk failed (setup.log has pip's output)")
        return False
    ui.record_change("installed", "sshpyk (pip)")
    return True


def _spec(name: str):
    from jupyter_client.kernelspec import KernelSpecManager, NoSuchKernel
    try:
        return KernelSpecManager().get_kernel_spec(name)
    except NoSuchKernel:
        return None


def _config(spec) -> dict:
    try:
        return dict(spec.metadata["kernel_provisioner"]["config"])
    except (AttributeError, KeyError, TypeError):
        return {}


def default_node(st, kernel: str) -> Optional[str]:
    """Where ``cubevis-KERNEL`` runs: the node the kernel was last checked
    on, else the first node with its environment."""
    k = st.get("kernels", kernel)
    nodes = [x for x in sorted(st.nodes()) if st.environment_on(k["environment"], x)]
    return k.get("checked_on") if k.get("checked_on") in nodes else (nodes or [None])[0]


def make(c, kernel: str, node: Optional[str] = None) -> Optional[str]:
    """Write (or keep, when it is already right) the Jupyter kernel for
    setup kernel *kernel*: ``cubevis-KERNEL`` on its default node, or
    ``cubevis-KERNEL-NODE`` on *node* (any node of its cluster, added on the
    way).  Returns the Jupyter kernel's name, or None (said why)."""
    ui, st = c.ui, c.st
    k = st.get("kernels", kernel)
    explicit = bool(node)
    if node:
        n = st.node_for(node)
        if n is None:
            ui.fail(f"{node} is not a node setup knows, nor one of a cluster's: "
                    f"python -m cubevis.setup {node} first")
            return None
        node = n
    else:
        node = default_node(st, kernel)
        if node is None:
            ui.fail(f"no node has kernel {kernel}'s environment")
            return None
    if not st.environment_on(k["environment"], node):
        ui.fail(f"kernel {kernel}'s environment is not on {node}")
        return None
    if not k.get("launcher"):
        ui.fail(f"kernel {kernel} has no launcher yet: python -m cubevis.setup HOST --kernel {kernel}")
        return None
    if not ensure_sshpyk(c):
        return None
    c.save()                                 # a cluster node added above: in the ssh config
    alias = st.nodes()[node]["alias"]
    name = jupyter_name(kernel, node if explicit else None)
    want = {"ssh": str(sshgen.wrapper_path()), "ssh_config": str(sshgen.config_path()),
            "ssh_host_alias": alias, "remote_python": k["launcher"]}
    old = _spec(name)
    if old is not None and all(_config(old).get(f) == v for f, v in want.items()):
        return name                          # already right: nothing written
    # sshpyk's command-line entry point ("python -m sshpyk" does nothing).
    cmd = [sys.executable, "-c",
           "import sys; from sshpyk.cli import main; sys.argv[0] = 'sshpyk'; sys.exit(main())",
           "add", "--kernel-name", name,
           "--display-name", f"cubevis {kernel} on {node}", "--language", "python",
           "--ssh-host-alias", alias, "--ssh-config", want["ssh_config"],
           "--ssh", want["ssh"], "--remote-python", k["launcher"],
           "--remote-kernel-name", "python3",
           "--launch-timeout", str(k.get("launch_timeout") or 120), "--replace"]
    r = ui.run(cmd, show=False)
    spec = _spec(name)
    if r.returncode != 0 or spec is None or _config(spec).get("remote_python") != k["launcher"]:
        from .steps import last_line
        ui.fail(f"writing Jupyter kernel {name} failed (sshpyk add: "
                f"{last_line(r.stdout + r.stderr) or 'no kernel written'})")
        return None
    ui.record_change("jupyter kernel", f"{name}: kernel {kernel} on {node} "
                     f"({tilde(spec.resource_dir)})")
    try:                                      # sshpyk writes its reconnection file there
        from jupyter_core.paths import jupyter_runtime_dir
        os.makedirs(jupyter_runtime_dir(), exist_ok=True)
    except Exception:
        pass
    return name


def specs_for(st, kernel: str) -> list:
    """``(jupyter name, node)`` of the Jupyter kernels that start setup
    kernel *kernel* here (by its launcher and this computer's ssh files)."""
    if not have_sshpyk():
        return []
    try:
        from jupyter_client.kernelspec import KernelSpecManager
        names = KernelSpecManager().find_kernel_specs()
    except Exception:
        return []
    k = st.section("kernels").get(kernel) or {}
    by_alias = {v.get("alias"): n for n, v in st.nodes().items()}
    out = []
    for name in sorted(names):
        if not name.startswith(f"cubevis-{kernel}"):
            continue
        cfg = _config(_spec(name))
        if k.get("launcher") and cfg.get("remote_python") == k["launcher"]:
            out.append((name, by_alias.get(cfg.get("ssh_host_alias"), cfg.get("ssh_host_alias"))))
    return out


def show_use(ui, name: str, example_ms: str = "sis14_twhya_calibrated_flagged.ms") -> None:
    """How to plot with Jupyter kernel *name*."""
    ui._p("")
    ui._p("    from cubevis import visplot")
    ui._p(f"    visplot(ms=\"{example_ms}\", backend=\"remote\", kernel_name=\"{name}\")")
    ui._p("")
