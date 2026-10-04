#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# nexlink-agent — agents du chantier Nexlink en mode headless, qui se relancent seuls.
#
#   nexlink-agent start <nom> <deepseek|claude|codex> "<consigne>" [session]
#        lance l'agent dans ~/development/manaty/nexlink-<nom> (fenêtre foot
#        « [nexlink/<nom>] … » sur le bureau 7). [session] reprend une session
#        existante (id Claude, id de fil Codex, session-… DeepSeek) : rien n'est perdu.
#   nexlink-agent stop <nom>        arrête la boucle (la session reste reprenable)
#   nexlink-agent list              agents, outil, état, session
#   nexlink-agent loop <nom>        (interne)
#
# Un tour = un appel headless (dsh --profile agent / claude -p / codex exec) qui
# travaille jusqu'à rendre la main. La boucle reprend la MÊME session dès qu'un
# message agent-mail arrive, et une fois après 20 min d'inactivité. Plus aucune
# relance à la main, plus aucune frappe dans une fenêtre.
set -uo pipefail

DSH=${DSH:-dsh}
M=$HOME/development/manaty
ST=$HOME/.local/state/nexlink-agents
INBOX=$HOME/.local/state/agent-mail/inbox
WS=7
IDLE_NUDGE=1200

die() { echo "$*" >&2; exit 2; }
valid() { [[ $1 =~ ^[a-z0-9-]{1,32}$ ]] || die "nom invalide : $1"; }

turn() { # nom texte
  local name=$1 text=$2 tool sid ev
  tool=$(cat "$ST/$name/tool"); sid=$(cat "$ST/$name/session" 2>/dev/null || true)
  ev="$ST/$name/events.jsonl"
  cd "$M/nexlink-$name" || return 1
  echo "── $(date '+%H:%M:%S') tour ($tool${sid:+, session $sid})"
  case $tool in
    deepseek)
      local a=(--profile agent --json); [[ -n $sid ]] && a+=(--session-id "$sid")
      DSH_PERMISSION_MODE=danger-full-access "$DSH" "${a[@]}" "$text" 2>>"$ST/$name/stderr.log" | tee -a "$ev" |
        jq -r --unbuffered 'if .type=="text" then .text elif .type=="final" then "== fin du tour : \(.text[0:300])" else empty end' 2>/dev/null
      [[ -z $sid ]] && jq -r 'select(.type=="session") | .sessionId' "$ev" 2>/dev/null | tail -1 >"$ST/$name/session" ;;
    claude)
      local a=(-p --output-format stream-json --verbose --dangerously-skip-permissions); [[ -n $sid ]] && a+=(--resume "$sid")
      CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1 claude "${a[@]}" "$text" 2>>"$ST/$name/stderr.log" | tee -a "$ev" |
        jq -r --unbuffered 'if .type=="assistant" then (.message.content[]? | select(.type=="text") | .text) elif .type=="result" then "== fin du tour : \(.result[0:300] // "")" else empty end' 2>/dev/null
      [[ -z $sid ]] && jq -r 'select(.type=="system" and .subtype=="init") | .session_id' "$ev" 2>/dev/null | tail -1 >"$ST/$name/session" ;;
    codex)
      if [[ -n $sid ]]; then
        codex --dangerously-bypass-hook-trust exec --dangerously-bypass-approvals-and-sandbox --json resume "$sid" "$text"
      else
        codex --dangerously-bypass-hook-trust exec --dangerously-bypass-approvals-and-sandbox --json "$text"
      fi 2>>"$ST/$name/stderr.log" | tee -a "$ev" |
        jq -r --unbuffered 'if .type=="item.completed" and .item.type=="agent_message" then .item.text elif .type=="turn.completed" then "== fin du tour" else empty end' 2>/dev/null
      [[ -z $sid ]] && jq -r 'select(.type=="thread.started") | .thread_id' "$ev" 2>/dev/null | tail -1 >"$ST/$name/session" ;;
  esac
  [[ -s $ST/$name/session ]] || rm -f "$ST/$name/session"
}

has_mail() { compgen -G "$INBOX/$1/*.json" >/dev/null; }

cmd_loop() {
  local name=$1; valid "$name"
  echo $$ >"$ST/$name/pid"
  if [[ -f $ST/$name/prompt ]]; then
    turn "$name" "$(cat "$ST/$name/prompt")"; rm -f "$ST/$name/prompt"
  fi
  [[ -f $ST/$name/session ]] || { echo "aucune session : arrêt"; return 1; }
  date +%s >"$ST/$name/idle-since"; local nudged=0
  while true; do
    if has_mail "$name"; then
      nudged=0
      t0=$(date +%s)
      turn "$name" "Tu as des messages agent-mail : lance agent-mail inbox, lis-les, puis continue ton travail sans attendre de confirmation."
      (( $(date +%s) - t0 < 20 )) && { echo "tour trop court : pause 5 min"; sleep 300; }
      date +%s >"$ST/$name/idle-since"
    elif (( nudged == 0 && $(date +%s) - $(cat "$ST/$name/idle-since") > IDLE_NUDGE )); then
      nudged=1
      turn "$name" "Reprise : si ton lot n'est ni gelé ni fusionné, continue-le ; sinon prends la suite de ton affectation (board, workstream). Si tu n'as rien à faire, dis-le par agent-mail send orchestrateur puis arrête-toi."
      date +%s >"$ST/$name/idle-since"
    else
      sleep 30
    fi
  done
}

case "${1:-}" in
  start)
    name=${2:-}; valid "$name"; tool=${3:-}; prompt=${4:-}; sess=${5:-}
    [[ $tool =~ ^(deepseek|claude|codex)$ ]] || die "outil : deepseek|claude|codex"
    [[ -d $M/nexlink-$name ]] || die "worktree absent : $M/nexlink-$name"
    pid=$(cat "$ST/$name/pid" 2>/dev/null || true)
    [[ -n $pid ]] && kill -0 "$pid" 2>/dev/null && die "$name tourne déjà (pid $pid)"
    mkdir -p "$ST/$name"; echo "$tool" >"$ST/$name/tool"
    if [[ -n $sess ]]; then echo "$sess" >"$ST/$name/session"; else rm -f "$ST/$name/session"; fi
    [[ -n $prompt ]] && printf '%s' "$prompt" >"$ST/$name/prompt"
    [[ -z $prompt && -z $sess ]] && die "consigne ou session requise"
    before=$(hyprctl clients -j | jq -r '.[].address' | sort)
    setsid -f foot -T "[nexlink/$name] $tool headless" -D "$M/nexlink-$name" "$0" loop "$name" >/dev/null 2>&1
    sleep 1.5
    new=$(comm -13 <(echo "$before") <(hyprctl clients -j | jq -r '.[].address' | sort) | head -1)
    [[ -n $new ]] && hyprctl dispatch "hl.dsp.window.move({ window = \"address:$new\", workspace = \"$WS\", follow = false })" >/dev/null
    echo "$name lancé ($tool, bureau $WS)" ;;
  stop)
    name=${2:-}; valid "$name"; pid=$(cat "$ST/$name/pid" 2>/dev/null || true)
    if [[ -n $pid ]]; then pkill -TERM -P "$pid" 2>/dev/null; kill "$pid" 2>/dev/null; fi
    rm -f "$ST/$name/pid"; echo "$name arrêté (session gardée : $(cat "$ST/$name/session" 2>/dev/null))" ;;
  list)
    for d in "$ST"/*/; do [[ -d $d ]] || continue; n=$(basename "$d"); p=$(cat "$d/pid" 2>/dev/null || true)
      s="arrêté"; [[ -n $p ]] && kill -0 "$p" 2>/dev/null && s="actif"
      printf '%-10s %-9s %-7s %s\n' "$n" "$(cat "$d/tool" 2>/dev/null)" "$s" "$(cat "$d/session" 2>/dev/null)"; done ;;
  loop) cmd_loop "${2:-}" ;;
  *) sed -n 2,16p "$0" ;;
esac
