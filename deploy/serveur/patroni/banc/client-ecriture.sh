#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Écriture continue du poste client du banc (lot #6902). Le conteneur « pc »
# joue le poste de l'owner : il découvre le primaire, écrit une ligne à la
# fois et journalise chaque identifiant confirmé. Le pilote du banc lit ce
# journal pour vérifier qu'aucune écriture confirmée ne manque après une
# bascule (RPO 0).
#
#   client-ecriture.sh /etat/ecritures.jsonl
#
# Variables : PGPASSWORD (secret du superutilisateur, fourni par banc.sh),
# BANC_NOEUDS (noms séparés par des virgules).
set -uo pipefail

SORTIE=${1:-/etat/ecritures.jsonl}
NOEUDS=${BANC_NOEUDS:-banc-1,banc-2,banc-3}
: "${PGPASSWORD:?PGPASSWORD manquant}"

mkdir -p -- "$(dirname -- "$SORTIE")"
: > "$SORTIE"

conn() {
    local hote=$1
    printf 'host=%s port=5432 user=postgres dbname=postgres sslmode=require connect_timeout=2' "$hote"
}

primaire() {
    local hote=$1
    psql "$(conn "$hote")" -X -q -tA -c \
        'SELECT NOT pg_is_in_recovery()' 2>/dev/null | grep -qx t
}

ecrire() {
    local hote=$1 id
    id=$(psql "$(conn "$hote")" -X -q -tA -v ON_ERROR_STOP=1 -c \
        "INSERT INTO banc_ecritures(noeud) VALUES ('${hote}') RETURNING id" 2>/dev/null) || return 1
    [[ $id =~ ^[0-9]+$ ]] || return 1
    printf '{"ts":%s,"noeud":"%s","id":%s}\n' "$(date +%s%N)" "$hote" "$id" >> "$SORTIE"
}

while true; do
    for hote in ${NOEUDS//,/ }; do
        if primaire "$hote"; then
            while ecrire "$hote"; do
                sleep 0.05
            done
        fi
    done
    sleep 0.2
done
