# Setting up visplot with a remote kernel

visplot can run its data side in a Jupyter kernel on another machine: the
one that holds the data, often reached through a login host (gateway) that
asks for your password every time. Setting that up is done by one script,
`setup/cubevis-setup`, from a cubevis checkout:

```
git clone https://github.com/casangi/cubevis.git     # anywhere you like
bash cubevis/setup/cubevis-setup
```

It asks before each change, says what it will change, and tests each step
before going on. It needs no administrator rights on either side, and
installs Python (with conda) where there is none. Everything it keeps is in
`~/.config/cubevis`, and every change it makes is listed in
`~/.config/cubevis/changes.log`. Run it again at any time: it carries on
where it stopped, checks what is there (`--check`), or adds another remote
host.

`setup/README.md` says what each step does and changes, how passwords are
kept, and how to take it all away again.

## The pieces

```
 your computer                     login host            remote host
 ─────────────                     ──────────            ───────────
 visplot (browser window)  ──ssh──▶  (password)  ──ssh──▶  Jupyter kernel
 sshpyk  (starts the kernel)                               visplot data side
 ssh wrapper (types the password, once)                    your measurement sets
```

* **Your computer** (macOS or Linux) runs the visplot window and `sshpyk`,
  which starts a Jupyter kernel on the remote host over ssh.
* **The login host** is the machine you reach first. Nothing runs there.
* **The remote host** holds the data and runs the kernel.

A kernel is started by ssh without a terminal, so a password prompt has
nobody to answer it. The **ssh wrapper** (`setup/bin/ssh`) logs in to each
host that wants a password once, with the password from your password store,
and leaves a shared connection that later ssh commands reuse. Only the
kernels use it: it is not put on your PATH.

## What to have ready

* the remote host's name (as in `ssh you@name`) and your user name there;
* the login host's name, if you go through one;
* your password, if they ask for one;
* a measurement set on the remote host, or let setup download a small test
  one.

## Then

```
. ~/.config/cubevis/activate
cd cubevis/demos/visplot-remote
python 03_remote_gui.py --kernel <kernel> --remote-ms /path/on/remote/data.ms
```

setup prints this line with your kernel's name at the end. `README.md` here
describes the demos.

The demos use the cubevis in this checkout; the remote host has the release
setup installed (the one matching the checkout's tag, if it is at one). If
they differ, visplot says so in a yellow line; that is harmless for trying
things out, and `git checkout v<release>` in the checkout makes them match.
