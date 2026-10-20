#!/bin/sh
# The ssh test rig for tests/manual/setup (CUBEVIS_SSH_RIG=1).
#
#   sudo devel/tools/ssh_rig.sh          # start (again): idempotent
#   sudo devel/tools/ssh_rig.sh stop
#
# FOR A DISPOSABLE CONTAINER OR VM ONLY: it adds names to /etc/hosts,
# creates the user alice with a known password, and runs two password-only
# sshd's on 127.0.0.1.  What the tests expect:
#
#   login.example.test:2201    the login host  } both 127.0.0.1, so with the
#   compute.example.test:2202  the node        } inside range 127.0.0.0/8 the
#                                                node is reached directly
#   alice / 's3cret pw!'       on both (the tests' store command echoes it)
#
# The rig's host keys are the machine's own; the tests keep them in their
# own known_hosts files (CUBEVIS_SETUP_KNOWN_HOSTS), not ~/.ssh.
set -eu
DIR=/run/cubevis-ssh-rig
PORTS="2201 2202"

if [ "$(id -u)" != 0 ]; then
    echo "run as root (sudo): it changes /etc/hosts and adds a user" >&2
    exit 1
fi

stop() {
    for p in $PORTS; do
        [ -f "$DIR/sshd_$p.pid" ] && kill "$(cat "$DIR/sshd_$p.pid")" 2>/dev/null || true
        rm -f "$DIR/sshd_$p.pid"
    done
}

if [ "${1:-start}" = stop ]; then
    stop
    echo "rig stopped (/etc/hosts and the user alice are left as they are)"
    exit 0
fi

SSHD=$(command -v sshd || echo /usr/sbin/sshd)
[ -x "$SSHD" ] || { echo "no sshd (install openssh-server)" >&2; exit 1; }

grep -q 'login.example.test' /etc/hosts ||
    echo "127.0.0.1 login.example.test compute.example.test" >> /etc/hosts
id alice >/dev/null 2>&1 || useradd -m -s /bin/sh alice
echo 'alice:s3cret pw!' | chpasswd
[ -f /etc/ssh/ssh_host_ed25519_key ] || ssh-keygen -A
mkdir -p "$DIR" /run/sshd

stop
for p in $PORTS; do
    cat > "$DIR/sshd_$p" <<CONF
Port $p
ListenAddress 127.0.0.1
HostKey /etc/ssh/ssh_host_ed25519_key
PasswordAuthentication yes
KbdInteractiveAuthentication no
PubkeyAuthentication no
PermitRootLogin no
AllowUsers alice
AllowTcpForwarding yes
UsePAM yes
PidFile $DIR/sshd_$p.pid
MaxStartups 50
CONF
    "$SSHD" -f "$DIR/sshd_$p"
done
echo "rig up: login.example.test:2201, compute.example.test:2202 (alice)"
echo "then:   CUBEVIS_SSH_RIG=1 pytest tests/manual/setup"
