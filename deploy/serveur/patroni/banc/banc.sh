#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Banc local jetable de la grappe PostgreSQL du mesh (lot #6902, EP8.6b).
#
#   banc.sh monter [--etat DOSSIER] [--sans-construire]
#   banc.sh etat
#   banc.sh reinitialiser
#   banc.sh couper NOEUD | retablir NOEUD
#   banc.sh tuer NOEUD | demarrer NOEUD
#   banc.sh arreter-pc | demarrer-pc
#   banc.sh journal NOEUD [LIGNES]
#   banc.sh versions
#   banc.sh demonter [--garder]
#   banc.sh verifier-marqueur   (interne : 0 si le dossier est un état du banc)
#
# Trois conteneurs portent chacun etcd + Patroni + PostgreSQL 17, comme les
# trois hôtes de l'étude ; un quatrième conteneur joue le poste client. Tout
# l'état (configurations rendues par #6870, matériel TLS, données, journaux)
# vit sous $BANC_ETAT, par défaut $TMPDIR/ameesh-banc-grappe. Aucune adresse
# du réseau privé du mesh n'entre dans ce banc.
set -euo pipefail

ICI=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ETAT=${BANC_ETAT:-${TMPDIR:-/tmp}/ameesh-banc-grappe}
IMAGE=${BANC_IMAGE:-ameesh-banc-grappe:6902}
TOPOLOGIE=${BANC_TOPOLOGIE:-$ICI/topologie-banc.json}
DELAI_SAIN=${BANC_DELAI_SAIN:-180}
DELAI_SYNC=${BANC_DELAI_SYNC:-90}
DELAI_ETCD=${BANC_DELAI_ETCD:-60}

log() { printf 'banc : %s\n' "$*" >&2; }
die() { log "$*"; exit 1; }

utiliser() {
    cat >&2 <<'TXT'
usage: banc.sh COMMANDE [options]

  monter [--etat DOSSIER] [--sans-construire]
  etat
  reinitialiser
  couper NOEUD | retablir NOEUD
  tuer NOEUD | demarrer NOEUD
  arreter-pc | demarrer-pc
  journal NOEUD [LIGNES]
  versions
  demonter [--garder]
  verifier-marqueur   (interne)

Variables : BANC_ETAT (défaut $TMPDIR/ameesh-banc-grappe), BANC_IMAGE,
BANC_TOPOLOGIE, BANC_DELAI_SAIN, BANC_DELAI_SYNC.
TXT
    exit 2
}

exiger_docker() {
    command -v docker >/dev/null || die "docker requis"
    docker info >/dev/null 2>&1 || die "démon docker injoignable"
}

# ---------------------------------------------------------------------------
# Topologie : une seule source, lue par les commandes qui en ont besoin.

lire_topologie() {
    [[ -r $TOPOLOGIE ]] || die "topologie illisible : $TOPOLOGIE"
    NOMS=()
    ADRESSES=()
    local ligne nom adresse
    while IFS=$'\t' read -r nom adresse; do
        [[ -n $nom ]] || continue
        NOMS+=("$nom")
        ADRESSES+=("$adresse")
    done < <(python3 - "$TOPOLOGIE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as flux:
    topologie = json.load(flux)
for noeud in topologie["noeuds"]:
    print("%s\t%s" % (noeud["nom"], noeud["adresse"]))
PY
    )
    (( ${#NOMS[@]} == 3 )) || die "trois nœuds attendus dans $TOPOLOGIE"
    RESEAU=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["reseau"])' "$TOPOLOGIE")
    ADRESSE_PC=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["adresse_pc"])' "$TOPOLOGIE")
}

# Nom de projet Docker dérivé du dossier d'état : deux bancs simultanés sur le
# même hôte ne se marchent pas dessus, sans variable globale à oublier.
nom_projet() {
    local base
    base=$(basename -- "$ETAT" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-')
    base=${base#-}
    base=${base%-}
    base=${base:0:24}
    [[ -n $base ]] || base=banc
    printf 'ameesh-banc-%s' "$base"
}

# ---------------------------------------------------------------------------
# Démarrage.

demarrer_noeuds() {
    local i nom adresse donnees etcd
    install -d -m 0750 "$ETAT/client" "$ETAT/logs"
    for i in "${!NOMS[@]}"; do
        nom=${NOMS[$i]}
        adresse=${ADRESSES[$i]}
        # Données sous $BANC_ETAT (« tout dans $TMPDIR ») et non en volumes
        # Docker nommés : le nettoyage reste dans le dossier d'état. L'entrée
        # du conteneur, en root, met le répertoire PostgreSQL au nom de
        # postgres ; etcd reste en root.
        donnees="$ETAT/donnees/$nom"
        etcd="$ETAT/etcd/$nom"
        install -d -m 0750 "$donnees" "$etcd"
        docker run -d \
            --name "$PROJET-$nom" \
            --hostname "$nom" \
            --label "ameesh.banc=6902" \
            --label "ameesh.banc.etat=$ETAT" \
            --network "$NOM_RESEAU" \
            --ip "$adresse" \
            --cap-add NET_ADMIN \
            --stop-timeout 20 \
            -e "BANC_NOEUD=$nom" \
            -e "BANC_ADRESSE=$adresse" \
            -v "$ETAT/config/$nom:/banc-ro/config:ro" \
            -v "$ETAT/tls/$nom:/banc-ro/tls:ro" \
            -v "$ETAT/tls/ca.crt:/banc-ro/ca.crt:ro" \
            -v "$donnees:/var/lib/postgresql/17/main" \
            -v "$etcd:/var/lib/etcd/ameesh" \
            "$IMAGE" >/dev/null
    done
}

role_noeud() {
    docker exec -u postgres "$PROJET-$1" \
        psql -X -q -tA -c 'SELECT pg_is_in_recovery()' 2>/dev/null || true
}

# Attend un primaire, deux standbys et un standby synchrone déclaré. Affiche
# le nom du primaire.
attendre_sain() {
    local i nom primaire="" prim stb sync r
    for ((i = 0; i < DELAI_SAIN; i++)); do
        primaire=""
        prim=0
        stb=0
        for nom in "${NOMS[@]}"; do
            r=$(role_noeud "$nom")
            case $r in
                f) prim=$((prim + 1)); primaire=$nom ;;
                t) stb=$((stb + 1)) ;;
            esac
        done
        if (( prim == 1 && stb == 2 )); then
            sync=$(docker exec -u postgres "$PROJET-$primaire" \
                psql -X -q -tA -c 'SHOW synchronous_standby_names' 2>/dev/null || true)
            if [[ -n $sync ]]; then
                printf '%s\n' "$primaire"
                return 0
            fi
        fi
        sleep 1
    done
    journal_grappe >&2 || true
    die "grappe non saine après ${DELAI_SAIN}s (primaire=${primaire:-aucun})"
}

journal_grappe() {
    local nom
    for nom in "${NOMS[@]}"; do
        printf '\n--- %s ---\n' "$nom" >&2
        docker logs --tail 30 "$PROJET-$nom" 2>&1 || true
    done
    printf '\n--- conteneurs ---\n' >&2
    docker ps -a --filter "label=ameesh.banc=6902" --format '{{.Names}} {{.Status}}' >&2 || true
}

initialiser_schema() {
    local primaire=$1
    docker exec -u postgres "$PROJET-$primaire" \
        psql -X -q -v ON_ERROR_STOP=1 -c '
            CREATE TABLE IF NOT EXISTS banc_jalons (
                id bigserial PRIMARY KEY,
                scenario text NOT NULL,
                cree_le timestamptz NOT NULL DEFAULT clock_timestamp()
            );
            CREATE TABLE IF NOT EXISTS banc_ecritures (
                id bigserial PRIMARY KEY,
                noeud text NOT NULL,
                cree_le timestamptz NOT NULL DEFAULT clock_timestamp()
            );
        ' >/dev/null
}

# Écart constaté à l'exécution (11/11/2026) : generer-configuration.py (#6870)
# écrit etcd3.hosts avec le schéma (« https://hôte:2379 »), alors que Patroni 4.x
# attend « hôte:port » et applique protocol: https. Avec le schéma, Patroni
# refuse de démarrer (ValueError: Invalid IPv6 URL). Le banc corrige sa copie
# avec corriger-configuration.py, consigne le constat dans constats.json et le
# rapport le cite : la correction doit partir dans un lot correctif de #6870
# avant EP8.6, pas être masquée par le banc.
demarrer_pc() {
    # Le poste client reçoit le mot de passe par un fichier d'environnement
    # 0600 : il n'apparaît ni dans la ligne de commande ni dans les journaux.
    local noeuds_csv
    noeuds_csv=$(IFS=,; printf '%s' "${NOMS[*]}")
    # shellcheck disable=SC1091
    PATRONI_SUPERUSER_PASSWORD=$(
        python3 - "$ETAT/tls/secrets.env" <<'PY'
import sys

for ligne in open(sys.argv[1], encoding="utf-8"):
    if ligne.startswith("PATRONI_SUPERUSER_PASSWORD="):
        print(ligne.split("=", 1)[1].strip())
        break
PY
    )
    [[ -n $PATRONI_SUPERUSER_PASSWORD ]] || die "mot de passe superutilisateur absent des secrets"
    (
        umask 077
        printf 'PGPASSWORD=%s\nBANC_NOEUDS=%s\n' \
            "$PATRONI_SUPERUSER_PASSWORD" "$noeuds_csv" > "$ETAT/client.env"
    )
    docker rm -f "$PROJET-pc" >/dev/null 2>&1 || true
    docker run -d \
        --name "$PROJET-pc" \
        --hostname "pc-banc" \
        --label "ameesh.banc=6902" \
        --label "ameesh.banc.etat=$ETAT" \
        --network "$NOM_RESEAU" \
        --ip "$ADRESSE_PC" \
        --env-file "$ETAT/client.env" \
        --entrypoint /bin/bash \
        -v "$ETAT/client:/etat" \
        "$IMAGE" /usr/local/bin/client-ecriture.sh /etat/ecritures.jsonl >/dev/null
}

# ---------------------------------------------------------------------------
# Commandes.

cmd_monter() {
    local sans_construire=0
    while (($#)); do
        case $1 in
            --sans-construire) sans_construire=1; shift ;;
            --etat) [[ $# -ge 2 ]] || utiliser; ETAT=$2; shift 2 ;;
            -h | --help) utiliser ;;
            *) die "option inconnue : $1" ;;
        esac
    done
    [[ ! -e $ETAT/etat.json ]] || die "banc déjà monté dans $ETAT (demonter d'abord)"
    # Ne jamais écrire dans un dossier étranger : un --etat non vide sans
    # marqueur du banc est refusé (l'opérateur le vide ou choisit un autre
    # dossier ; le banc ne supprime pas de données qu'il n'a pas créées).
    if [[ -e $ETAT ]] && ! marqueur_valide; then
        [[ -d $ETAT && -z $(ls -A -- "$ETAT") ]] || die \
            "refus : $ETAT n'est pas vide et ne porte pas le marqueur du banc ; choisir un dossier vide ou le vider à la main"
    fi
    exiger_docker
    TOPOLOGIE=${BANC_TOPOLOGIE:-$ICI/topologie-banc.json}
    lire_topologie
    PROJET=$(nom_projet)
    NOM_RESEAU="$PROJET-reseau"

    install -d -m 0750 "$ETAT"
    cp -- "$TOPOLOGIE" "$ETAT/topologie.json"
    TOPOLOGIE="$ETAT/topologie.json"

    if (( sans_construire == 0 )); then
        log "construction de $IMAGE"
        docker build -t "$IMAGE" "$ICI" >&2
    fi

    log "matériel TLS et configurations (hors dépôt, sous $ETAT)"
    "$ICI/../generer-tls.sh" --sortie "$ETAT/tls" --topologie "$TOPOLOGIE" >&2
    "$ICI/../generer-configuration.py" \
        --topologie "$TOPOLOGIE" \
        --secrets "$ETAT/tls/secrets.env" \
        --sortie "$ETAT/config" \
        --tous --verifier >&2
    "$ICI/corriger-configuration.py" --dossier "$ETAT" >&2

    if docker network inspect "$NOM_RESEAU" >/dev/null 2>&1; then
        die "réseau Docker déjà présent : $NOM_RESEAU"
    fi
    docker network create \
        --driver bridge \
        --subnet "$RESEAU" \
        --label "ameesh.banc=6902" \
        --label "ameesh.banc.etat=$ETAT" \
        "$NOM_RESEAU" >/dev/null

    demarrer_noeuds
    local primaire
    primaire=$(attendre_sain)
    initialiser_schema "$primaire"
    demarrer_pc

    python3 - "$ETAT" "$PROJET" "$NOM_RESEAU" "$IMAGE" "$TOPOLOGIE" <<'PY'
import json
import sys
import time

etat, projet, reseau, image, topologie = sys.argv[1:6]
with open(topologie, encoding="utf-8") as flux:
    topo = json.load(flux)
with open(etat + "/etat.json", "w", encoding="utf-8") as flux:
    json.dump({
        "banc": "ameesh-banc-grappe/1",
        "lot": 6902,
        "cree": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "projet": projet,
        "reseau": reseau,
        "image": image,
        "topologie": topo,
        "conteneurs": {n["nom"]: projet + "-" + n["nom"] for n in topo["noeuds"]},
        "conteneur_pc": projet + "-pc",
    }, flux, ensure_ascii=False, indent=2)
    flux.write("\n")
PY
    log "banc prêt : primaire $primaire, réseau $NOM_RESEAU"
    printf '%s\n' "$primaire" > "$ETAT/primaire-initial"
}

# Marqueur du banc : seul un dossier d'état portant ce fichier (schéma du lot
# et projet dérivé du chemin) peut voir ses fichiers supprimés. Un dossier
# quelconque passé par --etat n'est jamais effacé, même par demonter.
marqueur_valide() {
    [[ -r $ETAT/etat.json ]] || return 1
    local projet
    projet=$(nom_projet)
    python3 - "$ETAT/etat.json" "$projet" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as flux:
        etat = json.load(flux)
except (OSError, ValueError):
    raise SystemExit(1)
if etat.get("banc") != "ameesh-banc-grappe/1" or etat.get("projet") != sys.argv[2]:
    raise SystemExit(1)
PY
}

# Les données PostgreSQL appartiennent à postgres (uid 999) dans le conteneur :
# un rm direct échouerait. On les supprime depuis l'image, en root, avec le
# dossier d'état monté ; --rm ne laisse aucun conteneur derrière. Le marqueur
# est vérifié avant, et seuls donnees/ et etcd/ sont touchés.
supprimer_donnees() {
    [[ -d $ETAT/donnees || -d $ETAT/etcd ]] || return 0
    marqueur_valide || die "refus : $ETAT n'est pas l'état de ce banc (marqueur absent) ; données conservées"
    docker run --rm \
        -v "$ETAT:/etat-banc" \
        --entrypoint /bin/bash \
        "$IMAGE" -c 'rm -rf /etat-banc/donnees /etat-banc/etcd' >/dev/null
}

# Supprime les fichiers que le banc a lui-même créés, jamais un fichier étranger
# (le dossier peut contenir autre chose : rmdir échoue alors sans le détruire).
supprimer_etat() {
    marqueur_valide || die "refus : $ETAT n'est pas l'état de ce banc (marqueur absent) ; rien supprimé"
    supprimer_donnees
    rm -rf -- "$ETAT/client" "$ETAT/logs" "$ETAT/config" "$ETAT/tls" \
        "$ETAT/etat.json" "$ETAT/topologie.json" "$ETAT/constats.json" \
        "$ETAT/primaire-initial" "$ETAT/client.env"
    rmdir -- "$ETAT" 2>/dev/null || true
}

cmd_reinitialiser() {
    charger_etat
    local nom
    for nom in "${NOMS[@]}"; do
        docker rm -f "$PROJET-$nom" >/dev/null 2>&1 || true
    done
    docker rm -f "$PROJET-pc" >/dev/null 2>&1 || true
    supprimer_donnees
    rm -rf -- "$ETAT/client"
    demarrer_noeuds
    local primaire
    primaire=$(attendre_sain)
    initialiser_schema "$primaire"
    demarrer_pc
    log "banc réinitialisé : primaire $primaire"
    printf '%s\n' "$primaire" > "$ETAT/primaire-initial"
}

cmd_etat() {
    charger_etat
    local nom role primaire="" sync
    for nom in "${NOMS[@]}"; do
        role=$(role_noeud "$nom")
        case $role in
            f) role=primaire; primaire=$nom ;;
            t) role=standby ;;
            *) role=injoignable ;;
        esac
        printf '%-8s %-12s %s\n' "$nom" "$role" \
            "$(docker inspect -f '{{.State.Status}}' "$PROJET-$nom" 2>/dev/null || echo absent)"
    done
    if [[ -n $primaire ]]; then
        sync=$(docker exec -u postgres "$PROJET-$primaire" \
            psql -X -q -tA -c 'SHOW synchronous_standby_names' 2>/dev/null || true)
        printf 'synchrone : %s\n' "${sync:-aucun}"
    fi
    printf 'pc        : %s\n' \
        "$(docker inspect -f '{{.State.Status}}' "$PROJET-pc" 2>/dev/null || echo absent)"
}

cmd_couper() {
    local nom=${1:-}
    [[ -n $nom ]] || utiliser
    charger_etat
    verifier_noeud "$nom"
    # La coupure est interne au conteneur : docker exec continue de répondre
    # (le démon passe par un canal hors réseau), l'isolement est réel pour
    # etcd, Patroni, PostgreSQL et les clients.
    docker exec -u root "$PROJET-$nom" iptables -I INPUT 1 ! -i lo -j DROP
    docker exec -u root "$PROJET-$nom" iptables -I OUTPUT 1 ! -o lo -j DROP
    log "nœud $nom coupé du réseau du banc"
}

cmd_retablir() {
    local nom=${1:-}
    [[ -n $nom ]] || utiliser
    charger_etat
    verifier_noeud "$nom"
    docker exec -u root "$PROJET-$nom" iptables -D INPUT 1 2>/dev/null || true
    docker exec -u root "$PROJET-$nom" iptables -D OUTPUT 1 2>/dev/null || true
    log "nœud $nom rétabli sur le réseau du banc"
}

cmd_tuer() {
    local nom=${1:-}
    [[ -n $nom ]] || utiliser
    charger_etat
    verifier_noeud "$nom"
    docker kill "$PROJET-$nom" >/dev/null
    log "nœud $nom tué (SIGKILL)"
}

cmd_demarrer() {
    local nom=${1:-}
    [[ -n $nom ]] || utiliser
    charger_etat
    verifier_noeud "$nom"
    docker start "$PROJET-$nom" >/dev/null
    log "nœud $nom redémarré"
}

cmd_arreter_pc() {
    charger_etat
    docker stop "$PROJET-pc" >/dev/null
    log "poste client arrêté"
}

cmd_demarrer_pc() {
    charger_etat
    docker start "$PROJET-pc" >/dev/null
    log "poste client redémarré"
}

cmd_journal() {
    local nom=${1:-} lignes=${2:-200}
    [[ -n $nom ]] || utiliser
    charger_etat
    verifier_noeud "$nom"
    docker logs --timestamps --tail "$lignes" "$PROJET-$nom" 2>&1
}

cmd_versions() {
    docker run --rm --entrypoint /bin/bash "$IMAGE" -c \
        'postgres --version; /opt/patroni/venv/bin/patroni --version; etcd --version | head -1; psql --version'
}

cmd_demonter() {
    local garder=0
    while (($#)); do
        case $1 in
            --garder) garder=1; shift ;;
            *) die "option inconnue : $1" ;;
        esac
    done
    exiger_docker
    # Nettoyage par étiquette : il fonctionne même si monter a échoué avant
    # d'écrire etat.json, et ne touche jamais un autre banc du même hôte.
    local -a conteneurs reseaux
    mapfile -t conteneurs < <(docker ps -aq --filter "label=ameesh.banc.etat=$ETAT")
    ((${#conteneurs[@]} == 0)) || docker rm -f "${conteneurs[@]}" >/dev/null
    mapfile -t reseaux < <(docker network ls -q --filter "label=ameesh.banc.etat=$ETAT")
    ((${#reseaux[@]} == 0)) || docker network rm "${reseaux[@]}" >/dev/null 2>&1 || true
    # Les données sont sous $ETAT et appartiennent en partie à postgres dans
    # le conteneur : le nettoyage passe par l'image, en root, et seulement si
    # le marqueur du banc autorise à toucher ce dossier.
    if marqueur_valide; then
        IMAGE=$(image_du_banc)
        if (( garder == 0 )); then
            supprimer_etat
            log "banc démonté, état supprimé"
        else
            supprimer_donnees
            log "banc démonté, état conservé dans $ETAT"
        fi
    else
        log "marqueur du banc absent : fichiers de $ETAT conservés (conteneurs et réseau étiquetés retirés)"
    fi
}

# Image du banc monté, lue dans etat.json : un BANC_IMAGE différent de celui du
# montage ne doit pas empêcher le nettoyage des données.
image_du_banc() {
    if [[ -r $ETAT/etat.json ]]; then
        python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("image") or "")' "$ETAT/etat.json"
    else
        printf '%s\n' "$IMAGE"
    fi
}

charger_etat() {
    [[ -r $ETAT/etat.json ]] || die "aucun banc dans $ETAT (monter d'abord)"
    marqueur_valide || die "refus : $ETAT/etat.json n'est pas l'état d'un banc #6902"
    PROJET=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["projet"])' "$ETAT/etat.json")
    NOM_RESEAU=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["reseau"])' "$ETAT/etat.json")
    IMAGE=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("image") or "")' "$ETAT/etat.json")
    [[ -n $IMAGE ]] || IMAGE=${BANC_IMAGE:-ameesh-banc-grappe:6902}
    TOPOLOGIE=$ETAT/topologie.json
    lire_topologie
}

verifier_noeud() {
    local nom
    for nom in "${NOMS[@]}"; do
        [[ $nom == "$1" ]] && return 0
    done
    die "nœud inconnu : $1 (connus : ${NOMS[*]})"
}

main() {
    (($#)) || utiliser
    local commande=$1
    shift
    case $commande in
        monter) cmd_monter "$@" ;;
        reinitialiser) cmd_reinitialiser "$@" ;;
        etat) cmd_etat "$@" ;;
        couper) cmd_couper "$@" ;;
        retablir) cmd_retablir "$@" ;;
        tuer) cmd_tuer "$@" ;;
        demarrer) cmd_demarrer "$@" ;;
        arreter-pc) cmd_arreter_pc "$@" ;;
        demarrer-pc) cmd_demarrer_pc "$@" ;;
        journal) cmd_journal "$@" ;;
        versions) cmd_versions "$@" ;;
        demonter) cmd_demonter "$@" ;;
        verifier-marqueur) marqueur_valide ;;
        -h | --help | help) utiliser ;;
        *) die "commande inconnue : $commande" ;;
    esac
}

main "$@"
