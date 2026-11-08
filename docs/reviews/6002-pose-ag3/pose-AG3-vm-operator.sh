#!/bin/bash
# #6002 v2 — pose AG3 du compte VM non root de l'adaptateur (README #6276, prép. 1, 2 partiel, 3, 4, 5).
# Lancé depuis bureau-goal (root SSH existant vers AG3). Sans --apply : préalables seuls, aucun effet.
# remote.json et connexion DB NON posés (attendent #6296 et #6298) : vm-remote.py refuse alors TOUT (échec fermé,
# y compris true/journalctl) ; les commandes admises se qualifient après inscription réelle.
# Chaque bloc distant est un script entre guillemets envoyé par `bash -s -- <arguments>` : aucune expansion locale.
set -euo pipefail
AG3=51.15.137.245; SHA=41262fcde6bf74f70458387bc30ba728ebbb0699; LOGIN=vm-operator
PUB_FILE=$HOME/.ssh/id_ed25519.pub
APPLY=0; [ "${1:-}" = --apply ] && APPLY=1
R() { ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@$AG3" bash -s -- "$@"; }
stop() { echo "STOP: $*" >&2; exit 2; }
PUB=$(awk '{print $1" "$2}' "$PUB_FILE"); [[ $PUB =~ ^ssh-ed25519\ [A-Za-z0-9+/]+=*$ ]] || stop "clé publique opérateur illisible"

# 0. Préalables (lecture seule)
R "$LOGIN" <<'EOS' || stop "préalables AG3"
set -euo pipefail; L=$1
! getent passwd "$L" >/dev/null || { echo "compte $L existe"; exit 1; }
for f in /opt/ameesh-vm-adapter /var/lib/ameesh-vm-remote "/etc/ssh/authorized_keys/$L" /etc/ssh/sshd_config.d/55-ameesh-vm-operator.conf /etc/sudoers.d/ameesh-vm-operator; do
  { [ ! -e "$f" ] && [ ! -L "$f" ]; } || { echo "présent : $f"; exit 1; }; done
getent group systemd-journal >/dev/null; sshd -t; command -v python3.12 >/dev/null; command -v visudo >/dev/null
echo "préalables OK"
EOS
[ $APPLY = 1 ] || { echo "(contrôle seul, aucun effet)"; exit 0; }
TS=$(date -u +%Y%m%dT%H%MZ); OPS=/root/ameesh-operator-6002-AG3-$TS

# 1. compte non root, mot de passe verrouillé, groupe systemd-journal seul en plus ; relevé sshd avant
R "$LOGIN" "$OPS" <<'EOS'
set -euo pipefail; L=$1; OPS=$2; umask 077; install -d -m 700 "$OPS"
for u in root ameesh; do sshd -T -C "user=$u,host=x,addr=127.0.0.1" | sort > "$OPS/sshd-avant-$u.txt"; done
useradd --system --shell /bin/sh --home-dir /var/lib/ameesh-vm-remote --no-create-home --user-group "$L"
passwd -l "$L" >/dev/null; usermod -aG systemd-journal "$L"
[ "$(id -nG "$L" | tr ' ' '\n' | sort | tr '\n' ' ')" = "systemd-journal $L " ] || { echo "groupes inattendus : $(id -nG "$L")"; exit 1; }
id "$L"
EOS

# 2 (partiel). code + venv root non inscriptibles ; remote.json NON posé
git -C /home/agent/development/manaty/ameesh archive --format=tar --prefix="src-${SHA:0:12}/" "$SHA" > /tmp/claude-1001/6002-src.tar
scp -q -F none -o BatchMode=yes /tmp/claude-1001/6002-src.tar "root@$AG3:$OPS/src.tar"; rm -f /tmp/claude-1001/6002-src.tar
R "$SHA" "$OPS" <<'EOS'
set -euo pipefail; SHA=$1; OPS=$2; S12=${SHA:0:12}; O=/opt/ameesh-vm-adapter
install -d -m 755 -o root -g root "$O"; tar -C "$O" -xf "$OPS/src.tar"
cd "$O"; python3.12 -m venv "venv-$S12"; "venv-$S12/bin/pip" install -q --no-compile "src-$S12" >/dev/null; ln -sfn "venv-$S12" venv
install -d -m 755 -o root -g root remote; install -m 644 -o root -g root "src-$S12/deploy/vm-adapter/remote/vm-remote.py" remote/vm-remote.py
chown -R root:root "$O"; chmod -R go-w "$O"
ecrit=$(find "$O" -xdev \( -perm -g+w -o -perm -o+w \) ! -type l); [ -z "$ecrit" ] || { echo "inscriptible : $ecrit"; exit 1; }
nonroot=$(find "$O" -xdev ! -user root); [ -z "$nonroot" ] || { echo "non root : $nonroot"; exit 1; }
sha256sum remote/vm-remote.py
EOS

# 3. état du compte VM
R "$LOGIN" <<'EOS'
set -euo pipefail; L=$1; install -d -m 700 -o "$L" -g "$L" /var/lib/ameesh-vm-remote; stat -c '%a %U %n' /var/lib/ameesh-vm-remote
EOS

# 4. clé forcée root + Match sshd ; sshd -t ; sshd -T root/ameesh identiques ; reload sous session tenue
R "$LOGIN" "$OPS" "$PUB" <<'EOS'
set -euo pipefail; L=$1; OPS=$2; PUB=$3
FORCE='/opt/ameesh-vm-adapter/venv/bin/python -I /opt/ameesh-vm-adapter/remote/vm-remote.py dispatch'
install -d -m 755 -o root -g root /etc/ssh/authorized_keys
printf 'restrict,command="%s" %s vm-adapter\n' "$FORCE" "$PUB" > "/etc/ssh/authorized_keys/$L"; chown root:root "/etc/ssh/authorized_keys/$L"; chmod 644 "/etc/ssh/authorized_keys/$L"
cat > /etc/ssh/sshd_config.d/55-ameesh-vm-operator.conf <<CONF
# #6002 : compte VM non root de l'adaptateur (README #6276)
Match User $L
    AuthorizedKeysFile /etc/ssh/authorized_keys/$L
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PermitTTY no
    AllowTcpForwarding no
    AllowAgentForwarding no
    AllowStreamLocalForwarding no
    X11Forwarding no
    PermitTunnel no
    PermitUserEnvironment no
    ForceCommand $FORCE
Match all
CONF
chmod 644 /etc/ssh/sshd_config.d/55-ameesh-vm-operator.conf; sshd -t
for u in root ameesh; do sshd -T -C "user=$u,host=x,addr=127.0.0.1" | sort | cmp -s - "$OPS/sshd-avant-$u.txt" || { echo "ECART sshd -T $u"; exit 1; }; done
sshd -T -C "user=$L,host=x,addr=127.0.0.1" | grep -E '^(forcecommand|permittty|allowtcpforwarding|authorizedkeysfile|permituserenvironment|authenticationmethods) '
EOS
ssh -F none -o BatchMode=yes "root@$AG3" sleep 60 & HOLD=$!; sleep 2
R <<'EOS'
set -euo pipefail; systemctl reload ssh; systemctl is-active ssh
EOS
R <<<'true' && echo "root@AG3 OK après reload"; kill "$HOLD" 2>/dev/null || true

# 5. sudoers préparé dans $OPS (0700 root), validé, installé depuis ce même fichier
R "$LOGIN" "$OPS" <<'EOS'
set -euo pipefail; L=$1; OPS=$2; F="$OPS/ameesh-vm-operator.sudoers"
printf '%s\n' '# #6002 : arrêt de cette VM seulement' "$L ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff --no-block" > "$F"
visudo -cf "$F"; install -m 440 -o root -g root "$F" /etc/sudoers.d/ameesh-vm-operator; visudo -c >/dev/null
sudo -l -U "$L" | grep -F '(root) NOPASSWD: /usr/bin/systemctl poweroff --no-block'
EOS
echo "pose AG3 terminée $TS (reçus $OPS). Tant que remote.json est absent, la clé forcée refuse tout (attendu)."
