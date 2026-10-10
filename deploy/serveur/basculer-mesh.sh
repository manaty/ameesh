#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Bascule d'une organisation vers le serveur de son mesh (lot L57), lancée
# depuis le poste, étape par étape. Mise en œuvre de DEPLACER-UN-MESH.md.
#
#   deploy/serveur/basculer-mesh.sh <étape>
#
#   verifier     contrôles, rien n'est modifié
#   preparer     VM : accès forge, canon, configuration, dossiers de travail
#                (sans arrêt des personas)
#   arreter      arrête les exécuteurs du poste des personas de l'organisation,
#                chacune entre deux tours
#   copier       vidage, scission sur le poste, chargement sur la VM, migrate,
#                doctor, canon sync, sessions des personas déplacées
#   rebrancher   poste : l'ancienne base garde les autres organisations, la
#                configuration par défaut pointe sur la base du serveur
#   demarrer     exécuteurs : sur la VM pour les personas déplacées, sur le
#                poste pour les autres
#   etat         vue d'ensemble
#
# Paramètres : fichier $BASCULE_ENV (défaut ~/.config/ameesh/bascule.env),
# jamais dans le dépôt (il nomme l'organisation, ses dépôts et ses dossiers) :
#
#   VM=10.77.0.1                      adresse WireGuard du serveur
#   HOTE_VM=org-mesh-1                nom d'hôte ameesh du serveur (fiche Host)
#   EQUIPES='{equipe1,equipe2}'       équipes de l'organisation
#   CANON_GARDE=''                    canon gardé ('' = canon par défaut)
#   CANON_POSTE=~/canon               clone local du canon de l'organisation
#   CANON_DEPOT=git@github-canon:org/home.git   pour la VM (alias SSH ci-dessous)
#   CLE_CANON=canon CLE_CODE=code     clés de déploiement dans ~ameesh/.ssh
#   PERSONAS_VM="p1 p2"               personas qui tourneront sur la VM
#   DOSSIER_POSTE='~/dev/projet-{agent}'   dossier d'une persona sur le poste
#   DOSSIER_VM='/home/ameesh/projet-{agent}' son dossier sur la VM
#   PERSONAS_ATTACHEES="p3"           sessions interactives : pas d'exécuteur
#                                     relancé (à rattacher par leur humain)
#   UNITES_RESTE="ameesh-runner-x.service"  exécuteurs du poste qui restent
#                                     sur l'ancienne base (autres organisations)
#   DOSSIERS_RESTE="/chemin/org2"     dossiers dont les sessions restent sur
#                                     l'ancienne base (mise.local.toml)
#
# Aucun secret n'est affiché : le mot de passe de la base du serveur passe de
# /etc/ameesh/db.env au ~/.pgpass du poste sans sortir sur la console.
set -euo pipefail

BASCULE_ENV=${BASCULE_ENV:-$HOME/.config/ameesh/bascule.env}
[ -r "$BASCULE_ENV" ] || { echo "paramètres absents : $BASCULE_ENV" >&2; exit 2; }
# shellcheck disable=SC1090
. "$BASCULE_ENV"
: "${VM:?}" "${HOTE_VM:?}" "${EQUIPES:?}" "${CANON_POSTE:?}" "${CANON_DEPOT:?}"
: "${PERSONAS_VM:?}" "${DOSSIER_POSTE:?}" "${DOSSIER_VM:?}"
CANON_GARDE=${CANON_GARDE:-}
CLE_CANON=${CLE_CANON:-canon}
CLE_CODE=${CLE_CODE:-code}
PERSONAS_ATTACHEES=${PERSONAS_ATTACHEES:-}
UNITES_RESTE=${UNITES_RESTE:-}
DOSSIERS_RESTE=${DOSSIERS_RESTE:-}

ICI="$(cd "$(dirname "$0")" && pwd)"
CONF_DIR="$HOME/.config/ameesh"
CONFIG="$CONF_DIR/config.json"
CONFIG_RESTE="$CONF_DIR/config-ancienne-base.json"
TRAVAIL="$HOME/.local/state/ameesh/bascule"
mkdir -p "$TRAVAIL"; chmod 700 "$TRAVAIL"

ssh_vm() { ssh -o BatchMode=yes -o ConnectTimeout=10 "root@$VM" "$@"; }
dossier() { local g=$1; g=${g//\{agent\}/$2}; echo "${g/#\~/$HOME}"; }
etape() { printf '\n== %s\n' "$*"; }

# DSN de l'ancienne base : celle de la configuration d'avant la bascule
dsn_ancienne() {
  local f="$CONFIG"
  [ -f "$CONFIG_RESTE" ] && f="$CONFIG_RESTE"
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["dsn"])' "$f"
}
psql_ancienne() { psql -X -q -At -v ON_ERROR_STOP=1 "$(dsn_ancienne)" "$@"; }

# personas gardées : celles des équipes de l'organisation, dans le canon gardé
personas() {
  psql_ancienne -c "SELECT name FROM agent_registry
     WHERE team = ANY ('$EQUIPES'::text[]) AND coalesce(canon,'') = '$CANON_GARDE'
     ORDER BY name"
}
unite_active() { systemctl --user is-active --quiet "ameesh-runner-agent@$1.service"; }
est_dans() { local x=$1; shift; for y in "$@"; do [ "$x" = "$y" ] && return 0; done; return 1; }

verifier() {
  etape "serveur"
  ssh_vm 'hostname; test "$(grep -c "^DEEPSEEK_API_KEY=." /etc/ameesh/harnais.env)" = 1 && echo "clé DeepSeek : présente"'
  ssh_vm "ls /home/ameesh/.ssh/$CLE_CANON /home/ameesh/.ssh/$CLE_CODE >/dev/null && echo 'clés de déploiement : présentes'"
  n=$(ssh_vm "runuser -u postgres -- psql -At -d ameesh -c 'select count(*) from agent_registry'")
  echo "base du serveur : $n persona(s)"
  [ "$n" = 0 ] || { echo "la base du serveur n'est pas vide : arrêt" >&2; exit 1; }
  etape "canon"
  git -C "$CANON_POSTE" fetch -q
  git -C "$CANON_POSTE" cat-file -e "origin/main:hotes/$HOTE_VM.md" \
    && echo "fiche Host $HOTE_VM : sur origin/main" \
    || { echo "fiche Host $HOTE_VM absente d'origin/main (PR du canon à fusionner)" >&2; exit 1; }
  for p in $PERSONAS_VM; do
    git -C "$CANON_POSTE" grep -q "^host: $HOTE_VM" origin/main -- "placements/$p-$HOTE_VM.md" \
      && echo "placement $p → $HOTE_VM : ok" || { echo "placement $p → $HOTE_VM absent" >&2; exit 1; }
  done
  etape "personas de l'organisation (ancienne base)"
  psql_ancienne -F ' ' -c "SELECT name, status, coalesce(host,'') FROM agent_registry
     WHERE team = ANY ('$EQUIPES'::text[]) AND coalesce(canon,'') = '$CANON_GARDE' ORDER BY name"
  etape "dossiers des personas déplacées"
  for p in $PERSONAS_VM; do
    d=$(dossier "$DOSSIER_POSTE" "$p")
    printf '%s : %s, ' "$p" "$(git -C "$d" status -sb | head -1)"
    echo "$(git -C "$d" status --porcelain | wc -l) fichier(s) non validé(s)"
  done
}

preparer() {
  etape "accès forge de l'utilisateur ameesh"
  ssh_vm "set -e
    install -d -m 700 -o ameesh -g ameesh /home/ameesh/.ssh
    cat > /home/ameesh/.ssh/config <<EOF
# Clés de déploiement du mesh (L57)
Host github-canon
  HostName github.com
  User git
  IdentityFile ~/.ssh/$CLE_CANON
  IdentitiesOnly yes
Host github.com
  User git
  IdentityFile ~/.ssh/$CLE_CODE
  IdentitiesOnly yes
EOF
    chown ameesh:ameesh /home/ameesh/.ssh/config; chmod 600 /home/ameesh/.ssh/config"
  etape "canon"
  ssh_vm "set -e; [ -d /home/ameesh/canon ] || runuser -u ameesh -- git clone -q $CANON_DEPOT /home/ameesh/canon
    runuser -u ameesh -- git -C /home/ameesh/canon fetch -q
    runuser -u ameesh -- git -C /home/ameesh/canon log --oneline -1 origin/main"
  etape "configuration ameesh du serveur"
  budget=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("budget_usd_per_hour") or "")' "$CONFIG")
  humains=$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])).get("humans") or []))' "$CONFIG")
  ssh_vm "python3 - '$HOTE_VM' '$budget' '$humains' <<'EOF'
import json, sys
p = '/home/ameesh/.config/ameesh/config.json'
c = json.load(open(p))
c['host'] = sys.argv[1]
c['canons'] = [{'path': '/home/ameesh/canon', 'ref': 'origin/main'}]
c['humans'] = json.loads(sys.argv[3])
if sys.argv[2]:
    c['budget_usd_per_hour'] = float(sys.argv[2])
open(p, 'w').write(json.dumps(c, indent=2, ensure_ascii=False) + '\n')
print('clés :', ', '.join(sorted(c)))
EOF"
  etape "dossiers de travail des personas déplacées"
  for p in $PERSONAS_VM; do
    d=$(dossier "$DOSSIER_POSTE" "$p"); cible=$(dossier "$DOSSIER_VM" "$p")
    depot=$(git -C "$d" remote get-url origin)
    branche=$(git -C "$d" branch --show-current)
    sha=$(git -C "$d" rev-parse HEAD)
    ssh_vm "[ -d '$cible/.git' ] || runuser -u ameesh -- git clone -q '$depot' '$cible'
      cd '$cible' && runuser -u ameesh -- git fetch -q origin"
    # commit du poste absent de la VM après le fetch (commits jamais poussés,
    # ou branche distante supprimée depuis) : par un paquet git, rien n'est poussé
    if ! ssh_vm "cd '$cible' && runuser -u ameesh -- git cat-file -e '$sha^{commit}'" 2>/dev/null; then
      bases=$(git -C "$d" for-each-ref --format='^%(objectname)' refs/remotes/origin/main refs/remotes/origin/develop)
      # shellcheck disable=SC2086
      git -C "$d" bundle create -q "$TRAVAIL/$p.bundle" HEAD $bases
      scp -q -o BatchMode=yes "$TRAVAIL/$p.bundle" "root@$VM:/tmp/$p.bundle"
      ssh_vm "chown ameesh /tmp/$p.bundle; cd '$cible' && runuser -u ameesh -- git fetch -q /tmp/$p.bundle HEAD && rm -f /tmp/$p.bundle"
    fi
    # la branche pointe EXACTEMENT sur le commit du poste (pas sur la pointe
    # d'origin, qui peut différer quand les commits locaux sont sur d'autres
    # branches distantes)
    ssh_vm "cd '$cible' && runuser -u ameesh -- git checkout -q -B '$branche' '$sha' && \
      (runuser -u ameesh -- git branch -q --set-upstream-to='origin/$branche' 2>/dev/null || true) && \
      test \"\$(runuser -u ameesh -- git rev-parse HEAD)\" = '$sha' && \
      echo '$p : '\$(runuser -u ameesh -- git status -sb | head -1)' @ ${sha:0:9}'"
  done
  etape "clé DeepSeek vue par un exécuteur (solde, aucun tour)"
  ssh_vm "systemd-run --quiet --pipe --wait --uid=ameesh --gid=ameesh \
    -p EnvironmentFile=/etc/ameesh/harnais.env -p WorkingDirectory=/home/ameesh \
    /opt/ameesh/venv/bin/ameesh cost balance 2>&1 | tail -3" || echo "solde illisible (à vérifier)"
}

# arrêt d'UNE persona à son premier instant hors tour (sondage court)
arreter_une() {
  local p=$1 st
  for _ in $(seq 1 1440); do
    st=$(psql_ancienne -c "SELECT status FROM agent_registry WHERE name = '$p'")
    if [ "$st" != running ]; then
      systemctl --user stop "ameesh-runner-agent@$p.service"
      echo "$p : arrêté (était $st)"; return 0
    fi
    sleep 5
  done
  echo "$p : toujours en tour après 2 h : arrêt manuel requis" >&2
}

arreter() {
  mapfile -t garde < <(personas)
  etape "arrêt entre deux tours, en parallèle (${#garde[@]} personas)"
  local enfants=()
  for p in "${garde[@]}"; do
    unite_active "$p" || continue
    arreter_une "$p" & enfants+=($!)
  done
  [ ${#enfants[@]} -gt 0 ] && wait "${enfants[@]}"
  systemctl --user stop ameesh-notify.service 2>/dev/null || true
  for p in $PERSONAS_ATTACHEES; do
    echo "$p : session interactive — ne rien envoyer jusqu'au rebranchement"
  done
}

copier() {
  mapfile -t garde < <(personas)
  for p in "${garde[@]}"; do
    unite_active "$p" && { echo "$p tourne encore sur le poste : lancer 'arreter'" >&2; exit 1; }
  done
  base=$(dsn_ancienne); racine=${base%/*}
  etape "vidage et scission sur le poste"
  pg_dump -Fp --no-owner --no-privileges "$base" -f "$TRAVAIL/partage.sql"
  psql -X -q "$base" -c "DROP DATABASE IF EXISTS ameesh_scission" -c "CREATE DATABASE ameesh_scission"
  psql -X -q -v ON_ERROR_STOP=1 "$racine/ameesh_scission" -f "$TRAVAIL/partage.sql" > "$TRAVAIL/restauration.log"
  psql -X -q "$racine/ameesh_scission" -v equipes="$EQUIPES" -v canon_garde="$CANON_GARDE" -f "$ICI/scinder-mesh.sql"
  pg_dump -Fp --no-owner --no-privileges "$racine/ameesh_scission" -f "$TRAVAIL/mesh.sql"
  rm -f "$TRAVAIL/partage.sql"
  etape "chargement sur le serveur"
  scp -q -o BatchMode=yes "$TRAVAIL/mesh.sql" "root@$VM:/tmp/mesh.sql"
  ssh_vm "set -e
    runuser -u postgres -- psql -q -c 'DROP DATABASE IF EXISTS ameesh_essai'
    runuser -u postgres -- psql -q -c 'DROP DATABASE ameesh' -c 'CREATE DATABASE ameesh OWNER ameesh'
    runuser -u postgres -- psql -q -d ameesh -v ON_ERROR_STOP=1 -c 'set role ameesh' -f /tmp/mesh.sql > /tmp/mesh-restauration.log
    rm -f /tmp/mesh.sql
    runuser -l ameesh -c 'ameesh migrate && ameesh doctor' | tail -5
    runuser -l ameesh -c 'ameesh canon sync --fetch' | tail -8"
  # Les sessions DeepSeek ne se déplacent pas : dsh inscrit le dossier de
  # travail d'origine dans l'en-tête et refuse de les reprendre ailleurs
  # (« corrupt session log … cwd identify … »). `demarrer` reprend donc les
  # personas déplacées sur une session neuve (brief de reprise déterministe).
}

rebrancher() {
  [ -f "$CONFIG_RESTE" ] && { echo "déjà rebranché ($CONFIG_RESTE existe)" >&2; exit 1; }
  mapfile -t garde < <(personas)
  etape "ancienne base : personas de l'organisation marquées arrêtées"
  for p in "${garde[@]}"; do
    AMEESH_CONFIG="$CONFIG" agent-runner stop "$p" >/dev/null 2>&1 \
      || psql_ancienne -c "UPDATE agent_registry SET status = 'stopped', stop_reason = 'manuel',
           lease_owner = NULL, lease_expires_at = NULL WHERE name = '$p'" >/dev/null
  done
  echo "${#garde[@]} marquée(s)"
  etape "configuration de l'ancienne base (autres organisations)"
  cp -p "$CONFIG" "$CONF_DIR/config.json.avant-bascule"
  python3 - "$CONFIG" "$CONFIG_RESTE" "$CANON_POSTE" <<'EOF'
import json, os, sys
c = json.load(open(sys.argv[1]))
canon = os.path.realpath(os.path.expanduser(sys.argv[3]))
c["canons"] = [x for x in c.get("canons") or []
               if os.path.realpath(os.path.expanduser(x["path"])) != canon]
open(sys.argv[2], "w").write(json.dumps(c, indent=1, ensure_ascii=False) + "\n")
print("canons gardés :", [x["path"] for x in c["canons"]])
EOF
  chmod 600 "$CONFIG_RESTE"
  for u in $UNITES_RESTE; do
    mkdir -p "$HOME/.config/systemd/user/$u.d"
    printf '[Service]\nEnvironment=AMEESH_CONFIG=%s\n' "$CONFIG_RESTE" > "$HOME/.config/systemd/user/$u.d/ancienne-base.conf"
    echo "$u → ancienne base"
  done
  for d in $DOSSIERS_RESTE; do
    f="$d/mise.local.toml"
    grep -q '^AMEESH_CONFIG' "$f" 2>/dev/null && continue
    cp -p "$f" "$f.avant-bascule" 2>/dev/null || true
    python3 - "$f" "$CONFIG_RESTE" <<'EOF'
import sys
f, conf = sys.argv[1], sys.argv[2]
try:
    t = open(f).read()
except FileNotFoundError:
    t = ""
ligne = 'AMEESH_CONFIG = "%s"\n' % conf
if "[env]" in t:
    t = t.replace("[env]\n", "[env]\n" + ligne, 1)
else:
    t += "\n[env]\n" + ligne
open(f, "w").write(t)
EOF
    echo "$f → ancienne base"
  done
  etape "base du serveur : mot de passe dans ~/.pgpass (non affiché)"
  touch "$HOME/.pgpass"; chmod 600 "$HOME/.pgpass"
  grep -q "^$VM:5432:ameesh:ameesh:" "$HOME/.pgpass" || {
    mdp=$(ssh_vm 'sed -n "s/^PGPASSWORD=//p" /etc/ameesh/db.env')
    [ -n "$mdp" ] || { echo "mot de passe introuvable dans db.env" >&2; exit 1; }
    printf '%s:5432:ameesh:ameesh:%s\n' "$VM" "$mdp" >> "$HOME/.pgpass"; unset mdp; }
  etape "configuration par défaut → base du serveur"
  python3 - "$CONFIG" "$VM" "$CANON_POSTE" <<'EOF'
import json, os, sys
c = json.load(open(sys.argv[1]))
canon = os.path.realpath(os.path.expanduser(sys.argv[3]))
c["dsn"] = "postgresql://ameesh@%s:5432/ameesh?sslmode=require" % sys.argv[2]
c["canons"] = [x for x in c.get("canons") or []
               if os.path.realpath(os.path.expanduser(x["path"])) == canon]
open(sys.argv[1], "w").write(json.dumps(c, indent=1, ensure_ascii=False) + "\n")
print("canons :", [x["path"] for x in c["canons"]])
EOF
  ameesh doctor 2>&1 | tail -4
  systemctl --user daemon-reload
  for u in $UNITES_RESTE; do systemctl --user restart "$u"; done
  systemctl --user start ameesh-notify.service 2>/dev/null || true
}

demarrer() {
  mapfile -t garde < <(psql -X -q -At "postgresql://ameesh@$VM:5432/ameesh?sslmode=require" \
    -c "SELECT name FROM agent_registry WHERE status <> 'stopped' ORDER BY name")
  etape "serveur"
  for p in $PERSONAS_VM; do
    systemctl --user disable --now "ameesh-runner-agent@$p.service" 2>/dev/null || true
    # session non portable d'un dossier à l'autre : session neuve sur brief
    env -u AMEESH_DSN -u AGENT_MESH_DSN ameesh resume "$p" --fresh | tail -1
    ssh_vm "systemctl enable --now ameesh-runner@$p && systemctl is-active ameesh-runner@$p" | sed "s/^/$p : /"
  done
  etape "poste"
  for p in "${garde[@]}"; do
    est_dans "$p" $PERSONAS_VM && continue
    est_dans "$p" $PERSONAS_ATTACHEES && { echo "$p : session interactive, à rattacher (ameesh attach $p)"; continue; }
    systemctl --user cat "ameesh-runner-agent@$p.service" >/dev/null 2>&1 || continue
    systemctl --user is-enabled --quiet "ameesh-runner-agent@$p.service" 2>/dev/null || continue
    systemctl --user start "ameesh-runner-agent@$p.service" && echo "$p : relancé"
  done
}

etat() {
  ameesh list 2>&1 | head -25
  ssh_vm "for u in \$(systemctl list-units --plain --no-legend 'ameesh-runner@*' | awk '{print \$1}'); do echo \"== \$u\"; journalctl -u \$u -n 5 --no-pager -o cat; done"
}

case "${1:-}" in
  verifier|preparer|arreter|copier|rebrancher|demarrer|etat) "$1" ;;
  *) sed -n '3,22p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
