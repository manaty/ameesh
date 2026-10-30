#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Mise a jour « staging » du mesh ameesh : les quatre VM d'abord, le poste en
# dernier s'il est en ligne, sante bornee par hote, retour automatique, traces
# JSONL et attestation « staging actif » vers le rapporteur (#1323).
# Procedure : docs/DEPLOIEMENT-HOTE-AGENTS.md, « Sequence staging ».
#
#   deploy/mise-a-jour/staging.sh [--a-blanc] [--hotes "nom …"]
#                                [--lots "n …"] [--rapporteurs "agent …"]
#                                [--sans-sauvegarde] [--sans-sonde]
#                                [--sans-poste] [--absent nom=raison]
#                                [--sans-retour[=ROLLBACK]] [REF]
#
# REF : commit, branche ou etiquette du depot (defaut : HEAD). Il doit etre un
# ancetre de origin/main, sauf HORS_MAIN=1 (BRANCHE_MAIN pour viser une autre
# branche).
#
# La sequence est lancable depuis le poste ou depuis une VM (bureau-goal,
# VM d'agents). Le poste ne se met a jour qu'en local (poste.sh, unites
# systemd du poste) : executee ailleurs, la sequence le declare ABSENT, saute
# ses etapes (verifier, sauvegarde, migrations, role, installer, redemarrer,
# controler, sante) avec la trace « poste absent, mise a jour a son retour »,
# et n'attend pas son retour. Sur le poste, la sequence complete reste celle
# des cinq hotes (VM d'abord). L'hote local est HOTE_LOCAL s'il est pose,
# sinon AMEESH_HOST, sinon le champ « host » de la configuration ameesh, sinon
# `hostname -s` ; le poser dans BASCULE_ENV sur une VM dont la configuration
# porterait « pc-smichea » evite une fausse detection (#3848). Un serveur du
# mesh sans acces root (manaty-mesh-1, aucun executant, sauvegarde seulement)
# peut etre declare absent de la meme facon avec
# `--absent manaty-mesh-1=raison` : les trois VM d'agents restent requises et
# atteste ; l'attestation le nomme dans `reportes`, sans bloquer la sequence
# (bureau #147558). Les trois VM d'agents ne sont jamais reportables.
#
# Les outils python de la sequence (sonde de courrier, notification, et
# actif.py en fin de sequence) tournent avec le venv du lanceur sur le poste et
# les VM d'agents ; ailleurs (bureau-goal, sans venv ameesh ni route vers la
# base), sur HOTE_OUTILS, une VM d'agents de la sequence (HOTE_MIGRATIONS par
# defaut), par ssh (#3976). Le journal est recopie a actif.py, qui ecrit son
# attestation sur la VM et la depose aussi dans le journal partage du mesh
# (`ameesh_events`, #3981) ; le poste la relit sans ssh.
# Une sonde qui n'a pas pu tourner (outil absent, ssh, base injoignable) ou
# n'a pas conclu (sans-sonde) est non concluante (canon §3) : aucun retour de
# l'hote, la sequence continue et l'attestation porte, pour cet hote, la
# mention « sonde non concluante : <raison> », reportee au staging suivant
# (#4126, decision du pilote ; analogie §12). Seul un negatif la retient.
#
# Sans HOTES_STAGING (cas de bureau-goal, qui n'a qu'un fichier par VM), la
# topologie est composee des bascule-a1.env, bascule-a2.env et bascule-agents-3.env du dossier de
# BASCULE_ENV (AGENTS_SSH + HOTE_AGENTS) et de SERVEUR_SSH (ou VM) pour
# manaty-mesh-1 ; pc-smichea reste absent (poste reporte).
#
# HOTES_STAGING (BASCULE_ENV) : la topologie, une entree « nom=cible » par
# hote, dans cet ordre :
#   ameesh-agents-1=root@10.77.0.3 ameesh-agents-2=root@10.77.0.4 ameesh-agents-3=root@10.77.0.5
#   manaty-mesh-1=root@10.77.0.1 pc-smichea=poste
# Les quatre VM sont requises. pc-smichea=poste est optionnel : omis, il est
# declare absent et la sequence des quatre VM peut attester. « poste » (ou
# « local ») designe le poste, en dernier. Le mode de chaque hote est deduit de
# son nom : manaty-mesh-1 est le serveur du mesh (Postgres, `vm.sh`),
# pc-smichea le poste (`poste.sh`), les autres sont des VM d'agents
# (`vm.sh agents`). VM (BASCULE_ENV) doit etre l'adresse de manaty-mesh-1 ;
# PERSONAS_VM peut etre vide (hote sans executant).
#
# Options lues dans BASCULE_ENV : SONDE_<HOTE> (cible de la sonde de courrier,
# par defaut la premiere persona menee de l'hote ; occupee, elle est attendue
# ATTENTE_CIBLE secondes (300 s), puis remplacee par une persona libre du meme
# hote, #4126), ATTENTE_COURRIER (300 s),
# SOUFFRANCE_COURRIER (900 s), NOTIF_STAGING (mesh-design), DRAIN_ATTENTE
# (vm.sh, 0 = jamais, > 0 = pause immédiate), LOTS_VERIF et RAPPORTEURS_VERIF
# (voir le signal ci-dessous).
#
# --hotes limite la sequence a un sous-ensemble, dans l'ordre canonique. Les
# noms sont normalises : un seul exemplaire par hote, quel que soit le
# separateur (espace, tabulation, nouvelle ligne). Le poste ne peut pas y
# figurer sans les quatre VM : le canon §14 exige des VM saines avant toute
# modification du poste, et un sous-ensemble n'en apporte aucune preuve. Nomme
# alors que le poste est absent, il est ignore (deja reporte) : la selection
# est celle des VM nommees, sans lui.
#
# --sans-poste (ou SANS_POSTE=1 dans BASCULE_ENV) declare le poste absent meme
# si la sequence tourne sur lui : ses etapes sont sautees et tracees, comme
# lorsqu'elle part d'une VM.
#
# --absent nom=raison (repete) declare un hote sans acces reportable :
# manaty-mesh-1 ou pc-smichea. Les trois VM d'agents ne le sont jamais ; chaque
# absence est tracee au commit vise et reprise dans l'attestation.
#
# --sans-retour[=ROLLBACK] (#4616) desactive le retour automatique : apres une
# migration non additive (fenetre de migration, go owner), reinstaller le
# previous-sha sur la base deja migree serait dangereux (l'ancien code lit mal
# le nouvel etat). Un echec d'hote est alors trace (`retour` desactive), la
# sequence s'arrete sur cet hote sans `exec_hote retour`, et le courrier urgent
# au pilote donne la marche manuelle : executeurs arretes, rollback SQL
# d'abord (ROLLBACK, par exemple
# src/ameesh/migrations/rollback/0518_generic_delivery_state.sql), puis retour
# de code de l'hote. L'attestation d'une sequence saine porte la mention
# « retour automatique desactive ». Sans l'option, rien ne change.
#
# --lots / --rapporteurs (ou LOTS_VERIF / RAPPORTEURS_VERIF) : les lots livres
# par ce deploiement et les agents a reveiller quand il est reellement actif.
# En fin de sequence complete et saine, l'attestation « staging actif »
# (deploy/mise-a-jour/actif.py, #1323) est ecrite sous
# ~/.local/state/ameesh/mise-a-jour/actif-<commit>.json et chaque rapporteur
# recoit un courrier unique par commit (hotes, commit relu actif, preuves
# installer/redemarrer/controler/sante). « Complete » = les quatre VM prouvees,
# plus le poste s'il est en ligne ; poste absent, l'attestation porte les quatre
# VM et le champ `reportes` qui nomme le poste et sa mise a jour a son retour.
# Un echec ou un retour n'emet aucun signal de reussite ; un sous-ensemble de
# VM n'atteste rien ; aucun lot n'est clos automatiquement.
#
# La preuve d'activation d'un hote en mode serveur (sans executant, comme
# manaty-mesh-1) est specifique : son `controler` vert et une sonde croisee
# remise a une persona menee d'un AUTRE hote canonique, posterieure a son
# redemarrage — la remise traverse alors sa base (actif.py ; avis R2 B1 #1323).
# Un hote a executants garde la regle locale : une sante `sans-sonde` (aucune
# persona menee) n'atteste rien, et une remise croisee n'y est pas acceptee.
#
# Etapes : verifier chaque hote (lecture seule) ; sauvegarder puis migrer REF
# (additif seulement, base partagee) — la sauvegarde est celle du poste ; quand
# il est absent, c'est le dump du serveur seul (ameesh-sauvegarde.service sur
# manaty-mesh-1, gardé sur la VM, sans rapatriement), et les migrations sont
# alors appliquees depuis une VM d'agents (clone de travail DEPOT_VM verifie au
# preflight, snapshot git archive de src, dry-run additif d'abord, puis
# --additive-only), comme le chemin bureau-goal (#3848 ; clone absent, #4050 :
# refus nomme avant toute sauvegarde, sans retour automatique) ; puis, hote par hote
# dans l'ordre ci-dessus : installer, [poste] role, redemarrer, controler,
# sante (unites, baux et doctor par `controler`, puis courrier reellement remis
# par une sonde). Un echec = retour automatique de l'HOTE (previous-sha),
# controle du retour, arret de la sequence et courrier urgent au pilote. Rien
# n'est repris apres un echec ; relancer la commande est sur : un hote deja au
# commit vise ne le reinstallle pas (le previous-sha reste le bon).
# Un seul operateur a la fois : un second passage sort en 75 (verrou
# ~/.local/state/ameesh/mise-a-jour/staging.lock) sans rien modifier. Entre
# hotes et voies (executant elu, main, release), le debut exclusif dans le
# journal partage `staging.sequence` sort aussi en 75 (#4885), meme apres
# l'echeance de l'autre sequence ; STAGING_PREVIOUS_RUN=<run> leve ce refus
# pour une sequence dont l'arret est prouve.
#
# Traces : une ligne JSON par evenement (date, hote, etape, verdict, sha,
# precedent, detail) dans ~/.local/state/ameesh/mise-a-jour/staging-<date>.jsonl ;
# `precedent` nomme la version que « retour » reinstalle reellement. Le detail
# de l'etape `sante` est le rapport JSON de sante-courrier.py (hote, commit,
# verdict, message de sonde remis, destinataire, delai) : c'est la preuve
# structuree que actif.py exige (#1323) ; un verdict `sans-sonde` n'en est pas
# une : l'attestation le dit pour cet hote (« sonde non concluante »), sans
# le compter comme prouve (#4126).
set -euo pipefail
shopt -s inherit_errexit

# Jamais la base d'un environnement herite (meme piege que poste.sh/vm.sh).
unset AMEESH_DSN AGENT_MESH_DSN

BASCULE_ENV=${BASCULE_ENV:-$HOME/.config/ameesh/bascule.env}
[ -r "$BASCULE_ENV" ] || { echo "parametres absents : $BASCULE_ENV" >&2; exit 2; }
# shellcheck disable=SC1090
. "$BASCULE_ENV"

ICI="$(cd "$(dirname "$0")" && pwd)"
DEPOT="$(git -C "$ICI" rev-parse --show-toplevel)"
VM_SH="$ICI/vm.sh"
POSTE_SH="$ICI/poste.sh"
SONDE_PY="$ICI/sante-courrier.py"
ACTIF_PY="$ICI/actif.py"
SUIVI_PY="$DEPOT/src/ameesh/staging_watch.py"
PARTAGE="$HOME/.local/share/ameesh"
VENV="$PARTAGE/venv"
AMEESH="$VENV/bin/ameesh"
TRAVAIL="$HOME/.local/state/ameesh/mise-a-jour"
JOURNAL="$TRAVAIL/staging-$(date +%F).jsonl"
#: #3981 : etat de l'utilisateur ameesh sur HOTE_OUTILS, ou actif.py tourne
#: quand le lanceur n'a ni venv ni base (bureau-goal). Meme arbre que
#: py_sequence, qui y vise le venv et l'instantane src.
HOME_OUTILS=/home/ameesh
TRAVAIL_OUTILS="$HOME_OUTILS/.local/state/ameesh/mise-a-jour"
#: debut de l'execution (UTC) : fenetre de l'attestation, le journal etant
#: quotidien et pouvant contenir une execution precedente du meme jour.
DEPUIS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ATTENTE_COURRIER=${ATTENTE_COURRIER:-300}
ATTENTE_CIBLE=${ATTENTE_CIBLE:-300}
#: hotes dont la sonde n'a pas conclu (#4126) : nommes en fin de sequence
NON_CONCLUANTS=()
SOUFFRANCE_COURRIER=${SOUFFRANCE_COURRIER:-900}
NOTIF_STAGING=${NOTIF_STAGING:-mesh-design}
LOTS_VERIF=${LOTS_VERIF:-}
RAPPORTEURS_VERIF=${RAPPORTEURS_VERIF:-}
#: ordre canonique du staging : VM d'abord, poste en dernier
HOTES_AGENTS=(ameesh-agents-1 ameesh-agents-2 ameesh-agents-3)
HOTE_SERVEUR=manaty-mesh-1
ORDRE=("${HOTES_AGENTS[@]}" "$HOTE_SERVEUR" pc-smichea)
#: VM de la sequence complete ; seul le serveur sans acces root est reportable
HOTES_VMS=("${HOTES_AGENTS[@]}" "$HOTE_SERVEUR")
#: sans le poste, cette sequence pourra attester les quatre VM (poste reporte)
SANS_POSTE=${SANS_POSTE:-0}
SERVEUR_ABSENT=0
SERVEUR_RAISON=""
POSTE_RAISON_DECLAREE=""
declare -A ABSENT_DECLARE=()
#: dossier des dumps de ameesh-sauvegarde.service sur manaty-mesh-1 (poste.sh)
SAUVEGARDES_VM=${SAUVEGARDES_VM:-/home/ameesh/sauvegardes}
SAUVEGARDES_VM=${SAUVEGARDES_VM%/}
CONFIG_HOTE=${AMEESH_CONFIG:-${AGENT_MESH_CONFIG:-$HOME/.config/ameesh/config.json}}
#: hote qui execute la sequence : le poste (pc-smichea) ne se met a jour qu'en
#: local ; ailleurs, il est absent et sa mise a jour est reportee a son retour.
hote_courant() {
  local hote=""
  if [ -n "${HOTE_LOCAL:-}" ]; then
    printf '%s\n' "$HOTE_LOCAL"
    return 0
  fi
  # les services du poste posent AMEESH_HOST ; la configuration le repete.
  if [ -n "${AMEESH_HOST:-}" ]; then
    printf '%s\n' "${AMEESH_HOST,,}"
    return 0
  fi
  if [ -n "${AGENT_MESH_HOST:-}" ]; then
    printf '%s\n' "${AGENT_MESH_HOST,,}"
    return 0
  fi
  if [ -r "$CONFIG_HOTE" ]; then
    hote=$(python3 -c 'import json, sys
try:
    donnees = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
hote = donnees.get("host") if isinstance(donnees, dict) else None
if not isinstance(hote, str) or not hote.strip():
    raise SystemExit(1)
print(hote.strip().lower())' "$CONFIG_HOTE" 2>/dev/null) || hote=""
  fi
  if [ -z "$hote" ]; then
    hote=$(hostname -s 2>/dev/null || hostname)
  fi
  printf '%s\n' "${hote,,}"
}
HOTE_LOCAL=$(hote_courant)
HOTE_LOCAL=${HOTE_LOCAL,,}

A_BLANC=0
SAUVER=1
SONDE=1
HOTES_FILTRE=""
REF=HEAD
POSITIONNELS=0
#: #4616 : 1 = aucun retour automatique (migration non additive posee)
SANS_RETOUR=0
ROLLBACK_SQL=""

usage() { sed -n '9,13p' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --a-blanc) A_BLANC=1 ;;
    --sans-sauvegarde) SAUVER=0 ;;
    --sans-sonde) SONDE=0 ;;
    --sans-poste) SANS_POSTE=1 ;;
    --sans-retour) SANS_RETOUR=1 ;;
    --sans-retour=*)
      SANS_RETOUR=1
      ROLLBACK_SQL=${1#--sans-retour=}
      [ -n "$ROLLBACK_SQL" ] || { echo "--sans-retour= : rollback vide" >&2; exit 2; }
      ;;
    --absent)
      _absent=${2:?--absent attend NOM=RAISON}
      shift
      ABSENT_DECLARE[${_absent%%=*}]=${_absent#*=}
      ;;
    --hotes) HOTES_FILTRE=${2:?--hotes attend une liste de noms}; shift ;;
    --lots) LOTS_VERIF=${2:?--lots attend une liste de lots}; shift ;;
    --rapporteurs) RAPPORTEURS_VERIF=${2:?--rapporteurs attend une liste d agents}; shift ;;
    -h|--aide|--help) usage; exit 0 ;;
    --*) echo "option inconnue : $1" >&2; exit 2 ;;
    *) REF=$1; POSITIONNELS=$((POSITIONNELS + 1)) ;;
  esac
  shift
done
[ "$POSITIONNELS" -le 1 ] || { echo "un seul REF attendu" >&2; exit 2; }
for _nom in "${!ABSENT_DECLARE[@]}"; do
  _raison=${ABSENT_DECLARE[$_nom]}
  [ -n "$_raison" ] || { echo "--absent : raison vide pour « $_nom »" >&2; exit 2; }
  case "$_nom" in
    manaty-mesh-1) SERVEUR_ABSENT=1; SERVEUR_RAISON=$_raison ;;
    pc-smichea) SANS_POSTE=1; POSTE_RAISON_DECLAREE=$_raison ;;
    *) echo "--absent : hôte non reportable « $_nom » (seuls manaty-mesh-1 et pc-smichea)" >&2; exit 2 ;;
  esac
done

# Liste normalisee (virgule, espace, tabulation, nouvelle ligne) : un seul
# exemplaire par element, ordre d'apparition conserve. Le globbing est
# desactive le temps du decoupage.
normaliser_liste() {  # valeurs…
  local brut=$* item vus=""
  set -f
  for item in $(printf '%s' "$brut" | tr ',;\t\n' '    '); do
    [ -n "$item" ] || continue
    case " $vus " in *" $item "*) continue ;; esac
    vus="$vus $item"
    printf '%s\n' "$item"
  done
  set +f
}
VERIF_LOTS=()
VERIF_RAPPORTEURS=()
while IFS= read -r item; do
  [ -n "$item" ] && VERIF_LOTS+=("$item")
done < <(normaliser_liste "$LOTS_VERIF")
while IFS= read -r item; do
  [ -n "$item" ] && VERIF_RAPPORTEURS+=("$item")
done < <(normaliser_liste "$RAPPORTEURS_VERIF")

etape() { printf '\n== %s\n' "$*"; }
mode_de() {
  case "$1" in
    pc-smichea) echo poste ;;
    manaty-mesh-1) echo serveur ;;
    ameesh-agents-*) echo agents ;;
    *) echo inconnu ;;
  esac
}
# SONDE_<HOTE> : nom d'hote -> nom de variable (majuscules, - et . en _)
var_sonde_de() { printf 'SONDE_%s\n' "$(printf '%s' "$1" | tr 'a-z.-' 'A-Z__')"; }

# --- topologie ------------------------------------------------------------
declare -A CIBLE=()
declare -A FICHIER_HOTE=()
declare -A CHOISI=()   #: selection normalisee de --hotes (un nom = une entree)
POSTE_ABSENT=0
POSTE_RAISON=""
HOTES=()
TOPOLOGIE_COMPOSEE=0
if [ -z "${HOTES_STAGING:-}" ]; then
  # #3848 : le bureau VM n'a qu'un fichier par VM d'agents (bascule-a1/a2.env et bascule-agents-3.env).
  # Composer la topologie depuis ces trois fichiers ; manaty-mesh-1 vient de
  # SERVEUR_SSH (ou VM), pc-smichea reste absent (poste reporte).
  DIR_BASCULE=$(dirname "$BASCULE_ENV")
  compose=()
  for f in "$DIR_BASCULE/bascule-a1.env" "$DIR_BASCULE/bascule-a2.env" "$DIR_BASCULE/bascule-agents-3.env"; do
    [ -r "$f" ] || continue
    entree=$(
      # shellcheck disable=SC1090
      . "$f" >/dev/null 2>&1 || exit 1
      [ -n "${HOTE_AGENTS:-}" ] && [ -n "${AGENTS_SSH:-}" ] || exit 1
      printf '%s=%s' "$HOTE_AGENTS" "$AGENTS_SSH"
    ) || entree=""
    [ -n "$entree" ] && compose+=("$entree")
  done
  if [ ${#compose[@]} -ge ${#HOTES_AGENTS[@]} ]; then
    SERVEUR_SSH=${SERVEUR_SSH:-${VM:+root@$VM}}
    if [ -z "$SERVEUR_SSH" ]; then
      echo "HOTES_STAGING absent de $BASCULE_ENV ; topologie composee depuis $DIR_BASCULE/bascule-a{1,2}.env et bascule-agents-3.env," >&2
      echo "mais manaty-mesh-1 manque : poser SERVEUR_SSH=root@10.77.0.1 (ou VM=10.77.0.1) dans l'environnement ou BASCULE_ENV" >&2
      exit 2
    fi
    HOTES_STAGING="${compose[*]} manaty-mesh-1=$SERVEUR_SSH"
    TOPOLOGIE_COMPOSEE=1
    echo "topologie staging composee depuis $DIR_BASCULE/bascule-a{1,2}.env et bascule-agents-3.env : $HOTES_STAGING" >&2
  else
    echo "HOTES_STAGING absent de $BASCULE_ENV : declarer les quatre VM (pc-smichea=poste en option)" >&2
    echo "  ou poser $DIR_BASCULE/bascule-a1.env, bascule-a2.env et bascule-agents-3.env (AGENTS_SSH, HOTE_AGENTS) et SERVEUR_SSH" >&2
    echo "  HOTES_STAGING=\"ameesh-agents-1=root@… ameesh-agents-2=root@… ameesh-agents-3=root@… manaty-mesh-1=root@… pc-smichea=poste\"" >&2
    exit 2
  fi
fi
for entree in $HOTES_STAGING; do
  nom=${entree%%=*}
  cible=${entree#*=}
  if [ "$entree" = "$nom" ] || [ -z "$nom" ] || [ -z "$cible" ]; then
    echo "HOTES_STAGING : entree invalide « $entree » (nom=cible attendu)" >&2; exit 2
  fi
  [ "$(mode_de "$nom")" != inconnu ] \
    || { echo "HOTES_STAGING : hote inconnu « $nom » (connus : ${ORDRE[*]})" >&2; exit 2; }
  [ -z "${CIBLE[$nom]:-}" ] \
    || { echo "HOTES_STAGING : hote « $nom » declare deux fois" >&2; exit 2; }
  if [ "$(mode_de "$nom")" = poste ]; then
    case "$cible" in poste|local) ;; *) echo "HOTES_STAGING : $nom doit viser « poste » (local)" >&2; exit 2 ;; esac
  else
    [[ $cible =~ ^[a-zA-Z0-9_.@:-]+$ && $cible != -* ]] \
      || { echo "HOTES_STAGING : cible SSH invalide pour $nom" >&2; exit 2; }
  fi
  CIBLE[$nom]=$cible
done
# Les quatre VM sont requises : sans elles, aucune sequence n'apporte de preuve.
for nom in "${HOTES_VMS[@]}"; do
  [ -n "${CIBLE[$nom]:-}" ] \
    || { echo "HOTES_STAGING : les quatre VM sont requises (${HOTES_VMS[*]})" >&2; exit 2; }
done

# --- poste : present ou absent --------------------------------------------
# Le poste ne se met a jour qu'en local : poste.sh et ses unites systemd ne
# s'executent que sur lui. Depuis une VM, il est declare absent, ses etapes
# sont sautees et sa mise a jour reportee a son retour (canon §14) ; il ne
# retient ni le staging ni les lots. Sur le poste, la sequence complete reste
# celle des cinq hotes, VM d'abord.
if [ -n "${CIBLE[pc-smichea]:-}" ]; then
  if [ "$HOTE_LOCAL" = pc-smichea ] && [ "$SANS_POSTE" != 1 ]; then
    :
  else
    POSTE_ABSENT=1
    if [ -n "$POSTE_RAISON_DECLAREE" ]; then
      POSTE_RAISON=$POSTE_RAISON_DECLAREE
    elif [ "$HOTE_LOCAL" = pc-smichea ]; then
      POSTE_RAISON="poste declare absent par l'operateur : mise a jour a son retour, jamais en dessous"
    else
      POSTE_RAISON="poste absent (sequence lancee depuis $HOTE_LOCAL) : mise a jour a son retour, jamais en dessous"
    fi
  fi
else
  if [ "$HOTE_LOCAL" = pc-smichea ] && [ "$SANS_POSTE" != 1 ]; then
    echo "HOTES_STAGING sans pc-smichea alors que la sequence tourne sur le poste ($HOTE_LOCAL) : ajouter pc-smichea=poste, ou lancer depuis une VM (--sans-poste pour le declarer absent)" >&2
    exit 2
  fi
  POSTE_ABSENT=1
  if [ -n "$POSTE_RAISON_DECLAREE" ]; then
    POSTE_RAISON=$POSTE_RAISON_DECLAREE
  else
    POSTE_RAISON="poste absent de la topologie (sequence lancee depuis $HOTE_LOCAL) : mise a jour a son retour, jamais en dessous"
  fi
fi

# --- selection ------------------------------------------------------------
if [ -n "$HOTES_FILTRE" ]; then
  # Selection normalisee : un nom connu compte une seule fois, quel que soit
  # le separateur (espace, tabulation, nouvelle ligne) ; le globbing est
  # desactive pour que chaque mot soit compare tel quel. « pc-smichea » repete
  # quatre fois ne peut donc plus tenir lieu des quatre VM (avis R1 B4).
  set -f
  for choisi in $HOTES_FILTRE; do
    [ -n "${CIBLE[$choisi]:-}" ] \
      || { echo "--hotes : « $choisi » n'est pas dans HOTES_STAGING" >&2; exit 2; }
    CHOISI[$choisi]=1
  done
  set +f
  # Poste absent : nomme, il est ignore (deja reporte), et le controle §14
  # ci-dessous ne porte que sur un poste reellement mis a jour (relecture dev1).
  if [ "$POSTE_ABSENT" = 1 ] && [ -n "${CHOISI[pc-smichea]:-}" ]; then
    unset 'CHOISI[pc-smichea]'
    echo "--hotes : pc-smichea ignore (poste absent, deja reporte)" >&2
  fi
else
  for nom in "${HOTES_VMS[@]}"; do CHOISI[$nom]=1; done
  [ "$POSTE_ABSENT" = 1 ] || CHOISI[pc-smichea]=1
fi
# canon §14 : le poste ne se met a jour qu'apres des VM saines. Le controle
# porte sur la selection normalisee, jamais sur la chaine brute : le case
# sur espaces litteraux etait contourne par tabulation, nouvelle ligne et
# doublons (avis R1 B4).
if [ -n "${CHOISI[pc-smichea]:-}" ]; then
  for nom in "${HOTES_AGENTS[@]}"; do
    [ -n "${CHOISI[$nom]:-}" ] \
      || { echo "--hotes : pc-smichea ne se met a jour que dans la sequence complete des cinq hotes (canon §14 : VM saines d'abord)" >&2; exit 2; }
  done
  if [ "$SERVEUR_ABSENT" != 1 ]; then
    [ -n "${CHOISI[manaty-mesh-1]:-}" ] \
      || { echo "--hotes : pc-smichea ne se met a jour que dans la sequence complete des cinq hotes (canon §14 : VM saines d'abord)" >&2; exit 2; }
  fi
fi
HOTES=()
for nom in "${ORDRE[@]}"; do
  [ -n "${CHOISI[$nom]:-}" ] || continue
  if [ "$nom" = manaty-mesh-1 ] && [ "$SERVEUR_ABSENT" = 1 ]; then continue; fi
  HOTES+=("$nom")
done
[ ${#HOTES[@]} -gt 0 ] || { echo "--hotes : liste vide" >&2; exit 2; }
# Complete = les quatre VM, plus le poste s'il est en ligne. Un poste absent ne
# rend pas la sequence partielle : son absence est prouvee par la trace et
# l'attestation le nomme dans `reportes`.
SEQUENCE_COMPLETE=1
for nom in "${HOTES_AGENTS[@]}"; do
  case " ${HOTES[*]} " in *" $nom "*) ;; *) SEQUENCE_COMPLETE=0 ;; esac
done
if [ "$SERVEUR_ABSENT" = 0 ]; then
  case " ${HOTES[*]} " in *" $HOTE_SERVEUR "*) ;; *) SEQUENCE_COMPLETE=0 ;; esac
fi
if [ "$POSTE_ABSENT" = 0 ]; then
  case " ${HOTES[*]} " in *" pc-smichea "*) ;; *) SEQUENCE_COMPLETE=0 ;; esac
fi

# Hote des migrations quand le poste est absent (#3848), choisi et valide avant
# toute mutation : une VM d'agents de la sequence.
HOTE_MIGRATIONS=${HOTE_MIGRATIONS:-}
if [ "$POSTE_ABSENT" = 1 ]; then
  if [ -z "$HOTE_MIGRATIONS" ]; then
    for nom in ameesh-agents-2 ameesh-agents-1 ameesh-agents-3; do
      case " ${HOTES[*]} " in *" $nom "*) HOTE_MIGRATIONS=$nom; break ;; esac
    done
  fi
  if [ -z "$HOTE_MIGRATIONS" ] || [ "$(mode_de "$HOTE_MIGRATIONS")" != agents ]; then
    echo "HOTE_MIGRATIONS (« ${HOTE_MIGRATIONS:-absent} ») doit etre une VM d'agents de la sequence" >&2
    exit 2
  fi
  case " ${HOTES[*]} " in
    *" $HOTE_MIGRATIONS "*) ;;
    *) echo "HOTE_MIGRATIONS (« $HOTE_MIGRATIONS ») n'est pas dans la sequence (${HOTES[*]})" >&2; exit 2 ;;
  esac
fi

# #3976 : les outils python de la sequence (sonde de courrier, notification)
# ont besoin du venv ameesh et d'une route vers la base. Le poste et les VM
# d'agents les ont ; ailleurs (bureau-goal), ils tournent sur HOTE_OUTILS, une
# VM d'agents de la sequence (HOTE_MIGRATIONS par defaut), sous l'utilisateur
# ameesh, avec l'instantane src du commit vise (migrer_depuis_vm).
OUTILS_DISTANTS=0
if [ "$(mode_de "$HOTE_LOCAL")" != poste ] && [ "$(mode_de "$HOTE_LOCAL")" != agents ]; then
  OUTILS_DISTANTS=1
  HOTE_OUTILS=${HOTE_OUTILS:-$HOTE_MIGRATIONS}
  if [ -z "$HOTE_OUTILS" ] || [ "$(mode_de "$HOTE_OUTILS")" != agents ]; then
    echo "HOTE_OUTILS (« ${HOTE_OUTILS:-absent} ») doit etre une VM d'agents de la sequence" >&2
    exit 2
  fi
  case " ${HOTES[*]} " in
    *" $HOTE_OUTILS "*) ;;
    *) echo "HOTE_OUTILS (« $HOTE_OUTILS ») n'est pas dans la sequence (${HOTES[*]})" >&2; exit 2 ;;
  esac
fi

# VM sert aussi a poste.sh (migrations, sauvegarde) : la topologie et VM
# doivent dire la meme chose. La topologie composee pose VM depuis SERVEUR_SSH.
ADRESSE_MESH=${CIBLE[manaty-mesh-1]#*@}
if [ "$TOPOLOGIE_COMPOSEE" = 1 ] && [ -z "${VM:-}" ]; then
  VM=$ADRESSE_MESH
fi
if [ "${VM:-}" != "$ADRESSE_MESH" ]; then
  echo "VM (« ${VM:-absent} ») doit etre l'adresse de manaty-mesh-1 (« $ADRESSE_MESH ») dans $BASCULE_ENV" >&2
  exit 2
fi
PERSONAS_VM_VAL=${PERSONAS_VM:-}

# --- cible ----------------------------------------------------------------
SHA=$(git -C "$DEPOT" rev-parse --verify "${REF}^{commit}") \
  || { echo "REF introuvable dans $DEPOT : $REF" >&2; exit 2; }
SHA=${SHA,,}
if [ "${HORS_MAIN:-0}" != 1 ]; then
  BRANCHE=${BRANCHE_MAIN:-origin/main}
  git -C "$DEPOT" merge-base --is-ancestor "$SHA" "$BRANCHE" \
    || { echo "$SHA n'est pas un ancetre de $BRANCHE (HORS_MAIN=1 pour forcer)" >&2; exit 2; }
fi

mkdir -p "$TRAVAIL"; chmod 700 "$TRAVAIL"
touch "$JOURNAL"; chmod 600 "$JOURNAL"

# Operateur unique (#2448) : le staging est une sequence d'hotes, jamais deux a
# la fois, qu'il soit lance a la main ou par la release automatique. Le verrou
# tombe avec le processus ; un second passage sort en 75 sans rien modifier
# (un `--a-blanc` reste possible, il ne modifie rien).
if [ "$A_BLANC" != 1 ]; then
  VERROU="$TRAVAIL/staging.lock"
  exec 9>"$VERROU"
  if ! flock -n 9; then
    echo "staging deja en cours (verrou $VERROU) : rien n'est modifie" >&2
    exit 75
  fi
fi

# Un BASCULE_ENV temporaire par hote : vm.sh relit le fichier qu'on lui donne,
# et la topologie doit primer sur d'eventuels AGENTS_SSH/HOTE_AGENTS restes
# dans le fichier prive.
nettoyer() {
  local rc=$?
  if [ "${SUIVI_DEMARRE:-0}" = 1 ]; then
    # EXIT is best effort only. SIGKILL or a dead launcher is detected by
    # the independent alerts pass from the already committed deadline.
    publier_suivi failed || true
  fi
  local f
  for f in ${FICHIER_HOTE[@]+"${FICHIER_HOTE[@]}"}; do rm -f "$f"; done
  return "$rc"
}
trap nettoyer EXIT
for nom in "${HOTES[@]}"; do
  f="$TRAVAIL/staging-$$-$nom.env"
  ( umask 077; : > "$f" )
  case "$(mode_de "$nom")" in
    agents) printf 'AGENTS_SSH=%s\nHOTE_AGENTS=%s\n' "${CIBLE[$nom]}" "$nom" > "$f" ;;
    serveur) printf 'VM=%s\nPERSONAS_VM=%s\n' "$ADRESSE_MESH" "$PERSONAS_VM_VAL" > "$f" ;;
    poste) ;;
  esac
  FICHIER_HOTE[$nom]=$f
done

SHA_TRAVE=${SHA:0:9}
PRECEDENT=""
trace() {  # hote etape verdict [detail] [rapport-json]
  python3 - "$JOURNAL" "$1" "$2" "$3" "${4:-}" "$SHA" "$PRECEDENT" "${5:-}" <<'PY'
import json, sys, time
path, hote, etape, verdict, detail, sha, precedent, rapport = sys.argv[1:9]
line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hote": hote, "etape": etape, "verdict": verdict,
        "sha": sha or None, "precedent": precedent or None,
        "detail": detail or None}
# le rapport de sonde reste structure dans la trace, pas seulement en texte
# (#2190) ; `detail` garde sa forme historique, lue par actif.py.
if rapport:
    try:
        objet = json.loads(rapport)
    except ValueError:
        objet = None
    if isinstance(objet, dict):
        line["rapport"] = objet
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(line, ensure_ascii=False) + "\n")
PY
  if [ "${SUIVI_DEMARRE:-0}" = 1 ]; then
    publier_suivi running || echo "suivi staging indisponible" >&2
  fi
}

# --- gestes par hote ------------------------------------------------------
poste_transitoire() {  # redemarrer|retour
  # #3039 : le tick tourne dans ameesh-release.service. Le dispatcher poste
  # refuse ce cgroup (#2950), y compris pour le retour automatique. Seuls ces
  # gestes quittent le service ; les VM et les controles restent synchrones
  # dans le staging. Aucun repli direct : un echec doit declencher echec_hote.
  local unite="poste-maj-staging-$$-$1" env_args=()
  env_args+=(--setenv=BASCULE_ENV="$BASCULE_ENV")
  [ -n "${AMEESH_CONFIG:-}" ] && env_args+=(--setenv=AMEESH_CONFIG="$AMEESH_CONFIG")
  systemd-run --user --collect --wait --pipe --unit "$unite" \
    --working-directory "$PWD" "${env_args[@]}" "$POSTE_SH" "$@"
}
exec_hote() {  # nom action [args…]
  local nom=$1; shift
  case "$(mode_de "$nom")" in
    poste)
      case "$1" in
        redemarrer|retour) poste_transitoire "$@" ;;
        *) "$POSTE_SH" "$@" ;;
      esac ;;
    agents) env BASCULE_ENV="${FICHIER_HOTE[$nom]}" "$VM_SH" agents "$@" ;;
    serveur) env BASCULE_ENV="${FICHIER_HOTE[$nom]}" "$VM_SH" "$@" ;;
  esac
}
ssh_lire() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$1" "$2"; }
# commit installe sur l'hote, vide si illisible (jamais bloquant : c'est une
# lecture pour la trace et pour eviter de reinstaller le meme commit).
sha_installe() {
  local nom=$1
  case "$(mode_de "$nom")" in
    poste) readlink "$PARTAGE/src-current" 2>/dev/null | sed -n 's/^src-//p' ;;
    agents) ssh_lire "${CIBLE[$nom]}" \
      "runuser -u ameesh -- git -C /home/ameesh/.local/share/ameesh/src rev-parse HEAD" 2>/dev/null ;;
    serveur) ssh_lire "${CIBLE[$nom]}" "git -C /opt/ameesh/src rev-parse HEAD" 2>/dev/null ;;
  esac || true
}
precedent_retour() {  # commit que « retour » reinstalle reellement
  local nom=$1
  case "$(mode_de "$nom")" in
    poste) cat "$TRAVAIL/precedent" 2>/dev/null ;;
    agents) ssh_lire "${CIBLE[$nom]}" \
      "cat /home/ameesh/.local/share/ameesh/previous-sha" 2>/dev/null ;;
    serveur) ssh_lire "${CIBLE[$nom]}" "cat /opt/ameesh/precedent" 2>/dev/null ;;
  esac || true
}
# Migrations depuis une VM d'agents quand le poste est absent (#3848) : le
# chemin du bureau (snapshot git archive de src, venv courant, PYTHONPATH sur
# l'instantane), dry-run additif d'abord, puis application additive seule.
# La base est commune : un seul hote suffit. Aucun secret ne traverse la
# commande ; le SHA est un commit deja resolu par git.
DEPOT_VM=${DEPOT_VM:-/home/ameesh/work/ameesh-release}
SNAP_VM=${SNAP_VM:-/home/ameesh/work}
# #4050 : le clone de travail des migrations doit exister sur l'hote qui les
# applique quand le poste est absent. Un hote neuf n'en a pas : le preflight le
# constate avant sauvegarde, migration et tout arret de runner, nomme le geste
# exact et s'arrete sans retour automatique (rien n'a ete modifie).
verifier_clone_migrations() {  # nom
  local nom=$1 cible=${CIBLE[$1]} depot sortie
  printf -v depot '%q' "$DEPOT_VM"
  if sortie=$(ssh_lire "$cible" \
      "runuser -u ameesh -- git -C $depot rev-parse --verify HEAD && runuser -u ameesh -- git -C $depot remote get-url origin" 2>&1); then
    return 0
  fi
  {
    echo "clone des migrations absent ou illisible sur $nom ($DEPOT_VM)"
    echo "  geste exact (clone complet, jamais --depth/--shallow, canon §7) :"
    printf "    ssh %s 'runuser -u ameesh -- git clone https://github.com/manaty/ameesh.git %s'\n" \
      "$cible" "$DEPOT_VM"
    if [ -n "$sortie" ]; then printf '  lecture : %s\n' "$sortie"; fi
    echo "  rien n'a ete modifie ; aucun retour automatique"
  } >&2
  return 1
}
migrer_depuis_vm() {  # nom sha
  local nom=$1 sha=$2 cible=${CIBLE[$1]} a b c
  printf -v a '%q' "$sha"
  printf -v b '%q' "$DEPOT_VM"
  printf -v c '%q' "$SNAP_VM"
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$cible" \
    "runuser -u ameesh -- env HOME=/home/ameesh bash -s -- $a $b $c" <<'REMOTE'
set -euo pipefail
R=$1
depot=$2
snap=$3
d="$snap/snap-${R:0:8}"
rm -rf -- "$d"
mkdir -p -- "$d"
git -C "$depot" fetch -q origin
git -C "$depot" archive "$R" src | tar -x -C "$d"
cd "$d"
PYTHONPATH="$d/src" "$HOME/.local/share/ameesh/venv/bin/python3" -m ameesh migrate --dry-run --additive-only
PYTHONPATH="$d/src" "$HOME/.local/share/ameesh/venv/bin/python3" -m ameesh migrate --additive-only
REMOTE
}
# Sauvegarde du serveur quand le poste est absent (relecture dev1 #3848) : le
# meme service que poste.sh (ameesh-sauvegarde.service, pg_dump sur la VM),
# dont un nouveau dump est exige ; il reste sur manaty-mesh-1, sans
# rapatriement. Sortie : chemin, taille, SHA-256. Aucun DSN ne traverse la
# commande.
sauvegarde_serveur_vm() {
  printf '%s\n' "$SAUVEGARDES_VM" | ssh -o BatchMode=yes -o ConnectTimeout=10 \
    "${CIBLE[manaty-mesh-1]}" 'set -eu
    IFS= read -r d
    test -d "$d"
    before=$(mktemp "$d/.liste-avant.XXXXXX")
    after=$(mktemp "$d/.liste-apres.XXXXXX")
    trap "rm -f -- \"$before\" \"$after\"" EXIT HUP INT TERM
    find "$d" -maxdepth 1 -type f -name "ameesh-*.dump" -printf "%f %i %s %T@\n" | sort > "$before"
    systemctl start ameesh-sauvegarde.service
    test "$(systemctl show ameesh-sauvegarde.service -p Result --value)" = success
    test "$(systemctl show ameesh-sauvegarde.service -p ExecMainStatus --value)" = 0
    find "$d" -maxdepth 1 -type f -name "ameesh-*.dump" -printf "%f %i %s %T@\n" | sort > "$after"
    new=$(comm -13 "$before" "$after" | tail -1 | cut -d" " -f1)
    if [ -z "$new" ]; then
      echo "ameesh-sauvegarde : aucun nouveau dump dans $d" >&2
      exit 1
    fi
    f="$d/$new"
    test -f "$f"
    printf "%s\n" "$f"
    stat -c %s -- "$f"
    sha256sum -- "$f" | cut -d" " -f1
  '
}
# #3976 : un outil python de la sequence (script, ou « - » pour une source sur
# l'entree standard). En local avec le venv du lanceur ; sinon sur HOTE_OUTILS
# (voir plus haut) : la source part sur l'entree standard de ssh, chaque
# argument est cite pour le shell distant, PYTHONPATH vise l'instantane src du
# commit (absent, python l'ignore et prend le paquet installe). Un outil qui ne
# peut pas tourner (venv absent, ssh en echec : 127, 255…) n'est jamais lu comme
# un verdict sur un hote : voir verdict_sonde.
citer() { printf "'%s'" "${1//\'/\'\\\'\'}"; }
py_sequence() {  # script|- [args…]
  local script=$1 a commande
  shift
  if [ "$OUTILS_DISTANTS" != 1 ]; then
    if [ "$script" = "$SUIVI_PY" ]; then
      PYTHONPATH="$DEPOT/src${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" -m ameesh.staging_watch "$@"
    else
      "$VENV/bin/python" "$script" "$@"
    fi
    return
  fi
  commande="runuser -u ameesh -- env HOME=/home/ameesh"
  commande+=" PYTHONPATH=$(citer "$SNAP_VM/snap-${SHA:0:8}/src")"
  commande+=" /home/ameesh/.local/share/ameesh/venv/bin/python3 -"
  for a in "$@"; do commande+=" $(citer "$a")"; done
  if [ "$script" = - ]; then
    ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 \
      "${CIBLE[$HOTE_OUTILS]}" "$commande"
  else
    ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 \
      "${CIBLE[$HOTE_OUTILS]}" "$commande" < "$script"
  fi
}
# #3981 : le journal de la sequence reste ecrit par le lanceur ; actif.py qui
# tourne sur HOTE_OUTILS doit le lire la-bas. Copie root puis propriete a
# ameesh, 0600 : l'outil distant (runuser -u ameesh) le relit sans privilege.
copier_journal_outils() {  # source locale distant
  local source=$1 distant=$2
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 \
    "${CIBLE[$HOTE_OUTILS]}" \
    "umask 077; mkdir -p $(citer "$TRAVAIL_OUTILS") && cat > $(citer "$distant") && chown ameesh:ameesh $(citer "$distant")" \
    < "$source"
}
# Verdict d'une sonde de courrier (#3976, canon §3) : « ok » (code 0), « ko »
# (code 1 ET rapport JSON de sante-courrier.py au verdict ko : la sonde a
# tourne et constate un defaut de l'hote), sinon « non-concluant » : outil
# absent (126, 127), ssh en echec (255), base injoignable (verdict
# non-concluant, code 3), sortie sans rapport. Seul « ko » rend l'hote malsain.
verdict_sonde() {  # code sortie
  [ "$1" = 0 ] && { echo ok; return; }
  if [ "$1" = 1 ] && printf '%s\n' "$2" | python3 -c 'import json, sys
lignes = [l for l in sys.stdin.read().splitlines() if l.strip()]
try:
    rapport = json.loads(lignes[-1])
except (IndexError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if isinstance(rapport, dict) and rapport.get("verdict") == "ko" else 1)'; then
    echo ko
  else
    echo non-concluant
  fi
}
sante_hote() {  # nom [jamais] : la preuve (rapport JSON) va dans SANTE_DETAIL
  local nom=$1 sans=${2:-} cible_sonde var sortie rc args_hote=() args_sonde=()
  SANTE_DETAIL=""
  SANTE_RC=""
  var=$(var_sonde_de "$nom")
  cible_sonde=${!var:-}
  if [ "$(mode_de "$nom")" = poste ]; then
    etape "doctor --harness (harnais servis par le poste)"
    "$AMEESH" doctor --harness || return 1
  fi
  # un hote en mode serveur n'a pas d'executant : sante-courrier.py sonde
  # alors une persona menee d'un autre hote, remise a travers sa base
  # (actif.py, avis R2 B1 #1323).
  if [ "$(mode_de "$nom")" = serveur ]; then
    args_hote=(--serveur)
  fi
  if [ "$SONDE" = 1 ] && [ "$sans" != jamais ]; then
    etape "courrier de $nom (sonde de remise reelle)"
    args_sonde=(--sonde "${cible_sonde:-auto}" --attente "$ATTENTE_COURRIER"
      --attente-cible "$ATTENTE_CIBLE")
  else
    etape "courrier de $nom (lecture seule, sonde desactivee)"
    args_sonde=(--sonde jamais)
  fi
  rc=0
  sortie=$(py_sequence "$SONDE_PY" --hote "$nom" "${args_sonde[@]}" \
    --souffrance "$SOUFFRANCE_COURRIER" --sha "$SHA" --json \
    ${args_hote[@]+"${args_hote[@]}"}) || rc=$?
  printf '%s\n' "$sortie"
  SANTE_DETAIL=$sortie
  # 3 : la sonde n'a pas pu tourner ou conclure (non concluant), jamais un hote malsain
  case "$(verdict_sonde "$rc" "$sortie")" in
    ok) return 0 ;;
    ko) return 1 ;;
    *) SANTE_RC=$rc; return 3 ;;
  esac
}
notifier() {  # sujet corps
  local sujet=$1 corps=$2
  py_sequence - "$NOTIF_STAGING" "$sujet" "$corps" <<'PY' \
    || echo "notification non remise (courrier indisponible)" >&2
import sys
from ameesh import config, db, mail, work
dest, sujet, corps = sys.argv[1:4]
conn = db.connect(config.load())
try:
    mail.send(conn, work.SYSTEM_SENDER, dest, "%s\n\n%s" % (sujet, corps),
              kind="event", payload={"urgent": True, "staging": {"sujet": sujet}})
finally:
    conn.close()
PY
}
echec_hote() {  # nom etape detail : retour de l'hote, controle, arret, courrier
  local nom=$1 ou=$2 detail=$3 rapport="" complement=""
  # le rapport JSON de la sonde n'est pas perdu : il va dans la trace (objet
  # `rapport`) et dans la notification, pas seulement « sante en echec » (#2190).
  if [ "$ou" = sante ] && [ -n "$SANTE_DETAIL" ]; then
    rapport=$SANTE_DETAIL
    complement=$(printf '\n\nRapport de sonde (JSON) :\n%s' "$rapport")
  fi
  trace "$nom" "$ou" ko "$detail" "$rapport"
  echo "!! $nom : $ou en echec — $detail" >&2
  if [ -n "$rapport" ]; then
    printf '   rapport de sonde : %s\n' "$rapport" >&2
  fi
  if [ "$SANS_RETOUR" = 1 ]; then
    retour_desactive "$nom" "$ou" "$detail" "$complement"
  fi
  etape "$nom : retour automatique (previous-sha)"
  if ! exec_hote "$nom" retour; then
    trace "$nom" retour ko "le retour lui-meme a echoue"
    notifier "staging #1155 : retour de $nom en ECHEC" \
      "Le staging $SHA_TRAVE a echoue sur $nom ($ou : $detail) et son retour automatique a echoue.$complement Sequence arretee ; intervention requise. Trace : $JOURNAL"
    exit 1
  fi
  trace "$nom" retour ok "retour au commit precedent applique"
  etape "$nom : controle du retour"
  if ! exec_hote "$nom" controler; then
    trace "$nom" controle-retour ko "l'hote ne repond pas apres le retour"
    notifier "staging #1155 : retour de $nom non verifie" \
      "Le staging $SHA_TRAVE a echoue sur $nom ($ou : $detail) ; le code precedent est reinstalle mais ses controles echouent.$complement Sequence arretee ; intervention requise. Trace : $JOURNAL"
    exit 1
  fi
  trace "$nom" controle-retour ok "unites et baux verts apres retour"
  notifier "staging #1155 : $nom en echec, retour effectue" \
    "Le staging $SHA_TRAVE a echoue sur $nom ($ou : $detail). Retour automatique effectue et controle (unites, baux, doctor). Sequence arretee avant les hotes suivants : rien d'autre n'a ete modifie.$complement Trace : $JOURNAL"
  echo "sequence arretee sur $nom (retour effectue et controle)" >&2
  exit 1
}

# #4616 : --sans-retour. Apres une migration non additive, l'ancien code sur
# la base migree est dangereux : aucun `exec_hote retour`. L'echec est trace,
# la sequence s'arrete sur cet hote et le pilote recoit la marche manuelle,
# rollback SQL d'abord, retour de code ensuite.
retour_desactive() {  # nom etape detail complement
  local nom=$1 ou=$2 detail=$3 complement=$4 precedent rollback marche outil
  precedent=$(precedent_retour "$nom" || true)
  case "$(mode_de "$nom")" in
    poste) outil="poste.sh retour" ;;
    agents) outil="vm.sh agents retour" ;;
    serveur) outil="vm.sh retour" ;;
  esac
  rollback=${ROLLBACK_SQL:-le rollback SQL de la migration non additive posee (src/ameesh/migrations/rollback/)}
  marche=$(printf '%s\n' \
    "Marche manuelle (dans cet ordre) :" \
    "1. arreter les executeurs des hotes deja passes a $SHA_TRAVE ;" \
    "2. rollback SQL d'abord, en une transaction sur la base du mesh : $rollback ;" \
    "3. seulement ensuite, retour de code de $nom vers ${precedent:-previous-sha (illisible)} : $outil (BASCULE_ENV de l'hote), puis controler ; de meme pour chaque hote deja a $SHA_TRAVE ;" \
    "4. relancer les executeurs et verifier (doctor, courrier).")
  trace "$nom" retour desactive "retour automatique desactive (--sans-retour) : rollback SQL d'abord ($rollback), puis retour de code"
  printf '   retour automatique desactive (--sans-retour) : %s reste sur %s\n%s\n' \
    "$nom" "$SHA_TRAVE" "$marche" >&2
  notifier "staging #4616 : $nom en echec, retour automatique desactive" \
    "Le staging $SHA_TRAVE a echoue sur $nom ($ou : $detail). Retour automatique desactive (--sans-retour) : aucun retour n'a ete lance, $nom reste sur $SHA_TRAVE. Sequence arretee avant les hotes suivants ; intervention requise.

$marche$complement

Trace : $JOURNAL"
  echo "sequence arretee sur $nom (retour automatique desactive)" >&2
  exit 1
}

# #3976 : la sonde n'a pas pu tourner (outil absent, ssh, base injoignable)
# ou n'a pas conclu (#4089 : remise sans tour termine, attente budget) :
# non concluant (canon §3), jamais « malsain ». Aucun retour de l'hote, qui a
# passe installer, redemarrer et controler. #4126 (decision du pilote) : la
# sequence continue ; l'attestation porte la mention de cet hote, reportee
# au staging suivant.
sonde_non_concluante() {  # nom
  local nom=$1 detail
  detail="sonde de courrier non concluante (code ${SANTE_RC:-?}) : outil empeche ou tour non conclu, aucun retour"
  trace "$nom" sante non-concluant "$detail" "$SANTE_DETAIL"
  NON_CONCLUANTS+=("$nom")
  echo "!! $nom : $detail ; $nom reste sur $SHA_TRAVE, la sequence continue (mention dans l'attestation)" >&2
  notifier "staging : sonde de $nom non concluante, sequence poursuivie sans retour" \
    "Le staging $SHA_TRAVE a installe, redemarre et controle $nom, mais sa sonde de courrier n'a pas conclu ($detail). Non concluant (canon §3) : $nom n'est PAS remis en arriere et la sequence continue ; l'attestation portera « sonde non concluante » pour $nom, a reprendre au staging suivant. Sortie de la sonde : ${SANTE_DETAIL:-vide}. Trace : $JOURNAL"
}

# --- plan -----------------------------------------------------------------
etape "plan staging : ${HOTES[*]} ; cible $SHA_TRAVE"
for nom in "${HOTES[@]}"; do
  printf '  %-16s %-8s %s\n' "$nom" "$(mode_de "$nom")" "${CIBLE[$nom]}"
done
# #4880: a committed start must exist BEFORE any host mutation. Unlike the
# local flock and EXIT trap this evidence survives SIGKILL of the launcher.
SUIVI_RUN=${STAGING_RUN_ID:-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')}
SUIVI_DEBUT=$(date +%s)
SUIVI_DELAI=${STAGING_TIMEOUT_S:-7200}
SUIVI_DEMARRE=0
publier_suivi() {  # etat [options de staging_watch…]
  local etat=$1 instantane
  shift
  instantane=$(python3 - "$JOURNAL" "$SUIVI_RUN" "$SHA" "$SUIVI_DEBUT" \
    "$SUIVI_DELAI" "$NOTIF_STAGING" "$etat" "$DEPUIS" <<'PY'
import json, sys
path, run, sha, start, duration, notify, state, since = sys.argv[1:]
duration = float(duration)
if not 0 < duration < float('inf'):
    raise SystemExit('STAGING_TIMEOUT_S must be finite and positive')
hosts = {}
with open(path, encoding='utf-8') as stream:
    for line in stream:
        row = json.loads(line)
        if row.get('sha') == sha and row.get('ts', '') >= since:
            hosts[row['hote']] = {'stage': row['etape'], 'verdict': row['verdict']}
print(json.dumps(dict(run=run, sha=sha, started=float(start),
                     deadline=float(start)+duration, state=state,
                     hosts=hosts, notify=notify)))
PY
  ) || return
  py_sequence "$SUIVI_PY" "$instantane" "$@"
}
if [ "$POSTE_ABSENT" = 1 ]; then
  printf '  %-16s %-8s %s\n' pc-smichea absent "reporte — $POSTE_RAISON"
fi
if [ "$SERVEUR_ABSENT" = 1 ]; then
  printf '  %-16s %-8s %s\n' manaty-mesh-1 absent "reporte — $SERVEUR_RAISON"
fi
if [ "$A_BLANC" = 1 ]; then
  if [ "$POSTE_ABSENT" = 1 ]; then
    echo "  etapes : verifier les VM de la sequence ; sauvegarde du serveur sur manaty-mesh-1 (poste absent ; sautee si le serveur est reporte) ; migrations appliquees depuis une VM d'agents (additif, dry-run d'abord) ; puis par hote installer, redemarrer, timers (agents et poste), controler, sante"
  else
    echo "  etapes : verifier partout, sauvegarder+migrer ${SHA_TRAVE} (additif), puis par hote installer, redemarrer, timers (agents et poste), controler, sante"
  fi
  if [ "$SANS_RETOUR" = 1 ]; then
    echo "  retour automatique desactive (--sans-retour) : un echec arrete la sequence sans retour ; marche manuelle au pilote, rollback SQL ${ROLLBACK_SQL:-de la migration posee} d'abord"
  fi
  if [ "$SEQUENCE_COMPLETE" = 1 ]; then
    echo "  sequence complete et saine : attestation « staging actif » et courrier aux rapporteurs"
  else
    echo "  sequence partielle : aucune attestation staging"
  fi
  exit 0
fi

# La preuve d'absence du poste precede toute mutation : actif.py exige cet
# evenement pour attester les quatre VM sans lui. Il porte le commit vise,
# comme tous les autres, et n'est jamais un `retour`.
# #4885 : le verrou flock ne vaut que pour cet hote. Le debut est exclusif
# dans le journal partage : refuse (75, rien n'est modifie) tant qu'une autre
# sequence est `running`, quelle que soit sa voie (executant elu #4880,
# lancement a la main, release synchrone), meme apres son echeance : celle-ci
# alerte mais ne prouve aucun arret. STAGING_PREVIOUS_RUN nomme une sequence
# dont l'arret est prouve (sonde de quiescence de l'executant, ou l'operateur
# qui l'a verifie) : elle est close en `failed` et ne bloque plus.
rc_suivi=0
publier_suivi running --exclusive \
  ${STAGING_PREVIOUS_RUN:+--supersede "$STAGING_PREVIOUS_RUN"} || rc_suivi=$?
if [ "$rc_suivi" -eq 75 ]; then
  echo "staging deja en cours (journal partage staging.sequence) : rien n'est modifie" >&2
  exit 75
fi
[ "$rc_suivi" -eq 0 ] || exit "$rc_suivi"
SUIVI_DEMARRE=1
if [ "$POSTE_ABSENT" = 1 ]; then
  trace pc-smichea absent reporte "$POSTE_RAISON"
  echo "  pc-smichea : absent (reporte) — $POSTE_RAISON"
fi
if [ "$SERVEUR_ABSENT" = 1 ]; then
  trace "$HOTE_SERVEUR" absent reporte "$SERVEUR_RAISON"
  echo "  $HOTE_SERVEUR : absent (reporte) — $SERVEUR_RAISON"
fi

# --- preflight (lecture seule), sauvegarde, puis migrations ---------------
etape "preflight : verifier chaque hote (lecture seule)"
for nom in "${HOTES[@]}"; do
  # #4050 : sur le chemin sans poste, les migrations partent de
  # HOTE_MIGRATIONS avec le clone DEPOT_VM. Un hote neuf n'en a pas : le
  # constater ici, avant sauvegarde, migration et tout arret de runner. Un
  # refus nomme, sans retour automatique : rien n'a ete modifie.
  if [ "$POSTE_ABSENT" = 1 ] && [ "$nom" = "$HOTE_MIGRATIONS" ] \
     && ! verifier_clone_migrations "$nom"; then
    trace "$nom" verifier ko "clone des migrations absent ou illisible ($DEPOT_VM)"
    echo "preflight en echec sur $nom : rien n'a ete modifie" >&2
    exit 1
  fi
  if ! exec_hote "$nom" verifier; then
    trace "$nom" verifier ko "echec du preflight"
    echo "preflight en echec sur $nom : rien n'a ete modifie" >&2
    exit 1
  fi
  trace "$nom" verifier ok
done

if [ "$SAUVER" = 1 ]; then
  if [ "$POSTE_ABSENT" = 1 ] && [ "$SERVEUR_ABSENT" = 1 ]; then
    # serveur reporte (aucun acces root) : aucun dump possible d'ici ; la
    # sauvegarde quotidienne du serveur reste la seule (bureau #147558). Rien
    # n'est journalise a son nom : un hote reporte ne porte que son evenement
    # absent/reporte, toute autre etape contredirait l'absence (actif.py,
    # preuve_absence ; avis cx2 sur h-34).
    etape "sauvegarde sautee (poste et serveur reportes)"
    echo "  sauvegarde non faite (poste et serveur reportes) ; sauvegarde quotidienne du serveur seule"
  elif [ "$POSTE_ABSENT" = 1 ]; then
    etape "sauvegarde du serveur sur manaty-mesh-1 (poste absent, dump garde sur la VM)"
    if ! meta=$(sauvegarde_serveur_vm); then
      trace manaty-mesh-1 sauvegarde ko "sauvegarde serveur en echec"
      echo "sauvegarde serveur en echec : rien n'a ete modifie" >&2
      exit 1
    fi
    dump=$(sed -n '1p' <<<"$meta")
    taille=$(sed -n '2p' <<<"$meta")
    empreinte=$(sed -n '3p' <<<"$meta")
    case "$dump" in
      "$SAUVEGARDES_VM"/ameesh-*.dump) ;;
      *) echo "sauvegarde serveur : chemin de dump inattendu" >&2; exit 1 ;;
    esac
    case "$taille" in ''|*[!0-9]*|0) echo "sauvegarde serveur : taille invalide" >&2; exit 1 ;; esac
    case "$empreinte" in
      ''|*[!0-9a-f]*) echo "sauvegarde serveur : empreinte invalide" >&2; exit 1 ;;
    esac
    [ "${#empreinte}" -eq 64 ] || { echo "sauvegarde serveur : empreinte invalide" >&2; exit 1; }
    trace manaty-mesh-1 sauvegarde ok "dump serveur avant migrations : $dump ($taille octets, sha256 $empreinte)"
    echo "  sauvegarde serveur : $dump ($taille octets)"
  else
    etape "sauvegarde de la base du mesh (poste.sh sauvegarder)"
    "$POSTE_SH" sauvegarder || { echo "sauvegarde en echec : rien n'a ete modifie" >&2; exit 1; }
    trace poste sauvegarde ok "sauvegarde avant migrations"
  fi
fi

if [ "$POSTE_ABSENT" = 1 ]; then
  # Chemin VM (#3848) : la base commune est migree depuis une VM d'agents, avec
  # le meme instantane et la meme garde additive que le bureau (HOTE_MIGRATIONS
  # est choisi et valide avant le plan, avec HOTE_OUTILS).
  etape "migrations de $SHA_TRAVE depuis $HOTE_MIGRATIONS (poste absent, additives seulement)"
  if ! migrer_depuis_vm "$HOTE_MIGRATIONS" "$SHA"; then
    trace "$HOTE_MIGRATIONS" migrations ko "migrations en echec depuis la VM"
    echo "migrations en echec : aucune installation" >&2
    exit 1
  fi
  trace "$HOTE_MIGRATIONS" migrations ok "migrations de $SHA_TRAVE appliquees depuis un snapshot VM (additif, dry-run d'abord)"
else
  etape "migrations de $SHA_TRAVE (additives seulement, base partagee)"
  # #2357 : une sonde d'auto-prise en echec ne doit pas arreter la sequence. La
  # suite par hote installe et redemarre les VM d'abord, le poste en dernier
  # (canon §14) ; sans ce drapeau, poste.sh redemarrerait le poste tout de suite
  # et interromprait l'operateur avant les VM, sur une base deja migree.
  MIGRER_SANS_REDEMARRAGE=1 "$POSTE_SH" migrer --additif-seulement "$SHA" || { echo "migrations en echec : aucune installation" >&2; exit 1; }
  trace poste migrations ok "migrations de $SHA_TRAVE passees avant toute installation"
fi

# --- sequence par hote ----------------------------------------------------
for nom in "${HOTES[@]}"; do
  installe=$(sha_installe "$nom" || true)
  # le poste publie un nom court (src-<7>) ; les VM un SHA complet : comparer
  # le prefixe installe au prefixe correspondant de la cible.
  if [ -n "$installe" ] && [ "$installe" = "${SHA:0:${#installe}}" ]; then
    # rien n'est reinstallé : « retour » rejouerait le previous-sha conserve,
    # pas la version courante (avis R1 N2).
    PRECEDENT=$(precedent_retour "$nom" || true)
    etape "$nom : deja au commit $SHA_TRAVE — installation ignoree (previous-sha preserve)"
    trace "$nom" installer ok "deja installe (${installe:0:9})"
  else
    # l'installateur enregistre la version courante comme cible de retour.
    PRECEDENT=$installe
    etape "$nom : installer $SHA_TRAVE"
    exec_hote "$nom" installer "$SHA" || echec_hote "$nom" installer "installation en echec"
    trace "$nom" installer ok "installe ${SHA_TRAVE} (precedent ${PRECEDENT:-inconnu})"
  fi
  if [ "$(mode_de "$nom")" = poste ]; then
    etape "$nom : role"
    exec_hote "$nom" role || echec_hote "$nom" role "role en echec"
    trace "$nom" role ok
  fi
  etape "$nom : redemarrer"
  exec_hote "$nom" redemarrer || echec_hote "$nom" redemarrer "redemarrage en echec"
  trace "$nom" redemarrer ok
  # #4416 : installer les nouvelles unites avant leur controle, sur le poste
  # et les VM d'agents ; le serveur historique n'expose pas cette action.
  if [ "$(mode_de "$nom")" != serveur ]; then
    etape "$nom : timers"
    exec_hote "$nom" timers || echec_hote "$nom" timers "installation des minuteries en echec"
    trace "$nom" timers ok
  fi
  etape "$nom : controler"
  exec_hote "$nom" controler || echec_hote "$nom" controler "controles en echec"
  trace "$nom" controler ok
  etape "$nom : sante"
  rc=0
  sante_hote "$nom" || rc=$?
  case $rc in
    0) ;;
    3) sonde_non_concluante "$nom"; continue ;;
    *) echec_hote "$nom" sante "sante en echec" ;;
  esac
  # `detail` reste le rapport JSON (lu par actif.py) ; le meme contenu part en
  # objet `rapport` pour la lecture structuree de la trace (#2190).
  trace "$nom" sante ok "$SANTE_DETAIL" "$SANTE_DETAIL"
done

etape "sequence staging terminee : ${HOTES[*]} sur $SHA_TRAVE"
if [ "${#NON_CONCLUANTS[@]}" -gt 0 ]; then
  echo "sonde non concluante (reportee au staging suivant) : ${NON_CONCLUANTS[*]}" >&2
fi
echo "traces : $JOURNAL"
trace sequence sequence ok

# --- signal « staging actif » (#1323) ------------------------------------
# Durable et borne : l'attestation n'est ecrite que si les quatre VM (et le
# poste s'il est en ligne) sont tous prouves (installer, redemarrer, controler,
# sante reelle) et si le commit relu apres la sequence est la cible ; chaque
# rapporteur n'est reveille qu'une fois par commit. Un poste absent est admis :
# l'attestation porte les quatre VM et `reportes` nomme le poste et sa mise a
# jour a son retour. Un echec d'emission ne remet pas en cause le deploiement :
# l'attestation reste, le relais operateur prend le courrier.
if [ "$SEQUENCE_COMPLETE" != 1 ]; then
  echo "signal actif : sequence partielle (${HOTES[*]}), aucune attestation staging" >&2
else
  etape "signal actif staging ($SHA_TRAVE)"
  ARGS_ACTIF=(--sha "$SHA" --depuis "$DEPUIS")
  [ "$SONDE" = 1 ] || ARGS_ACTIF+=(--sans-sonde)
  [ "$SANS_RETOUR" = 0 ] || ARGS_ACTIF+=(--sans-retour)
  for nom in "${HOTES[@]}"; do
    actif_sha=$(sha_installe "$nom" || true)
    ARGS_ACTIF+=(--hote "$nom=${actif_sha,,}" --mode "$nom=$(mode_de "$nom")")
  done
  if [ "$POSTE_ABSENT" = 1 ]; then
    ARGS_ACTIF+=(--absent "pc-smichea=$POSTE_RAISON")
  fi
  if [ "$SERVEUR_ABSENT" = 1 ]; then
    ARGS_ACTIF+=(--absent "$HOTE_SERVEUR=$SERVEUR_RAISON")
  fi
  for lot in ${VERIF_LOTS[@]+"${VERIF_LOTS[@]}"}; do
    ARGS_ACTIF+=(--lot "$lot")
  done
  for rapporteur in ${VERIF_RAPPORTEURS[@]+"${VERIF_RAPPORTEURS[@]}"}; do
    ARGS_ACTIF+=(--rapporteur "$rapporteur")
  done
  rc=0
  ATTESTATION_ACTIF="$TRAVAIL/actif-${SHA_TRAVE:0:12}.json"
  JOURNAL_ACTIF="$JOURNAL"
  # #3981 : depuis bureau-goal, actif.py tourne sur HOTE_OUTILS avec le venv et
  # la base d'une VM ; le journal local part avec lui. La preuve partagee est
  # un evenement `ameesh_events` que release_cutoff relit sur le poste.
  if [ "$OUTILS_DISTANTS" = 1 ]; then
    ATTESTATION_ACTIF="$TRAVAIL_OUTILS/actif-${SHA_TRAVE:0:12}.json"
    JOURNAL_ACTIF="$TRAVAIL_OUTILS/staging-$(date +%F)-$$.jsonl"
    if copier_journal_outils "$JOURNAL" "$JOURNAL_ACTIF"; then
      py_sequence "$ACTIF_PY" --journal "$JOURNAL_ACTIF" "${ARGS_ACTIF[@]}" || rc=$?
    else
      echo "signal actif non emis : journal non copie vers $HOTE_OUTILS ($JOURNAL)" >&2
      rc=1
    fi
  else
    "$VENV/bin/python" "$ACTIF_PY" --journal "$JOURNAL_ACTIF" "${ARGS_ACTIF[@]}" || rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    publier_suivi unattested || echo "suivi staging indisponible" >&2
    SUIVI_DEMARRE=0
    if [ "$rc" -eq 5 ]; then
      echo "signal actif ambigu (emission anterieure non prouvee, aucun renvoi automatique) : attestation $ATTESTATION_ACTIF a verifier, journal $JOURNAL_ACTIF a relayer a la main" >&2
    elif [ "$OUTILS_DISTANTS" = 1 ]; then
      echo "signal actif non emis (rc=$rc) : journal $JOURNAL_ACTIF sur $HOTE_OUTILS a relayer a la main" >&2
    else
      echo "signal actif non emis (rc=$rc) : journal $JOURNAL_ACTIF a relayer a la main" >&2
    fi
  fi
fi

if [ "$SUIVI_DEMARRE" = 1 ]; then
  if [ "$SEQUENCE_COMPLETE" = 1 ]; then
    publier_suivi completed || echo "suivi staging indisponible" >&2
  else
    publier_suivi partial || echo "suivi staging indisponible" >&2
  fi
  SUIVI_DEMARRE=0
fi
