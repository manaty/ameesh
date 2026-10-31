# SPDX-License-Identifier: AGPL-3.0-only
"""Profil d'hébergement « cluster » (lot L25) : sonde, rôle superviseur, manifestes.

* `ameesh doctor --probe` : sonde légère de conteneur (base, schéma,
  migrations à jour), en lecture seule ;
* `deploy/sql/role-superviseur.sql` appliqué sur le Postgres du banc : le rôle
  lit ce qui est accordé, rien d'autre, et n'écrit nulle part ; toute colonne
  du schéma est soit accordée, soit exclue explicitement ;
* `deploy/k8s/` et `Dockerfile` : contrôles statiques (aucun cluster, aucun
  registre, aucune construction d'image ici).
"""
from __future__ import annotations

import os
import secrets
import subprocess
import unittest
import urllib.parse
import uuid

from ameesh import db as db_mod
from ameesh import migrations
from tests.support import REPO, TEST_DSN, PgTestCase
from tests.role_acl import serialize_role_acl

ROLE_SQL = os.path.join(REPO, "deploy", "sql", "role-superviseur.sql")
ROLE_CONTENUS_SQL = os.path.join(REPO, "deploy", "sql", "role-superviseur-contenus.sql")
K8S = os.path.join(REPO, "deploy", "k8s")

#: matériel de signature, de reçu et clés : refusé à TOUS les rôles superviseurs
#: (miroir de l'en-tête de role-superviseur.sql)
SIGNATURES = {
    "agent_mailbox": {"signature", "signed_payload", "nonce"},
    "mesh_approvals": {"signature", "signed_payload", "nonce"},
    "mesh_approvals_status": {"signature", "signed_payload", "nonce"},
    "mesh_consumed_nonces": {"nonce", "challenge"},
    "actions": {"auth_receipt", "auth_nonce", "auth_challenge",
                "replace_receipt", "replace_nonce"},
    "action_attempts": {"receipt", "nonce"},
    "standing_approvals": {"receipt", "nonce", "challenge"},
    "agent_registry": {"public_key",
                       # L87b (0441) : geste opérateur (motif, acteur,
                       # mandant, horodatage) — choix fermé, ouvrable plus
                       # tard comme état s'il est jugé utile à la supervision.
                       "operator_paused", "operator_pause_reason",
                       "operator_pause_by", "operator_pause_for",
                       "operator_pause_at",
                       # #3961 (0499) : échéance de la pause, même choix fermé.
                       "operator_pause_until"},
    "agent_mesh_overview": {"public_key"},
    "key_holders": {"public_key"},     # #330
    "mesh_keys": {"public_key"},
    "authenticators": {"public_key", "credential_id"},
    # exécuteurs médiés (voie B, 0062 et 0063)
    "executor_tokens": {"token_sha256"},
    "executor_invitations": {"code_sha256"},
    "executor_assertion_jti": {"jti"},
    "executors": {"public_key", "device_attestation"},
    "exec_idempotency": {"response"},
    # L87 (0440) : clés d'idempotence de l'API et réponses rejouables — même
    # nature que `exec_idempotency` ; tout est fermé tant que la classification
    # d'état n'est pas tranchée (correctif fermé 27665f633, décision pilote
    # 15/10 : conservation des droits actuels, aucun GRANT modifié).
    "api_idempotency": {"human", "key", "fingerprint", "status", "response",
                        "created_at", "done_at"},
    # L87b (0441) : journal des transitions de la pause opérateur.
    "operator_pause_events": {"id", "agent", "paused", "reason", "actor",
                              "principal", "created_at",
                              "until"},  # #3961 (0499)
    # L87c (0444): native-operator acknowledgement audit. Preserve the
    # migration's closed rights; this classification adds no GRANT.
    # (#1473/0457) : `perimeter` garde les seuils de l'accusé, même refus.
    "alert_acknowledgements": {"id", "alert_key", "severity", "observation_host",
                               "perimeter", "actor", "principal", "acknowledged_at",
                               "confirmed_at", "invalidated_at", "invalidation_reason"},
    # #210: fingerprint of a refresh token or key
    "credential_health": {"failing_fingerprint"},
}
#: contenus (texte libre) : refusés au rôle de base, seuls lisibles par le rôle
#: « contenus » (role-superviseur-contenus.sql)
CONTENUS = {
    "agent_peek": {"payload"},
    "agent_mailbox": {"body", "payload", "meta"},
    "agent_registry": {"pending_prompt", "current_prompt", "restart_brief",
                       "election_brief"},
    "agent_mesh_overview": {"pending_prompt", "current_prompt", "restart_brief"},
    "thread_index": {"last_excerpt"},
    "conversation_log": {"text", "meta", "thread_text"},
    "actions": {"args", "last_note"},
    "action_events": {"note"},
    "work_items": {"body"},
    "commitments": {"note"},
    "work_item_events": {"note"},
    "work_item_milestones": {"note"},
    "project_modules": {"schemas", "rules"},
    "work_module_milestones": {"payload"},
    "mesh_approvals": {"meta"},
    "mesh_approvals_status": {"meta"},
    # L161c (0055) : sortie d'un test en échec
    "ci_test_failures": {"excerpt"},
    # L232a (0371) : plans, preuves et détails de travail, sans secrets
    "host_handoffs": {"plan", "evidence"},
    "host_handoff_events": {"detail"},
    "host_lifecycle_agents": {"missing"},
    # #4148 : sortie du crochet de libération et journal des transitions
    "host_lifecycle": {"release_error"},
    "host_lifecycle_transitions": {"detail"},
    # L243b étape 3 (0516, #4299) : motif libre d'un renommage d'agent
    "agent_renames": {"reason"},
}
#: colonnes refusées au rôle de base. Une colonne nouvelle, ni accordée ni
#: listée ici, fait échouer le test : une migration oblige à trancher.
#: colonnes fermées en attendant leur classement (aucun GRANT modifié)
FERMEES = {
    # #5087 / 0529: review admission and withdrawals remain internal authority;
    # neither supervisor role receives additional access.
    "agent_registry": {"review_hold"},
    "work_review_withdrawals": {"work_item_id", "reviewer", "withdrawn"},
    # #4872 / 0526: budget authority and relay accounting remain closed to
    # supervisor roles; classification grants no additional access.
    "tranche_limits": {"id", "project", "cap_usd", "currency", "tz", "starts_at",
                       "ends_at", "state", "overdraft", "admitted_by", "admitted_ref",
                       "created_at"},
    "tranche_reservations": {"id", "tranche_id", "spend_key", "turn_id", "agent",
                            "billing_account", "scope_digest", "path", "reserved_usd",
                            "usd", "state", "created_at", "closed_at", "closed_by"},
    "tranche_events": {"id", "tranche_id", "reservation_id", "action", "actor",
                       "reason", "at", "data"},
    "tranche_relay_requests": {"id", "turn_id", "state", "created_at", "recorded_at"},
    # #4867 / 0520: dormant native sandbox authority, immutable admissions and
    # application-bound receipts/results. Preserve the existing closed rights
    # for BOTH supervisor roles; S5 owns any explicit read projection/grant.
    "sandbox_turns": {
        "turn_id", "spend_key", "agent", "owner", "lease_epoch", "sandbox_session",
        "request_ids", "work_item_id", "scope", "scope_digest", "retry_family",
        "scope_generation", "attempt", "max_attempts", "profile_ref", "profile_digest",
        "placement_ref", "placement_digest", "canon_ref", "tranche_id", "limits",
        "image_digest", "provider", "namespace", "job_name", "job_uid", "job_phase",
        "state", "result_id", "result_sha256", "refusal", "cancel_requested_at",
        "cancel_observed_at", "deadline_ts", "started_at", "completed_at", "source"},
    "sandbox_results": {"turn_id", "epoch", "result_id", "result_sha256", "payload",
                        "receipt", "accepted_at"},

    # #4991: signed replacement audit, not part of supervisor visibility.
    "decision_supersessions": {"supersedes", "superseded_by", "person", "occurred_at", "proof"},
    # #4256: private validation authority and per-person audit; owner CLI only.
    "decision_validations": {"decision_id", "routing", "escalated_at"},
    "validation_events": {"id", "decision_id", "person", "kind", "occurred_at", "facts", "event_key"},
    "validation_decision_links": {"source_id", "target_id", "relation", "response_key"},
    # #3963 (0498) : coordination interne de canon sync, sans nouveau GRANT.
    "canon_state": {"sync_claimed_at", "sync_token", "sync_completed_token",
                    "sync_signature"},
    # #1467 (0442): plans and replayable admission/fire receipts stay closed
    # to both supervisor roles. No new grants accompany this classification.
    "integration_trains": {"target_key", "revision", "document", "updated_at"},
    "integration_train_fires": {"id", "target_key", "plan_id", "document", "state",
                                "changed_by", "owner", "epoch", "created_at", "updated_at"},
    "integration_train_queue_receipts": {"id", "work_item_id", "generation", "frozen_id",
                                         "kind", "created_at", "target"},
    # #1465 (0445), required by #1467: exact provenance receipts also keep
    # their existing closed rights; this does not qualify their producers.
    "provenance_receipts": {"subject", "sha", "actor", "session", "turn", "harness",
                            "work_item_id", "at"},
    # #373 (L85) : rôles des agents dans la vue d'ensemble — fermés tant que le
    # contrat des rôles superviseur ne les accorde pas (paquet horaire du 15/10)
    "agent_mesh_overview": {"roles", "aliases"},  # #423 : 0418 avant 0428 sur un schéma neuf
    # #1883 (0451) : l'identité d'un compte est l'empreinte sha256 du dossier
    # résolu qu'il lit — jamais le chemin. Le propriétaire est alerté du conflit
    # par notify (`account_identity`) et le lot n'ouvre aucun accès distant
    # (arbitrage pilote du 16/10) : la table reste fermée, aucun GRANT modifié.
    "account_identities": {"host", "harness", "account", "identity",
                           "first_seen", "last_seen"},
    # #1922 (0452) : registre Process des workflows (révision/étape, reçus d'étape) —
    # fermé tant que le contrat des rôles superviseur ne le classe pas (paquet du 16/10)
    "work_items": {"workflow_ref", "workflow_stage", "workflow_version"},
    "work_item_events": {"command_id", "evidence", "workflow_stage", "workflow_version"},
    # #1104 (0438) : la borne d'activation du rattrapage des relectures après
    # fusion est un état interne, lu par les exécuteurs et non par le rôle
    # superviseur externe ; classement fermé, aucun GRANT modifié.
    "review_catch_up": {"id", "bound_at", "actor"},
    # #1945 (0453) : attente d'atterrissage planifiée — un lot, son véhicule,
    # la tête épinglée, l'échéance et l'auteur. État interne lu par l'exécuteur
    # (`planned_waits`), sans contenu ni secret ; fermé aux deux rôles
    # superviseur, aucun GRANT modifié.
    "work_landings": {"work_item_id", "vehicle_id", "pinned_sha", "until_on",
                      "created_by", "created_at"},
    # #4167 (0501) : liens typés entre lots (doublon, remplacement, part,
    # conflit) et la décision consignée sur un conflit (texte libre). État
    # interne lu par les passes de backlog ; fermé aux deux rôles superviseur
    # en attendant un classement, aucun GRANT modifié.
    "work_item_links": {"work_item_id", "other_id", "kind", "created_by", "created_at",
                        "decision", "decided_by", "decided_at"},
    # #4173 (0502) : empreinte d'un lot (chemins de fichiers du dépôt, source,
    # tête du diff). État interne lu par l'ordonnancement (prise, attribution) ;
    # fermé aux deux rôles superviseur en attendant un classement, aucun GRANT
    # modifié.
    "work_item_footprints": {"work_item_id", "source", "path", "head", "created_by",
                             "created_at"},
    # #4818 (0519) : politique d'empreinte d'un dépôt (inconnue, plafond,
    # chemins partagés) et dernière analyse d'impact d'un lot (chemins,
    # confiance, budget). État interne lu par la garde d'attribution ; fermé
    # aux deux rôles superviseur en attendant un classement, aucun GRANT
    # modifié.
    "repository_footprint_policies": {"app", "unknown_policy", "max_in_flight",
                                      "shared_paths", "updated_by", "updated_at"},
    "work_item_impact_analyses": {"work_item_id", "status", "reason", "paths", "weak",
                                  "policy", "elapsed_ms", "git_calls", "attempts",
                                  "analyzed_by", "analyzed_at"},
    # #1770 (0450) : le rapport de prévalidation au gel (sélection, tests, hôte,
    # chemin du plan) est une preuve interne lue par l'exécuteur, pas un état
    # de supervision ; table fermée aux deux rôles, aucun GRANT modifié.
    "prevalidation_reports": {"work_item_id", "head_sha", "base_sha", "card_commit",
                              "schema", "outcome", "ok", "causes", "report",
                              "recorded_at"},
    # #2421 (0464) : le payload d'usage résident d'un tour non réglé est un état
    # interne borné au tour (réparation d'un worker mort), pas un état de
    # supervision ; colonne fermée aux deux rôles, aucun GRANT modifié.
    "spend_pending": {"resident_usage"},
    # L165 (EP1, 0473) : verdicts de placement par hôte candidat, lus par la
    # condition de réclamation et non par le rôle superviseur ; table interne
    # fermée aux deux rôles, aucun GRANT ajouté.
    "agent_candidates": {"name", "host", "canon", "cwd", "placement_ok",
                         "placement_diagnostic", "placement_ref",
                         "placement_profile", "updated_at"},
    # #2443 (0465) : appartenance d'une unité à un paquet, écrite à la
    # composition (porteur, commit de composition, gel effectif). État interne
    # lu par `work show` et `sync-merges` ; fermé aux deux rôles superviseur,
    # aucun GRANT modifié.
    "work_packet_members": {"package_id", "work_item_id", "package_branch",
                            "base_sha", "head_sha", "unit_sha", "source_sha",
                            "frozen_sha", "recorded_by", "recorded_at"},
    # #3631 (0488) : agrégat des relevés d'hôte par tranche de 10 min, tenu
    # et lu par l'exécuteur (`lend_basis`), pas par le rôle superviseur ;
    # état de mesure interne, fermé aux deux rôles, aucun GRANT modifié.
    "host_resource_slots": {"host", "slot_start", "readings", "cpu_sum", "cpu_n",
                            "free_mem_min", "first_cpu_at", "last_cpu_at"},
    # #3921 (0496) : présence des relevés d'hôte par tranche de 120 s, tenue
    # par déclencheurs et lue par l'élection (`role_standings_pg.coverage`) ;
    # état de mesure interne, fermé aux deux rôles, aucun GRANT modifié.
    "host_presence": {"host", "bucket_start", "readings", "first_at", "last_at"},
    # #2845 (0470) : curseur, reçus et progression de la veille des tickets
    # GitHub. État interne de l'agent de veille, pas un état de supervision ;
    # fermé aux deux rôles superviseur, aucun GRANT modifié.
    "github_watch_cursors": {"project", "repo", "scanned_through"},
    "github_watch_events": {"project", "repo", "kind", "object_id", "digest"},
    "github_watch_scans": {"project", "repo", "progress"},
    # #288 (L80b, 0472) : attribution des messages servis par un tour résident
    # (`turn_costs.inputs`), consommée par le rapport local
    # `ameesh cost turns --message` ; le contrat des rôles superviseur reste
    # inchangé : colonne fermée aux deux rôles, aucun GRANT modifié.
    "turn_costs": {"inputs"},
    # L165 (EP2, 0476) : scores de candidats d'un rôle élu, lus par les
    # exécuteurs candidats et `ameesh role status`, pas par le rôle
    # superviseur ; table interne fermée aux deux rôles, aucun GRANT ajouté.
    # #3568 (0485) : la route de poussée publiée avec le relevé (notify_ok,
    # notify_diagnostic) reste interne à l'élection, fermée de même.
    "role_standings": {"id", "name", "host", "evaluated_at", "availability",
                       "capacity", "score", "ready", "diagnostic",
                       "notify_ok", "notify_diagnostic"},
    # #3099 (0478) : marque de libération d'une bascule de rôle élu, côté hôte
    # détenteur, relue par l'exécuteur au redémarrage ; état interne fermé aux
    # deux rôles superviseur, aucun GRANT modifié.
    "role_handover": {"host", "name", "challenger", "released_until", "updated_at"},
    # #4468 (0514) : mémoire de la remise de la passe alerts (hôte, instant),
    # écrite par le garde sous le bail d'exécution et lue par l'élection ;
    # état interne fermé aux deux rôles superviseur, aucun GRANT modifié.
    "alerts_delegations": {"role", "host", "entrusted_at"},
    # #3007 (0474) : battement partagé du notificateur et réclamation des
    # briefs par (destinataire, humain). État interne lu par l'exécuteur, les
    # alertes et les décisions, pas par le rôle superviseur ; tables fermées
    # aux deux rôles, aucun GRANT modifié.
    "notify_liveness": {"host", "human", "seen_at"},
    "notify_briefs": {"recipient", "human", "sent_at"},
    # #3576 (0487) : document de livraison de la passe alerts et son bail
    # d'exécution, écrits et lus par le notificateur seul ; état interne fermé
    # aux deux rôles superviseur, aucun GRANT modifié.
    "notify_state": {"scope", "body", "owner", "epoch", "expires_at", "updated_at"},
    # #3779 (0491) : journal partagé des refus de fenêtre courte (429), des
    # départs de tour par compte logique et des places actives effectives, lus
    # par `ameesh.account_window` et l'exécuteur, pas par le rôle superviseur ;
    # état interne d'admission, fermé aux deux rôles, aucun GRANT modifié.
    "account_rate_events": {"id", "harness", "account", "host", "agent", "kind",
                            "detail", "at"},
    "account_turn_starts": {"id", "harness", "account", "host", "agent", "at"},
    "account_turn_slots": {"agent", "harness", "account", "host", "at"},
    # #3885 (0492) : curseur et état de la passe de veille des empreintes
    # d'échec (projet, URL, curseur, dernier relevé et erreur), tenus et lus
    # par l'agent de veille, pas par le rôle superviseur ; état interne fermé
    # aux deux rôles, aucun GRANT modifié.
    "fingerprint_watch_state": {"project", "url", "cursor", "last_read_at",
                                "last_error", "updated_at"},
    # #4164 (0500) : crédits du compte fournisseur ; l'état est accordé au rôle
    # superviseur (alertes), le texte d'erreur libre du fournisseur reste fermé
    "provider_account_credits": {"detail"},
    # #4291 (0512) : relevé propre à la veille (ce qu'elle a réellement vu,
    # par harnais et fournisseur), tenu et lu par `ameesh veille`, pas par le
    # rôle superviseur ; fermé aux deux rôles, aucun GRANT modifié.
    "veille_readings": {"harness", "provider", "model_id", "first_seen",
                        "last_seen"},
    # #4293 : état interne d'une adoption de version (rapport de rejeu, séquence
    # canari/bascule, valeurs inverses), tenu et lu par `ameesh harness adopt`, pas par
    # le rôle superviseur ; fermé aux deux rôles, aucun GRANT modifié.
    "harness_adoptions": {"lot_id", "project", "harness", "object",
                          "from_version", "to_version", "stage", "report",
                          "report_digest", "state", "created_at", "updated_at"},
}
#: L232b (0417) : l'état interne du contrôleur de release reste hors contrat,
#: sauf `host_release_fences.host`, lu par `ameesh_host_gate_state` (l.314).
HORS_CONTRAT = {
    "host_release_operations": {"operation", "host", "manifest_digest", "created_at"},
    "host_release_fences": {"operation", "probe", "created_at"},
    "host_release_fence_receipts": {"action_id", "operation", "step", "receipt",
                                    "completed_at"},
}
EXCLUES = {rel: SIGNATURES.get(rel, set()) | CONTENUS.get(rel, set())
           | FERMEES.get(rel, set()) | HORS_CONTRAT.get(rel, set())
           for rel in set(SIGNATURES) | set(CONTENUS) | set(FERMEES) | set(HORS_CONTRAT)}


def ecarts_de_classement(cols: dict[str, dict[str, bool]]) -> list[str]:
    """Colonnes d'un relevé `columns()` ni accordées ni exclues, classées.

    Chaque écart nomme la colonne et l'endroit où la trancher : le contrat des
    rôles superviseur (`deploy/sql/role-superviseur.sql`, ou
    `role-superviseur-contenus.sql` pour les contenus) s'il faut la lire, ou
    `EXCLUES` s'il faut la fermer. Un rouge de ce garde est un classement
    oublié (#1018), pas un défaut d'environnement : le message doit le dire.
    """
    ecarts = []
    for rel, names in cols.items():
        for col, lisible in names.items():
            attendu = col not in EXCLUES.get(rel, set())
            if lisible == attendu:
                continue
            if attendu:
                ecarts.append(
                    "%s.%s : refusée — colonne absente du contrat des rôles superviseur "
                    "(lecture : deploy/sql/role-superviseur.sql ; contenus : "
                    "deploy/sql/role-superviseur-contenus.sql) ; si elle doit rester "
                    "fermée, classe-la dans EXCLUES (tests/test_cluster.py)"
                    % (rel, col))
            else:
                ecarts.append(
                    "%s.%s : lisible — colonne classée fermée (EXCLUES) mais accordée "
                    "(droit hérité, PUBLIC ou ACL par défaut) ; retire le droit ou "
                    "reclasse-la" % (rel, col))
    return ecarts


class ClassementColonnesTest(unittest.TestCase):
    """#1018 : le garde de classement est déterministe ; son message doit
    désigner le contrat à mettre à jour, pas ressembler à un flake."""

    def test_colonne_neuve_non_classee_nomme_le_contrat(self):
        ecarts = ecarts_de_classement({"agent_registry": {"roles": False}})
        self.assertEqual(len(ecarts), 1)
        self.assertIn("agent_registry.roles", ecarts[0])
        self.assertIn("deploy/sql/role-superviseur.sql", ecarts[0])
        self.assertIn("EXCLUES", ecarts[0])

    def test_colonnes_classees_ne_rendent_aucun_ecart(self):
        self.assertEqual(ecarts_de_classement({
            "agent_registry": {"roles": True, "public_key": False},
            "work_items": {"body": False},
        }), [])

    def test_colonne_exclue_mais_lisible_est_nommee(self):
        ecarts = ecarts_de_classement({"agent_registry": {"public_key": True}})
        self.assertEqual(len(ecarts), 1)
        self.assertIn("agent_registry.public_key", ecarts[0])
        self.assertIn("lisible", ecarts[0])


class ProbeTest(PgTestCase):
    """`doctor --probe` : OK sur un schéma à jour, KO sinon, sans rien écrire."""

    def probe(self, schema: str, via: str = "main") -> subprocess.CompletedProcess:
        env = self.env(AMEESH_SCHEMA=schema)
        if via == "cli":
            return self.cli("doctor", "--probe", env=env)
        return self.mesh("doctor", "--probe", env=env)

    def scratch_schema(self) -> str:
        """Schéma migré à part, effacé en fin de test (on y abîme les migrations)."""
        schema = "t_probe_%s" % uuid.uuid4().hex[:8]
        db = self.connect(schema)
        migrations.migrate(db, log=None)
        self.addCleanup(lambda: (db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema),
                                 db.close()))
        self.scratch_db = db
        return schema

    def test_ok_par_les_deux_commandes(self):
        for via in ("main", "cli"):
            proc = self.probe(self.schema, via)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("probe: OK", proc.stdout)
            # sonde légère : ni comptage, ni NOTIFY
            self.assertNotIn("agents", proc.stdout)
            self.assertNotIn("notify", proc.stdout)

    def test_schema_absent(self):
        proc = self.probe("t_probe_absent_%s" % uuid.uuid4().hex[:6])
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("schema missing", proc.stdout)

    def test_schema_absent_n_est_pas_cree(self):
        schema = "t_probe_rien_%s" % uuid.uuid4().hex[:6]
        self.probe(schema)
        rows = self.db.query(
            "SELECT count(*) AS n FROM pg_namespace WHERE nspname = %s", (schema,))
        self.assertEqual(int(rows[0]["n"]), 0)

    def test_base_injoignable(self):
        env = self.env(AMEESH_DSN="postgresql://nobody@127.0.0.1:1/none",
                       AMEESH_CONNECT_TIMEOUT="1")
        proc = self.mesh("doctor", "--probe", env=env)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("database unreachable", proc.stdout)

    def test_migration_manquante(self):
        schema = self.scratch_schema()
        derniere = migrations.discover()[-1]
        self.scratch_db.execute(
            "DELETE FROM schema_migrations WHERE version = %s", (derniere.version,))
        proc = self.probe(schema)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("missing %s" % derniere.label, proc.stdout)
        # la sonde n'a rien réappliqué
        self.assertIn(derniere.version, [m.version for m in migrations.check(self.scratch_db)[0]])

    def test_migration_modifiee(self):
        schema = self.scratch_schema()
        premiere = migrations.discover()[0]
        self.scratch_db.execute(
            "UPDATE schema_migrations SET checksum = 'x' WHERE version = %s",
            (premiere.version,))
        proc = self.probe(schema)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("modified %s" % premiere.label, proc.stdout)

    def test_base_plus_recente_reste_ok(self):
        schema = self.scratch_schema()
        self.scratch_db.execute(
            "INSERT INTO schema_migrations (version, name, checksum) "
            "VALUES (9999, 'future', 'x')")
        proc = self.probe(schema)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("newer than this code", proc.stdout)
        self.assertIn("9999", proc.stdout)


def _dsn_for(user: str, password: str) -> str:
    """Le DSN du banc, avec un autre utilisateur (format URL seulement)."""
    parts = urllib.parse.urlsplit(TEST_DSN)
    if parts.scheme not in ("postgresql", "postgres") or not parts.hostname:
        raise unittest.SkipTest("AMEESH_TEST_DSN n'est pas une URL : test de connexion sauté")
    host = parts.hostname + (":%d" % parts.port if parts.port else "")
    netloc = "%s:%s@%s" % (urllib.parse.quote(user), urllib.parse.quote(password), host)
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


class SuperviseurRoleTest(PgTestCase):
    """Les rôles en lecture seule du superviseur externe, sur un Postgres réel :
    le rôle de base (état) et le rôle « contenus », accordé en plus."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        serialize_role_acl(cls)
        suffix = uuid.uuid4().hex[:8]
        cls.role = "t_sup_%s" % suffix
        cls.role_contenus = "t_supc_%s" % suffix
        cls.login = "t_sup_login_%s" % suffix          # rôle de base seul
        cls.login_contenus = "t_supc_login_%s" % suffix  # base + contenus
        cls.password = secrets.token_hex(16)  # éphémère, détruit avec les rôles
        try:
            for _ in range(2):  # idempotent : la seconde passe ne change rien
                cls.apply_script(ROLE_SQL, cls.role)
                cls.apply_script(ROLE_CONTENUS_SQL, cls.role_contenus)
            for login, roles in ((cls.login, [cls.role]),
                                 (cls.login_contenus, [cls.role, cls.role_contenus])):
                cls.db.execute(
                    "CREATE ROLE %s LOGIN PASSWORD '%s' IN ROLE %s"
                    % (db_mod.quote_ident(login), cls.password,
                       ", ".join(db_mod.quote_ident(r) for r in roles)))
                cls.db.execute("ALTER ROLE %s SET search_path = %s"
                               % (db_mod.quote_ident(login), db_mod.quote_ident(cls.schema)))
        except Exception:
            cls.drop_roles()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.drop_roles()
        finally:
            super().tearDownClass()

    @classmethod
    def drop_roles(cls) -> None:
        for name in (cls.login, cls.login_contenus, cls.role, cls.role_contenus):
            exists = cls.db.query("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
            if exists:
                cls.db.execute("DROP OWNED BY %s" % db_mod.quote_ident(name))
                cls.db.execute("DROP ROLE %s" % db_mod.quote_ident(name))

    @classmethod
    def run_script(cls, path: str, role: str) -> subprocess.CompletedProcess:
        """Lance un script tel que documenté : sans `-1`, il ouvre sa propre
        transaction."""
        env = dict(os.environ, PGOPTIONS="-c search_path=%s" % cls.schema)
        return subprocess.run(
            ["psql", TEST_DSN, "-X", "-q", "-v", "ON_ERROR_STOP=1",
             "-v", "role=%s" % role, "-f", path],
            capture_output=True, text=True, timeout=60, env=env)

    @classmethod
    def apply_script(cls, path: str, role: str) -> subprocess.CompletedProcess:
        proc = cls.run_script(path, role)
        if proc.returncode != 0:
            raise AssertionError("%s en échec : %s" % (os.path.basename(path), proc.stderr))
        return proc

    def role_exists(self, name: str) -> bool:
        return bool(self.db.query("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)))

    def assert_refused(self, fragments: list[str], path: str = ROLE_SQL,
                       aussi_un_role_neuf: bool = True) -> None:
        """Le script refuse, avec chaque écart attendu dans le message ; la
        transaction est annulée : un rôle neuf n'est pas créé, le rôle existant
        garde exactement ses droits."""
        role = self.role if path == ROLE_SQL else self.role_contenus
        avant = self.columns(role)
        neuf = "t_supn_%s" % uuid.uuid4().hex[:8]
        try:
            for cible in ((neuf, role) if aussi_un_role_neuf else (role,)):
                proc = self.run_script(path, cible)
                self.assertNotEqual(proc.returncode, 0, "installation acceptée à tort")
                self.assertIn("NON TENU", proc.stderr)
                self.assertIn("installation annulée", proc.stderr)
                for fragment in fragments:
                    self.assertIn(fragment, proc.stderr)
            self.assertFalse(self.role_exists(neuf), "rôle créé malgré le refus")
            self.assertEqual(self.columns(role), avant, "droits changés malgré le refus")
        finally:
            if self.role_exists(neuf):
                self.db.execute("DROP OWNED BY %s" % db_mod.quote_ident(neuf))
                self.db.execute("DROP ROLE %s" % db_mod.quote_ident(neuf))

    def test_refus_droits_accordes_a_public(self):
        s = self.schema
        self.db.execute('GRANT SELECT (signature) ON "%s".agent_mailbox TO PUBLIC' % s)
        self.db.execute('GRANT SELECT (current_prompt) ON "%s".agent_mesh_overview TO PUBLIC' % s)
        try:
            self.assert_refused(["lecture hors contrat : agent_mailbox.signature",
                                 "lecture hors contrat : agent_mesh_overview.current_prompt"])
            # current_prompt est au contrat du rôle « contenus », pas signature
            self.assert_refused(["lecture hors contrat : agent_mailbox.signature"],
                                ROLE_CONTENUS_SQL)
        finally:
            self.db.execute('REVOKE SELECT (signature) ON "%s".agent_mailbox FROM PUBLIC' % s)
            self.db.execute(
                'REVOKE SELECT (current_prompt) ON "%s".agent_mesh_overview FROM PUBLIC' % s)
        self.apply_script(ROLE_SQL, self.role)
        self.apply_script(ROLE_CONTENUS_SQL, self.role_contenus)

    def test_refus_droit_herite_d_un_role_parent(self):
        parent = "t_supp_%s" % uuid.uuid4().hex[:8]
        self.db.execute("CREATE ROLE %s NOLOGIN" % db_mod.quote_ident(parent))
        try:
            self.db.execute('GRANT UPDATE ON "%s".agent_registry TO %s'
                            % (self.schema, db_mod.quote_ident(parent)))
            for role in (self.role, self.role_contenus):
                self.db.execute("GRANT %s TO %s"
                                % (db_mod.quote_ident(parent), db_mod.quote_ident(role)))
            self.assertTrue(self.db.query(
                "SELECT has_table_privilege(%s, %s, 'UPDATE') AS ok",
                (self.role, '"%s".agent_registry' % self.schema))[0]["ok"])
            for path in (ROLE_SQL, ROLE_CONTENUS_SQL):
                role = self.role if path == ROLE_SQL else self.role_contenus
                avant = self.columns(role)
                proc = self.run_script(path, role)
                self.assertNotEqual(proc.returncode, 0, "installation acceptée à tort")
                self.assertIn("UPDATE sur agent_registry", proc.stderr)
                self.assertIn("appartenance interdite", proc.stderr)
                self.assertIn("installation annulée", proc.stderr)
                self.assertEqual(self.columns(role), avant)
        finally:
            self.db.execute("DROP OWNED BY %s" % db_mod.quote_ident(parent))
            self.db.execute("DROP ROLE %s" % db_mod.quote_ident(parent))
        self.apply_script(ROLE_SQL, self.role)
        self.apply_script(ROLE_CONTENUS_SQL, self.role_contenus)

    def test_refus_appartenance_sans_heritage_mais_set_role(self):
        """Verdict codex3 B3 : INHERIT FALSE, SET TRUE — les droits effectifs du
        superviseur sont propres, mais un login fait SET ROLE parent et lit la
        signature. Toute appartenance du rôle superviseur est refusée."""
        if int(self.db.query("SELECT current_setting('server_version_num') AS v")[0]["v"]) < 160000:
            self.skipTest("GRANT … WITH INHERIT/SET : PostgreSQL 16+")
        parent = "t_supp_%s" % uuid.uuid4().hex[:8]
        q = db_mod.quote_ident
        self.db.execute("CREATE ROLE %s NOLOGIN" % q(parent))
        try:
            self.db.execute('GRANT USAGE ON SCHEMA "%s" TO %s' % (self.schema, q(parent)))
            self.db.execute('GRANT SELECT (signature) ON "%s".agent_mailbox TO %s'
                            % (self.schema, q(parent)))
            for role in (self.role, self.role_contenus):
                self.db.execute("GRANT %s TO %s WITH INHERIT FALSE, SET TRUE"
                                % (q(parent), q(role)))
            # la menace est réelle : droits effectifs propres, mais SET ROLE
            self.assertFalse(self.columns()["agent_mailbox"]["signature"])
            lu = self.as_supervisor(
                "SET ROLE %s; SELECT count(signature) FROM agent_mailbox" % q(parent))
            self.assertEqual(lu.returncode, 0, lu.stderr)
            for path in (ROLE_SQL, ROLE_CONTENUS_SQL):
                role = self.role if path == ROLE_SQL else self.role_contenus
                avant = self.columns(role)
                proc = self.run_script(path, role)
                self.assertNotEqual(proc.returncode, 0, "installation acceptée à tort")
                self.assertIn("appartenance interdite : %s est membre de %s" % (role, parent),
                              proc.stderr)
                self.assertIn("installation annulée", proc.stderr)
                self.assertEqual(self.columns(role), avant)
        finally:
            self.db.execute("DROP OWNED BY %s" % q(parent))
            self.db.execute("DROP ROLE %s" % q(parent))
        self.apply_script(ROLE_SQL, self.role)
        self.apply_script(ROLE_CONTENUS_SQL, self.role_contenus)

    def test_refus_appartenance_a_un_role_predefini(self):
        self.db.execute("GRANT pg_read_all_data TO %s" % db_mod.quote_ident(self.role))
        try:
            self.assert_refused(["appartenance interdite", "pg_read_all_data",
                                 "lecture hors contrat : agent_mailbox.signature"],
                                aussi_un_role_neuf=False)
        finally:
            self.db.execute("REVOKE pg_read_all_data FROM %s" % db_mod.quote_ident(self.role))
        self.apply_script(ROLE_SQL, self.role)

    def test_refus_droits_sur_un_autre_schema(self):
        """Verdict codex2 (L27 B2) : le contrat ne vaut que pour CE schéma ; un
        droit sur le schéma d'une autre équipe est refusé, jamais laissé ni
        révoqué en silence, pour les deux rôles superviseur."""
        q = db_mod.quote_ident
        autre = "t_supx_%s" % uuid.uuid4().hex[:8]
        self.db.execute("CREATE SCHEMA %s" % q(autre))
        self.db.execute("CREATE TABLE %s.secrets (id int, corps text)" % q(autre))
        try:
            for path, role in ((ROLE_SQL, self.role), (ROLE_CONTENUS_SQL, self.role_contenus)):
                with self.subTest(script=os.path.basename(path)):
                    self.db.execute("GRANT USAGE ON SCHEMA %s TO %s" % (q(autre), q(role)))
                    self.db.execute("GRANT SELECT ON %s.secrets TO %s" % (q(autre), q(role)))
                    try:
                        avant = self.columns(role)
                        proc = self.run_script(path, role)
                        self.assertNotEqual(proc.returncode, 0, "installation acceptée à tort")
                        self.assertIn("installation annulée", proc.stderr)
                        self.assertIn("lecture hors du schéma %s : %s.secrets"
                                      % (self.schema, autre), proc.stderr)
                        self.assertIn("droit accordé au rôle sur le schéma %s" % autre,
                                      proc.stderr)
                        self.assertEqual(self.columns(role), avant)
                        # rien révoqué en silence : le droit pollué est toujours là
                        self.assertTrue(self.db.query(
                            "SELECT has_table_privilege(%s, %s, 'SELECT') AS ok",
                            (role, "%s.secrets" % q(autre)))[0]["ok"])
                    finally:
                        self.db.execute("REVOKE ALL ON %s.secrets FROM %s" % (q(autre), q(role)))
                        self.db.execute("REVOKE ALL ON SCHEMA %s FROM %s" % (q(autre), q(role)))
            # droit latent (sans USAGE) accordé au rôle : refusé aussi
            self.db.execute("GRANT SELECT ON %s.secrets TO %s" % (q(autre), q(self.role)))
            try:
                proc = self.run_script(ROLE_SQL, self.role)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("droit accordé au rôle hors du schéma %s : %s.secrets"
                              % (self.schema, autre), proc.stderr)
            finally:
                self.db.execute("REVOKE ALL ON %s.secrets FROM %s" % (q(autre), q(self.role)))
        finally:
            self.db.execute("DROP SCHEMA %s CASCADE" % q(autre))
        self.apply_script(ROLE_SQL, self.role)
        self.apply_script(ROLE_CONTENUS_SQL, self.role_contenus)

    def test_refus_acl_par_defaut_d_un_autre_schema(self):
        """Droits FUTURS hors du schéma (verdict codex2, L27) : une ACL par
        défaut d'un autre schéma vers le rôle, sans table ni USAGE
        aujourd'hui, fait refuser les deux rôles superviseur."""
        q = db_mod.quote_ident
        autre = "t_supd_%s" % uuid.uuid4().hex[:8]
        self.db.execute("CREATE SCHEMA %s" % q(autre))
        try:
            for path, role in ((ROLE_SQL, self.role), (ROLE_CONTENUS_SQL, self.role_contenus)):
                with self.subTest(script=os.path.basename(path)):
                    self.db.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA %s GRANT SELECT ON "
                                    "TABLES TO %s" % (q(autre), q(role)))
                    try:
                        avant = self.columns(role)
                        proc = self.run_script(path, role)
                        self.assertNotEqual(proc.returncode, 0, "installation acceptée à tort")
                        self.assertIn("installation annulée", proc.stderr)
                        self.assertIn("ACL par défaut hors du schéma %s (r, %s) : SELECT à %s"
                                      % (self.schema, autre, role), proc.stderr)
                        self.assertEqual(self.columns(role), avant)
                    finally:
                        self.db.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA %s REVOKE SELECT ON "
                                        "TABLES FROM %s" % (q(autre), q(role)))
            # #5082 : fenêtre exacte du test approve qui polluait les autres
            # workers ; le refus global reste nécessaire en production.
            self.db.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA %s GRANT SELECT ON "
                            "TABLES TO PUBLIC" % q(autre))
            self.db.execute("GRANT USAGE ON SCHEMA %s TO PUBLIC" % q(autre))
            try:
                for path, role in ((ROLE_SQL, self.role), (ROLE_CONTENUS_SQL, self.role_contenus)):
                    proc = self.run_script(path, role)
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn("ACL par défaut hors du schéma %s (r, %s) : SELECT à PUBLIC"
                                  % (self.schema, autre), proc.stderr)
            finally:
                self.db.execute("REVOKE USAGE ON SCHEMA %s FROM PUBLIC" % q(autre))
                self.db.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA %s REVOKE SELECT ON "
                                "TABLES FROM PUBLIC" % q(autre))
        finally:
            self.db.execute("DROP SCHEMA %s CASCADE" % q(autre))
        self.apply_script(ROLE_SQL, self.role)
        self.apply_script(ROLE_CONTENUS_SQL, self.role_contenus)

    def test_refus_droits_au_niveau_de_la_base(self):
        q = db_mod.quote_ident
        base = self.db.query("SELECT current_database() AS d")[0]["d"]
        self.db.execute("GRANT CREATE ON DATABASE %s TO %s" % (q(base), q(self.role)))
        try:
            self.assert_refused(["CREATE sur la base %s" % base,
                                 "droit accordé au rôle sur la base %s" % base],
                                aussi_un_role_neuf=False)
        finally:
            self.db.execute("REVOKE CREATE ON DATABASE %s FROM %s" % (q(base), q(self.role)))
        self.apply_script(ROLE_SQL, self.role)

    def test_refus_acl_par_defaut(self):
        s = db_mod.quote_ident(self.schema)
        self.db.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA %s GRANT SELECT ON TABLES TO PUBLIC" % s)
        try:
            # une future table de signature serait lisible par tous : refus
            self.assert_refused(["ACL par défaut", "SELECT à PUBLIC"])
            self.assert_refused(["ACL par défaut", "SELECT à PUBLIC"], ROLE_CONTENUS_SQL)
        finally:
            self.db.execute(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA %s REVOKE SELECT ON TABLES FROM PUBLIC" % s)
        self.db.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA %s GRANT SELECT ON TABLES TO %s"
                        % (s, db_mod.quote_ident(self.role)))
        try:
            # visant le rôle lui-même (un rôle neuf, non visé, serait accepté)
            self.assert_refused(["ACL par défaut", "SELECT à %s" % self.role],
                                aussi_un_role_neuf=False)
        finally:
            self.db.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA %s REVOKE SELECT ON TABLES FROM %s"
                            % (s, db_mod.quote_ident(self.role)))
        self.apply_script(ROLE_SQL, self.role)

    def test_refus_colonne_du_contrat_absente(self):
        """Base en retard de migrations : refus avant tout changement."""
        schema = "t_supr_%s" % uuid.uuid4().hex[:6]
        self.db.execute('CREATE SCHEMA "%s"' % schema)
        try:
            self.db.execute('CREATE TABLE "%s".agent_registry (name text)' % schema)
            env = dict(os.environ, PGOPTIONS="-c search_path=%s" % schema)
            neuf = "t_supn_%s" % uuid.uuid4().hex[:8]
            proc = subprocess.run(
                ["psql", TEST_DSN, "-X", "-q", "-v", "ON_ERROR_STOP=1",
                 "-v", "role=%s" % neuf, "-f", ROLE_SQL],
                capture_output=True, text=True, timeout=60, env=env)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("colonnes absentes du schéma", proc.stderr)
            self.assertFalse(self.role_exists(neuf))
        finally:
            self.db.execute('DROP SCHEMA "%s" CASCADE' % schema)

    def as_supervisor(self, sql: str, login: str | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("PGPASSWORD", None)
        env.pop("PGOPTIONS", None)
        return subprocess.run(
            ["psql", _dsn_for(login or self.login, self.password), "-X", "-At",
             "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True, text=True, timeout=30, env=env)

    def columns(self, role: str | None = None) -> dict[str, dict[str, bool]]:
        """{relation: {colonne: lisible par le rôle}} pour tout le schéma."""
        rows = self.db.query(
            "SELECT c.relname AS rel, a.attname AS col, "
            "has_column_privilege(%s, c.oid, a.attnum, 'SELECT') AS ok "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
            "WHERE n.nspname = %s AND c.relkind IN ('r', 'v', 'm', 'p') "
            "ORDER BY 1, a.attnum", (role or self.role, self.schema))
        out: dict[str, dict[str, bool]] = {}
        for row in rows:
            out.setdefault(row["rel"], {})[row["col"]] = bool(row["ok"])
        return out

    def test_chaque_colonne_est_accordee_ou_exclue(self):
        cols = self.columns()
        self.assertTrue(set(EXCLUES) <= set(cols), set(EXCLUES) - set(cols))
        ecarts = ecarts_de_classement(cols)
        self.assertEqual(ecarts, [], "colonnes ni accordées ni exclues (ou l'inverse)")

    def test_le_superviseur_evalue_la_porte_de_release(self):
        # L232b (0417) : `ameesh_host_gate_state` lit `host_release_fences.host` ;
        # le périmètre d'opération et les reçus restent hors contrat.
        lu = self.as_supervisor("SELECT ameesh_host_gate_state('synthetic-host', 'idle')")
        self.assertEqual(lu.returncode, 0, lu.stderr)
        self.assertEqual(lu.stdout.strip(), "idle")
        refus = self.as_supervisor("SELECT operation FROM host_release_fences")
        self.assertNotEqual(refus.returncode, 0)
        self.assertIn("permission denied", refus.stderr.lower())

    def test_le_role_contenus_lit_exactement_les_contenus(self):
        for rel, cols in CONTENUS.items():
            self.assertFalse(cols & SIGNATURES.get(rel, set()), rel)
        lisibles = {(rel, col) for rel, names in self.columns(self.role_contenus).items()
                    for col, ok in names.items() if ok}
        attendues = {(rel, col) for rel, cols in CONTENUS.items() for col in cols}
        self.assertEqual(lisibles, attendues)

    def test_la_seconde_passe_retire_un_droit_ajoute_a_la_main(self):
        self.db.execute('GRANT SELECT (body) ON "%s".agent_mailbox TO %s'
                        % (self.schema, db_mod.quote_ident(self.role)))
        self.db.execute('GRANT INSERT ON "%s".work_items TO %s'
                        % (self.schema, db_mod.quote_ident(self.role)))
        self.assertTrue(self.columns()["agent_mailbox"]["body"])
        self.apply_script(ROLE_SQL, self.role)
        self.assertFalse(self.columns()["agent_mailbox"]["body"])
        rows = self.db.query("SELECT has_table_privilege(%s, %s, 'INSERT') AS ok",
                             (self.role, '"%s".work_items' % self.schema))
        self.assertFalse(rows[0]["ok"])

    def test_aucun_droit_d_ecriture(self):
        for role in (self.role, self.role_contenus):
            rows = self.db.query(
                "SELECT c.relname AS rel, p.priv FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', "
                "'REFERENCES', 'TRIGGER']) AS p(priv) "
                "WHERE n.nspname = %s AND c.relkind IN ('r', 'v', 'm', 'p') "
                "AND (has_table_privilege(%s, c.oid, p.priv) "
                "     OR (p.priv IN ('INSERT', 'UPDATE', 'REFERENCES') "
                "         AND has_any_column_privilege(%s, c.oid, p.priv)))",
                (self.schema, role, role))
            self.assertEqual(rows, [], role)
            rows = self.db.query(
                "SELECT count(*) AS n FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND CASE WHEN c.relkind = 'S' "
                "THEN has_sequence_privilege(%s, c.oid, 'USAGE,SELECT,UPDATE') END",
                (self.schema, role))
            self.assertEqual(int(rows[0]["n"]), 0, role)
            rows = self.db.query("SELECT has_schema_privilege(%s, %s, 'CREATE') AS ok",
                                 (role, self.schema))
            self.assertFalse(rows[0]["ok"], role)
            rows = self.db.query(
                "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls "
                "FROM pg_roles WHERE rolname = %s", (role,))
            self.assertEqual(rows[0], {"rolcanlogin": False, "rolsuper": False,
                                       "rolcreaterole": False, "rolcreatedb": False,
                                       "rolbypassrls": False}, role)

    def test_un_superviseur_lit_mais_n_ecrit_pas(self):
        proc = self.runner("register", "sup1", "claude", "--cwd", self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = self.mesh("mail", "send", "sup1", "bonjour superviseur", "--from", "orchestrateur")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        lu = self.as_supervisor(
            "SELECT name || '|' || status FROM agent_registry ORDER BY name")
        self.assertEqual(lu.returncode, 0, lu.stderr)
        self.assertIn("sup1|", lu.stdout)
        lu = self.as_supervisor(
            "SELECT name, unread, passive_unread FROM agent_mesh_overview")
        self.assertEqual(lu.returncode, 0, lu.stderr)
        self.assertIn("sup1", lu.stdout)
        lu = self.as_supervisor("SELECT count(*) FROM schema_migrations")
        self.assertEqual(lu.stdout.strip(), str(len(migrations.discover())), lu.stderr)
        lu = self.as_supervisor("SELECT recipient, kind, status FROM agent_mailbox")
        self.assertEqual(lu.returncode, 0, lu.stderr)
        self.assertIn("sup1", lu.stdout)

        refus = [
            "SELECT public_key FROM agent_registry",
            "SELECT * FROM agent_registry",
            "SELECT signature FROM agent_mailbox",
            "SELECT nonce FROM mesh_consumed_nonces",
            "SELECT auth_receipt FROM actions",
            # contenus : refusés au rôle de base
            "SELECT body FROM agent_mailbox",
            "SELECT payload FROM agent_mailbox",
            "SELECT pending_prompt FROM agent_registry",
            "SELECT current_prompt FROM agent_mesh_overview",
            "SELECT last_excerpt FROM thread_index",
            "SELECT args FROM actions",
            "SELECT body FROM work_items",
            "INSERT INTO agent_mailbox (sender, recipient, body) VALUES ('x', 'sup1', 'y')",
            "UPDATE agent_registry SET status = 'stopped'",
            "DELETE FROM work_items",
            "TRUNCATE thread_index",
            "CREATE TABLE intrus (x int)",
        ]
        for sql in refus:
            proc = self.as_supervisor(sql)
            self.assertNotEqual(proc.returncode, 0, "accepté à tort : %s" % sql)
            self.assertIn("permission denied", proc.stderr.lower(), sql)
        # rien n'a bougé
        rows = self.db.query("SELECT status FROM agent_registry WHERE name = 'sup1'")
        self.assertNotEqual(rows[0]["status"], "stopped")

    def test_le_role_contenus_en_plus_lit_les_messages(self):
        proc = self.runner("register", "sup2", "claude", "--cwd", self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = self.mesh("mail", "send", "sup2", "contenu confidentiel", "--from", "orchestrateur")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lu = self.as_supervisor(
            "SELECT recipient || '|' || body FROM agent_mailbox", self.login_contenus)
        self.assertEqual(lu.returncode, 0, lu.stderr)
        self.assertIn("sup2|contenu confidentiel", lu.stdout)
        lu = self.as_supervisor("SELECT pending_prompt FROM agent_registry", self.login_contenus)
        self.assertEqual(lu.returncode, 0, lu.stderr)
        for sql in ("SELECT signature FROM agent_mailbox",
                    "SELECT auth_receipt FROM actions",
                    "UPDATE agent_mailbox SET body = 'x'"):
            proc = self.as_supervisor(sql, self.login_contenus)
            self.assertNotEqual(proc.returncode, 0, "accepté à tort : %s" % sql)
            self.assertIn("permission denied", proc.stderr.lower(), sql)


try:
    import yaml  # PyYAML : facultatif, présent sur la plupart des postes
except ImportError:  # pragma: no cover - dépend de l'environnement
    yaml = None


@unittest.skipIf(yaml is None, "PyYAML absent : manifestes non vérifiés (kubectl kustomize le fait)")
class ManifestesTest(unittest.TestCase):
    """deploy/k8s : chargement YAML et invariants du profil cluster."""

    @classmethod
    def setUpClass(cls) -> None:
        with open(os.path.join(K8S, "kustomization.yaml"), encoding="utf-8") as fh:
            cls.kustomization = yaml.safe_load(fh)
        cls.docs = []
        for name in cls.kustomization["resources"]:
            with open(os.path.join(K8S, name), encoding="utf-8") as fh:
                cls.docs.extend(d for d in yaml.safe_load_all(fh) if d)

    def of_kind(self, kind: str) -> list[dict]:
        return [d for d in self.docs if d.get("kind") == kind]

    def statefulset(self) -> dict:
        (sts,) = self.of_kind("StatefulSet")
        return sts

    def test_tous_les_yaml_se_chargent(self):
        for name in sorted(os.listdir(K8S)):
            if name.endswith(".yaml"):
                with open(os.path.join(K8S, name), encoding="utf-8") as fh:
                    list(yaml.safe_load_all(fh))

    def test_statefulset_un_agent(self):
        sts = self.statefulset()
        spec = sts["spec"]
        self.assertEqual(spec["replicas"], 1)
        pod = spec["template"]["spec"]
        self.assertGreaterEqual(pod["terminationGracePeriodSeconds"], 45)
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        (tpl,) = spec["volumeClaimTemplates"]
        runner = [c for c in pod["containers"] if c["name"] == "runner"][0]
        mounts = {m["name"]: m for m in runner["volumeMounts"]}
        self.assertEqual(mounts[tpl["metadata"]["name"]]["mountPath"], "/home/ameesh")
        self.assertTrue(mounts["canon"].get("readOnly"), "le canon est en lecture seule")
        env = {e["name"]: e for e in runner["env"]}
        self.assertEqual(env["AMEESH_HOST"]["valueFrom"]["fieldRef"]["fieldPath"],
                         "metadata.name")
        probe = runner["readinessProbe"]["exec"]["command"]
        self.assertEqual(probe, ["ameesh", "doctor", "--probe"])
        # `args` seulement : tini (ENTRYPOINT de l'image) reste le PID 1 qui
        # relaie SIGTERM ; `command` le remplacerait
        for c in pod["containers"] + pod["initContainers"]:
            self.assertNotIn("command", c, c["name"])
        # la clé de déploiement du canon n'est jamais montée dans l'exécuteur
        self.assertNotIn("canon-key", mounts)
        # aucune exposition réseau
        self.assertNotIn("ports", runner)
        for svc in self.of_kind("Service"):
            self.assertEqual(svc["spec"].get("clusterIP"), "None")
            self.assertFalse(svc["spec"].get("ports"))

    def test_init_containers(self):
        pod = self.statefulset()["spec"]["template"]["spec"]
        noms = [c["name"] for c in pod["initContainers"]]
        self.assertEqual(noms, ["migrate", "canon"])
        canon = " ".join(pod["initContainers"][1]["args"])
        self.assertIn("git clone", canon)
        self.assertIn("ameesh canon sync --fetch", canon)

    def test_secrets_references_jamais_remplis(self):
        self.assertEqual(self.of_kind("Secret"), [], "aucun Secret dans les ressources")
        with open(os.path.join(K8S, "secrets.exemple.yaml"), encoding="utf-8") as fh:
            for doc in yaml.safe_load_all(fh):
                for champ in ("data", "stringData"):
                    for cle, valeur in (doc.get(champ) or {}).items():
                        self.assertIn(valeur, ("", None), "%s rempli" % cle)
        pod = self.statefulset()["spec"]["template"]["spec"]
        noms = set()
        for c in pod["containers"] + pod["initContainers"]:
            for e in c.get("env", []):
                ref = (e.get("valueFrom") or {}).get("secretKeyRef")
                if ref:
                    noms.add(ref["name"])
            for e in c.get("envFrom", []):
                if "secretRef" in e:
                    noms.add(e["secretRef"]["name"])
        for v in pod["volumes"]:
            if "secret" in v:
                noms.add(v["secret"]["secretName"])
        self.assertTrue(noms)
        with open(os.path.join(K8S, "secrets.exemple.yaml"), encoding="utf-8") as fh:
            declares = {d["metadata"]["name"] for d in yaml.safe_load_all(fh) if d}
        self.assertEqual(noms, declares)

    def test_configmap_config_json(self):
        import json
        gen = self.kustomization["configMapGenerator"][0]
        fichier = gen["files"][0].split("=")[-1]
        with open(os.path.join(K8S, fichier), encoding="utf-8") as fh:
            conf = json.load(fh)
        self.assertNotIn(":", urllib.parse.urlsplit(conf["dsn"]).netloc.split("@")[0],
                         "aucun mot de passe dans le DSN de la ConfigMap")
        self.assertEqual(conf["canon"], "/var/lib/ameesh/canon/repo")

    def test_networkpolicy_refuse_l_entree(self):
        (pol,) = self.of_kind("NetworkPolicy")
        self.assertIn("Ingress", pol["spec"]["policyTypes"])
        self.assertFalse(pol["spec"].get("ingress"))


class DockerfileTest(unittest.TestCase):
    """Contrôles statiques de l'image (la construction est vérifiée à part)."""

    def setUp(self) -> None:
        with open(os.path.join(REPO, "Dockerfile"), encoding="utf-8") as fh:
            self.text = fh.read()

    def test_utilisateur_non_root(self):
        users = [l.split()[1] for l in self.text.splitlines() if l.startswith("USER ")]
        self.assertTrue(users)
        self.assertNotIn(users[-1], ("root", "0"))

    def test_aucun_secret(self):
        for ligne in self.text.splitlines():
            haut = ligne.upper()
            if ligne.startswith(("ENV ", "ARG ")):
                for mot in ("PASSWORD", "TOKEN", "API_KEY", "SECRET", "PGPASS"):
                    self.assertNotIn(mot, haut, ligne)
        with open(os.path.join(REPO, ".dockerignore"), encoding="utf-8") as fh:
            ignore = fh.read().split()
        for motif in (".git", ".venv", "**/.env", "**/*.pem", "**/id_*"):
            self.assertIn(motif, ignore)


if __name__ == "__main__":
    unittest.main()
