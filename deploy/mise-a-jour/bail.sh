#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# #502/#503: one lease criterion for the workstation, the server and the agents
# host. A restart check, not monitoring: a lease lost after the first good
# reading is not seen. The caller defines `lire_bail <args…>`, which prints the
# persona's `ameesh show --json`, and `heure_base <args…>`, which prints the
# database clock in epoch seconds (a lease expires at a database time).

bail_epoque() {  # stdin: show JSON → lease epoch (0 when absent)
  python3 -c 'import json,sys; print(int(json.loads(sys.stdin.read() or "{}").get("lease_epoch") or 0))'
}

bail_proprietaire() {  # stdin: show JSON → lease owner (empty when none)
  python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("lease_owner") or "")'
}

# Passes when the persona is stopped, or when its lease is held by the expected
# owner, at an epoch past epoch-before (claim and release both advance it), and
# is still valid at the database clock. The expected owner is the unit's
# --runner-id; without one, the runner's default is host:pid, so ":<MainPID>"
# stands for the persona's registry host followed by that PID.
bail_valide() {  # epoch-before expected-owner database-clock; stdin: show JSON
  python3 -c 'import json, sys
row = json.loads(sys.stdin.read() or "{}")
owner, expected = row.get("lease_owner") or "", sys.argv[2]
if expected.startswith(":"):
    expected = (row.get("host") or "") + expected
mine = bool(expected) and owner == expected
sys.exit(0 if row.get("status") == "stopped" or (
    mine and int(row.get("lease_epoch") or 0) > int(sys.argv[1])
    and float(row.get("lease_expires_ts") or 0) > float(sys.argv[3])) else 1)' "$1" "$2" "$3"
}

attendre_bail() {  # epoch-before expected-owner, then the arguments of lire_bail
  local avant=$1 attendu=$2 heure essai
  shift 2
  for ((essai = 0; essai < 24; essai++)); do
    heure=$(heure_base "$@") && lire_bail "$@" | bail_valide "$avant" "$attendu" "$heure" \
      && return 0
    if ((essai < 23)); then sleep 5; fi
  done
  return 1
}
