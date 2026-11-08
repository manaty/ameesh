#!/bin/bash
# #6291 — postgresql-client-17 (PGDG officiel) sur AG1 et AG2, modèle AG3 (déjà en 17.11-1.pgdg24.04+2).
# Lancé depuis bureau-goal (root SSH existant). Sans --apply : préalables seuls. Ni serveur, ni client 16 touchés.
set -euo pipefail
FPR=B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8          # clé « PostgreSQL Debian Repository » (apt.postgresql.org)
AG3=51.15.137.245; CIBLES=(78.232.56.22 78.232.60.1)
APPLY=0; [ "${1:-}" = --apply ] && APPLY=1
stop() { echo "STOP: $*" >&2; exit 2; }
KEY=$(mktemp "${TMPDIR:-/tmp}/pgdg.XXXXXX"); trap 'shred -u "$KEY" 2>/dev/null || true' EXIT
ssh -F none -o BatchMode=yes "root@$AG3" cat /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc > "$KEY"
got=$(gpg --show-keys --with-colons "$KEY" 2>/dev/null | awk -F: '$1=="fpr"{print $10; exit}'); [ "$got" = "$FPR" ] || stop "empreinte clé PGDG : $got"
echo "clé PGDG vérifiée : $FPR"
for h in "${CIBLES[@]}"; do
  ssh -F none -o BatchMode=yes "root@$h" bash -s <<'EOS' || stop "préalables $h"
set -euo pipefail; . /etc/os-release; [ "$VERSION_CODENAME" = noble ] || { echo "pas noble"; exit 1; }
! grep -rqs apt.postgresql.org /etc/apt/sources.list /etc/apt/sources.list.d/ || { echo "source PGDG déjà présente"; exit 1; }
{ [ ! -e /etc/apt/keyrings/pgdg.asc ] && [ ! -L /etc/apt/keyrings/pgdg.asc ]; } || { echo "clé déjà présente"; exit 1; }
! dpkg -s postgresql-client-17 >/dev/null 2>&1 || { echo "client 17 déjà installé"; exit 1; }
echo "$(hostname) : $(pg_dump --version) ; préalables OK"
EOS
done
[ $APPLY = 1 ] || { echo "(contrôle seul, aucun effet)"; exit 0; }
for h in "${CIBLES[@]}"; do
  scp -q -F none -o BatchMode=yes "$KEY" "root@$h:/root/pgdg-6291.asc"
  ssh -F none -o BatchMode=yes "root@$h" bash -s -- "$FPR" <<'EOS'
set -euo pipefail; FPR=$1; TS=$(date -u +%Y%m%dT%H%MZ); OPS=/root/ameesh-operator-6291-$TS; install -d -m 700 "$OPS"
dpkg -l | awk '/^ii/{print $2,$3}' > "$OPS/paquets-avant.txt"
got=$(gpg --show-keys --with-colons /root/pgdg-6291.asc 2>/dev/null | awk -F: '$1=="fpr"{print $10; exit}'); [ "$got" = "$FPR" ] || { echo "empreinte"; exit 1; }
install -d -m 755 /etc/apt/keyrings; install -m 644 -o root -g root /root/pgdg-6291.asc /etc/apt/keyrings/pgdg.asc; rm -f /root/pgdg-6291.asc
printf '%s\n' 'Types: deb' 'URIs: https://apt.postgresql.org/pub/repos/apt' 'Suites: noble-pgdg' 'Components: main' 'Signed-By: /etc/apt/keyrings/pgdg.asc' > /etc/apt/sources.list.d/pgdg.sources
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends postgresql-client-17 > "$OPS/install.log" 2>&1
dpkg -l | awk '/^ii/{print $2,$3}' > "$OPS/paquets-apres.txt"; diff "$OPS/paquets-avant.txt" "$OPS/paquets-apres.txt" | grep '^>' || true
dpkg -s postgresql-client-16 | grep -q 'Status: install ok installed'; ! dpkg -s postgresql-17 >/dev/null 2>&1
echo "$(hostname) : $(pg_dump --version) ; /usr/lib/postgresql/17/bin/pg_dump $(/usr/lib/postgresql/17/bin/pg_dump --version | awk '{print $3}') ; reçus $OPS"
EOS
done
