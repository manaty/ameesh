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

# Déduire la plage comme Git : upstream (ou @{upstream}), branche optionnelle,
# --root et --fork-point. --onto change la destination, pas la borne de départ.
# Les options à valeur sont consommées pour ne pas prendre leur valeur pour
# upstream. Une forme inconnue est refusée avant toute modification du dépôt.
noter_sujets() {
  local branche=HEAD upstream= racine=0 fork=auto merges=0 reapply=auto keep=0
  local -a positions=() args=("$@") revisions=() options_log=()
  local i arg config_merges config_fork
  config_merges=$(git config --get rebase.rebaseMerges || :)
  case "$config_merges" in true|rebase-cousins|no-rebase-cousins) merges=1 ;; esac
  config_fork=$(git config --bool --get rebase.forkPoint || :)
  [ "$config_fork" != false ] || fork=non
  for ((i=0; i<${#args[@]}; i++)); do
    arg=${args[i]}
    case "$arg" in
      --root) racine=1 ;;
      --no-root) racine=0 ;;
      --fork-point) fork=oui ;;
      --no-fork-point) fork=non ;;
      --keep-base) keep=1 ;;
      --no-keep-base) keep=0 ;;
      -r|--rebase-merges|--rebase-merges=*) merges=1 ;;
      --no-rebase-merges) merges=0 ;;
      --reapply-cherry-picks) reapply=1 ;;
      --no-reapply-cherry-picks) reapply=0 ;;
      --onto|--exec|-x|--strategy|-s|--strategy-option|-X|--whitespace|--empty|-C|--trailer)
        i=$((i+1))
        if ((i>=${#args[@]})); then
          printf 'rebase-lot : valeur manquante pour %s\n' "$arg" >&2
          return 2
        fi ;;
      --onto=*|--exec=*|--strategy=*|--strategy-option=*|--whitespace=*|--empty=*|--trailer=*|-C?*|-X?*|-s?*|-x?*|-S*) ;;
      --) positions+=("${args[@]:i+1}"); break ;;
      -i|--interactive|-m|--merge|--apply|-q|--quiet|-v|--verbose|-n|--no-stat|--stat|-f|--force-rebase|--no-force-rebase|--no-ff|--ff|--verify|--no-verify|--signoff|--no-signoff|--committer-date-is-author-date|--no-committer-date-is-author-date|--reset-author-date|--no-reset-author-date|--ignore-whitespace|--no-ignore-whitespace|--autosquash|--no-autosquash|--update-refs|--no-update-refs|--gpg-sign|--gpg-sign=*|--no-gpg-sign|--autostash|--no-autostash|--rerere-autoupdate|--no-rerere-autoupdate|--reschedule-failed-exec|--no-reschedule-failed-exec) ;;
      -*) printf 'rebase-lot : option de plage non reconnue : %s\n' "$arg" >&2; return 2 ;;
      *) positions+=("$arg") ;;
    esac
  done
  if ((racine)); then
    ((${#positions[@]}<=1)) || return 2
    branche=${positions[0]:-HEAD}
    revisions=("$branche")
  else
    ((${#positions[@]}<=2)) || return 2
    branche=${positions[1]:-HEAD}
    if ((${#positions[@]})); then
      upstream=${positions[0]}
      [ "$fork" != auto ] || fork=non
    else
      upstream=$(git rev-parse --symbolic-full-name '@{upstream}') || return
      [ "$fork" != auto ] || fork=oui
    fi
    revisions=("$upstream...$branche")
    # Git retire aussi les commits devenus amont après un rewind de branche.
    if [ "$fork" = oui ] && (( ! keep )); then
      local point
      point=$(git merge-base --fork-point "$upstream" "$branche" || :)
      [ -z "$point" ] || revisions+=("^$point")
    fi
    options_log+=(--right-only)
    if [ "$reapply" = auto ]; then reapply=$keep; fi
    ((reapply)) || options_log+=(--cherry-pick)
  fi
  # Les commits de fusion ne sont pas rejoués par un rebase ordinaire.
  ((merges)) || options_log+=(--no-merges)
  local sujets
  sujets=$(git log --format='%s' "${options_log[@]}" "${revisions[@]}" --) || return
  printf '%s\n' "$sujets" | { grep '^#' || :; } >"$etat"
}

# Au démarrage seulement ; garder le même état après un conflit.
controle=0
abandon=0
for arg in "$@"; do
  case "$arg" in
    --continue|--skip|--edit-todo|--show-current-patch) controle=1 ;;
    --abort|--quit) controle=1; abandon=1 ;;
  esac
done
if ! en_cours && (( ! controle )); then
  noter_sujets "$@"
fi

git -c core.commentChar=';' rebase "$@"

# Un abandon n'a pas rejoué la plage jusqu'au bout : aucune perte à diagnostiquer.
if ((abandon)); then
  rm -f "$etat"
  exit 0
fi

# Rebasage terminé : chaque sujet de la plage notée doit exister, sans borne.
if [ -f "$etat" ] && ! en_cours; then
  perdus=$(grep -F -x -v -f <(git log --format='%s' HEAD) "$etat" || :)
  rm -f "$etat"
  if [ -n "$perdus" ]; then
    printf 'rebase-lot : sujets « #… » absents après le rebasage :\n%s\n' "$perdus" >&2
    printf "rebase-lot : contrôle « git log --format='%%s' » — un sujet a pu passer en corps.\n" >&2
    exit 4
  fi
fi
