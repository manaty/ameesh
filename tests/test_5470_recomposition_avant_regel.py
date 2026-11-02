# SPDX-License-Identifier: AGPL-3.0-only
"""#5470 : une recomposition enregistrée avant son regel garde ses membres.

Le 02/11, « SLO breach, level 1 » (#183979, 07:21:10Z) sur nexlink #5364,
membre du paquet 352 (#5468) depuis 07:12. La recomposition a été enregistrée
à 07:20:23 (540d32c8), le regel n'est venu qu'à 07:21:08. Le porteur n'avait
pas de tête observée : `recorded_packet_attachments` comparait la tête
enregistrée au gel précédent (e9c4fa1e), et la grâce de #5206 n'était tentée
qu'avec une `branch_head`. Pendant 45 s, les six membres étaient détachés.

Sans tête observée, la grâce se mesure depuis le dernier gel du porteur.
"""
from __future__ import annotations

import time
from unittest import mock

from ameesh import planned_waits, storage

from . import test_5206_recomposition_membership as base


class RecompositionAvantRegelTest(base.RecompositionTest):
    def freeze_vehicle(self, sha, seconds_ago):
        """Le porteur est gelé sur `sha` il y a `seconds_ago` secondes."""
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, actor)"
            " VALUES (%s, 'frozen', now() - make_interval(secs => %s), %s, 'orch')",
            (int(self.vehicle["id"]), float(seconds_ago), sha))

    # -- témoin : la forme du paquet 352 ------------------------------------
    def test_une_recomposition_posterieure_au_dernier_gel_garde_le_membre(self):
        self.freeze_vehicle(self.OLD, 500)     # 07:12:55, l'ancienne tête
        self.recorded(45)                      # 07:20:23, pas encore regelée
        self.assertEqual(self.verdict_merge(), [])

    def test_le_membre_porte_l_instant_du_dernier_gel(self):
        self.freeze_vehicle(self.OLD, 500)
        self.freeze_vehicle(self.HEAD, 60)
        rows = storage.of(self.db).work.packet_members(
            package_ids=[int(self.vehicle["id"])])
        self.assertEqual(rows[0]["package_frozen_sha"], self.HEAD)
        self.assertAlmostEqual(float(rows[0]["package_frozen_ts"]),
                               time.time() - 60, delta=60)

    # -- #4111 gardé --------------------------------------------------------
    def test_un_gel_posterieur_sur_une_autre_tete_detache_encore(self):
        self.recorded(300)
        self.freeze_vehicle(self.OLD, 60)      # une adaptation gelée depuis
        self.assertEqual([a["step"] for a in self.verdict_merge()], ["verdict_merge"])

    def test_une_composition_jamais_regelee_cesse_de_compter_apres_la_grace(self):
        self.freeze_vehicle(self.OLD, planned_waits.RECORD_AHEAD_GRACE_S + 900)
        self.recorded(planned_waits.RECORD_AHEAD_GRACE_S + 300)
        self.assertEqual([a["step"] for a in self.verdict_merge()], ["verdict_merge"])

    def test_la_regle_sans_tete_observee_sans_stockage(self):
        carrier = dict(id=90, state="qa", branch="integration/paquet-test")
        unit = dict(id=1, state="validated", branch_head=self.freeze)
        member = dict(package_id=90, work_item_id=1, head_sha=self.HEAD,
                      source_sha=self.freeze, recorded_ts=1100.0,
                      package_frozen_sha=self.OLD, package_frozen_ts=1000.0)
        store = mock.Mock()
        store.progress.lot_milestones.return_value = []
        attach = planned_waits.recorded_packet_attachments

        def members(**changes):
            store.work.packet_members.return_value = [dict(member, **changes)]
            return store

        self.assertEqual(attach(members(), [carrier], [unit], now=1200.0), {90: {1}})
        self.assertEqual(attach(members(), [carrier], [unit],
                                now=1100.0 + planned_waits.RECORD_AHEAD_GRACE_S + 1), {})
        self.assertEqual(attach(members(package_frozen_ts=1150.0), [carrier], [unit],
                                now=1200.0), {})
        # un gel non daté (ligne d'avant #5470) garde la règle de #4111
        self.assertEqual(attach(members(package_frozen_ts=None), [carrier], [unit],
                                now=1200.0), {})
