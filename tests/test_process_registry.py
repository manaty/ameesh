# SPDX-License-Identifier: AGPL-3.0-only
"""#1922 — registre natif Process : révision/étape et reçus idempotents.

Preuves ciblées sur un vrai PostgreSQL, avec les deux pilotes (le bench
sélectionne psycopg ou psql par `AMEESH_DRIVER`) :

* avance nominale, rejeu après connexion fraîche, conflit d'intention,
  concurrence de la même clé et de deux commandes à la même version ;
* autorité : acteur inconnu/arrêté, bail d'un autre runner, époque périmée,
  bail échu, course avec une révocation (la ligne du registre est tenue
  `FOR SHARE` pendant toute la transaction) ;
* retenues `blocked`/`waiting_human` (cette dernière exige une demande de
  décision native ouverte), reprise, terminal jamais rouvert, boucle de
  réparation bornée ;
* transfert de révision explicite, jamais automatique, sans régression ;
* annulation sans demi-événement (trigger qui casse l'UPDATE), historique
  existant intact, lots sans workflow inchangés ;
* contraintes de la migration additive 0452 (reçu complet, unicité partielle
  (lot, clé)).

Les organisations et agents sont fictifs (`acme`). Aucune activation :
`INSTALLED_VERIFIERS` est vide par défaut, les tests injectent leurs
vérificateurs par le seam documenté.
"""
from __future__ import annotations

import contextlib
import json
import threading
import unittest
import uuid
from dataclasses import dataclass

from ameesh import process_registry as pr
from ameesh import work
from ameesh.db import DbError

from .support import PgTestCase

try:  # tranche A (#1918), pas encore fusionnée au moment d'écrire ces tests
    from ameesh import process_workflow as pw
except ImportError:  # pragma: no cover - dépend de l'état de main
    pw = None

ACME_REF = "acme/process-workflow@0f1e2d3c"
ACME_VERSION = 3
ACME_ACTOR = "acme-dev1"
ACME_RUNNER = "acme-runner-1"
ACME_EPOCH = 4


@dataclass(frozen=True)
class _Stage:
    status: str
    core: str


@dataclass(frozen=True)
class _Transition:
    source: str
    target: str
    guard: str


ACME_STAGES = (
    _Stage("draft", "intake"),
    _Stage("implementation", "build"),
    _Stage("review", "qa"),
    _Stage("staging", "merged"),
    _Stage("done", "promoted"),
    _Stage("stopped", "closed"),
)
ACME_TRANSITIONS = (
    _Transition("draft", "implementation", "planning"),
    _Transition("implementation", "review", "review_requested"),
    _Transition("review", "staging", "qa_started"),
    _Transition("review", "implementation", "qa_repair"),
    _Transition("staging", "done", "work_completed"),
    _Transition("draft", "stopped", "stop_work"),
)
ACME_GUARDS = tuple(t.guard for t in ACME_TRANSITIONS)


class _Spec:
    """Artefact minimal qui satisfait le protocole `WorkflowSpec` publié."""

    def __init__(self, *, ref: str = ACME_REF, version: int = ACME_VERSION,
                 initial: str = "draft", stages=ACME_STAGES,
                 transitions=ACME_TRANSITIONS) -> None:
        self.workflow_ref = ref
        self.workflow_version = version
        self.initial = initial
        self._stages = tuple(stages)
        self._transitions = tuple(transitions)

    def statuses(self):
        return tuple(stage.status for stage in self._stages)

    def core_of(self, status):
        for stage in self._stages:
            if stage.status == status:
                return stage.core
        raise KeyError(status)

    def transitions_from(self, source):
        return tuple(t for t in self._transitions if t.source == source)

    def stage(self, status):
        for stage in self._stages:
            if stage.status == status:
                return stage
        raise KeyError(status)


def _ok(context=None):
    return pr.Verification(ok=True, reason="prouvé")


class RegistryTest(PgTestCase):
    """Socle : un agent runner lié, un lot pinné, un vérificateur par garde."""

    def setUp(self) -> None:
        super().setUp()
        self.spec = _Spec()
        self.calls: list[str] = []
        self.verifiers = {guard: self._recording(guard) for guard in ACME_GUARDS}

        def transfer(context):
            self.calls.append(pr.TRANSFER_VERIFIER)
            return pr.Verification(ok=True, reason="transfert relu")

        self.verifiers[pr.TRANSFER_VERIFIER] = transfer
        self.agent(ACME_ACTOR, lease_owner=ACME_RUNNER, epoch=ACME_EPOCH)

    # -- fabriques ---------------------------------------------------------

    def agent(self, name: str, *, status: str = "idle", lease_owner: str | None = None,
              epoch: int = 0, live: bool = True) -> None:
        self.db.execute(
            "INSERT INTO agent_registry (name, status, lease_owner, lease_epoch,"
            "                            lease_expires_at)"
            " VALUES (%s, %s, %s, %s,"
            "         CASE WHEN %s THEN now() + interval '1 hour'"
            "              ELSE now() - interval '1 hour' END)",
            (name, status, lease_owner, epoch, live))

    def lot(self, *, state: str = "intake", ref: str | None = None,
            stage: str | None = None, version: int = 0, loops: int = 0,
            title: str = "acme : un lot") -> int:
        row = self.db.query(
            "INSERT INTO work_items (title, state, app, workflow_ref, workflow_stage,"
            "                        workflow_version, loops)"
            " VALUES (%s, %s, 'acme', %s, %s, %s, %s) RETURNING id",
            (title, state, ref, stage, version, loops))
        return int(row[0]["id"])

    def pinned(self, *, stage: str = "draft", state: str = "intake",
               ref: str | None = None, version: int = ACME_VERSION,
               loops: int = 0) -> int:
        return self.lot(state=state, ref=ref or self.spec.workflow_ref,
                        stage=stage, version=version, loops=loops)

    def _recording(self, guard: str):
        def verifier(context):
            self.calls.append(guard)
            return _ok(context)
        return verifier

    def authority(self, *, actor: str = ACME_ACTOR, source: str = "runner",
                  epoch: int = ACME_EPOCH, runner_id: str = ACME_RUNNER):
        return pr.Authority(actor=actor, source=source, epoch=epoch,
                            runner_id=runner_id)

    def command(self, lot_id: int, *, action: str = "advance", frm: str = "draft",
                to: str = "implementation", guard: str = "planning",
                hold: str = "", version: int = 0, command_id: str | None = None,
                ref: str | None = None, revision: int = ACME_VERSION,
                authority=None, facts=None):
        return pr.Command(
            work_item_id=lot_id,
            command_id=command_id or str(uuid.uuid4()),
            expected_version=version,
            authority=authority or self.authority(),
            workflow_ref=ref or self.spec.workflow_ref,
            workflow_version=revision,
            action=action, from_stage=frm, to_stage=to, guard=guard, hold=hold,
            evidence=dict(facts or {}))

    def apply(self, command, *, spec=None, verifiers=None):
        return pr.apply_command(self.db, command, spec=spec or self.spec,
                                verifiers=self.verifiers if verifiers is None else verifiers)

    @contextlib.contextmanager
    def refusal(self, code: str):
        with self.assertRaises(pr.RegistryRefusal) as ctx:
            yield
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))

    def events(self, lot_id: int) -> list[dict]:
        return self.db.query(
            "SELECT id, state, note, actor, command_id, workflow_stage,"
            "       workflow_version, evidence"
            "  FROM work_item_events WHERE work_item_id = %s ORDER BY id", (lot_id,))

    def row(self, lot_id: int) -> dict:
        return self.db.query("SELECT * FROM work_items WHERE id = %s", (lot_id,))[0]

    def journal_version(self, lot_id: int) -> int:
        return int(self.db.query(
            "SELECT coalesce(max(id), 0) AS v FROM work_item_events"
            " WHERE work_item_id = %s", (lot_id,))[0]["v"])

    def request_open_for(self, lot_id: int) -> int:
        """L233 : une demande de décision native ouverte pour ce lot."""
        row = self.db.query(
            "INSERT INTO agent_mailbox (sender, recipient, body, kind, payload,"
            "                           work_item_id)"
            " VALUES ('acme-orch', 'human:*', 'which option?', 'request',"
            "         '{\"decision\": {\"v\": 1, \"state\": \"pending\"}}'::jsonb, %s)"
            " RETURNING id", (str(lot_id),))
        return int(row[0]["id"])

    # -- avance nominale ---------------------------------------------------

    def test_avance_nominale_ecrit_core_etape_et_recu(self):
        lot = self.pinned()
        cmd = self.command(lot)
        receipt = self.apply(cmd)
        self.assertTrue(receipt.accepted)
        self.assertFalse(receipt.replayed)
        self.assertEqual(receipt.state, "build")
        self.assertEqual(receipt.workflow_stage, "implementation")
        self.assertEqual(receipt.version, receipt.event_id)
        row = self.row(lot)
        self.assertEqual(row["state"], "build")
        self.assertEqual(row["workflow_stage"], "implementation")
        self.assertEqual(row["workflow_version"], ACME_VERSION)
        self.assertIsNotNone(row["started_at"])
        events = self.events(lot)
        self.assertEqual(len(events), 1)
        self.assertEqual(str(events[0]["command_id"]), cmd.command_id)
        evidence = events[0]["evidence"]
        self.assertEqual(evidence["schema"], pr.RECEIPT_SCHEMA)
        self.assertEqual(evidence["intent"]["sha256"], pr.intent_sha256(cmd))
        self.assertEqual(evidence["command"]["guard"], "planning")
        self.assertEqual(evidence["authority"]["epoch"], ACME_EPOCH)
        self.assertTrue(evidence["verification"]["required"])
        self.assertEqual(evidence["verification"]["verifier"], "planning")
        self.assertEqual(evidence["facts"], {})
        self.assertEqual(self.calls, ["planning"])

    def test_rejeu_meme_intention_retrouve_le_recu(self):
        lot = self.pinned()
        cmd = self.command(lot, facts={"ticket": "ACME-7"})
        first = self.apply(cmd)
        fresh = self.connect()                      # connexion neuve
        try:
            again = pr.apply_command(fresh, cmd, spec=self.spec,
                                     verifiers=self.verifiers)
        finally:
            fresh.close()
        self.assertTrue(again.replayed)
        self.assertEqual(again.event_id, first.event_id)
        self.assertEqual(len(self.events(lot)), 1)
        self.assertEqual(self.calls, ["planning"])   # le vérificateur n'a pas rejoué

    def test_meme_cle_autre_intention_conflit(self):
        lot = self.pinned()
        command_id = str(uuid.uuid4())
        self.apply(self.command(lot, command_id=command_id, facts={"ticket": "ACME-1"}))
        same_key = self.command(lot, command_id=command_id, facts={"ticket": "ACME-2"})
        with self.refusal("intent-conflict"):
            self.apply(same_key)
        changed = self.command(lot, command_id=command_id, to="stopped",
                               guard="stop_work", facts={"ticket": "ACME-1"})
        with self.refusal("intent-conflict"):
            self.apply(changed)
        self.assertEqual(len(self.events(lot)), 1)

    def test_version_optimiste_suit_le_journal(self):
        lot = self.pinned()
        first = self.apply(self.command(lot))
        self.assertEqual(first.version, self.journal_version(lot))
        stale = self.command(lot, frm="implementation", to="review",
                             guard="review_requested", version=first.version - 1)
        with self.refusal("version-conflict"):
            self.apply(stale)
        following = self.command(lot, frm="implementation", to="review",
                                 guard="review_requested", version=first.version)
        self.assertEqual(self.apply(following).state, "qa")

    def test_ecriture_legacy_invalide_la_version(self):
        lot = self.pinned()
        first = self.apply(self.command(lot))
        work.move(self.db, lot, "blocked", actor=ACME_ACTOR)   # journal legacy
        self.assertGreater(self.journal_version(lot), first.version)
        stale = self.command(lot, frm="implementation", to="review",
                             guard="review_requested", version=first.version)
        with self.refusal("version-conflict"):
            self.apply(stale)

    # -- autorité ----------------------------------------------------------

    def test_acteur_inconnu_refuse(self):
        lot = self.pinned()
        cmd = self.command(lot, authority=pr.Authority(
            actor="acme-ghost", source="runner", epoch=1, runner_id=ACME_RUNNER))
        with self.refusal("authority-unknown-actor"):
            self.apply(cmd)
        self.assertEqual(self.row(lot)["state"], "intake")
        self.assertEqual(self.events(lot), [])

    def test_acteur_arrete_refuse_meme_un_rejeu(self):
        lot = self.pinned()
        cmd = self.command(lot)
        first = self.apply(cmd)
        self.db.execute("UPDATE agent_registry SET status = 'stopped' WHERE name = %s",
                        (ACME_ACTOR,))
        with self.refusal("authority-revoked"):
            self.apply(cmd)
        # le reçu existe, mais un acteur révoqué ne le récupère pas
        self.assertEqual(len(self.events(lot)), 1)
        self.assertEqual(first.event_id, self.events(lot)[0]["id"])

    def test_epoque_perimee_refuse_meme_un_rejeu(self):
        lot = self.pinned()
        cmd = self.command(lot)
        self.apply(cmd)
        self.db.execute("UPDATE agent_registry SET lease_epoch = lease_epoch + 1"
                        " WHERE name = %s", (ACME_ACTOR,))
        with self.refusal("authority-epoch"):
            self.apply(cmd)

    def test_bail_d_un_autre_runner_refuse(self):
        lot = self.pinned()
        cmd = self.command(lot, authority=pr.Authority(
            actor=ACME_ACTOR, source="runner", epoch=ACME_EPOCH,
            runner_id="acme-runner-2"))
        with self.refusal("authority-lease"):
            self.apply(cmd)

    def test_bail_echu_refuse(self):
        lot = self.pinned()
        self.db.execute("UPDATE agent_registry SET lease_expires_at = now() - interval '1 s'"
                        " WHERE name = %s", (ACME_ACTOR,))
        with self.refusal("authority-expired"):
            self.apply(self.command(lot))

    def test_source_session_sans_bail(self):
        lot = self.pinned()
        self.agent("acme-dev2")           # ni bail ni époque
        cmd = self.command(lot, authority=pr.Authority(actor="acme-dev2",
                                                       source="session", epoch=0))
        self.assertEqual(self.apply(cmd).state, "build")
        bad = self.command(lot, version=self.journal_version(lot),
                           authority=pr.Authority(actor="acme-dev2", source="session",
                                                  epoch=7))
        with self.refusal("authority-epoch"):
            self.apply(bad)

    def test_course_avec_une_revocation(self):
        """La ligne du registre tient FOR SHARE tant que la transaction vit."""
        lot = self.pinned()
        entered, release = threading.Event(), threading.Event()

        def blocking(context):
            entered.set()
            release.wait(10)
            return _ok(context)

        outcome: list = []

        def run():
            db = self.connect()
            try:
                outcome.append(pr.apply_command(
                    db, self.command(lot), spec=self.spec,
                    verifiers={"planning": blocking}))
            except Exception as exc:                     # pragma: no cover - diagnostic
                outcome.append(exc)
            finally:
                db.close()

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(entered.wait(10), "le vérificateur n'a pas démarré")
        try:
            with self.assertRaises(DbError):
                with self.db.transaction() as tx:
                    tx.execute("SET LOCAL lock_timeout = '300ms'")
                    tx.execute("UPDATE agent_registry SET lease_epoch = lease_epoch + 1"
                               " WHERE name = %s", (ACME_ACTOR,))
        finally:
            release.set()
            thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], pr.Receipt)
        self.assertEqual(outcome[0].state, "build")

    # -- gardes ------------------------------------------------------------

    def test_avance_sans_verificateur_installe(self):
        lot = self.pinned()
        with self.refusal("verifier-uninstalled"):
            self.apply(self.command(lot), verifiers={})
        self.assertEqual(self.events(lot), [])

    def test_verificateur_qui_refuse(self):
        lot = self.pinned()

        def refuses(context):
            return pr.Verification(ok=False, reason="preuve absente")

        with self.refusal("verifier-refused"):
            self.apply(self.command(lot), verifiers={"planning": refuses})
        self.assertEqual(self.events(lot), [])

    def test_booleen_client_refuse(self):
        lot = self.pinned()
        with self.refusal("verifier-invalid"):
            self.apply(self.command(lot), verifiers={"planning": lambda context: True})
        self.assertEqual(self.events(lot), [])

    def test_arete_non_declaree(self):
        lot = self.pinned()
        with self.refusal("edge-undeclared"):
            self.apply(self.command(lot, to="review", guard="qa_started"))
        with self.refusal("edge-undeclared"):
            self.apply(self.command(lot, to="implementation", guard="qa_repair"))
        self.assertEqual(self.events(lot), [])

    def test_etape_courante_differente(self):
        lot = self.pinned(stage="review", state="qa")
        with self.refusal("stage-mismatch"):
            self.apply(self.command(lot, frm="draft", to="implementation",
                                    guard="planning"))
        self.assertEqual(self.events(lot), [])

    def test_terminal_non_rouvert(self):
        lot = self.lot(state="promoted", ref=self.spec.workflow_ref,
                       stage="done", version=ACME_VERSION)
        with self.refusal("terminal-reopened"):
            self.apply(self.command(lot, frm="done", to="staging",
                                    guard="qa_started", version=0))
        with self.refusal("terminal-reopened"):
            self.apply(self.command(lot, action="hold", hold="blocked",
                                    frm="", to="", guard="", version=0))
        self.assertEqual(self.events(lot), [])

    def test_boucle_de_reparation_bornee(self):
        lot = self.pinned(stage="review", state="qa", loops=0)
        first = self.apply(self.command(lot, frm="review", to="implementation",
                                        guard="qa_repair"))
        self.assertEqual(first.state, "build")
        self.assertEqual(self.row(lot)["loops"], 1)
        forward = self.apply(self.command(lot, frm="implementation", to="review",
                                          guard="review_requested", version=first.version))
        self.assertEqual(forward.state, "qa")
        self.assertEqual(self.row(lot)["loops"], 1)
        second = self.apply(self.command(lot, frm="review", to="implementation",
                                         guard="qa_repair", version=forward.version))
        self.assertEqual(self.row(lot)["loops"], 2)
        self.assertEqual(second.state, "build")
        forward = self.apply(self.command(lot, frm="implementation", to="review",
                                          guard="review_requested", version=second.version))
        exhausted = self.command(lot, frm="review", to="implementation",
                                 guard="qa_repair", version=forward.version)
        with self.refusal("repair-exhausted"):
            self.apply(exhausted)
        self.assertEqual(len(self.events(lot)), 4)

    # -- retenues ----------------------------------------------------------

    def test_retenue_et_reprise_conservent_l_etape(self):
        lot = self.pinned(stage="implementation", state="build")
        held = self.apply(self.command(lot, action="hold", hold="blocked",
                                       frm="", to="", guard=""))
        self.assertEqual(held.state, "blocked")
        self.assertEqual(held.workflow_stage, "implementation")
        self.assertEqual(self.row(lot)["workflow_stage"], "implementation")
        evidence = self.events(lot)[-1]["evidence"]
        self.assertFalse(evidence["verification"]["required"])
        with self.refusal("hold-redundant"):
            self.apply(self.command(lot, action="hold", hold="blocked",
                                    frm="", to="", guard="", version=held.version))
        released = self.apply(self.command(lot, action="release", frm="", to="",
                                           guard="", version=held.version))
        self.assertEqual(released.state, "build")
        self.assertEqual(released.workflow_stage, "implementation")

    def test_reprise_sans_retenue_refusee(self):
        lot = self.pinned()
        with self.refusal("not-held"):
            self.apply(self.command(lot, action="release", frm="", to="", guard=""))

    def test_waiting_human_exige_une_decision_ouverte(self):
        lot = self.pinned(stage="implementation", state="build")
        cmd = self.command(lot, action="hold", hold="waiting_human",
                           frm="", to="", guard="")
        with self.refusal("hold-no-decision"):
            self.apply(cmd)
        self.request_open_for(lot)
        held = self.apply(cmd)
        self.assertEqual(held.state, "waiting_human")
        self.assertEqual(self.row(lot)["workflow_stage"], "implementation")

    def test_waiting_human_apres_reponse_fermee_refuse(self):
        lot = self.pinned(stage="implementation", state="build")
        request = self.request_open_for(lot)
        self.db.execute("UPDATE agent_mailbox SET delivered_at = now(), status = 'answered'"
                        " WHERE id = %s", (request,))
        with self.refusal("hold-no-decision"):
            self.apply(self.command(lot, action="hold", hold="waiting_human",
                                    frm="", to="", guard=""))

    # -- transfert de révision --------------------------------------------

    def transfer(self, lot_id: int, *, ref: str | None = None, version: int = ACME_VERSION,
                 spec=None, expected: int | None = None, command_id: str | None = None,
                 verifiers=None):
        return pr.transfer_revision(
            self.db, work_item_id=lot_id, command_id=command_id or str(uuid.uuid4()),
            expected_version=self.journal_version(lot_id) if expected is None else expected,
            workflow_ref=ref or self.spec.workflow_ref, workflow_version=version,
            authority=self.authority(), spec=spec or self.spec,
            evidence={"review": "relu"},
            verifiers=self.verifiers if verifiers is None else verifiers)

    def test_adoption_explicite_d_un_workflow(self):
        lot = self.lot()                       # aucun workflow
        receipt = self.transfer(lot)
        self.assertEqual(receipt.action, "transfer")
        row = self.row(lot)
        self.assertEqual(row["workflow_ref"], self.spec.workflow_ref)
        self.assertEqual(row["workflow_version"], ACME_VERSION)
        self.assertEqual(row["workflow_stage"], "draft")
        self.assertEqual(row["state"], "intake")
        self.assertIn(pr.TRANSFER_VERIFIER, self.calls)
        event = self.events(lot)[-1]
        self.assertTrue(event["evidence"]["verification"]["required"])
        self.assertEqual(event["evidence"]["verification"]["verifier"],
                         pr.TRANSFER_VERIFIER)
        self.assertEqual(event["evidence"]["command"]["previous_ref"], None)

    def test_transfert_sans_verificateur_refuse(self):
        lot = self.lot()
        with self.refusal("verifier-uninstalled"):
            self.transfer(lot, verifiers={})
        self.assertIsNone(self.row(lot)["workflow_ref"])

    def test_transfert_conserve_l_etape_et_ne_regresse_pas(self):
        lot = self.pinned(stage="review", state="qa")
        newer = _Spec(version=ACME_VERSION + 1)
        receipt = self.transfer(lot, version=ACME_VERSION + 1, spec=newer)
        row = self.row(lot)
        self.assertEqual(row["workflow_version"], ACME_VERSION + 1)
        self.assertEqual(row["workflow_stage"], "review")
        self.assertEqual(row["state"], "qa")
        self.assertEqual(receipt.version, self.journal_version(lot))
        with self.refusal("revision-regression"):
            self.transfer(lot, version=ACME_VERSION, expected=self.journal_version(lot))
        self.assertEqual(self.row(lot)["workflow_version"], ACME_VERSION + 1)

    def test_apres_transfert_l_ancienne_revision_est_refusee(self):
        lot = self.pinned()
        first = self.apply(self.command(lot))
        self.transfer(lot, version=ACME_VERSION + 1, spec=_Spec(version=ACME_VERSION + 1))
        with self.refusal("revision-mismatch"):
            self.apply(self.command(lot, frm="implementation", to="review",
                                    guard="review_requested", version=first.version))
        # variante : référence différente (le lot reste sur l'ancienne)
        with self.refusal("ref-mismatch"):
            self.apply(self.command(lot, ref="acme/process-workflow@other",
                                    frm="implementation", to="review",
                                    guard="review_requested", version=first.version),
                       spec=_Spec(ref="acme/process-workflow@other"))

    def test_transfert_rejoue(self):
        lot = self.lot()
        cid = str(uuid.uuid4())
        first = self.transfer(lot, command_id=cid, expected=0)
        again = self.transfer(lot, command_id=cid, expected=0)
        self.assertTrue(again.replayed)
        self.assertEqual(again.event_id, first.event_id)
        self.assertEqual(len(self.events(lot)), 1)

    # -- lots sans workflow, historique, annulation ------------------------

    def test_lot_sans_workflow_inchange(self):
        lot = self.lot(state="build")
        before = self.events(lot)
        with self.refusal("workflow-absent"):
            self.apply(self.command(lot))
        work.move(self.db, lot, "qa", actor=ACME_ACTOR)
        row = self.row(lot)
        self.assertEqual(row["state"], "qa")
        self.assertIsNone(row["workflow_ref"])
        self.assertIsNone(row["workflow_stage"])
        self.assertEqual(row["workflow_version"], 0)
        after = self.events(lot)
        self.assertEqual([e["id"] for e in before], [e["id"] for e in after][:len(before)])
        self.assertTrue(all(e["command_id"] is None for e in after))

    def test_historique_existant_intact(self):
        lot = self.lot(state="build")
        work.move(self.db, lot, "qa", actor=ACME_ACTOR, note="revue demandée")
        before = self.events(lot)
        pinned = self.pinned()
        self.apply(self.command(pinned))
        after = self.events(lot)
        self.assertEqual(before, after)
        self.assertIsNone(after[0]["command_id"])
        self.assertIsNone(after[0]["evidence"])

    def test_annulation_sans_demi_evenement(self):
        lot = self.pinned()
        self.db.script(
            "CREATE FUNCTION t_1922_boom() RETURNS trigger LANGUAGE plpgsql AS $$"
            " BEGIN RAISE EXCEPTION 'boom 1922'; END $$;"
            " CREATE TRIGGER t_1922_boom BEFORE UPDATE ON work_items"
            "   FOR EACH ROW EXECUTE FUNCTION t_1922_boom();")
        try:
            with self.assertRaises(DbError):
                self.apply(self.command(lot))
        finally:
            self.db.script("DROP TRIGGER t_1922_boom ON work_items;"
                           " DROP FUNCTION t_1922_boom();")
        row = self.row(lot)
        self.assertEqual(row["state"], "intake")
        self.assertEqual(row["workflow_stage"], "draft")
        self.assertEqual(self.events(lot), [])

    def test_echec_du_verificateur_annule_tout(self):
        lot = self.pinned()

        def explodes(context):
            raise RuntimeError("panne du composant installé")

        with self.refusal("verifier-error"):
            self.apply(self.command(lot), verifiers={"planning": explodes})
        self.assertEqual(self.events(lot), [])
        self.assertEqual(self.row(lot)["state"], "intake")

    # -- concurrence -------------------------------------------------------

    def _concurrent(self, functions):
        results, errors = [], []
        barrier = threading.Barrier(len(functions), timeout=15)

        def wrap(function):
            def run():
                db = self.connect()
                try:
                    barrier.wait()
                    results.append(function(db))
                except Exception as exc:
                    errors.append(exc)
                finally:
                    db.close()
            return run

        threads = [threading.Thread(target=wrap(f)) for f in functions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertFalse(any(t.is_alive() for t in threads), "thread bloqué")
        return results, errors

    def test_meme_cle_concurrente_un_seul_recu(self):
        lot = self.pinned()
        cmd = self.command(lot)

        def run(db):
            return pr.apply_command(db, cmd, spec=self.spec, verifiers=self.verifiers)

        results, errors = self._concurrent([run, run])
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.accepted for r in results))
        self.assertEqual(sum(1 for r in results if r.replayed), 1)
        self.assertEqual(len(self.events(lot)), 1)
        self.assertEqual(self.calls, ["planning"])

    def test_deux_commandes_meme_version_une_seule_gagne(self):
        lot = self.pinned()
        first = self.command(lot)
        second = self.command(lot)

        def run_a(db):
            return pr.apply_command(db, first, spec=self.spec, verifiers=self.verifiers)

        def run_b(db):
            return pr.apply_command(db, second, spec=self.spec, verifiers=self.verifiers)

        results, errors = self._concurrent([run_a, run_b])
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], pr.RegistryRefusal)
        self.assertEqual(errors[0].code, "version-conflict")
        self.assertEqual(len(self.events(lot)), 1)

    # -- lecture et contraintes de migration -------------------------------

    def test_etat_du_registre_et_recu(self):
        lot = self.pinned()
        cmd = self.command(lot)
        state = pr.registry_state(self.db, lot)
        self.assertEqual((state.workflow_ref, state.workflow_stage,
                          state.workflow_version, state.version, state.receipts),
                         (ACME_REF, "draft", ACME_VERSION, 0, 0))
        receipt = self.apply(cmd)
        state = pr.registry_state(self.db, lot)
        self.assertEqual(state.version, receipt.event_id)
        self.assertEqual(state.receipts, 1)
        stored = pr.receipt_of(self.db, lot, cmd.command_id)
        self.assertEqual(stored["workflow_stage"], "implementation")
        self.assertEqual(stored["evidence"]["intent"]["sha256"],
                         pr.intent_sha256(cmd))
        self.assertIsNone(pr.receipt_of(self.db, lot, str(uuid.uuid4())))

    def test_migration_unicite_et_completude_du_recu(self):
        lot = self.pinned()
        cmd = self.command(lot)
        receipt = self.apply(cmd)
        with self.assertRaises(DbError):
            self.db.execute(
                "INSERT INTO work_item_events (work_item_id, state, command_id)"
                " VALUES (%s, 'build', %s::uuid)",
                (lot, str(uuid.uuid4())))
        with self.assertRaises(DbError):
            self.db.execute(
                "INSERT INTO work_item_events (work_item_id, state, command_id,"
                "  workflow_stage, workflow_version, evidence)"
                " VALUES (%s, 'build', %s::uuid, 'implementation', %s, %s::jsonb)",
                (lot, cmd.command_id, ACME_VERSION, json.dumps(
                    {"schema": pr.RECEIPT_SCHEMA, "intent": {"sha256": "0" * 64},
                     "command": {"action": "advance"},
                     "authority": {}, "verification": {}})))
        with self.assertRaises(DbError):
            self.db.execute(
                "INSERT INTO work_item_events (work_item_id, state, command_id,"
                "  workflow_stage, workflow_version, evidence)"
                " VALUES (%s, 'build', %s::uuid, 'implementation', %s, %s::jsonb)",
                (lot, str(uuid.uuid4()), ACME_VERSION, json.dumps(
                    {"schema": pr.RECEIPT_SCHEMA, "intent": {"sha256": "0" * 64},
                     "command": {"action": "teleport"},
                     "authority": {}, "verification": {}})))
        self.assertEqual(len(self.events(lot)), 1)
        self.assertEqual(pr.receipt_of(self.db, lot, cmd.command_id)["id"],
                         receipt.event_id)

    def test_migration_etape_sans_reference_refusee(self):
        with self.assertRaises(DbError):
            self.db.execute(
                "INSERT INTO work_items (title, state, workflow_stage)"
                " VALUES ('acme : étape orpheline', 'intake', 'draft')")

    def test_migration_version_negative_refusee(self):
        with self.assertRaises(DbError):
            self.db.execute(
                "INSERT INTO work_items (title, state, workflow_ref, workflow_version)"
                " VALUES ('acme : version négative', 'intake', %s, -1)", (ACME_REF,))

    # -- validation publique ----------------------------------------------

    def test_refus_de_forme(self):
        lot = self.pinned()
        with self.refusal("command-invalid"):
            self.apply(self.command(lot, command_id="pas-un-uuid"))
        with self.refusal("command-invalid"):
            self.apply(self.command(lot, action="teleport"))
        with self.refusal("authority-invalid"):
            self.apply(self.command(lot, authority=pr.Authority(actor="")))
        with self.refusal("authority-source"):
            self.apply(self.command(lot, authority=pr.Authority(
                actor=ACME_ACTOR, source="dream")))
        with self.refusal("evidence-invalid"):
            self.apply(self.command(lot, facts={"ratio": 1.5}))
        with self.refusal("hold-invalid"):
            self.apply(self.command(lot, action="hold", hold="paused",
                                    frm="", to="", guard=""))
        with self.refusal("spec-mismatch"):
            self.apply(self.command(lot), spec=_Spec(version=ACME_VERSION + 9))
        with self.refusal("spec-invalid"):
            self.apply(self.command(lot), spec=object())

    def test_installer_un_verificateur(self):
        lot = self.pinned()
        self.addCleanup(pr.INSTALLED_VERIFIERS.pop, "planning", None)
        with self.refusal("verifier-name"):
            pr.install_verifier("Planning!", _ok)
        pr.install_verifier("planning", _ok)
        self.assertTrue(pr.apply_command(self.db, self.command(lot),
                                         spec=self.spec).accepted)
        with self.refusal("verifier-name"):
            pr.install_verifier("planning", lambda context: _ok())
        pr.install_verifier("planning", _ok)          # idempotent, même objet
        self.assertEqual(list(pr.installed_verifiers()), ["planning"])


@unittest.skipUnless(pw is not None, "tranche A (#1918) absente de main")
class CompiledArtifactTest(PgTestCase):
    """L'artefact de la tranche A satisfait le protocole sans adaptation."""

    def test_artefact_compile_accepte_par_le_registre(self):
        document = {
            "schema": "process-workflow/1",
            "initial": "draft",
            "types": ["evolution"],
            "stages": [{"status": s.status, "core": s.core} for s in ACME_STAGES],
            "transitions": [{"from": t.source, "to": t.target, "guard": t.guard}
                            for t in ACME_TRANSITIONS],
            "projection": {
                "project": "acme", "status_field": "state",
                "product_field": "product",
                "issue_open": ["intake", "build", "qa", "merged"],
                "issue_completed": ["promoted"],
                "issue_not_planned": ["closed"],
            },
        }
        compiled = pw.compile_workflow(document, ref=ACME_REF, version=ACME_VERSION)
        self.db.execute(
            "INSERT INTO agent_registry (name, lease_owner, lease_epoch,"
            "                            lease_expires_at)"
            " VALUES ('acme-dev1', 'acme-runner-1', 4, now() + interval '1 hour')")
        row = self.db.query(
            "INSERT INTO work_items (title, state, app, workflow_ref, workflow_stage,"
            "                        workflow_version)"
            " VALUES ('acme : lot compilé', 'intake', 'acme', %s, 'draft', %s)"
            " RETURNING id", (ACME_REF, ACME_VERSION))
        command = pr.Command(
            work_item_id=int(row[0]["id"]), command_id=str(uuid.uuid4()),
            expected_version=0,
            authority=pr.Authority(actor="acme-dev1", source="runner", epoch=4,
                                   runner_id="acme-runner-1"),
            workflow_ref=ACME_REF, workflow_version=ACME_VERSION, action="advance",
            from_stage="draft", to_stage="implementation", guard="planning")
        receipt = pr.apply_command(
            self.db, command, spec=compiled,
            verifiers={"planning": lambda context: pr.Verification(ok=True)})
        self.assertEqual(receipt.state, "build")
        self.assertEqual(receipt.workflow_stage, "implementation")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
