#!/usr/bin/env bash
# #5495 C3 v2.3 — témoins locaux du seul delta de retour-c3-v23.sh (faux ssh, aucune VM) :
# état gardé de la minuterie release vide/inconnu → relance vérifiée si enabled, STOP avant tout geste sinon.
set -u
ICI=$(cd "$(dirname "$0")" && pwd); F=$(mktemp -d); trap 'rm -rf "$F"' EXIT
ECHEC=0; ok() { echo "OK   $*"; }; ko() { echo "ECHEC $*"; ECHEC=1; }
faux() {  # $1 = 2e ligne du reçu, $2 = UnitFileState de la minuterie release
  cat > "$F/ssh" <<FAUX
#!/usr/bin/env bash
cmd="\${@: -1}"
case "\$cmd" in
 *"test -d /root/ameesh-operator-C3-AG1-activation-"*) exit 0;;
 *"test -d /root/ameesh-operator-C3-AG2-activation-"*) exit 1;;
 *"10-sans-staging.conf && echo dropin"*) printf 'dropin\ndisabled\nsans-soumission\n';;
 *"sed -n 2p"*) printf '%s\n' "$1";;
 *"ameesh-release.timer -p UnitFileState"*) echo "$2";;
 *ActiveState*) echo inactive;;
 *) exit 0;;
esac
FAUX
  chmod 755 "$F/ssh"; }
lancer() { PATH="$F:$PATH" "$ICI/retour-c3-v23.sh" activation 20261109T094500Z-1 2>&1; }
faux "" enabled; out=$(lancer); rc=$?
{ printf '%s\n' "$out" | grep -q "relance explicite vérifiée" && printf '%s\n' "$out" | grep -q "GESTE \[AG1\] : minuterie release restaurée : active"; } \
  && ok "état gardé vide + enabled → relance explicite vérifiée (rc $rc)" || { ko "vide+enabled rc=$rc"; printf '%s\n' "$out" | sed 's/^/     /'; }
faux "" disabled; out=$(lancer); rc=$?
{ [ $rc != 0 ] && printf '%s\n' "$out" | grep -q "minuterie release NON arrêtée" && ! printf '%s\n' "$out" | grep -q "^GESTE"; } \
  && ok "état gardé vide + disabled → STOP avant tout geste (rc $rc)" || { ko "vide+disabled rc=$rc"; printf '%s\n' "$out" | sed 's/^/     /'; }
faux "bizarre" ERREUR; out=$(PATH="$F:$PATH" "$ICI/retour-c3-v23.sh" activation 20261109T094500Z-1 2>&1); rc=$?
{ [ $rc != 0 ] && ! printf '%s\n' "$out" | grep -q "^GESTE"; } && ok "état gardé inconnu + UnitFileState illisible → STOP avant tout geste" || ko "inconnu+illisible rc=$rc"
faux "inactive" enabled; out=$(lancer); rc=$?
printf '%s\n' "$out" | grep -q "GESTE \[AG1\] : minuterie release restaurée : inactive" && ok "état gardé inactive → suivi (pas de relance)" || ko "inactive"
faux "active" enabled; out=$(lancer); rc=$?
printf '%s\n' "$out" | grep -q "GESTE \[AG1\] : minuterie release restaurée : active" && ok "état gardé active → relance vérifiée" || ko "active"
exit $ECHEC
