#!/usr/bin/env bash
# #5495 C3 v2.4 — témoins locaux de fenetre-release.sh (faux ssh et faux systemctl, aucune VM).
# Couvre : état lu sans geste ; fermeture avec un tick qui finit ; borne dépassée = STOP ; état
# gardé inconnu = STOP avant tout geste ; restauration active ; état vide + enabled = relance
# explicite vérifiée (règle v2.3) ; refus d'une minuterie non active avant tout arrêt.
set -u
ICI=$(cd "$(dirname "$0")" && pwd); F=$(mktemp -d); trap 'rm -rf "$F"' EXIT
ECHEC=0; ok() { echo "OK   $*"; }; ko() { echo "ECHEC $*"; ECHEC=1; }
mkdir -p "$F/bin"
cat > "$F/bin/ssh" <<'SH'
#!/usr/bin/env bash
last=""; for a; do last=$a; done
exec bash -c "$last"
SH
cat > "$F/bin/systemctl" <<'SH'
#!/usr/bin/env bash
F=${FAKE_STATE:?}; args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --user) shift ;;
    -M) shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
cmd=${args[0]:-}
case "$cmd" in
  show)
    unit=${args[1]:-}; prop=""
    for ((i=2; i<${#args[@]}; i++)); do [ "${args[$i]}" = -p ] && prop=${args[$((i+1))]}; done
    case "$unit:$prop" in
      ameesh-release.timer:UnitFileState) cat "$F/ufs" ;;
      ameesh-release.timer:ActiveState) cat "$F/active" ;;
      ameesh-release.service:ActiveState)
        if [ -s "$F/ticks" ]; then
          n=$(cat "$F/ticks")
          if [ "$n" -gt 0 ]; then echo $((n-1)) > "$F/ticks"; echo active; else echo inactive; fi
        else cat "$F/service"; fi ;;
      *) echo "systemctl faux : $unit $prop inconnu" >&2; exit 1 ;;
    esac ;;
  list-jobs) cat "$F/jobs" 2>/dev/null || true ;;
  stop) echo stop >> "$F/calls"; echo inactive > "$F/active" ;;
  start) echo start >> "$F/calls"; echo active > "$F/active" ;;
  *) echo "systemctl faux : $cmd inconnu" >&2; exit 1 ;;
esac
SH
chmod 755 "$F/bin/ssh" "$F/bin/systemctl"
export PATH="$F/bin:$PATH" FAKE_STATE="$F" RECUS_BASE="$F/recus" POLL_S=1
: > "$F/jobs"; echo inactive > "$F/service"; : > "$F/calls"
etat() { echo enabled > "$F/ufs"; echo "$1" > "$F/active"; }
lancer() { "$ICI/fenetre-release.sh" "$@" 2>&1; }

# 1. état : lecture seule, aucun geste.
etat active; out=$(lancer etat); rc=$?
{ [ $rc = 0 ] && printf '%s\n' "$out" | grep -q "timer UnitFileState=enabled ActiveState=active ; service=inactive ; jobs_release=0"; } \
  && ok "état lu, aucun geste" || { ko "état rc=$rc"; printf '%s\n' "$out" | sed 's/^/     /'; }
[ ! -s "$F/calls" ] && ok "état : aucun stop/start" || ko "état a appelé systemctl : $(cat "$F/calls")"

# 2. fermer : la minuterie passe inactive, le service finit après un poll, aucun job.
etat active; echo 1 > "$F/ticks"; out=$(lancer --apply fermer); rc=$?
TS=$(printf '%s\n' "$out" | sed -n 's/.*TS=\([0-9TZ-]*\).*/\1/p' | tail -1)
{ [ $rc = 0 ] && printf '%s\n' "$out" | grep -q "OK  AG1 fenêtre fermée" && printf '%s\n' "$out" | grep -q "OK  AG2 fenêtre fermée"; } \
  && ok "fermer avec tick finissant (rc $rc)" || { ko "fermer rc=$rc"; printf '%s\n' "$out" | sed 's/^/     /'; }
[ "$(wc -l < "$F/recus/ameesh-operator-C3-fenetre-AG1-$TS/release-timer.etat")" = 2 ] \
  && ok "reçu AG1 : 2 lignes d'état" || ko "reçu AG1 absent ou incomplet"

# 3. restaurer : état gardé active -> start + ActiveState=active.
etat active; out=$(lancer --apply restaurer "$TS"); rc=$?
{ [ $rc = 0 ] && printf '%s\n' "$out" | grep -q "GESTE \[AG1\] : minuterie release restaurée" && [ "$(cat "$F/active")" = active ]; } \
  && ok "restaurer active vérifié (rc $rc)" || { ko "restaurer rc=$rc"; printf '%s\n' "$out" | sed 's/^/     /'; }

# 4. borne dépassée : le service ne finit pas -> STOP, l'état gardé reste.
etat active; echo 99 > "$F/ticks"; out=$(BORNE_S=2 lancer --apply fermer); rc=$?
{ [ $rc != 0 ] && printf '%s\n' "$out" | grep -q "tick de release non terminé" && printf '%s\n' "$out" | grep -q "^GESTE \[AG1\] : minuterie release arrêtée"; } \
  && ok "borne dépassée -> STOP avec état gardé (rc $rc)" || { ko "borne rc=$rc"; printf '%s\n' "$out" | sed 's/^/     /'; }

# 5. état gardé inconnu + UnitFileState=disabled : STOP AVANT tout geste.
etat inactive; echo disabled > "$F/ufs"; mkdir -p "$F/recus/ameesh-operator-C3-fenetre-AG1-A/"; printf 'bizarre\n' > "$F/recus/ameesh-operator-C3-fenetre-AG1-A/release-timer.etat"
mkdir -p "$F/recus/ameesh-operator-C3-fenetre-AG2-A/"; printf 'bizarre\n' > "$F/recus/ameesh-operator-C3-fenetre-AG2-A/release-timer.etat"
: > "$F/calls"; out=$(lancer --apply restaurer A); rc=$?
{ [ $rc != 0 ] && ! grep -q start "$F/calls"; } \
  && ok "état inconnu + disabled -> STOP avant tout geste (rc $rc)" || { ko "état inconnu rc=$rc calls=$(cat "$F/calls")"; printf '%s\n' "$out" | sed 's/^/     /'; }

# 6. état gardé vide + UnitFileState=enabled : relance explicite vérifiée (règle v2.3).
etat inactive; echo enabled > "$F/ufs"; printf '\n' > "$F/recus/ameesh-operator-C3-fenetre-AG1-A/release-timer.etat"
printf '\n' > "$F/recus/ameesh-operator-C3-fenetre-AG2-A/release-timer.etat"
: > "$F/calls"; out=$(lancer --apply restaurer A); rc=$?
{ [ $rc = 0 ] && printf '%s\n' "$out" | grep -q "relance explicite vérifiée" && [ "$(cat "$F/active")" = active ]; } \
  && ok "état vide + enabled -> relance explicite vérifiée (rc $rc)" || { ko "état vide rc=$rc"; printf '%s\n' "$out" | sed 's/^/     /'; }

# 7. fermer refuse une minuterie non active AVANT tout arrêt.
etat inactive; : > "$F/calls"; out=$(lancer --apply fermer); rc=$?
{ [ $rc != 0 ] && printf '%s\n' "$out" | grep -q "fenêtre non fermée" && ! grep -q stop "$F/calls"; } \
  && ok "minuterie non active -> STOP avant tout arrêt (rc $rc)" || { ko "non active rc=$rc calls=$(cat "$F/calls")"; printf '%s\n' "$out" | sed 's/^/     /'; }

exit $ECHEC
