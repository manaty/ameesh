#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Recette ISOLÉE #1915 — API native Process sur une installation contenant #1994.
#
# Objet : prouver, sur un paquet réellement installé dans un venv dédié et une
# base PG jetable, les quatre points demandés par le pilote (mesh-design, 16/10
# 17:17) : API native + migration 0452, capacité-refus (fail-closed),
# idempotence/conflit, et la couture #1994 (acteur du verdict + id de livraison
# en chaîne lus par `read_sources`). Aucune suite complète, aucune activation
# consommateur : on réutilise les ciblés auteurs acquis.
#
# Usage :
#   .qa-1915/recette-1915-iso.sh [--build] [--schema iso1915_xxx]
#     --build   reconstruit la roue et le venv depuis $WT avant de mesurer
#   variables : AMEESH_TEST_DSN (banc local par défaut), ISO_SCHEMA
#
# Garde-fous : le DSN doit être le banc local 127.0.0.1:55432 et le schéma doit
# commencer par `iso1915_` ; la base de service (10.77.0.1) ne peut pas être
# visée, même par erreur d'environnement.
set -uo pipefail

WS=${WS:-/home/ameesh/work/mty-amh-impl-08}
BENCH=$WS/.qa-1915
WT=${WT:-$WS/.wt1915iso}
VENV=${VENV:-$BENCH/venv}
PY=$VENV/bin/python
WHEEL=${WHEEL:-$BENCH/dist/ameesh-1.11.3-py3-none-any.whl}
TARGET_SHA=${TARGET_SHA:-dffe30eaa54809e8e3350aaea046ed7c06b58e9b}
UPSTREAM_FIX=204082b7447317c03841f37ef96c06f43a12229b
TEST_DSN=${AMEESH_TEST_DSN:-postgresql://agent_mesh@127.0.0.1:55432/agent_mesh}
DO_BUILD=0
SCHEMA=${ISO_SCHEMA:-}
while [ $# -gt 0 ]; do
  case "$1" in
    --build) DO_BUILD=1 ;;
    --schema) shift; SCHEMA=${1:-} ;;
    *) echo "argument inconnu : $1" >&2; exit 2 ;;
  esac
  shift
done
SCHEMA=${SCHEMA:-iso1915_$(date -u +%Y%m%d_%H%M%S)}
RUN=${RUN:-$BENCH/logs/recette-$SCHEMA}
mkdir -p "$RUN"

case "$TEST_DSN" in
  *@127.0.0.1:55432/*) ;;
  *) echo "REFUS: DSN non isolé (banc local 127.0.0.1:55432 attendu): $TEST_DSN" >&2; exit 2 ;;
esac
case "$SCHEMA" in
  iso1915_*) ;;
  *) echo "REFUS: schéma non isolé (préfixe iso1915_ attendu): $SCHEMA" >&2; exit 2 ;;
esac

export AMEESH_CONFIG="$RUN/config-vide.json"
printf '{}\n' > "$AMEESH_CONFIG"
export AMEESH_DSN="$TEST_DSN"
export AMEESH_TEST_DSN="$TEST_DSN"
export AMEESH_REQUIRE_DB=1
export AMEESH_DRIVER=psycopg
export AMEESH_SCHEMA="$SCHEMA"
export AMEESH_STATE="$RUN/state"
mkdir -p "$AMEESH_STATE"
unset PYTHONPATH || true

RC=0
echo "=== recette isolée #1915 : $SCHEMA — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

if [ "$DO_BUILD" = 1 ]; then
  echo "--- reconstruction de l'installation depuis $WT"
  rm -rf "$VENV"
  python3 -m venv "$VENV" || RC=1
  "$VENV/bin/pip" -q install --upgrade pip setuptools wheel || RC=1
  "$VENV/bin/pip" wheel --no-deps -w "$BENCH/dist" "$WT" || RC=1
  "$VENV/bin/pip" install "$WHEEL" || RC=1
fi

# ---------------------------------------------------------------- 0. identité
echo "--- 0. identité de l'installation"
{
  echo "date_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "host=$(hostname)"
  echo "venv=$VENV"
  echo "source_worktree=$WT"
  echo "source_head=$TARGET_SHA"
  git -C "$WT" rev-parse HEAD
  git -C "$WT" log -1 --format='source_subject=%H %s'
  if git -C "$WT" merge-base --is-ancestor "$UPSTREAM_FIX" HEAD; then
    echo "contient_1994=$UPSTREAM_FIX oui"
  else
    echo "contient_1994=$UPSTREAM_FIX NON"
  fi
  sha256sum "$WHEEL"
  "$PY" - <<'PYEOF'
import importlib.metadata as m
import ameesh
print("ameesh_version=%s" % m.version("ameesh"))
print("ameesh_module=%s" % ameesh.__file__)
import ameesh.process_workflow as pw
import ameesh.process_registry as pr
import ameesh.process_verifiers as pv
print("process_workflow=%s" % pw.__file__)
print("process_registry=%s" % pr.__file__)
print("process_verifiers=%s" % pv.__file__)
print("gardes=%d" % len(pv.GUARD_NAMES))
print("reserves=%d" % len(pv.RESERVED_VERIFIERS))
print("types_installes=%d" % len(pv.INSTALLED_TYPES))
PYEOF
  # marqueurs de la couture #1994 dans le fichier réellement installé
  SP="$VENV/lib/python3.12/site-packages/ameesh"
  grep -n '"actor": str(row\["actor"\]' "$SP/process_registry.py" && echo "marqueur_acteur=présent"
  grep -n '"id": str(row\["id"\])' "$SP/process_registry.py" && echo "marqueur_livraison_chaine=présent"
  ls "$SP/migrations/0452_process_workflow_registry.sql" >/dev/null && echo "migration_0452_installee=présente"
} 2>&1 | tee "$RUN/00-identite.txt"

# ------------------------------------------------------- 1. migration 0452
echo "--- 1. migration 0452 sur schéma jetable $SCHEMA"
{
  "$PY" -m ameesh migrate
  echo "migrate_rc=$?"
  psql "$TEST_DSN" -X -A -F' | ' -c "
    SELECT version, name FROM \"$SCHEMA\".schema_migrations WHERE version = '0452'"
  psql "$TEST_DSN" -X -A -F' | ' -c "
    SELECT column_name, data_type, is_nullable, column_default
      FROM information_schema.columns
     WHERE table_schema = '$SCHEMA' AND table_name IN ('work_items','work_item_events')
       AND column_name IN ('workflow_ref','workflow_stage','workflow_version',
                           'command_id','evidence')
     ORDER BY table_name, column_name"
  psql "$TEST_DSN" -X -A -F' | ' -c "
    SELECT indexname FROM pg_indexes
     WHERE schemaname = '$SCHEMA' AND indexname = 'work_item_events_command_uidx'"
  psql "$TEST_DSN" -X -A -F' | ' -c "
    SELECT conname, pg_get_constraintdef(oid)
      FROM pg_constraint
     WHERE connamespace = '$SCHEMA'::regnamespace
       AND conrelid = '$SCHEMA.work_item_events'::regclass
     ORDER BY conname"
} 2>&1 | tee "$RUN/01-migration-0452.txt"

# ------------------------------------- 2. capacité-refus (matrice fail-closed)
echo "--- 2. capacité-refus (sans base, introspection pure)"
{
  cd "$WT"
  "$PY" -m unittest -v \
    tests.test_process_verifiers.CapacitesTest \
    tests.test_process_verifiers_seam.SeamTest
  echo "capacite_rc=$?"
} 2>&1 | tee "$RUN/02-capacite-refus.txt"

# ------------------------------- 3. idempotence/conflit/autorité/0452 + couture
echo "--- 3. idempotence, conflit, autorité, concurrence, 0452, couture #1994"
{
  cd "$WT"
  "$PY" -c "import ameesh; print('ameesh_testé=', ameesh.__file__)"
  "$PY" -m unittest -v \
    tests.test_process_registry.RegistryTest.test_nominal_advance_writes_core_stage_and_receipt \
    tests.test_process_registry.RegistryTest.test_replay_same_intent_finds_the_receipt \
    tests.test_process_registry.RegistryTest.test_same_key_other_intent_conflict \
    tests.test_process_registry.RegistryTest.test_optimistic_version_follows_the_journal \
    tests.test_process_registry.RegistryTest.test_concurrent_same_key_single_receipt \
    tests.test_process_registry.RegistryTest.test_two_commands_same_version_only_one_wins \
    tests.test_process_registry.RegistryTest.test_unknown_actor_refused \
    tests.test_process_registry.RegistryTest.test_stopped_actor_refuses_even_a_replay \
    tests.test_process_registry.RegistryTest.test_advance_without_installed_verifier \
    tests.test_process_registry.RegistryTest.test_verifier_that_refuses \
    tests.test_process_registry.RegistryTest.test_client_boolean_refused \
    tests.test_process_registry.RegistryTest.test_migration_receipt_uniqueness_and_completeness \
    tests.test_process_registry.RegistryTest.test_migration_complete_receipt_requires_every_field \
    tests.test_process_registry.RegistryTest.test_migration_reapplied_reinstalls_the_hardened_constraint \
    tests.test_process_registry.RegistryTest.test_migration_stage_without_reference_refused \
    tests.test_process_registry.RegistryTest.test_before_0452_historical_commands_do_not_break \
    tests.test_process_registry.RegistryTest.test_read_sources_declares_only_the_real \
    tests.test_process_registry.RegistryTest.test_real_seam_with_slice_c \
    tests.test_process_registry.RegistryTest.test_seam_reel_acteur_du_verdict_et_id_de_livraison \
    tests.test_process_registry.CompiledArtifactTest.test_compiled_artifact_accepted_by_the_registry
  echo "natif_rc=$?"
} 2>&1 | tee "$RUN/03-natif-idempotence-conflit-couture.txt"

# ------------------------------------------------------------------- résumé
"$PY" - "$RUN" "$SCHEMA" "$TARGET_SHA" <<'PYEOF' | tee "$RUN/04-resume.txt"
import json, re, socket, sys, datetime, pathlib
run, schema, sha = sys.argv[1], sys.argv[2], sys.argv[3]
run_dir = pathlib.Path(run)
res = {"schema": schema, "source_head": sha, "date_utc":
       datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
       "host": socket.gethostname(), "sections": {}}

def text_of(name):
    return (run_dir / name).read_text(encoding="utf-8", errors="replace")

# 0. identité : installation, roue, commit source et marqueurs #1994
ident = text_of("00-identite.txt")
res["sections"]["identite"] = {
    "version": (re.search(r"ameesh_version=(\S+)", ident) or [None, ""])[1],
    "module_installe_venv": "/.qa-1915/venv/" in ident and "ameesh_module=" in ident,
    "contient_1994": "contient_1994=204082b7" in ident and " oui" in ident,
    "marqueur_acteur": "marqueur_acteur=présent" in ident,
    "marqueur_livraison_chaine": "marqueur_livraison_chaine=présent" in ident,
    "migration_0452_dans_le_paquet": "migration_0452_installee=présente" in ident,
    "gardes": (re.search(r"gardes=(\d+)", ident) or [None, ""])[1],
    "reserves": (re.search(r"reserves=(\d+)", ident) or [None, ""])[1],
    "types_installes": (re.search(r"types_installes=(\d+)", ident) or [None, ""])[1],
}
res["sections"]["identite"]["ok"] = all(
    res["sections"]["identite"][k] for k in
    ("module_installe_venv", "contient_1994", "marqueur_acteur",
     "marqueur_livraison_chaine", "migration_0452_dans_le_paquet"))

# 1. migration 0452 sur schéma jetable
mig = text_of("01-migration-0452.txt")
res["sections"]["migration_0452"] = {
    "0452_appliquee": "0452_process_workflow_registry applied" in mig,
    "0452_enregistree": bool(re.search(r"^0?452 \| process_workflow_registry",
                                       mig, re.M)),
    "index_present": "work_item_events_command_uidx" in mig,
    "contrainte_recu_complet": "work_item_events_receipt_complete" in mig,
    "colonnes_registre": all(c in mig for c in
                             ("workflow_ref", "workflow_stage",
                              "workflow_version", "command_id", "evidence")),
    "migrate_rc": (re.search(r"migrate_rc=(\d+)", mig) or [None, ""])[1],
}
res["sections"]["migration_0452"]["ok"] = all(
    res["sections"]["migration_0452"][k] for k in
    ("0452_appliquee", "0452_enregistree", "index_present",
     "contrainte_recu_complet", "colonnes_registre")) and \
    res["sections"]["migration_0452"]["migrate_rc"] == "0"

# 2 et 3. unittest : lances, OK/FAILED, sautés
for name, key in (("02-capacite-refus.txt", "capacite_refus"),
                  ("03-natif-idempotence-conflit-couture.txt", "natif")):
    text = text_of(name)
    ran = re.findall(r"^Ran (\d+) tests?", text, re.M)
    failed = re.findall(r"^FAILED \(.*\)$", text, re.M)
    ok = re.findall(r"^OK(?: \(.*\))?$", text, re.M)
    res["sections"][key] = {
        "tests_lances": [int(x) for x in ran],
        "ok": bool(ok) and not failed,
        "failed": failed,
        "skipped": len(re.findall(r"\.\.\. skipped ", text)),
    }
res["sections"]["natif"]["couture_1994_executee"] = bool(re.search(
    r"test_seam_reel_acteur_du_verdict_et_id_de_livraison[\s\S]{0,400}?\.\.\. ok",
    text_of("03-natif-idempotence-conflit-couture.txt")))
_cap = text_of("02-capacite-refus.txt")
res["sections"]["capacite_refus"]["matrice_sondee"] = bool(re.search(
    r"test_capabilities_sondent_le_registre_installe[\s\S]{0,400}?\.\.\. ok", _cap))
res["sections"]["capacite_refus"]["fail_closed_statique"] = bool(re.search(
    r"test_contrat_statique_non_sonde_et_fail_closed[\s\S]{0,400}?\.\.\. ok", _cap))

res["statut"] = "OK" if all(v.get("ok") for v in res["sections"].values()) \
    else "A_EXAMINER"
print(json.dumps(res, ensure_ascii=False, indent=2))
PYEOF

if grep -qE '^FAILED|Traceback|REFUS' "$RUN/03-natif-idempotence-conflit-couture.txt" \
   "$RUN/02-capacite-refus.txt"; then
  RC=1
fi
echo "=== recette terminée rc=$RC — preuves dans $RUN ==="
exit "$RC"
