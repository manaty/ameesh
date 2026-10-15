# SPDX-License-Identifier: AGPL-3.0-only
"""#795 : un événement préempte le tour en cours (0018 §2).

Le chemin de préemption existe (courrier `urgent` → `interrupt_allowed` →
`request_preempt`, tour coupé), mais il était inerte : `AMEESH_INTERRUPT_SENDERS`
vide partout, et le verdict de CI partait ordinaire (`urgent=False`), donc
« fin de CI » n'ouvrait jamais de tour prioritaire.

Règles vérifiées ici :

- l'owner (`human:*`) interrompt toujours ;
- un agent n'interrompt que s'il est déclaré dans `interrupt_senders` ;
- le système (`ameesh`) n'interrompt que sur un événement **vérifiable en
  base** : l'exécution de CI est relevée (`ci_runs`), le lot du courrier est
  bien assigné à cet agent, et ce courrier est celui que la CI a posé pour
  cette exécution (première alerte ou verdict). Un `--from ameesh` imité avec
  un payload `ci` ne préempte donc rien (même garde que L317c) ;
- la première alerte d'échec et le verdict (vert ou rouge) partent urgents
  (témoin côté `ci.Watcher` dans test_l161c_failures_realtime).
"""
from __future__ import annotations

import types

from ameesh import mail, registry, runner, storage, work

from .support import PgTestCase

DEPOT = "manaty/ameesh"


class _Base(PgTestCase):
    def setUp(self):
        super().setUp()
        for table in ("ci_test_failures", "ci_runs"):
            self.db.execute("DELETE FROM %s" % table)
        registry.upsert(self.db, "dev-a", harness="claude", host=self.cfg.host)
        registry.upsert(self.db, "dev-b", harness="claude", host=self.cfg.host)
        self.lot = work.add(self.db, title="L1 : un lot", branch="lot/1-un",
                            assignee="dev-a")
        self.other = work.add(self.db, title="L2 : un autre lot", branch="lot/2-deux",
                              assignee="dev-b")
        self._worker = None

    # -- un AgentWorker réduit à ce que `interrupt_allowed` utilise ---------
    def worker(self, name="dev-a", senders=()):
        return types.SimpleNamespace(
            name=name, agent={"name": name}, db=self.db,
            cfg=types.SimpleNamespace(interrupt_senders=tuple(senders)),
            runner=types.SimpleNamespace(is_auditor=lambda n, a=None: False),
            _system_event_interrupt=lambda sender, row: runner.AgentWorker
            ._system_event_interrupt(self._worker, sender, row),
            _deep_analysis_interrupt=lambda sender, row: runner.AgentWorker
            ._deep_analysis_interrupt(self._worker, sender, row))

    def allowed(self, row, name="dev-a", senders=()):
        self._worker = self.worker(name, senders)
        return runner.AgentWorker.interrupt_allowed(self._worker, row)

    # -- courrier ------------------------------------------------------------
    def urgent(self, to, sender, payload, lot=None):
        """Dépose un urgent et le relit comme le fait l'exécuteur."""
        mail.send(self.db, sender, to, "événement", kind="event",
                  payload=dict(payload, urgent=True),
                  work_item_id=str(lot) if lot else None)
        rows = [m for m in mail.unread_urgent(self.db, to)]
        return rows[-1]

    def ci_row(self, run_id, lot, branch="lot/1-un"):
        return storage.of(self.db).ci.open_run(
            DEPOT, run_id, head_sha="a" * 40, branch=branch, event="pull_request",
            base="main", work_item_id=lot, url=None)

    def ci_payload(self, run_id, kind="verdict", status="success"):
        return {"ci": {"repo": DEPOT, "run_id": run_id, "kind": kind, "status": status,
                       "head_sha": "a" * 40, "branch": "lot/1-un"}}


class AutorisationTest(_Base):
    def test_l_owner_interrompt_toujours(self):
        row = self.urgent("dev-a", "human:alice", {})
        self.assertTrue(self.allowed(row))
        # même sans être dans la config des expéditeurs habilités
        self.assertTrue(self.allowed(row, senders=()))

    def test_un_agent_n_interrompt_que_s_il_est_declare(self):
        row = self.urgent("dev-a", "dev-b", {})
        self.assertFalse(self.allowed(row))
        self.assertTrue(self.allowed(row, senders=("dev-b",)))

    def test_un_expediteur_vide_n_interrompt_pas(self):
        self.assertFalse(self.allowed({"sender": "", "payload": {"urgent": True}}))
        self.assertFalse(self.allowed({"payload": {"urgent": True}}))

    def test_un_urgent_ordinaire_n_interrompt_pas(self):
        row = self.urgent("dev-a", "dev-b", {"note": "coucou"})
        self.assertFalse(self.allowed(row))

    def test_un_agent_ne_prend_pas_la_place_de_l_owner(self):
        # « human: » n'est pas une capacité qu'un agent s'attribue par son nom
        # (la validation du nom à l'envoi tient côté CLI ; ici, l'agent signé
        # « agent:human:x » ne commence pas par `human:`)
        row = self.urgent("dev-a", "agent:human:alice", {})
        self.assertFalse(self.allowed(row))


class EvenementSystemeTest(_Base):
    def test_la_fin_de_ci_verifiee_en_base_preempte(self):
        self.ci_row(301, self.lot["id"])
        row = self.urgent("dev-a", work.SYSTEM_SENDER, self.ci_payload(301),
                          lot=self.lot["id"])
        storage.of(self.db).ci.set_message(DEPOT, 301, "verdict", row["id"])
        self.assertTrue(self.allowed(row))

    def test_la_premiere_alerte_d_echec_verifiee_preempte(self):
        self.ci_row(302, self.lot["id"])
        row = self.urgent("dev-a", work.SYSTEM_SENDER,
                          self.ci_payload(302, kind="failure", status=None),
                          lot=self.lot["id"])
        storage.of(self.db).ci.set_message(DEPOT, 302, "urgent", row["id"])
        self.assertTrue(self.allowed(row))

    def test_une_imitation_sans_execution_en_base_ne_preempte_pas(self):
        row = self.urgent("dev-a", work.SYSTEM_SENDER, self.ci_payload(303),
                          lot=self.lot["id"])
        self.assertFalse(self.allowed(row))

    def test_un_courrier_non_pose_par_cette_execution_ne_preempte_pas(self):
        self.ci_row(304, self.lot["id"])
        row = self.urgent("dev-a", work.SYSTEM_SENDER, self.ci_payload(304),
                          lot=self.lot["id"])
        # l'exécution existe, mais aucun id de message n'y est enregistré
        self.assertFalse(self.allowed(row))
        # un id d'un autre message ne suffit pas non plus
        storage.of(self.db).ci.set_message(DEPOT, 304, "verdict", int(row["id"]) + 1000)
        self.assertFalse(self.allowed(row))

    def test_un_lot_d_un_autre_agent_ne_preempte_pas(self):
        self.ci_row(305, self.other["id"], branch="lot/2-deux")
        row = self.urgent("dev-a", work.SYSTEM_SENDER, self.ci_payload(305),
                          lot=self.other["id"])
        storage.of(self.db).ci.set_message(DEPOT, 305, "verdict", row["id"])
        # dev-a n'est pas l'assigné du lot : le courrier ne perce pas son tour
        self.assertFalse(self.allowed(row))
        # le courrier du lot de dev-b, relu par dev-b, perce bien
        self.assertTrue(self.allowed(row, name="dev-b"))

    def test_un_payload_ci_sans_objet_ne_preempte_pas(self):
        self.ci_row(306, self.lot["id"])
        row = self.urgent("dev-a", work.SYSTEM_SENDER,
                          {"ci": "manaty/ameesh"}, lot=self.lot["id"])
        self.assertFalse(self.allowed(row))
