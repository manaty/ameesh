#!/bin/bash
# #6002 v4.6 — partie DISTANTE (exécutée en root sur AG3, octets envoyés par maj-adaptateur-AG3-v46.sh sur
# l'entrée standard ; exécutée telle quelle par temoin-maj-v46.sh en conteneur). Mise à jour d'une pose
# EXISTANTE (v4.5, M) vers M' : code et venv neufs à côté des anciens, lien venv basculé atomiquement,
# vm-remote.py / health.py / unité de santé de M' ; rien d'autre (cartes, compte, sshd, sudoers inchangés).
#   maj-adaptateur-distant-v46.sh verifier  <M> <M'> <reçus>   lecture seule
#   maj-adaptateur-distant-v46.sh appliquer <M> <M'> <reçus>   (src.tar de M' déjà dans <reçus>)
#   maj-adaptateur-distant-v46.sh retour    <M> <M'> <reçus>   lien et fichiers de M remis ; M' conservé
# Empreintes attendues des fichiers installés (git show <sha>:<chemin> | sha256sum) :
set -euo pipefail
MODE=$1; OLD=$2; NEW=$3; OPS=$4; O=/opt/ameesh-vm-adapter; O12=${OLD:0:12}; N12=${NEW:0:12}
U=/home/ameesh/.config/systemd/user; E=/etc/ameesh-vm-adapter
declare -A A_M=([remote/vm-remote.py]=a4ffc047239825705db03f72e6801f363e390077d3155dd6574a0cbc81df4171
                [checks/health.py]=15919bc1718fcc0926118d755af0d22ebb48aa6defe7b6875db7769519b6291b
                [checks/venv-processus.py]=412b1b76248e637dc876c44680c3457148f485a00e3060eb527ded9717cd953c
                [unit]=497e2d2ebe1a5b5697e93fefc5ef759b1c61f1ef55852b25a2f62962eb9435cc)
declare -A A_N=([remote/vm-remote.py]=b9872817133779165674e659996c42663a60e93cda108c5a9f206019fe487cb0
                [checks/health.py]=e7f1f4f9b20c6bca76bc48c157f51d5bd8289cf65c60ece811ec76dded6a63cc
                [checks/venv-processus.py]=412b1b76248e637dc876c44680c3457148f485a00e3060eb527ded9717cd953c
                [unit]=0f2554be824330ceb541ead33d16df032443ef1a055c1fedbbadb16f33da7ebf)
stop() { echo "STOP : $*" >&2; exit 1; }
sha() { sha256sum < "$1" | cut -c1-64; }
etat() {  # $1 = M ou N : empreintes installées attendues
  local -n A=A_$1; local f
  for f in remote/vm-remote.py checks/health.py checks/venv-processus.py; do [ "$(sha "$O/$f")" = "${A[$f]}" ] || return 1; done
  [ "$(sha "$U/ameesh-vm-health.service")" = "${A[unit]}" ]
}
releve() {
  echo "lien venv : $(readlink "$O/venv")"
  for f in remote/vm-remote.py checks/health.py checks/venv-processus.py; do echo "$(sha "$O/$f") $f $(stat -c '%a %U:%G' "$O/$f")"; done
  echo "$(sha "$U/ameesh-vm-health.service") ameesh-vm-health.service $(stat -c '%a %U:%G' "$U/ameesh-vm-health.service")"
}
# Les cartes root et les unités ne nomment aucun src-<sha> ni venv-<sha> : seul le lien venv bouge.
refs=$(grep -l -E 'src-[0-9a-f]{12}|venv-[0-9a-f]{12}' "$E"/* "$U"/ameesh-vm-health.* 2>/dev/null || true)
[ -z "$refs" ] || stop "référence à un src-/venv-<sha> dans : $refs"
installer() {  # $1 = sha12 source, $2 = propriétaire de l'unité ; écriture temporaire puis renommage
  local s=$O/src-$1
  install -m 644 -o root -g root "$s/deploy/vm-adapter/remote/vm-remote.py" "$O/remote/.vm-remote.py.maj" && mv -f "$O/remote/.vm-remote.py.maj" "$O/remote/vm-remote.py"
  install -m 644 -o root -g root "$s/deploy/vm-adapter/remote/checks/health.py" "$O/checks/.health.py.maj" && mv -f "$O/checks/.health.py.maj" "$O/checks/health.py"
  install -m 644 -o ameesh -g ameesh "$s/deploy/vm-adapter/remote/checks/ameesh-vm-health.service" "$U/.ameesh-vm-health.service.maj" && mv -f "$U/.ameesh-vm-health.service.maj" "$U/ameesh-vm-health.service"
  systemctl --user -M ameesh@ daemon-reload
}
lier() { ln -sfn "venv-$1" "$O/venv.maj" && mv -T "$O/venv.maj" "$O/venv"; }
controles() {
  local w n; w=$(find "$O" -xdev \( -perm -g+w -o -perm -o+w \) ! -type l); [ -z "$w" ] || stop "inscriptible : $w"
  n=$(find "$O" -xdev ! -user root); [ -z "$n" ] || stop "non root : $n"
  "$O/venv/bin/python" -I -c 'import ameesh' || stop "ameesh non importable depuis $O/venv"
}
case $MODE in
verifier)
  [ "$(readlink "$O/venv")" = "venv-$O12" ] && etat M || stop "pose v4.5 attendue (lien venv-$O12, fichiers de M)"
  [ -d "$O/src-$O12" ] && [ -d "$O/venv-$O12" ] || stop "src-/venv-$O12 absents"
  [ ! -e "$O/src-$N12" ] && [ ! -e "$O/venv-$N12" ] || stop "src-/venv-$N12 déjà présents : réconcilier"
  releve; echo "vérification OK : pose à M, place libre pour M'" ;;
appliquer)
  [ "$(readlink "$O/venv")" = "venv-$O12" ] && etat M || stop "pose v4.5 attendue"
  [ ! -e "$O/src-$N12" ] && [ ! -e "$O/venv-$N12" ] || stop "src-/venv-$N12 déjà présents"
  [ -f "$OPS/src.tar" ] || stop "archive de M' absente des reçus"
  releve > "$OPS/recu-avant.txt"
  tar -C "$O" -xf "$OPS/src.tar"; chown -R root:root "$O/src-$N12"; chmod -R go-w "$O/src-$N12"
  ( cd "$O" && python3.12 -m venv "venv-$N12" && "venv-$N12/bin/pip" install -q --no-compile "./src-$N12" >/dev/null )
  chown -R root:root "$O/venv-$N12"; chmod -R go-w "$O/venv-$N12"
  "$O/venv-$N12/bin/python" -I -c 'import ameesh' || stop "venv-$N12 : ameesh non importable (lien inchangé)"
  echo "intention : bascule vers $N12" > "$OPS/intention-bascule"
  lier "$N12"; installer "$N12"
  etat N || stop "empreintes de M' non atteintes après bascule : retour conseillé"
  controles; releve > "$OPS/recu-apres.txt"; cat "$OPS/recu-apres.txt"; echo "mise à jour vers $N12 OK ; M conservé pour le retour" ;;
retour)
  [ -d "$O/src-$O12" ] && [ -d "$O/venv-$O12" ] || stop "src-/venv-$O12 absents : retour impossible"
  lier "$O12"; installer "$O12"
  etat M || stop "empreintes de M non atteintes après retour"
  controles; releve > "$OPS/recu-retour.txt"; cat "$OPS/recu-retour.txt"; echo "retour vers $O12 OK ; src-/venv-$N12 conservés" ;;
*) stop "mode inconnu" ;;
esac
