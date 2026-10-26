#!/bin/bash
# ssh test rig for tests/manual/setup (CUBEVIS_SSH_RIG=1). Disposable container only.
set -e
if [ "$1" = stop ]; then pkill -f 'sshd -f /etc/ssh/rig-' || true; exit 0; fi
grep -q compute2.example.test /etc/hosts || echo "127.0.0.1 login.example.test compute.example.test compute2.example.test" >> /etc/hosts
id alice >/dev/null 2>&1 || useradd -m -s /bin/bash alice
echo 'alice:s3cret pw!' | chpasswd
ssh-keygen -A >/dev/null
mkdir -p /run/sshd
for p in 2201 2202; do
  cat > /etc/ssh/rig-$p.conf <<C
Port $p
ListenAddress 127.0.0.1
HostKey /etc/ssh/ssh_host_ed25519_key
HostKey /etc/ssh/ssh_host_rsa_key
PasswordAuthentication yes
KbdInteractiveAuthentication no
PubkeyAuthentication no
UsePAM yes
PidFile /run/sshd-rig-$p.pid
AllowTcpForwarding yes
Subsystem sftp /usr/lib/openssh/sftp-server
C
  pgrep -f "sshd -f /etc/ssh/rig-$p.conf" >/dev/null || /usr/sbin/sshd -f /etc/ssh/rig-$p.conf
done
echo rig up
