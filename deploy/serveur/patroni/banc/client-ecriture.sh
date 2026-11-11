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
# Ne jamais tronquer : le journal des identifiants confirmés survit au
# redémarrage du poste et sert de preuve RPO (lot #6902). Seul
# `banc.sh reinitialiser` recrée $BANC_ETAT/client entre deux scénarios.
touch -- "$SORTIE"

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
    local hote=$1 id jeton
    # Jeton unique par tentative : l'identité d'une confirmation est le couple
    # (id, jeton), pas le seul id — une branche divergente peut réattribuer un
    # id, et la preuve RPO doit alors voir une substitution, pas une présence.
    jeton=$(cat /proc/sys/kernel/random/uuid) || return 1
    id=$(psql "$(conn "$hote")" -X -q -tA -v ON_ERROR_STOP=1 -c \
        "INSERT INTO banc_ecritures(noeud,jeton) VALUES ('${hote}','${jeton}') RETURNING id" 2>/dev/null) || return 1
    [[ $id =~ ^[0-9]+$ ]] || return 1
    printf '{"ts":%s,"noeud":"%s","id":%s,"jeton":"%s"}\n' \
        "$(date +%s%N)" "$hote" "$id" "$jeton" >> "$SORTIE"
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
