#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Mise a jour « staging » du mesh ameesh : les quatre hotes, VM d'abord, poste
# en dernier, sante bornee par hote, retour automatique, traces JSONL et
# attestation « staging actif » vers le rapporteur (#1323).
# Procedure : docs/DEPLOIEMENT-HOTE-AGENTS.md, « Sequence staging ».
#
#   deploy/mise-a-jour/staging.sh [--a-blanc] [--hotes "nom …"]
#                                [--lots "n …"] [--rapporteurs "agent …"]
#                                [--sans-sauvegarde] [--sans-sonde] [REF]
#
# REF : commit, branche ou etiquette du depot (defaut : HEAD). Il doit etre un
# ancetre de origin/main, sauf HORS_MAIN=1 (BRANCHE_MAIN pour viser une autre
# branche).
#
# HOTES_STAGING (BASCULE_ENV) : la topologie, une entree « nom=cible » par
# hote, dans cet ordre :
#   ameesh-agents-1=root@10.77.0.3 ameesh-agents-2=root@10.77.0.4
#   manaty-mesh-1=root@10.77.0.2 pc-smichea=poste
# « poste » (ou « local ») designe le poste, toujours en dernier. Le mode de
# chaque hote est deduit de son nom : manaty-mesh-1 est le serveur du mesh
# (Postgres, `vm.sh`), pc-smichea le poste (`poste.sh`), les autres sont des
# VM d'agents (`vm.sh agents`). VM (BASCULE_ENV) doit etre l'adresse de
# manaty-mesh-1 ; PERSONAS_VM peut etre vide (hote sans executant).
#
# Options lues dans BASCULE_ENV : SONDE_<HOTE> (cible de la sonde de courrier,
# par defaut la premiere persona menee de l'hote), ATTENTE_COURRIER (300 s),
# SOUFFRANCE_COURRIER (900 s), NOTIF_STAGING (mesh-design), ATTENTE_MAX
# (vm.sh/poste.sh), LOTS_VERIF et RAPPORTEURS_VERIF (voir le signal ci-dessous).
#
# --hotes limite la sequence a un sous-ensemble, dans l'ordre canonique. Les
# noms sont normalises : un seul exemplaire par hote, quel que soit le
# separateur (espace, tabulation, nouvelle ligne). Le poste ne peut pas y
# figurer sans les trois VM : le canon §14 exige des VM saines avant toute
# modification du poste, et un sous-ensemble n'en apporte aucune preuve.
#
# --lots / --rapporteurs (ou LOTS_VERIF / RAPPORTEURS_VERIF) : les lots livres
# par ce deploiement et les agents a reveiller quand il est reellement actif.
# En fin de sequence complete et saine, l'attestation « staging actif »
# (deploy/mise-a-jour/actif.py, #1323) est ecrite sous
# ~/.local/state/ameesh/mise-a-jour/actif-<commit>.json et chaque rapporteur
# recoit un courrier unique par commit (hotes, commit relu actif, preuves
# installer/redemarrer/controler/sante). Un echec ou un retour n'emet aucun
# signal de reussite ; un sous-ensemble n'atteste rien ; aucun lot n'est clos
# automatiquement.
#
# La preuve d'activation d'un hote en mode serveur (sans executant, comme
# manaty-mesh-1) est specifique : son `controler` vert et une sonde croisee
# remise a une persona menee d'un AUTRE hote canonique, posterieure a son
# redemarrage — la remise traverse alors sa base (actif.py ; avis R2 B1 #1323).
# Un hote a executants garde la regle locale : une sante `sans-sonde` (aucune
# persona menee) n'atteste rien, et une remise croisee n'y est pas acceptee.
#
# Etapes : verifier chaque hote (lecture seule) ; sauvegarder puis migrer REF
# (additif seulement, base partagee) ; puis, hote par hote dans l'ordre
# ci-dessus : installer, [poste] role, redemarrer, controler, sante (unites,
# baux et doctor par `controler`, puis courrier reellement remis par une
# sonde). Un echec = retour automatique de l'HOTE (previous-sha), controle du
# retour, arret de la sequence et courrier urgent au pilote. Rien n'est
# repris apres un echec ; relancer la commande est sur : un hote deja au
# commit vise ne le reinstallle pas (le previous-sha reste le bon).
#
# Traces : une ligne JSON par evenement (date, hote, etape, verdict, sha,
# precedent, detail) dans ~/.local/state/ameesh/mise-a-jour/staging-<date>.jsonl ;
# `precedent` nomme la version que « retour » reinstalle reellement. Le detail
# de l'etape `sante` est le rapport JSON de sante-courrier.py (hote, commit,
# verdict, message de sonde remis, destinataire, delai) : c'est la preuve
# structuree que actif.py exige (#1323) ; un verdict `sans-sonde` n'en est pas
# une et aucun signal de reussite n'est emis dans ce cas.
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
PARTAGE="$HOME/.local/share/ameesh"
VENV="$PARTAGE/venv"
AMEESH="$VENV/bin/ameesh"
TRAVAIL="$HOME/.local/state/ameesh/mise-a-jour"
JOURNAL="$TRAVAIL/staging-$(date +%F).jsonl"
#: debut de l'execution (UTC) : fenetre de l'attestation, le journal etant
#: quotidien et pouvant contenir une execution precedente du meme jour.
DEPUIS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ATTENTE_COURRIER=${ATTENTE_COURRIER:-300}
SOUFFRANCE_COURRIER=${SOUFFRANCE_COURRIER:-900}
NOTIF_STAGING=${NOTIF_STAGING:-mesh-design}
LOTS_VERIF=${LOTS_VERIF:-}
RAPPORTEURS_VERIF=${RAPPORTEURS_VERIF:-}
#: ordre canonique du staging : VM d'abord, poste en dernier
ORDRE=(ameesh-agents-1 ameesh-agents-2 manaty-mesh-1 pc-smichea)

A_BLANC=0
SAUVER=1
SONDE=1
HOTES_FILTRE=""
REF=HEAD
POSITIONNELS=0

usage() { sed -n '7,20p' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --a-blanc) A_BLANC=1 ;;
    --sans-sauvegarde) SAUVER=0 ;;
    --sans-sonde) SONDE=0 ;;
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
HOTES=()
if [ -z "${HOTES_STAGING:-}" ]; then
  echo "HOTES_STAGING absent de $BASCULE_ENV : declarer les quatre hotes" >&2
  echo "  HOTES_STAGING=\"ameesh-agents-1=root@… ameesh-agents-2=root@… manaty-mesh-1=root@… pc-smichea=poste\"" >&2
  exit 2
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
for nom in "${ORDRE[@]}"; do [ -n "${CIBLE[$nom]:-}" ] && HOTES+=("$nom"); done
[ ${#HOTES[@]} -eq ${#ORDRE[@]} ] \
  || { echo "HOTES_STAGING : les quatre hotes sont requis (${ORDRE[*]})" >&2; exit 2; }
[ "${HOTES[-1]}" = pc-smichea ] || { echo "HOTES_STAGING : pc-smichea doit etre present" >&2; exit 2; }

if [ -n "$HOTES_FILTRE" ]; then
  # Selection normalisee : un nom connu compte une seule fois, quel que soit
  # le separateur (espace, tabulation, nouvelle ligne) ; le globbing est
  # desactive pour que chaque mot soit compare tel quel. « pc-smichea » repete
  # quatre fois ne peut donc plus tenir lieu des trois VM (avis R1 B4).
  set -f
  for choisi in $HOTES_FILTRE; do
    [ -n "${CIBLE[$choisi]:-}" ] \
      || { echo "--hotes : « $choisi » n'est pas dans HOTES_STAGING" >&2; exit 2; }
    CHOISI[$choisi]=1
  done
  set +f
  HOTES=()
  for nom in "${ORDRE[@]}"; do
    [ -n "${CHOISI[$nom]:-}" ] && HOTES+=("$nom")
  done
  # canon §14 : le poste ne se met a jour qu'apres des VM saines. Le controle
  # porte sur la selection normalisee, jamais sur la chaine brute : le case
  # sur espaces litteraux etait contourne par tabulation, nouvelle ligne et
  # doublons (avis R1 B4).
  if [ -n "${CHOISI[pc-smichea]:-}" ]; then
    for nom in "${ORDRE[@]}"; do
      [ -n "${CHOISI[$nom]:-}" ] \
        || { echo "--hotes : pc-smichea ne se met a jour que dans la sequence complete des quatre hotes (canon §14 : VM saines d'abord)" >&2; exit 2; }
    done
  fi
fi
[ ${#HOTES[@]} -gt 0 ] || { echo "--hotes : liste vide" >&2; exit 2; }

# VM sert aussi a poste.sh (migrations, sauvegarde) : la topologie et VM
# doivent dire la meme chose.
ADRESSE_MESH=${CIBLE[manaty-mesh-1]#*@}
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

# Un BASCULE_ENV temporaire par hote : vm.sh relit le fichier qu'on lui donne,
# et la topologie doit primer sur d'eventuels AGENTS_SSH/HOTE_AGENTS restes
# dans le fichier prive.
nettoyer() {
  local f
  for f in ${FICHIER_HOTE[@]+"${FICHIER_HOTE[@]}"}; do rm -f "$f"; done
}
trap nettoyer EXIT
for nom in "${HOTES[@]}"; do
  f="$TRAVAIL/staging-$$-$nom.env"
  ( umask 077; : > "$f" )
  case "$(mode_de "$nom")" in
    agents) printf 'AGENTS_SSH=%q\nHOTE_AGENTS=%q\n' "${CIBLE[$nom]}" "$nom" > "$f" ;;
    serveur) printf 'VM=%q\nPERSONAS_VM=%q\n' "$ADRESSE_MESH" "$PERSONAS_VM_VAL" > "$f" ;;
    poste) ;;
  esac
  FICHIER_HOTE[$nom]=$f
done

SHA_TRAVE=${SHA:0:9}
PRECEDENT=""
trace() {  # hote etape verdict [detail]
  python3 - "$JOURNAL" "$1" "$2" "$3" "${4:-}" "$SHA" "$PRECEDENT" <<'PY'
import json, sys, time
path, hote, etape, verdict, detail, sha, precedent = sys.argv[1:8]
line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hote": hote, "etape": etape, "verdict": verdict,
        "sha": sha or None, "precedent": precedent or None,
        "detail": detail or None}
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(line, ensure_ascii=False) + "\n")
PY
}

# --- gestes par hote ------------------------------------------------------
exec_hote() {  # nom action [args…]
  local nom=$1; shift
  case "$(mode_de "$nom")" in
    poste) "$POSTE_SH" "$@" ;;
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
sante_hote() {  # nom [jamais] : la preuve (rapport JSON) va dans SANTE_DETAIL
  local nom=$1 sans=${2:-} cible_sonde var sortie args_hote=()
  SANTE_DETAIL=""
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
    sortie=$("$VENV/bin/python" "$SONDE_PY" --hote "$nom" --sonde "${cible_sonde:-auto}" \
      --attente "$ATTENTE_COURRIER" --souffrance "$SOUFFRANCE_COURRIER" --sha "$SHA" --json \
      ${args_hote[@]+"${args_hote[@]}"}) \
      || { printf '%s\n' "$sortie"; return 1; }
    printf '%s\n' "$sortie"
    SANTE_DETAIL=$sortie
  else
    etape "courrier de $nom (lecture seule, sonde desactivee)"
    sortie=$("$VENV/bin/python" "$SONDE_PY" --hote "$nom" --sonde jamais \
      --souffrance "$SOUFFRANCE_COURRIER" --sha "$SHA" --json \
      ${args_hote[@]+"${args_hote[@]}"}) \
      || { printf '%s\n' "$sortie"; return 1; }
    printf '%s\n' "$sortie"
    SANTE_DETAIL=$sortie
  fi
}
notifier() {  # sujet corps
  local sujet=$1 corps=$2
  "$VENV/bin/python" - "$NOTIF_STAGING" "$sujet" "$corps" <<'PY' \
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
  local nom=$1 ou=$2 detail=$3
  trace "$nom" "$ou" ko "$detail"
  echo "!! $nom : $ou en echec — $detail" >&2
  etape "$nom : retour automatique (previous-sha)"
  if ! exec_hote "$nom" retour; then
    trace "$nom" retour ko "le retour lui-meme a echoue"
    notifier "staging #1155 : retour de $nom en ECHEC" \
      "Le staging $SHA_TRAVE a echoue sur $nom ($ou : $detail) et son retour automatique a echoue. Sequence arretee ; intervention requise. Trace : $JOURNAL"
    exit 1
  fi
  trace "$nom" retour ok "retour au commit precedent applique"
  etape "$nom : controle du retour"
  if ! exec_hote "$nom" controler; then
    trace "$nom" controle-retour ko "l'hote ne repond pas apres le retour"
    notifier "staging #1155 : retour de $nom non verifie" \
      "Le staging $SHA_TRAVE a echoue sur $nom ($ou : $detail) ; le code precedent est reinstalle mais ses controles echouent. Sequence arretee ; intervention requise. Trace : $JOURNAL"
    exit 1
  fi
  trace "$nom" controle-retour ok "unites et baux verts apres retour"
  notifier "staging #1155 : $nom en echec, retour effectue" \
    "Le staging $SHA_TRAVE a echoue sur $nom ($ou : $detail). Retour automatique effectue et controle (unites, baux, doctor). Sequence arretee avant les hotes suivants : rien d'autre n'a ete modifie. Trace : $JOURNAL"
  echo "sequence arretee sur $nom (retour effectue et controle)" >&2
  exit 1
}

# --- plan -----------------------------------------------------------------
etape "plan staging : ${HOTES[*]} ; cible $SHA_TRAVE"
for nom in "${HOTES[@]}"; do
  printf '  %-16s %-8s %s\n' "$nom" "$(mode_de "$nom")" "${CIBLE[$nom]}"
done
if [ "$A_BLANC" = 1 ]; then
  echo "  etapes : verifier partout, sauvegarder+migrer ${SHA_TRAVE} (additif), puis par hote installer, redemarrer, controler, sante ; sequence complete et saine : attestation « staging actif » et courrier aux rapporteurs"
  exit 0
fi

# --- preflight (lecture seule), sauvegarde, puis migrations ---------------
etape "preflight : verifier chaque hote (lecture seule)"
for nom in "${HOTES[@]}"; do
  if ! exec_hote "$nom" verifier; then
    trace "$nom" verifier ko "echec du preflight"
    echo "preflight en echec sur $nom : rien n'a ete modifie" >&2
    exit 1
  fi
  trace "$nom" verifier ok
done

if [ "$SAUVER" = 1 ]; then
  etape "sauvegarde de la base du mesh (poste.sh sauvegarder)"
  "$POSTE_SH" sauvegarder || { echo "sauvegarde en echec : rien n'a ete modifie" >&2; exit 1; }
  trace poste sauvegarde ok "sauvegarde avant migrations"
fi

etape "migrations de $SHA_TRAVE (additives seulement, base partagee)"
"$POSTE_SH" migrer --additif-seulement "$SHA" || { echo "migrations en echec : aucune installation" >&2; exit 1; }
trace poste migrations ok "migrations de $SHA_TRAVE passees avant toute installation"

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
  etape "$nom : controler"
  exec_hote "$nom" controler || echec_hote "$nom" controler "controles en echec"
  trace "$nom" controler ok
  etape "$nom : sante"
  sante_hote "$nom" || echec_hote "$nom" sante "sante en echec"
  trace "$nom" sante ok "$SANTE_DETAIL"
done

etape "sequence staging terminee : ${HOTES[*]} sur $SHA_TRAVE"
echo "traces : $JOURNAL"
trace sequence sequence ok

# --- signal « staging actif » (#1323) ------------------------------------
# Durable et borne : l'attestation n'est ecrite que si les quatre hotes sont
# tous prouves (installer, redemarrer, controler, sante reelle) et si le commit
# relu apres la sequence est la cible ; chaque rapporteur n'est reveille qu'une
# fois par commit. Un echec d'emission ne remet pas en cause le deploiement :
# l'attestation reste, le relais operateur prend le courrier.
if [ ${#HOTES[@]} -ne 4 ]; then
  echo "signal actif : sequence partielle (${HOTES[*]}), aucune attestation staging" >&2
else
  etape "signal actif staging ($SHA_TRAVE)"
  ARGS_ACTIF=(--journal "$JOURNAL" --sha "$SHA" --depuis "$DEPUIS")
  [ "$SONDE" = 1 ] || ARGS_ACTIF+=(--sans-sonde)
  for nom in "${HOTES[@]}"; do
    actif_sha=$(sha_installe "$nom" || true)
    ARGS_ACTIF+=(--hote "$nom=${actif_sha,,}" --mode "$nom=$(mode_de "$nom")")
  done
  for lot in ${VERIF_LOTS[@]+"${VERIF_LOTS[@]}"}; do
    ARGS_ACTIF+=(--lot "$lot")
  done
  for rapporteur in ${VERIF_RAPPORTEURS[@]+"${VERIF_RAPPORTEURS[@]}"}; do
    ARGS_ACTIF+=(--rapporteur "$rapporteur")
  done
  rc=0
  "$VENV/bin/python" "$ACTIF_PY" "${ARGS_ACTIF[@]}" || rc=$?
  if [ "$rc" -ne 0 ]; then
    if [ "$rc" -eq 5 ]; then
      echo "signal actif ambigu (emission anterieure non prouvee, aucun renvoi automatique) : attestation $TRAVAIL/actif-${SHA_TRAVE:0:12}.json a verifier, journal $JOURNAL a relayer a la main" >&2
    else
      echo "signal actif non emis (rc=$rc) : journal $JOURNAL a relayer a la main" >&2
    fi
  fi
fi
