#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# ameesh — rebaser sans perdre la ligne « #<lot> … » d'un sujet de commit.
#
# Pourquoi (lot #1277) : `git rebase --continue`, comme `git commit --amend`,
# ouvre un éditeur dont le nettoyage par défaut prend une ligne commençant par
# le caractère de commentaire (« # ») pour un commentaire et la retire. Un
# sujet écrit « #748 : … », la convention des lots, disparaît alors : le corps
# devient le sujet et le numéro du lot est perdu dans l'historique.
#
# Ce lanceur passe core.commentChar=';' à *chaque* invocation de git rebase —
# y compris --continue, là où l'éditeur s'ouvre réellement. Le rebasage
# terminé, il contrôle que les sujets « #… » notés au départ existent encore ;
# un sujet perdu est signalé et le lanceur sort en 4.
#
# Usage : scripts/rebase-lot.sh <arguments de git rebase>
#   scripts/rebase-lot.sh origin/main
#   scripts/rebase-lot.sh --continue
#   scripts/rebase-lot.sh --abort
set -euo pipefail

etat=$(git rev-parse --git-path ameesh-rebase-lot.sujets)
en_cours() {
  [ -d "$(git rev-parse --git-path rebase-merge)" ] \
    || [ -d "$(git rev-parse --git-path rebase-apply)" ]
}

# Au démarrage d'un rebasage : noter les sujets « #… » que git va réécrire.
if ! en_cours; then
  git log --format='%s' -n 50 HEAD 2>/dev/null | grep '^#' >"$etat" || :
fi

git -c core.commentChar=';' rebase "$@"

# Rebasage terminé (fusionné ou abandonné) : chaque sujet noté doit exister.
if [ -s "$etat" ] && ! en_cours; then
  perdus=$(grep -F -x -v -f <(git log --format='%s' -n 100 HEAD) "$etat" || :)
  rm -f "$etat"
  if [ -n "$perdus" ]; then
    printf 'rebase-lot : sujets « #… » absents après le rebasage :\n%s\n' "$perdus" >&2
    printf "rebase-lot : contrôle « git log --format='%%s' » — un sujet a pu passer en corps.\n" >&2
    exit 4
  fi
fi
