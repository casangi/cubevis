"""cubevis-setup's last test: start a remote kernel, then draw a plot with it.

    python check_kernel.py KERNEL [--ms PATH] [--field NAME] [--png FILE]

Run with the environment's python on your computer. Prints one line per
test, starting "ok" or "FAIL", and exits 1 if one failed.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import warnings


def kernel_info(name: str) -> str:
    """Start the kernel, ask it for its host and cubevis version, stop it."""
    from jupyter_client.manager import start_new_kernel
    from jupyter_core.paths import jupyter_runtime_dir

    # sshpyk writes its connection file there; a new account has none yet.
    os.makedirs(jupyter_runtime_dir(), mode=0o700, exist_ok=True)
    km, kc = start_new_kernel(kernel_name=name, startup_timeout=120)
    try:
        code = ("import socket, importlib.metadata as m, sys\n"
                "print(socket.gethostname(), m.version('cubevis'), sys.executable)")
        out = []

        def hook(msg):
            if msg["msg_type"] == "stream":
                out.append(msg["content"]["text"])
            elif msg["msg_type"] == "error":
                out.append("\n".join(msg["content"]["traceback"]))

        reply = kc.execute_interactive(code, timeout=60, output_hook=hook)
        if reply["content"]["status"] != "ok":
            raise RuntimeError("".join(out).strip() or "the kernel reported an error")
        return "".join(out).strip()
    finally:
        kc.stop_channels()
        km.shutdown_kernel(now=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kernel")
    ap.add_argument("--ms")
    ap.add_argument("--field")
    ap.add_argument("--png")
    a = ap.parse_args()
    failed = 0

    t = time.time()
    try:
        host, version, python = kernel_info(a.kernel).split()[:3]
        print(f"ok    kernel '{a.kernel}' started on {host}: cubevis {version}, "
              f"{python} ({time.time() - t:.0f} s)")
    except Exception as e:  # report, do not trace back
        print(f"FAIL  kernel '{a.kernel}' did not start: {type(e).__name__}: {e}")
        return 1

    if not a.ms:
        return 0
    t = time.time()
    try:
        warnings.filterwarnings("ignore")
        from cubevis.toolbox.visplot import VisibilityPlotter
        # cubevis sets its loggers to INFO when imported: quieter here.
        for name in list(logging.root.manager.loggerDict):
            if name.startswith("cubevis"):
                logging.getLogger(name).setLevel(logging.WARNING)
        kw = {"field": a.field} if a.field else {}
        vp = VisibilityPlotter(ms=a.ms, headless=True, backend="remote",
                               kernel_name=a.kernel, **kw)
        path = vp(plotfile=a.png or os.path.abspath("cubevis-test.png"), theme="light")
        note = vp._version_warning
        vp.close()
        print(f"ok    plot of {a.ms} drawn by the remote kernel: {path} "
              f"({time.time() - t:.0f} s)")
        if note:
            print(f"note  {note}")
    except Exception as e:
        print(f"FAIL  plot of {a.ms}: {type(e).__name__}: {e}")
        failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
