#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Mode veille de la base du mesh (lot #8027).
#
#   mode-veille.sh entrer [--raison TEXTE] [--sans-sauvegarde RAISON]
#   mode-veille.sh sortir [--raison TEXTE]
#   mode-veille.sh etat
#
# Au repos, seul le hub reste allumé : il doit être le primaire. Avant que le
# déclencheur de veille (#8029) n'arrête ameesh-agents-1 et ameesh-agents-2,
# « entrer » vide synchronous_standby_names — sans quoi chaque COMMIT
# attendrait un standby éteint — puis attend la fin des tours et consigne
# l'état. Au réveil, « sortir » attend qu'une réplique soit en flux et ait
# rejoué le flush courant, puis rétablit la contrainte.
#
# Le script est idempotent et ne fait aucun geste si le primaire n'est pas le
# hub : dans ce cas le déclencheur doit d'abord ramener le primaire sur le hub
# (bascule planifiée), jamais arrêter les agents.
#
# Contrat avec le déclencheur : sortie 0 = le geste peut suivre (arrêt ou
# réveil des agents) ; sortie non nulle = ne pas arrêter les agents, l'état
# est consigné dans $AMEESH_REPLICATION_VEILLE (/etc/ameesh/mode-veille).
#
# Sauvegarde logique : « entrer » exige un dump de moins de 26 h dans
# /home/ameesh/sauvegardes (le minuteur ameesh-sauvegarde) ; --sans-sauvegarde
# RAISON est la dérogation d'exploitant, consignée. Aucun secret n'est lu pour
# ce contrôle ; replication.env (0600) ne sert qu'au fragment de configuration.
set -euo pipefail
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() {
  cat <<'EOF'
usage: mode-veille.sh entrer [--raison TEXTE] [--sans-sauvegarde RAISON]
       mode-veille.sh sortir [--raison TEXTE]
       mode-veille.sh etat

  entrer   vide synchronous_standby_names sur le primaire (le hub), attend la
           fin des tours (aucun bail vivant) et consigne l'état. Exige une
           sauvegarde logique de moins de 26 h ; --sans-sauvegarde RAISON
           déroge, la raison est consignée.
  sortir   attend qu'au moins une réplique synchrone soit en flux et ait
           rejoué le flush courant, rétablit ANY 1, recharge et consigne.
  etat     lit le marqueur local (aucun geste, aucun secret).

Sortie 0 si le geste peut suivre (arrêt ou réveil des agents), non nulle
sinon. Le primaire doit être le hub : ailleurs, le script refuse.
EOF
}

action=${1:-}
(( $# )) && shift
raison=""
sans_sauvegarde=""
while (( $# )); do
  case $1 in
    --raison)
      shift
      (( $# )) || die "--raison attend un texte"
      raison=$1
      ;;
    --sans-sauvegarde)
      shift
      (( $# )) || die "--sans-sauvegarde attend une raison"
      sans_sauvegarde=$1
      ;;
    -h | --help) usage; exit 0 ;;
    --) shift; break ;;
    -*) die "option inconnue : $1 (voir --help)" ;;
    *) die "argument inattendu : $1 (voir --help)" ;;
  esac
  shift
done
[[ -z $raison ]] || raison=${raison//$'\n'/ }
[[ -z $sans_sauvegarde ]] || sans_sauvegarde=${sans_sauvegarde//$'\n'/ }

case $action in
  etat)
    if mode_veille_actif; then
      printf 'mode=veille\n'
      sed -e '/^mode=/d' -- "$VEILLE_ETAT"
    else
      printf 'mode=actif\n'
    fi
    exit 0
    ;;
  entrer | sortir) ;;
  *) usage >&2; exit 2 ;;
esac

charger_env
lire_noeuds
trouver_primaire
charger_noeud_local

veille=0
mode_veille_actif && veille=1
sync_avant=""
sync_apres=""
sauvegarde=""
raison_etat=$raison
marqueur_pose=0
nouvelle_entree=0
resultat_echec=""
tours_echec="?"

maintenant() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Écrit l'état clé=valeur atomiquement (0644 : lisible par le déclencheur et
# les opérateurs, sans secret). Le marqueur « mode=veille » force la liste
# vide dans le fragment (lib.sh).
ecrire_etat() {
  local mode=$1
  shift
  local dossier tmp
  dossier=$(dirname -- "$VEILLE_ETAT")
  [[ -d $dossier ]] || install -d -m 0755 -- "$dossier"
  tmp=$(mktemp "$dossier/.mode-veille.XXXXXX") || die "temporaire impossible dans $dossier"
  {
    printf 'mode=%s\n' "$mode"
    printf '%s\n' "$@"
  } > "$tmp"
  chmod 0644 -- "$tmp"
  if (( EUID == 0 )); then
    chown root:root -- "$tmp" || die "chown root impossible sur $tmp"
  fi
  mv -f -- "$tmp" "$VEILLE_ETAT"
}

consigner() {
  local mode=$1 tours=$2 resultat=$3
  ecrire_etat "$mode" \
    "at=$(maintenant)" \
    "noeud=$NOM_LOCAL" \
    "primaire=$PRIMAIRE_NOM" \
    "sync_avant=${sync_avant:-}" \
    "sync_apres=${sync_apres:-}" \
    "sauvegarde=${sauvegarde:-}" \
    "tours=${tours}" \
    "resultat=${resultat}" \
    "raison=${raison_etat:-}"
}

# Annule une entrée fraîche qui a échoué avant que le déclencheur n'arrête
# les agents : sans cela le marqueur resterait posé et la liste synchrone
# vide, dégradant la durabilité sans échéance. On rétablit donc la liste si
# un candidat est en flux, on retire le marqueur et on consigne l'échec en
# mode=actif (aucun forçage de liste vide). Une entrée déjà en veille n'est
# jamais défaite par ce retour arrière : l'état de veille existant prime.
annuler_entree() {
  set +e
  local resultat=${resultat_echec:-echec-entree}
  (( marqueur_pose && nouvelle_entree )) || return 0
  retirer_mode_veille
  ecrire_fichier "$CONF_FRAGMENT" 0644 "$(fragment_conf)"
  pg_ctlcluster "$PG_VERSION" main reload
  sync_apres=$(psql_local -tAc 'SHOW synchronous_standby_names' 2>/dev/null || true)
  consigner actif "$tours_echec" "$resultat"
  log "entrée annulée : marqueur retiré, liste synchrone rétablie (${sync_apres:-vide}) ; agents à ne pas arrêter"
}

trap 'code=$?; if (( code != 0 )); then annuler_entree; fi; exit "$code"' EXIT

# Le primaire doit être le hub : la veille n'a de sens que sur la VM qui reste
# allumée. Un primaire sur un agent est une bascule de secours, pas un état de
# veille ; on refuse avant tout geste.
exiger_primaire() {
  [[ $NOM_LOCAL == "$PRIMAIRE_NOM" ]] \
    || die "le primaire d'inventaire est $PRIMAIRE_NOM, pas $NOM_LOCAL : la veille exige le primaire sur le hub (ramener le primaire par une bascule planifiée avant d'arrêter les agents)"
  pg_isready -q 2>/dev/null \
    || die "PostgreSQL ne répond pas sur $NOM_LOCAL : veille refusée"
  role=$(role_effectif)
  [[ $role == primaire ]] \
    || die "$NOM_LOCAL est $role, pas primaire : veille refusée (le primaire doit être le hub)"
}

# Dernier dump logique assez récent, sans pipeline (les noms peuvent contenir
# des espaces). Retourne 1 si aucun : l'appelant refuse ou déroge.
trouver_sauvegarde() {
  local fichier="" dernier="" recent=0 horodatage maintenant_s
  [[ -d $SAUVEGARDES_DIR ]] || return 1
  for fichier in "$SAUVEGARDES_DIR"/ameesh-*.dump; do
    [[ -f $fichier ]] || continue
    horodatage=$(stat -c '%Y' -- "$fichier" 2>/dev/null) || continue
    [[ $horodatage =~ ^[0-9]+$ ]] || continue
    if (( horodatage > recent )); then
      recent=$horodatage
      dernier=$fichier
    fi
  done
  (( recent > 0 )) || return 1
  maintenant_s=$(date +%s)
  (( maintenant_s - recent <= SAUVEGARDE_MAX_AGE_S )) || return 1
  printf '%s' "$dernier"
}

# Attend qu'aucun bail de tour ne soit vivant ; imprime le dernier compte.
attendre_tours() {
  local essais=$(( ATTENTE_TOURS / 2 )) i compte=0
  (( essais > 0 )) || essais=1
  for ((i = 0; i < essais; i++)); do
    compte=$(tours_en_cours) \
      || die "baux des tours illisibles (base ameesh) : impossible de prouver la fin des tours, agents à ne pas arrêter"
    [[ $compte =~ ^[0-9]+$ ]] || die "compte de tours invalide « $compte » : agents à ne pas arrêter"
    if (( compte == 0 )); then
      printf '0'
      return 0
    fi
    (( i + 1 < essais )) && sleep 2
  done
  printf '%s' "$compte"
  return 1
}

# Attend qu'au moins un candidat synchrone soit en flux et rattrapé ;
# imprime les noms rattrapés, ou rien en cas de dépassement.
attendre_rattrapage() {
  local essais=$(( ATTENTE_REVEIL / 2 )) i rattrapes=""
  (( essais > 0 )) || essais=1
  for ((i = 0; i < essais; i++)); do
    rattrapes=$(sync_candidats_rattrapes || true)
    if [[ -n $rattrapes ]]; then
      printf '%s' "$rattrapes"
      return 0
    fi
    (( i + 1 < essais )) && sleep 2
  done
  return 1
}

entrer() {
  exiger_primaire

  if (( veille )); then
    sync_avant=$(valeur_veille sync_avant || true)
    sauvegarde=$(valeur_veille sauvegarde || true)
    raison_etat=$(valeur_veille raison || true)
    log "mode veille déjà actif : vérification de la liste synchrone et des tours"
  else
    sync_avant=$(psql_local -tAc 'SHOW synchronous_standby_names') \
      || die "synchronous_standby_names illisible : veille refusée"
    if [[ -n $sans_sauvegarde ]]; then
      sauvegarde="sans: $sans_sauvegarde"
      info "sauvegarde logique non vérifiée (--sans-sauvegarde) : $sans_sauvegarde"
    else
      sauvegarde=$(trouver_sauvegarde) \
        || die "aucune sauvegarde logique de moins de $((SAUVEGARDE_MAX_AGE_S / 3600)) h dans $SAUVEGARDES_DIR : lancer deploy/serveur/sauvegarder-base.sh, ou --sans-sauvegarde <raison>"
      log "sauvegarde logique : $sauvegarde"
    fi
  fi

  # Le marqueur est posé avant la configuration : le fragment le lit et vide
  # la liste, même si un candidat est encore en flux. Une entrée fraîche est
  # annulée par le trap EXIT si la suite échoue (les agents ne sont pas
  # arrêtés) ; une entrée déjà en veille n'est jamais défaite.
  (( veille )) || nouvelle_entree=1
  consigner veille "?" "en-cours"
  marqueur_pose=1
  ecrire_fichier "$CONF_FRAGMENT" 0644 "$(fragment_conf)"
  controler_configuration
  pg_ctlcluster "$PG_VERSION" main reload
  log "configuration rechargée (pg_ctlcluster $PG_VERSION main reload)"

  apres=""
  for ((i = 0; i < 5; i++)); do
    apres=$(psql_local -tAc 'SHOW synchronous_standby_names' || true)
    [[ -z $apres ]] && break
    sleep 1
  done
  if [[ -n $apres ]]; then
    sync_apres=$apres
    resultat_echec="echec-gestionnaire"
    consigner veille "?" "echec-gestionnaire"
    die "synchronous_standby_names=$apres après rechargement : un autre gestionnaire (Patroni ?) tient la liste ; veille refusée, agents à ne pas arrêter"
  fi

  if ! restants=$(attendre_tours); then
    tours_echec=$restants
    resultat_echec="tours-restants"
    consigner veille "$restants" "tours-restants"
    die "tours encore en cours ($restants) après ${ATTENTE_TOURS}s : ne pas arrêter les agents (entrée annulée, liste synchrone rétablie)"
  fi
  consigner veille "0" "ok"

  log "veille : synchronous_standby_names vidé (avant : ${sync_avant:-aucun})"
  log "veille : tours en cours : 0 ; sauvegarde : ${sauvegarde:-?}"
  log "veille : consigné dans $VEILLE_ETAT ; le déclencheur peut arrêter les agents"
}

sortir() {
  exiger_primaire

  if (( !veille )); then
    log "mode veille inactif : rien à rétablir"
    return 0
  fi
  sync_avant=$(valeur_veille sync_avant || true)
  sauvegarde=$(valeur_veille sauvegarde || true)
  raison_etat=$(valeur_veille raison || true)

  if ! rattrapes=$(attendre_rattrapage); then
    flux=$(sync_candidats_en_flux || true)
    sync_apres=""
    consigner veille "0" "reveil-incomplet"
    die "aucune réplique rattrapée après ${ATTENTE_REVEIL}s (en flux : ${flux:-aucune}) : liste synchrone laissée vide, agents à vérifier avant de rétablir"
  fi

  retirer_mode_veille
  ecrire_fichier "$CONF_FRAGMENT" 0644 "$(fragment_conf)"
  controler_configuration
  pg_ctlcluster "$PG_VERSION" main reload
  log "configuration rechargée (pg_ctlcluster $PG_VERSION main reload)"

  apres=""
  for ((i = 0; i < 5; i++)); do
    apres=$(psql_local -tAc 'SHOW synchronous_standby_names' || true)
    [[ -n $apres ]] && break
    sleep 1
  done
  if [[ -z $apres ]]; then
    # Revenir à l'état veille plutôt que laisser un marqueur « actif » mentir :
    # la liste vide est sûre (aucun COMMIT bloqué), la veille sera rejouée.
    sync_apres=""
    consigner veille "0" "echec-reveil"
    ecrire_fichier "$CONF_FRAGMENT" 0644 "$(fragment_conf)"
    pg_ctlcluster "$PG_VERSION" main reload
    die "synchronous_standby_names toujours vide après le réveil : état de veille conservé, réplique en flux non confirmée"
  fi
  sync_apres=$apres
  consigner actif "0" "ok"

  log "réveil : réplique(s) rattrapée(s) : $(printf '%s' "$rattrapes" | paste -sd, -)"
  log "réveil : synchronous_standby_names rétabli : $apres"
  log "réveil : consigné dans $VEILLE_ETAT ; le déclencheur peut rendre les agents"
}

case $action in
  entrer) entrer ;;
  sortir) sortir ;;
esac
