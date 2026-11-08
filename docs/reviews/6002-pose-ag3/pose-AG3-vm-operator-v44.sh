#!/bin/bash
# #6002 v4.4 — acl-retour-v44.sh (lectures getfacl exigées réussies). v4.3 — seul changement : acl-retour-v44.sh déposé dans les reçus (retour v4.3). v4.2 — ACL par acl-pose-v42.sh (reçus root 0600, base du retour borné) ; dossier authorized_keys
#   noté s'il est créé par la pose. Sinon identique à v4.1.
# #6002 v4.1 — pose AG3 (delta de v4 : O1 ACL décidée #200777, pose INACTIVE seulement, O6/#6321)
# #6002 v4 — pose AG3 du compte VM non root de l'adaptateur (README deploy/vm-adapter, prép. 1-6),
# delta de v3 : code de référence M = 97285a076dee (actif en staging, inclut #6296 et #6298),
# contrôles #6296 (checks/, reçu partagé 2750, unités utilisateur d'ameesh), inscription remote.json,
# connexion DB vm_probe (JSON 0640 + passfile 0600 de vm-operator), secret généré SUR AG3.
# Le rôle DB lui-même est posé par pose-DB-vm_probe.sh (base centrale), après ce script.
# Lancé depuis bureau-goal (root SSH existant vers AG3). Sans --apply : préalables seuls, aucun effet.
#   ./pose-AG3-vm-operator-v41.sh [--apply]
# v4.1 : accès au venv décidé par le pilote (#200777 O1) : paquet acl, puis u:vm-operator:--x
# (traversée seule, sans r, sans ACL par défaut) sur /home/ameesh, .local, .local/share,
# .local/share/ameesh. Plus d'option « aucun ».
# v4.1, O6 (#200777, lot #6321) : POSE INACTIVE et rôle vm_probe seulement. L'activation (clé cloud de
# l'adaptateur, inscription de la cible côté contrôleur, cycles réels) attend #6321 : ce dossier
# n'installe pas cible-ameesh-agents-3.v2.json sur BG et ne donne aucun accès cloud.
set -euo pipefail
ICI=$(cd "$(dirname "$0")" && pwd)
AG3=51.15.137.245; M=97285a076dee285097c412d3bd0f2649438a3257; LOGIN=vm-operator; HOTE=ameesh-agents-3
PROBE=mty-amh-impl-15
PUB_FILE=$HOME/.ssh/id_ed25519.pub
APPLY=0 ACCES=acl
while [ $# -gt 0 ]; do case $1 in --apply) APPLY=1; shift;; *) echo "argument $1"; exit 2;; esac; done
R() { ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@$AG3" bash -s -- "$@"; }
stop() { echo "STOP: $*" >&2; exit 2; }
PUB_TYPE=$(awk '{print $1}' "$PUB_FILE"); PUB_B64=$(awk '{print $2}' "$PUB_FILE")
[ "$PUB_TYPE" = ssh-ed25519 ] && [[ $PUB_B64 =~ ^[A-Za-z0-9+/]+=*$ ]] || stop "clé publique opérateur illisible"
SRC=/home/agent/development/manaty/ameesh
git -C "$SRC" cat-file -e "$M^{commit}" || stop "M absent du clone"
[ "$(sha256sum "$ICI/secret-vm-probe-v41.py" | cut -c1-64)" = "$(awk '$2 ~ /secret-vm-probe-v41.py$/ {print $1}' "$ICI/SHA256SUMS-v44")" ] || stop "secret-vm-probe-v41.py ≠ SHA256SUMS-v44"

# 0. Préalables (lecture seule)
R "$LOGIN" <<'EOS' || stop "préalables AG3"
set -euo pipefail; L=$1
! getent passwd "$L" >/dev/null || { echo "compte $L existe"; exit 1; }
for f in /opt/ameesh-vm-adapter /var/lib/ameesh-vm-remote /var/lib/ameesh-vm-health /etc/ameesh-vm-adapter \
         "/etc/ssh/authorized_keys/$L" /etc/ssh/sshd_config.d/55-ameesh-vm-operator.conf /etc/sudoers.d/ameesh-vm-operator \
         /home/ameesh/.config/systemd/user/ameesh-vm-health.service /home/ameesh/.config/systemd/user/ameesh-vm-health.timer; do
  { [ ! -e "$f" ] && [ ! -L "$f" ]; } || { echo "présent : $f"; exit 1; }; done
getent group systemd-journal >/dev/null; sshd -t; command -v python3.12 >/dev/null; command -v visudo >/dev/null
[ "$(readlink -f /usr/bin/python3)" = /usr/bin/python3.12 ] || { echo "/usr/bin/python3 ≠ python3.12"; exit 1; }
[ "$(stat -c '%U:%a' /home/ameesh/.config/ameesh/env)" = ameesh:600 ] && [ "$(loginctl show-user ameesh -p Linger --value)" = yes ] || { echo "env d'ameesh non ameesh:0600, ou linger"; exit 1; }
# v4.1 (impl-03 B1 #200802) : vm-remote.py refuse un argv dont un élément est un lien ; /usr/bin/python3
# est un lien 0777. Les contrôles inscrits utilisent /usr/bin/python3.12, fichier régulier root 0755.
[ "$(stat -c '%F %U %a' /usr/bin/python3.12)" = "regular file root 755" ] || { echo "/usr/bin/python3.12 n'est pas un fichier régulier root 0755"; exit 1; }
[ "$(sshd -T | awk '$1=="permituserenvironment"{print $2}')" = no ] || { echo "PermitUserEnvironment global ≠ no"; exit 1; }
timeout 3 bash -c 'echo > /dev/tcp/10.77.0.1/5432' || { echo "10.77.0.1:5432 injoignable"; exit 1; }
echo "préalables OK ; runners actifs : $(systemctl --user -M ameesh@ list-units 'ameesh-runner-agent@*' --state=active --no-legend | awk '{print $1}' | tr '\n' ' ')"
EOS
echo "accès au venv : ACL u:$LOGIN:--x sur 4 parents (décision #200777 O1)"
[ $APPLY = 1 ] || { echo "(contrôle seul, aucun effet)"; exit 0; }
TS=$(date -u +%Y%m%dT%H%M%SZ)-$$; OPS=/root/ameesh-operator-6002-AG3-$TS

# 1. compte non root, mot de passe verrouillé, groupe systemd-journal seul en plus ; relevé sshd avant (v3)
R "$LOGIN" "$OPS" <<'EOS'
set -euo pipefail; L=$1; OPS=$2; umask 077; install -d -m 700 "$OPS"
for u in root ameesh; do sshd -T -C "user=$u,host=x,addr=127.0.0.1" | sort > "$OPS/sshd-avant-$u.txt"; done
useradd --system --shell /bin/sh --home-dir /var/lib/ameesh-vm-remote --no-create-home --user-group "$L"
passwd -l "$L" >/dev/null; usermod -aG systemd-journal "$L"
[ "$(id -nG "$L" | tr ' ' '\n' | sort | tr '\n' ' ')" = "systemd-journal $L " ] || { echo "groupes inattendus : $(id -nG "$L")"; exit 1; }
id "$L"
EOS

# 2. code + venv root non inscriptibles, au SHA M ; vm-remote.py tel que fusionné ; checks/ de #6296
TAR=$(mktemp "${TMPDIR:-/tmp}/6002-src.XXXXXX"); trap 'rm -f "$TAR"' EXIT
git -C "$SRC" archive --format=tar --prefix="src-${M:0:12}/" "$M" > "$TAR"
scp -q -F none -o BatchMode=yes "$TAR" "root@$AG3:$OPS/src.tar"
scp -q -F none -o BatchMode=yes "$ICI/secret-vm-probe-v41.py" "$ICI/acl-pose-v42.sh" "$ICI/acl-retour-v44.sh" "root@$AG3:$OPS/"
R "$M" "$OPS" <<'EOS'
set -euo pipefail; SHA=$1; OPS=$2; S12=${SHA:0:12}; O=/opt/ameesh-vm-adapter
install -d -m 755 -o root -g root "$O"; tar -C "$O" -xf "$OPS/src.tar"
cd "$O"; python3.12 -m venv "venv-$S12"; "venv-$S12/bin/pip" install -q --no-compile "src-$S12" >/dev/null; ln -sfn "venv-$S12" venv
install -d -m 755 -o root -g root remote checks
install -m 644 -o root -g root "src-$S12/deploy/vm-adapter/remote/vm-remote.py" remote/vm-remote.py
install -m 644 -o root -g root "src-$S12/deploy/vm-adapter/remote/checks/health.py" checks/health.py
install -m 644 -o root -g root "src-$S12/deploy/mise-a-jour/venv-processus.py" checks/venv-processus.py
chown -R root:root "$O"; chmod -R go-w "$O"
ecrit=$(find "$O" -xdev \( -perm -g+w -o -perm -o+w \) ! -type l); [ -z "$ecrit" ] || { echo "inscriptible : $ecrit"; exit 1; }
nonroot=$(find "$O" -xdev ! -user root); [ -z "$nonroot" ] || { echo "non root : $nonroot"; exit 1; }
sha256sum remote/vm-remote.py checks/health.py checks/venv-processus.py
EOS

# 3. états : reçus du compte VM (0700) et reçu partagé de santé (ameesh:vm-operator 2750)
R "$LOGIN" <<'EOS'
set -euo pipefail; L=$1
install -d -m 700 -o "$L" -g "$L" /var/lib/ameesh-vm-remote
install -d -m 2750 -o ameesh -g "$L" /var/lib/ameesh-vm-health
stat -c '%a %U:%G %n' /var/lib/ameesh-vm-remote /var/lib/ameesh-vm-health
EOS

# 4. clé forcée root + Match sshd (modèle M : PermitListen none ajouté) ; contrôles comme v3
R "$LOGIN" "$OPS" "$PUB_B64" <<'EOS'
set -euo pipefail; L=$1; OPS=$2; PUB="ssh-ed25519 $3"
FORCE='/opt/ameesh-vm-adapter/venv/bin/python -I /opt/ameesh-vm-adapter/remote/vm-remote.py dispatch'
[ -d /etc/ssh/authorized_keys ] || touch "$OPS/authorized_keys-dir-cree"   # v4.2 : retiré au retour s'il est vide
install -d -m 755 -o root -g root /etc/ssh/authorized_keys
printf 'restrict,command="%s" %s vm-adapter\n' "$FORCE" "$PUB" > "/etc/ssh/authorized_keys/$L"; chown root:root "/etc/ssh/authorized_keys/$L"; chmod 644 "/etc/ssh/authorized_keys/$L"
FRAG="$OPS/55-ameesh-vm-operator.conf"
cat > "$FRAG" <<CONF
# #6002 : compte VM non root de l'adaptateur (deploy/vm-adapter/remote/sshd-match.example à 97285a07).
# PermitUserEnvironment reste global (« no », relevé avant pose) : interdit dans Match.
Match User $L
    AuthorizedKeysFile /etc/ssh/authorized_keys/$L
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PermitTTY no
    AllowTcpForwarding no
    AllowAgentForwarding no
    AllowStreamLocalForwarding no
    PermitTunnel no
    PermitListen none
    X11Forwarding no
    ForceCommand $FORCE
Match all
CONF
TEST="$OPS/sshd-test.conf"; { cat /etc/ssh/sshd_config; echo; cat "$FRAG"; } > "$TEST"
sshd -t -f "$TEST" || { echo "fragment sshd invalide, rien installé"; exit 1; }
install -m 644 -o root -g root "$FRAG" /etc/ssh/sshd_config.d/55-ameesh-vm-operator.conf
sshd -t || { mv /etc/ssh/sshd_config.d/55-ameesh-vm-operator.conf "$OPS/55-refuse.conf"; echo "sshd -t en échec : drop-in retiré, rien rechargé"; exit 1; }
for u in root ameesh; do sshd -T -C "user=$u,host=x,addr=127.0.0.1" | sort | cmp -s - "$OPS/sshd-avant-$u.txt" || { echo "ECART sshd -T $u"; exit 1; }; done
eff=$(sshd -T -C "user=$L,host=localhost,addr=127.0.0.1")
for kv in "permituserenvironment no" "allowagentforwarding no" "allowstreamlocalforwarding no" "allowtcpforwarding no" "permittunnel no" "permitlisten none" "permittty no"; do
  printf '%s\n' "$eff" | grep -qx "$kv" || { echo "sshd -T $L : « $kv » absent"; exit 1; }; done
echo "sshd -T $L conforme au README (étape 4)"
EOS
ssh -F none -o BatchMode=yes "root@$AG3" sleep 60 & HOLD=$!; sleep 2
R <<'EOS'
set -euo pipefail; systemctl reload ssh; systemctl is-active ssh
EOS
R <<<'true' && echo "root@AG3 OK après reload"; kill "$HOLD" 2>/dev/null || true

# 5. sudoers (v3, inchangé)
R "$LOGIN" "$OPS" <<'EOS'
set -euo pipefail; L=$1; OPS=$2; F="$OPS/ameesh-vm-operator.sudoers"
printf '%s\n' '# #6002 : arrêt de cette VM seulement' "$L ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff --no-block" > "$F"
visudo -cf "$F"; install -m 440 -o root -g root "$F" /etc/sudoers.d/ameesh-vm-operator; visudo -c >/dev/null
sudo -l -U "$L" | grep -F '(root) NOPASSWD: /usr/bin/systemctl poweroff --no-block'
EOS

# 6. cartes root (README étapes 2 et 6) : remote.json, connexion DB, lecteur 0640 root:vm-operator ;
#    producteur 0644 root:root. Secret vm_probe généré ICI : passfile 0600 vm-operator, vérificateur
#    SCRAM root 0600 dans $OPS (seul ce vérificateur quitte AG3, vers pose-DB-vm_probe.sh).
R "$LOGIN" "$OPS" "$HOTE" "$PROBE" <<'EOS'
set -euo pipefail; L=$1; OPS=$2; HOTE=$3; PROBE=$4; E=/etc/ameesh-vm-adapter
install -d -m 755 -o root -g root "$E"
UNITS=$(systemctl --user -M ameesh@ list-units 'ameesh-runner-agent@*' --state=active --no-legend | awk '{print $1}' | sort)
[ -n "$UNITS" ] || { echo "aucune unité runner active"; exit 1; }
python3 -I - "$E" "$HOTE" "$PROBE" "$(id -u ameesh)" $UNITS <<'PY'
import json, os, sys
e, hote, probe, uid, units = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5:]
venv = '/home/ameesh/.local/share/ameesh/venv'
docs = {
 'remote.json': (0o640, {'host': hote, 'probe_agent': probe,
     'ameesh_config': e + '/remote-database.json', 'state_dir': '/var/lib/ameesh-vm-remote',
     'fresh_seconds': 60, 'reconcile_deadline_s': 900,
     'checks': {k: ['/usr/bin/python3.12', '-I', '/opt/ameesh-vm-adapter/checks/health.py', k]
                for k in ('version-mode', 'doctor')}}),
 'remote-database.json': (0o640, {'host': hote, 'schema': 'public',
     'dsn': 'host=10.77.0.1 port=5432 dbname=ameesh user=vm_probe sslmode=require '
            'passfile=/var/lib/ameesh-vm-remote/vm_probe.pgpass'}),
 'health-reader.json': (0o640, {'receipt': '/var/lib/ameesh-vm-health/health.json',
     'producer_uid': uid, 'period_s': 60, 'venv': venv}),
 'health-producer.json': (0o644, {'venv': venv, 'verifier': '/opt/ameesh-vm-adapter/checks/venv-processus.py',
     'units': units, 'environment_file': '/home/ameesh/.config/ameesh/env',
     'receipt': '/var/lib/ameesh-vm-health/health.json'}),
}
for name, (mode, value) in docs.items():
    path = os.path.join(e, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, indent=2, sort_keys=True); f.write('\n')
print('cartes écrites :', ', '.join(docs))
PY
chown root:"$L" "$E/remote.json" "$E/remote-database.json" "$E/health-reader.json"; chmod 640 "$E/remote.json" "$E/remote-database.json" "$E/health-reader.json"
chown root:root "$E/health-producer.json"; chmod 644 "$E/health-producer.json"
python3 -I "$OPS/secret-vm-probe-v41.py" /var/lib/ameesh-vm-remote/vm_probe.pgpass "$OPS/vm_probe.scram" "$(id -u "$L")" "$(id -g "$L")"
stat -c '%a %U:%G %n' "$E"/* /var/lib/ameesh-vm-remote/vm_probe.pgpass "$OPS/vm_probe.scram"
EOS

# 6 bis. accès du lecteur à la cible de venv-courant (README étape 6 ; pilote #200777 O1) :
#   acl-pose-v42.sh (mêmes octets que le témoin en conteneur) ; preuves liste refusée / python exécuté.
R "$LOGIN" "$OPS" <<'EOS'
set -euo pipefail; L=$1; OPS=$2
bash "$OPS/acl-pose-v42.sh" "$OPS" "$L"
if runuser -u "$L" -- ls /home/ameesh >/dev/null 2>&1; then echo "vm-operator LISTE /home/ameesh"; exit 1; fi
runuser -u "$L" -- /home/ameesh/.local/share/ameesh/venv-courant/bin/python -I -c 'import sys; print("python de venv-courant exécuté :", sys.prefix)'
EOS

# 7. unités utilisateur d'ameesh (modèles de M), minuteur activé ; reçu frais attendu dans 70 s
R "$M" "$OPS" <<'EOS'
set -euo pipefail; SHA=$1; OPS=$2; U=/home/ameesh/.config/systemd/user; S=/opt/ameesh-vm-adapter/src-${SHA:0:12}/deploy/vm-adapter/remote/checks
for f in ameesh-vm-health.service ameesh-vm-health.timer; do install -m 644 -o ameesh -g ameesh "$S/$f" "$U/$f"; done
systemctl --user -M ameesh@ daemon-reload; systemctl --user -M ameesh@ enable --now ameesh-vm-health.timer
sleep 70; stat -c '%a %U:%G %y %n' /var/lib/ameesh-vm-health/health.json
EOS
echo "pose AG3 v4 terminée ($OPS). Suite : pose-DB-vm_probe-v42.sh --apply $TS, puis tests-AG3-vm-operator-v42.sh B."
echo "TS=$TS"
