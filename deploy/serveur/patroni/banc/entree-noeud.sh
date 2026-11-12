#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Entrée d'un nœud jetable du banc (lot #6902). Le conteneur porte etcd puis
# Patroni, comme un hôte réel ; Patroni gère le PostgreSQL 17 local.
#
# Variables attendues (posées par banc.sh) :
#   BANC_NOEUD    nom du nœud, identique à celui de topologie.json
#   BANC_ADRESSE  adresse du nœud sur le réseau Docker du banc
#
# Les sources sont montées en lecture seule sous /banc-ro puis copiées dans
# l'image : les fichiers du dépôt et de $BANC_ETAT ne sont jamais modifiés,
# et un chown dans le conteneur ne bloque pas le nettoyage du banc.
set -euo pipefail

: "${BANC_NOEUD:?BANC_NOEUD manquant}"
: "${BANC_ADRESSE:?BANC_ADRESSE manquant}"

log() { printf 'banc/%s : %s\n' "$BANC_NOEUD" "$*" >&2; }

install -d -m 0750 /etc/patroni /etc/etcd /etc/ameesh/tls /var/lib/ameesh-db
cp /banc-ro/config/patroni.yml /etc/patroni/patroni.yml
cp /banc-ro/config/patroni.env /etc/ameesh/patroni.env
cp /banc-ro/config/etcd.yml /etc/etcd/etcd.yml
cp -a /banc-ro/tls/. /etc/patroni/tls/
cp -a /banc-ro/tls/. /etc/etcd/tls/
cp /banc-ro/ca.crt /etc/ameesh/tls/ca.crt

# Patroni tourne en postgres ; etcd en root. Les secrets et les clés restent
# hors du dépôt et sont rattachés au compte qui les lit dans le conteneur.
chown -R postgres:postgres /etc/patroni /etc/etcd /etc/ameesh /var/lib/ameesh-db
chmod 0600 /etc/ameesh/patroni.env
install -d -o postgres -g postgres -m 0700 /var/lib/postgresql/17/main
install -d -o postgres -g postgres -m 0750 /etc/postgresql/17/main

# Patroni renomme le postgresql.conf du répertoire de configuration en
# postgresql.base.conf avant d'écrire le sien (comportement Debian, où le
# paquet fournit ce fichier). Le banc n'installe pas le paquet : on pose le
# fichier vide attendu, sinon le bootstrap échoue sur un rename.
if [[ ! -e /etc/postgresql/17/main/postgresql.conf ]]; then
    : > /etc/postgresql/17/main/postgresql.conf
fi
chown -R postgres:postgres /etc/postgresql/17/main

# etcd : le membre local démarre en premier, Patroni attend qu'il réponde.
/usr/local/bin/etcd --config-file /etc/etcd/etcd.yml &
ETCD_PID=$!

for _ in $(seq 1 60); do
    if /usr/local/bin/etcdctl \
        --endpoints="https://${BANC_ADRESSE}:2379" \
        --cacert=/etc/ameesh/tls/ca.crt \
        --cert=/etc/patroni/tls/etcd-client.crt \
        --key=/etc/patroni/tls/etcd-client.key \
        endpoint health >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 "$ETCD_PID" 2>/dev/null; then
        log "etcd est mort avant le premier contrôle"
        wait "$ETCD_PID" || true
        exit 1
    fi
    sleep 1
done

# Patroni lit ses secrets par l'environnement (mêmes variables que l'unité
# systemd de #6870). Les AMEESH_DB_* servent au collecteur de santé, inutilisé
# par le banc mais conservés pour rester proche de la configuration réelle.
set -a
# shellcheck disable=SC1091
. /etc/ameesh/patroni.env
set +a

gosu postgres /opt/patroni/venv/bin/patroni /etc/patroni/patroni.yml &
PATRONI_PID=$!

arreter() {
    kill -TERM "$PATRONI_PID" "$ETCD_PID" 2>/dev/null || true
    wait "$PATRONI_PID" 2>/dev/null || true
    wait "$ETCD_PID" 2>/dev/null || true
}
trap arreter TERM INT

# Si l'un des deux meurt, le conteneur meurt : le pilote du banc voit un nœud
# arrêté au lieu d'un conteneur vivant avec un service silencieusement absent.
set +e
wait -n "$ETCD_PID" "$PATRONI_PID"
STATUT=$?
set -e
arreter
exit "$STATUT"
