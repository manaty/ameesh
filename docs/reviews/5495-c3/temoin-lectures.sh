#!/usr/bin/env bash
# #5495 C3 v2.2 — témoins locaux (faux ssh dans le PATH, aucune VM) :
#  B2 (#200781) : un systemctl en rc 1 sur ExecStart ou list-jobs doit donner KO / ERREUR-LECTURE
#                 (v2.1 le lisait « sans » → OK) ;
#  B3 (#200781) : activation partielle (reçus sur AG1 seulement) → retour d'AG1, AG2 vérifié intact
#                 (rc 0) ; AG2 sans reçus mais modifié → incident rc 4, AG1 quand même traité.
set -u
ICI=$(cd "$(dirname "$0")" && pwd); F=$(mktemp -d); trap 'rm -rf "$F"' EXIT
ECHEC=0; ok() { echo "OK   $*"; }; ko() { echo "ECHEC $*"; ECHEC=1; }
faux_ssh() {  # $1 = scénario
  cat > "$F/ssh" <<FAUX
#!/usr/bin/env bash
cmd="\${@: -1}"; hote=""; for a in "\$@"; do case \$a in root@*) hote=\${a#root@};; esac; done
case "$1" in
 systemctl-rc1)  # la commande distante tourne vraiment, avec un faux systemctl en rc 1 (témoin impl-11)
  mkdir -p "$F/bin"; printf '%s\n' '#!/usr/bin/env bash' 'case "\$*" in *ExecStart*|*list-jobs*) exit 1;; esac; echo inactive' > "$F/bin/systemctl"
  chmod 755 "$F/bin/systemctl"; PATH="$F/bin:/usr/bin:/bin" exec bash -c "\$cmd";;
 canon-absent)  # impl-03 B2 #200802 : premier mv fait, second échoué
  case "\$cmd" in
   *UnitFileState*) echo disabled;; *ActiveState*) echo inactive;;
   *canon-avant*) echo ab4ae5b1d6f3d91b2c477db7bb7a1acb2de0c5a5;;
   *canon-receipt.json*) echo b8c4ea12ca5fcac799329dd5ac54386384cbe223;;
   *"test -e /home/ameesh-staging/staging/canon"*) exit 1;;
   *"retained-before"*"rev-parse HEAD"*) echo ab4ae5b1d6f3d91b2c477db7bb7a1acb2de0c5a5;;
   *) exit 0;; esac; exit 0;;
 partiel-intact|partiel-modifie)
  case "\$cmd" in
   *"test -d /root/ameesh-operator-C3-AG1-activation-"*) exit 0;;
   *"test -d /root/ameesh-operator-C3-AG2-activation-"*) exit 1;;
   *"10-sans-staging.conf && echo dropin"*) [ "$1" = partiel-intact ] && echo dropin; echo disabled; echo sans-soumission; exit 0;;
   *ActiveState*) echo inactive; exit 0;;
   *) exit 0;; esac;;
esac
FAUX
  chmod 755 "$F/ssh"
}
# --- B2
faux_ssh systemctl-rc1
out=$(PATH="$F:$PATH" SONDE_BIBLIO=1 bash -c ". '$ICI/sonde-c3-v22.sh'; TIMER_ATTENDU=x; etat AG1 active; echo jobs=\$(jobs_release AG1); echo exec=\$(execstart AG1)")
printf '%s\n' "$out" | grep -q '^KO  AG1 ExecStart sans --no-staging' && ok "B2 v2.2 : ExecStart illisible → KO" || ko "B2 v2.2 ExecStart : $out"
printf '%s\n' "$out" | grep -q '^jobs=ERREUR-LECTURE' && ok "B2 v2.2 : list-jobs illisible → ERREUR-LECTURE" || ko "B2 v2.2 list-jobs"
old=$(PATH="$F:$PATH" SONDE_BIBLIO=1 bash -c ". '$ICI/sonde-c3-v21.sh'; TIMER_ATTENDU=x; etat AG1 active" | grep 'ExecStart sans')
case "$old" in OK*) ok "B2 reproduit sur v2.1 : « $old » (faux OK, corrigé en v2.2)";; *) ko "B2 non reproduit sur v2.1 : $old";; esac
# --- B3
faux_ssh partiel-intact
out=$(PATH="$F:$PATH" "$ICI/retour-c3-v22.sh" activation 20261109T094500Z-1 2>&1); rc=$?
{ [ $rc = 0 ] && printf '%s\n' "$out" | grep -q 'OK  AG2 intact' && printf '%s\n' "$out" | grep -q 'GESTE \[AG1\] : drop-in' && ! printf '%s\n' "$out" | grep -q 'GESTE \[AG2\]'; } \
  && ok "B3 activation partielle, AG2 intact : retour d'AG1 seul, rc 0" || { ko "B3 partiel intact rc=$rc"; printf '%s\n' "$out" | sed 's/^/     /'; }
faux_ssh partiel-modifie
out=$(PATH="$F:$PATH" "$ICI/retour-c3-v22.sh" activation 20261109T094500Z-1 2>&1); rc=$?
{ [ $rc = 4 ] && printf '%s\n' "$out" | grep -q 'INCIDENT AG2' && printf '%s\n' "$out" | grep -q 'GESTE \[AG1\] : drop-in' && ! printf '%s\n' "$out" | grep -q 'GESTE \[AG2\]'; } \
  && ok "B3 AG2 sans reçus mais modifié : incident rc 4, AG1 traité, AG2 non touché" || { ko "B3 partiel modifié rc=$rc"; printf '%s\n' "$out" | sed 's/^/     /'; }
# --- impl-03 B2 (#200802) : canon absent, canon conservé présent au bon HEAD → remis (à blanc)
faux_ssh canon-absent
out=$(PATH="$F:$PATH" "$ICI/retour-c3-v22.sh" prealables AG1 20261109T090000Z-1 2>&1); rc=$?
printf '%s\n' "$out" | grep -q "GESTE \[AG1\] : canon absent : canon conservé ab4ae5b1 remis" && ok "B2 impl-03 : canon absent → canon conservé remis (rc=$rc)" || { ko "B2 impl-03 canon absent rc=$rc"; printf '%s\n' "$out" | sed 's/^/     /'; }
exit $ECHEC
