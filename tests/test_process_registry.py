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
  décision native ouverte, et une réponse avant reprise) : entrée et sortie
  passent par le vérificateur réservé `hold` (tranche C : `verify_hold`), une
  avance depuis une retenue est refusée (`held`), terminal jamais rouvert
  (même par adoption), boucle de réparation bornée ;
* transfert de révision explicite, jamais automatique, sans régression ni
  changement de cœur natif, sans injection de vérificateurs par l'appelant
  (l'API publique ne lit que le registre installé) ;
* annulation sans demi-événement (trigger qui casse l'UPDATE), historique
  existant intact, lots sans workflow inchangés ; `work.move`/`work.close`/
  `work.close_merged` refusent un lot pinné (le registre est la seule
  écriture) ;
* contraintes de la migration additive 0452 : reçu complet exigé `IS TRUE`
  (toute clé absente ou `null` imbriqué est refusé), unicité partielle
  (lot, clé) ;
* lecture de la garde par l'interface de stockage (`work.get`), y compris
  sur une base d'avant 0452 (colonnes du registre absentes) : aucune sonde
  de colonne, aucun SELECT brut (B5).

Les organisations et agents sont fictifs (`acme`). Aucune activation :
`INSTALLED_VERIFIERS` est vide par défaut, les tests injectent leurs
vérificateurs par le seam documenté.
"""
from __future__ import annotations

import contextlib
import glob
import json
import os
import threading
import unittest
import uuid
from dataclasses import dataclass

from ameesh import process_registry as pr
from ameesh import storage
from ameesh import work
from ameesh.db import DbError

from .support import REPO, PgTestCase

try:  # tranche A (#1918), pas encore fusionnée au moment d'écrire ces tests
    from ameesh import process_workflow as pw
except ImportError:  # pragma: no cover - dépend de l'état de main
    pw = None

try:  # tranche C (#1924), pas encore fusionnée au moment d'écrire ces tests
    from ameesh import process_verifiers as pv
except ImportError:  # pragma: no cover - dépend de l'état de main
    pv = None

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

        def hold(context):
            self.calls.append(pr.HOLD_VERIFIER)
            return pr.Verification(ok=True, reason="retenue relue")

        self.verifiers[pr.HOLD_VERIFIER] = hold
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
        # `_apply` : injection réservée au banc ; l'API publique ne prend que
        # les vérificateurs installés (`install_verifier`).
        return pr._apply(self.db, command, spec=spec or self.spec,
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
            again = pr._apply(fresh, cmd, spec=self.spec,
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
        # `work.move` refuse désormais un lot pinné (revue B3) : le banc écrit
        # la ligne de journal legacy directement — c'est le journal, pas le
        # compteur de reçus, qui fait foi pour la version attendue.
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor)"
            " VALUES (%s, 'blocked', 'legacy', %s)", (lot, ACME_ACTOR))
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

    def test_acteur_dead_refuse(self):
        lot = self.pinned()
        self.db.execute("UPDATE agent_registry SET status = 'dead' WHERE name = %s",
                        (ACME_ACTOR,))
        with self.refusal("authority-revoked"):
            self.apply(self.command(lot))
        self.assertEqual(self.events(lot), [])

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
                outcome.append(pr._apply(
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
        self.assertTrue(evidence["verification"]["required"])
        self.assertEqual(evidence["verification"]["verifier"], pr.HOLD_VERIFIER)
        with self.refusal("hold-redundant"):
            self.apply(self.command(lot, action="hold", hold="blocked",
                                    frm="", to="", guard="", version=held.version))
        released = self.apply(self.command(lot, action="release", frm="", to="",
                                           guard="", version=held.version))
        self.assertEqual(released.state, "build")
        self.assertEqual(released.workflow_stage, "implementation")
        reprise = self.events(lot)[-1]["evidence"]
        self.assertTrue(reprise["verification"]["required"])
        self.assertEqual(reprise["verification"]["verifier"], pr.HOLD_VERIFIER)

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

    def test_retenue_exige_le_verificateur_installe(self):
        lot = self.pinned(stage="implementation", state="build")
        with self.refusal("verifier-uninstalled"):
            self.apply(self.command(lot, action="hold", hold="blocked",
                                    frm="", to="", guard=""), verifiers={})
        self.assertEqual(self.events(lot), [])
        self.assertEqual(self.row(lot)["state"], "build")

    def test_reprise_exige_le_verificateur_installe(self):
        lot = self.pinned(stage="implementation", state="build")
        held = self.apply(self.command(lot, action="hold", hold="blocked",
                                       frm="", to="", guard=""))
        with self.refusal("verifier-uninstalled"):
            self.apply(self.command(lot, action="release", frm="", to="", guard="",
                                    version=held.version), verifiers={})
        self.assertEqual(self.row(lot)["state"], "blocked")
        self.assertEqual(len(self.events(lot)), 1)

    def test_reprise_waiting_human_attend_la_reponse_et_le_verificateur(self):
        lot = self.pinned(stage="implementation", state="build")
        request = self.request_open_for(lot)
        held = self.apply(self.command(lot, action="hold", hold="waiting_human",
                                       frm="", to="", guard=""))
        with self.refusal("decision-pending"):
            self.apply(self.command(lot, action="release", frm="", to="", guard="",
                                    version=held.version))
        self.db.execute(
            "UPDATE agent_mailbox SET delivered_at = now(), status = 'answered'"
            " WHERE id = %s", (request,))
        with self.refusal("verifier-uninstalled"):
            self.apply(self.command(lot, action="release", frm="", to="", guard="",
                                    version=held.version), verifiers={})
        self.assertEqual(self.row(lot)["state"], "waiting_human")
        released = self.apply(self.command(lot, action="release", frm="", to="",
                                           guard="", version=held.version))
        self.assertEqual(released.state, "build")

    def test_avance_depuis_une_retenue_refusee(self):
        for hold in pr.HOLDS:
            with self.subTest(retenue=hold):
                lot = self.pinned(stage="implementation", state="build")
                if hold == "waiting_human":
                    self.request_open_for(lot)
                held = self.apply(self.command(lot, action="hold", hold=hold,
                                               frm="", to="", guard=""))
                with self.refusal("held"):
                    self.apply(self.command(lot, frm="implementation", to="review",
                                            guard="review_requested",
                                            version=held.version))
                self.assertEqual(self.row(lot)["state"], hold)
                self.assertEqual(len(self.events(lot)), 1)

    def test_verificateur_de_retenue_qui_refuse(self):
        lot = self.pinned(stage="implementation", state="build")

        def refuses(context):
            return pr.Verification(ok=False, reason="retenue non prouvée")

        verifiers = dict(self.verifiers)
        verifiers[pr.HOLD_VERIFIER] = refuses
        with self.refusal("verifier-refused"):
            self.apply(self.command(lot, action="hold", hold="blocked",
                                    frm="", to="", guard=""), verifiers=verifiers)
        self.assertEqual(self.events(lot), [])

    def test_booleen_de_retenue_refuse(self):
        lot = self.pinned(stage="implementation", state="build")
        verifiers = dict(self.verifiers)
        verifiers[pr.HOLD_VERIFIER] = lambda context: True
        with self.refusal("verifier-invalid"):
            self.apply(self.command(lot, action="hold", hold="blocked",
                                    frm="", to="", guard=""), verifiers=verifiers)
        self.assertEqual(self.events(lot), [])

    def test_contexte_du_verificateur_de_retenue(self):
        lot = self.pinned(stage="implementation", state="build")
        seen = []

        def capture(context):
            seen.append((context.command.action, context.command.hold,
                         str(context.work_item["state"]), context.from_stage,
                         context.to_stage))
            return pr.Verification(ok=True, reason="vu")

        verifiers = dict(self.verifiers)
        verifiers[pr.HOLD_VERIFIER] = capture
        held = self.apply(self.command(lot, action="hold", hold="blocked",
                                       frm="", to="", guard=""), verifiers=verifiers)
        self.apply(self.command(lot, action="release", frm="", to="", guard="",
                                version=held.version), verifiers=verifiers)
        self.assertEqual(
            seen,
            [("hold", "blocked", "build", "implementation", "implementation"),
             ("release", "", "blocked", "implementation", "implementation")])

    def test_reponse_et_retenue_ne_s_interbloquent_pas(self):
        """N6 : `agent_mailbox` avant `work_items`, comme `decisions.answer`.

        La retenue verrouille la demande ouverte AVANT le lot ; la réponse
        ferme la demande (`decisions.close`) : les deux se sérialisent, aucune
        ne se bloque sur l'autre, et l'issue reste cohérente (retenue acceptée
        si elle a vu la demande, sinon `hold-no-decision`)."""
        lot = self.pinned(stage="implementation", state="build")
        request = self.request_open_for(lot)

        def hold(db):
            return pr._apply(db, self.command(lot, action="hold",
                                              hold="waiting_human",
                                              frm="", to="", guard=""),
                             spec=self.spec, verifiers=self.verifiers)

        def answer(db):
            return storage.of(db).decisions.close(
                request, status="answered",
                record={"state": "answered",
                        "answer": {"by": "human:acme", "channel": "terminal",
                                   "text": "ok", "option": None}})

        results, errors = self._concurrent([hold, answer])
        self.assertEqual(len(results) + len(errors), 2)
        self.assertEqual([e for e in errors
                          if not isinstance(e, pr.RegistryRefusal)], [])
        for exc in errors:
            self.assertEqual(exc.code, "hold-no-decision")
        closed = self.db.query(
            "SELECT delivered_at FROM agent_mailbox WHERE id = %s",
            (request,))[0]["delivered_at"]
        self.assertIsNotNone(closed)
        state = self.row(lot)["state"]
        if errors:                       # la réponse est passée d'abord
            self.assertEqual(state, "build")
            self.assertEqual(self.events(lot), [])
        else:                            # la retenue a vu la demande ouverte
            self.assertEqual(state, "waiting_human")

    # -- transfert de révision --------------------------------------------

    def transfer(self, lot_id: int, *, ref: str | None = None, version: int = ACME_VERSION,
                 spec=None, expected: int | None = None, command_id: str | None = None,
                 verifiers=None):
        return pr._transfer(
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

    def test_adoption_refuse_un_lot_terminal(self):
        for state in sorted(pr.TERMINAL_CORES):
            with self.subTest(etat=state):
                lot = self.lot()                   # aucun workflow
                self.db.execute(
                    "UPDATE work_items SET state = %s, close_reason = %s WHERE id = %s",
                    (state, "abandoned" if state == "closed" else None, lot))
                with self.refusal("terminal-reopened"):
                    self.transfer(lot)
                self.assertEqual(self.events(lot), [])
                row = self.row(lot)
                self.assertEqual(row["state"], state)
                self.assertIsNone(row["workflow_ref"])
                self.assertEqual(row["workflow_version"], 0)

    def test_adoption_refuse_un_coeur_different(self):
        # L'adoption ne recule pas l'état natif : l'étape initiale doit
        # projeter le cœur courant (une retenue, elle, est conservée).
        for state in ("build", "qa", "merged"):
            with self.subTest(etat=state):
                lot = self.lot(state=state)        # aucun workflow
                with self.refusal("stage-mismatch"):
                    self.transfer(lot)
                self.assertEqual(self.events(lot), [])
                self.assertEqual(self.row(lot)["state"], state)
        held = self.lot(state="blocked")           # retenue conservée
        receipt = self.transfer(held)
        self.assertEqual(receipt.state, "blocked")
        self.assertEqual(receipt.workflow_stage, "draft")

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

    def test_transfert_refuse_un_coeur_incoherent(self):
        lot = self.pinned(stage="review", state="qa")
        shifted = _Spec(stages=(
            _Stage("draft", "intake"),
            _Stage("implementation", "build"),
            _Stage("review", "build"),          # même statut, autre cœur
            _Stage("staging", "merged"),
            _Stage("done", "promoted"),
            _Stage("stopped", "closed"),
        ))
        with self.refusal("stage-mismatch"):
            self.transfer(lot, spec=shifted)
        self.assertEqual(self.row(lot)["workflow_ref"], self.spec.workflow_ref)
        self.assertEqual(self.row(lot)["workflow_version"], ACME_VERSION)

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
            return pr._apply(db, cmd, spec=self.spec, verifiers=self.verifiers)

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
            return pr._apply(db, first, spec=self.spec, verifiers=self.verifiers)

        def run_b(db):
            return pr._apply(db, second, spec=self.spec, verifiers=self.verifiers)

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

    def test_migration_recu_complet_exige_chaque_champ(self):
        """`IS TRUE` : toute omission — clé absente ou `null` imbriqué — est refusée."""
        lot = self.pinned()
        full = {"schema": pr.RECEIPT_SCHEMA, "intent": {"sha256": "0" * 64},
                "command": {"action": "advance"},
                "authority": {}, "verification": {}}

        def insert(evidence):
            self.db.execute(
                "INSERT INTO work_item_events (work_item_id, state, command_id,"
                "  workflow_stage, workflow_version, evidence)"
                " VALUES (%s, 'build', %s::uuid, 'implementation', %s, %s::jsonb)",
                (lot, str(uuid.uuid4()), ACME_VERSION, json.dumps(evidence)))

        insert(full)                      # contrôle positif : la forme pleine passe
        self.assertEqual(len(self.events(lot)), 1)
        for field in ("schema", "intent", "command", "authority", "verification"):
            with self.subTest(omission=field):
                with self.assertRaises(DbError):
                    insert({k: v for k, v in full.items() if k != field})
        refusals = (
            {},                                      # reçu vide, tous les champs NULL
            {"schema": None, "intent": full["intent"], "command": full["command"],
             "authority": {}, "verification": {}},
            dict(full, intent=None),
            dict(full, intent={}),                   # sha256 absent (imbriqué)
            dict(full, intent={"sha256": None}),
            dict(full, command={}),                  # action absente (imbriqué)
            dict(full, command={"action": None}),
            dict(full, authority=None),
            dict(full, verification=None),
        )
        for evidence in refusals:
            with self.subTest(evidence=json.dumps(evidence, sort_keys=True)):
                with self.assertRaises(DbError):
                    insert(evidence)
        self.assertEqual(len(self.events(lot)), 1)

    def test_migration_reappliquee_reinstalle_la_contrainte_durcie(self):
        """N11 (relecture Claude) : une base de banc qui avait appliqué la 0452
        d'avant le durcissement gardait l'ancien CHECK laxiste ; la
        réapplication du fichier repart de la définition courante
        (`drop constraint` + `add`)."""
        lot = self.pinned()
        self.db.execute("ALTER TABLE work_item_events"
                        " DROP CONSTRAINT work_item_events_receipt_complete")
        self.db.execute(
            "ALTER TABLE work_item_events ADD CONSTRAINT"
            " work_item_events_receipt_complete CHECK (command_id is null"
            "  or evidence->>'schema' = 'process-receipt/1')")
        [path] = glob.glob(os.path.join(REPO, "src", "ameesh", "migrations",
                                        "0452_*.sql"))
        with open(path, encoding="utf-8") as fh:
            self.db.script(fh.read())
        with self.assertRaises(DbError):
            self.db.execute(
                "INSERT INTO work_item_events (work_item_id, state, command_id,"
                "  workflow_stage, workflow_version, evidence)"
                " VALUES (%s, 'build', %s::uuid, 'implementation', %s, '{}'::jsonb)",
                (lot, str(uuid.uuid4()), ACME_VERSION))
        self.assertEqual(self.events(lot), [])

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

    def test_api_publique_sans_injection_de_verificateurs(self):
        """Le résultat d'une garde ne vient jamais de l'appelant (seam publié)."""
        lot = self.pinned()
        with self.assertRaises(TypeError):
            pr.apply_command(self.db, self.command(lot), spec=self.spec,
                             verifiers={"planning": _ok})
        with self.assertRaises(TypeError):
            pr.transfer_revision(
                self.db, work_item_id=lot, command_id=str(uuid.uuid4()),
                expected_version=0, workflow_ref=self.spec.workflow_ref,
                workflow_version=ACME_VERSION, authority=self.authority(),
                spec=self.spec, verifiers=self.verifiers)

    def test_move_close_refusent_un_lot_pinne(self):
        lot = self.pinned(stage="implementation", state="build")
        with self.assertRaises(work.WorkError):
            work.move(self.db, lot, "qa")
        with self.assertRaises(work.WorkError):
            work.close(self.db, lot, abandoned=True)
        refused = work.close_merged(self.db, lot)
        self.assertEqual(refused["result"], "refused")
        self.assertIn("process_registry", refused["detail"])
        self.assertEqual(self.row(lot)["state"], "build")
        self.assertEqual(self.events(lot), [])

    def test_les_ecritures_legacy_filtrent_le_lot_pinne_dans_le_sql(self):
        """N8 (relecture Claude) : la garde applicative lit `workflow_ref` hors
        du verrou du lot ; entre cette lecture et l'UPDATE, une adoption
        concurrente (qui garde l'état) laissait passer le move legacy. Le
        filtre est dans le SQL : la couche de stockage ne touche plus un lot
        pinné, même appelée avec l'instantané d'avant l'adoption."""
        lot = self.pinned(stage="review", state="qa")
        store = storage.of(self.db).work
        self.assertIsNone(store.move(lot, "build", current="qa", loops=1,
                                     note="n", actor="test"))
        self.assertIsNone(store.close(
            lot, current="qa", reason="abandoned", superseded_by=None,
            sha="", actor="test", note="n"))
        self.assertIsNone(store.close_merged(
            lot, current="qa", sha="a" * 40, actor="test", note="n", pr_ref=None))
        self.assertEqual(self.row(lot)["state"], "qa")
        self.assertEqual(self.events(lot), [])

    def _restore_workflow_columns(self) -> None:
        """Réapplique les colonnes et gardes de la 0452 (migration additive)."""
        self.db.execute("ALTER TABLE work_items"
                        " ADD COLUMN IF NOT EXISTS workflow_ref text")
        self.db.execute("ALTER TABLE work_items"
                        " ADD COLUMN IF NOT EXISTS workflow_stage text")
        self.db.execute("ALTER TABLE work_items"
                        " ADD COLUMN IF NOT EXISTS workflow_version bigint"
                        " NOT NULL DEFAULT 0")
        self.db.execute(
            "ALTER TABLE work_items DROP CONSTRAINT IF EXISTS"
            " work_items_workflow_version_nonnegative")
        self.db.execute(
            "ALTER TABLE work_items ADD CONSTRAINT"
            " work_items_workflow_version_nonnegative"
            " CHECK (workflow_version >= 0)")
        self.db.execute(
            "ALTER TABLE work_items DROP CONSTRAINT IF EXISTS"
            " work_items_workflow_stage_needs_ref")
        self.db.execute(
            "ALTER TABLE work_items ADD CONSTRAINT"
            " work_items_workflow_stage_needs_ref"
            " CHECK (workflow_stage is null or workflow_ref is not null)")

    def test_avant_la_0452_les_commandes_historiques_ne_cassent_pas(self):
        """B5 (paquet 0650), support avant 0452 : sans les colonnes du
        registre, `work.get` rend `workflow_ref` vide (aucun lot n'y est
        pinné) et les commandes historiques restent celles d'avant le
        registre — la lecture ne dépend d'aucune sonde de colonne."""
        bouge = self.lot(state="build")
        ferme = self.lot(state="build")
        self.db.execute("ALTER TABLE work_items DROP COLUMN workflow_ref CASCADE")
        self.db.execute("ALTER TABLE work_items DROP COLUMN workflow_stage")
        self.db.execute("ALTER TABLE work_items DROP COLUMN workflow_version")
        self.addCleanup(self._restore_workflow_columns)

        self.assertEqual(work.get(self.db, bouge)["workflow_ref"], "")
        self.assertEqual(work.get(self.db, ferme)["workflow_ref"], "")
        row = work.move(self.db, bouge, "qa", actor=ACME_ACTOR)
        self.assertEqual(row["state"], "qa")
        closed = work.close(self.db, ferme, abandoned=True, actor=ACME_ACTOR)
        self.assertEqual(closed["state"], "closed")
        self.assertEqual(self.row(bouge)["state"], "qa")
        self.assertEqual(self.row(ferme)["state"], "closed")

    def test_read_sources_ne_declare_que_le_reel(self):
        lot = self.pinned(stage="implementation", state="build")
        receipt = self.apply(self.command(
            lot, frm="implementation", to="review", guard="review_requested",
            facts={"ticket": "ACME-7"}))
        self.request_open_for(lot)
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor, note)"
            " VALUES (%s, 'delivered', %s, %s, 'livré hors cible')",
            (lot, "a" * 40, ACME_ACTOR))
        context = pr.VerificationContext(
            db=self.db, work_item=self.row(lot),
            command=self.command(lot, frm="implementation", to="review",
                                 guard="review_requested", version=receipt.version),
            spec=self.spec, from_stage="implementation", to_stage="review")
        sources = pr.read_sources(context)
        self.assertEqual(set(sources), set(pr.SOURCE_KEYS))
        self.assertNotIn("observations", sources)      # source non installée
        got = sources["receipts"][0]
        self.assertTrue(got["accepted"])
        self.assertEqual(got["guard"], "review_requested")
        self.assertEqual(got["facts"], {"ticket": "ACME-7"})
        self.assertEqual(got["sha256"], receipt.intent_sha256)
        self.assertEqual(got["command_id"], receipt.command_id)
        self.assertEqual([d["sha"] for d in sources["deliveries"]], ["a" * 40])
        self.assertTrue(sources["deliveries"][0]["accepted"])
        self.assertEqual(len(sources["decisions"]), 1)
        # `requested` est posé par le trigger de 0013 : le lecteur rend le
        # registre réel, pas seulement le jalon ajouté par le banc.
        self.assertEqual([m["kind"] for m in sources["milestones"]],
                         ["requested", "delivered"])
        with self.refusal("verifier-invalid"):
            pr.read_sources(object())

    def test_read_sources_nomme_les_verificateurs_reserves(self):
        lot = self.pinned(stage="implementation", state="build")
        held = self.apply(self.command(lot, action="hold", hold="blocked",
                                       frm="", to="", guard=""))
        context = pr.VerificationContext(
            db=self.db, work_item=self.row(lot),
            command=self.command(lot, version=held.version), spec=self.spec,
            from_stage="implementation", to_stage="implementation")
        sources = pr.read_sources(context)
        self.assertEqual([r["guard"] for r in sources["receipts"]],
                         [pr.HOLD_VERIFIER])

    # -- seam réel avec la tranche C (#1924) -------------------------------

    @unittest.skipUnless(pv is not None, "tranche C (#1924) absente de main")
    def test_seam_reel_avec_la_tranche_C(self):
        """Les 15 appelables de C installés, l'API publique les fait parler.

        Le vrai C lit le snapshot par `read_sources(context)` : avance gardée
        `planning`, retenue `blocked` et reprise par le nom réservé `hold`,
        `waiting_human` sur la collection `decisions`, et avance refusée depuis
        une retenue."""
        saved = pr.installed_verifiers()

        def restore():
            pr.INSTALLED_VERIFIERS.clear()
            pr.INSTALLED_VERIFIERS.update(saved)

        self.addCleanup(restore)
        names = pv.install_verifiers(pr)
        self.assertEqual(len(names), len(pv.GUARD_NAMES) + 1)
        self.assertIn(pr.HOLD_VERIFIER, names)

        lot = self.pinned(stage="draft", state="intake")
        first = pr.apply_command(self.db, self.command(lot), spec=self.spec)
        self.assertEqual((first.state, first.workflow_stage),
                         ("build", "implementation"))

        held = pr.apply_command(self.db, self.command(
            lot, action="hold", hold="blocked", frm="", to="", guard="",
            version=first.version), spec=self.spec)
        self.assertEqual(held.state, "blocked")
        with self.refusal("held"):          # une avance ne lève pas la retenue
            pr.apply_command(self.db, self.command(
                lot, frm="implementation", to="review", guard="review_requested",
                version=held.version), spec=self.spec)
        released = pr.apply_command(self.db, self.command(
            lot, action="release", frm="", to="", guard="",
            version=held.version), spec=self.spec)
        self.assertEqual(released.state, "build")

        request = self.request_open_for(lot)
        waiting = pr.apply_command(self.db, self.command(
            lot, action="hold", hold="waiting_human", frm="", to="", guard="",
            version=released.version), spec=self.spec)
        self.assertEqual(waiting.state, "waiting_human")
        with self.refusal("decision-pending"):   # la réponse d'abord
            pr.apply_command(self.db, self.command(
                lot, action="release", frm="", to="", guard="",
                version=waiting.version), spec=self.spec)
        self.db.execute("UPDATE agent_mailbox SET delivered_at = now(),"
                        " status = 'answered' WHERE id = %s", (request,))
        after = pr.apply_command(self.db, self.command(
            lot, action="release", frm="", to="", guard="",
            version=waiting.version), spec=self.spec)
        self.assertEqual(after.state, "build")

    @unittest.skipUnless(pv is not None, "tranche C (#1924) absente de main")
    def test_seam_reel_acteur_du_verdict_et_id_de_livraison(self):
        """#1994 : les deux coutures B -> C passent par le `read_sources` réel.

        (1) l'acteur du verdict est projeté : `work_completed` accepte une revue
        réelle (avant le correctif : `completion-review-missing`) ; (2) l'id de
        livraison est projeté en chaîne, comme la référence client : une
        livraison de code référencée par son id est trouvée (avant :
        `completion-evidence-missing`)."""
        saved = pr.installed_verifiers()

        def restore():
            pr.INSTALLED_VERIFIERS.clear()
            pr.INSTALLED_VERIFIERS.update(saved)

        self.addCleanup(restore)
        pv.install_verifiers(pr)

        sha = "c" * 40
        # Un lot de code : la livraison est référencée par son id (chaîne) et
        # la revue par son acteur — les deux coutures du même `read_sources`.
        code = self.pinned(stage="staging", state="merged")
        delivered = int(self.db.query(
            "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor)"
            " VALUES (%s, 'delivered', %s, %s) RETURNING id",
            (code, sha, ACME_ACTOR))[0]["id"])
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor,"
            "                                verdict)"
            " VALUES (%s, 'verdict', %s, %s, 'ok')", (code, sha, "relecteur-1"))
        context = pr.VerificationContext(
            db=self.db, work_item=self.row(code),
            command=self.command(code, frm="staging", to="done",
                                 guard="work_completed"),
            spec=self.spec, from_stage="staging", to_stage="done")
        sources = pr.read_sources(context)
        verdicts = [m for m in sources["milestones"] if m["kind"] == "verdict"]
        self.assertEqual([m["actor"] for m in verdicts], ["relecteur-1"])
        self.assertEqual([d["id"] for d in sources["deliveries"]], [str(delivered)])
        completed = pr.apply_command(self.db, self.command(
            code, frm="staging", to="done", guard="work_completed",
            facts={"delivery": str(delivered)}), spec=self.spec)
        self.assertEqual(completed.state, "promoted")


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
        receipt = pr._apply(
            self.db, command, spec=compiled,
            verifiers={"planning": lambda context: pr.Verification(ok=True)})
        self.assertEqual(receipt.state, "build")
        self.assertEqual(receipt.workflow_stage, "implementation")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
