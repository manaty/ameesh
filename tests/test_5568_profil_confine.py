# SPDX-License-Identifier: AGPL-3.0-only
"""#5568 : un profil de harnais connu d'un hôte (le poste) mais absent des
hôtes qui font tourner ce harnais (les VM) a une alerte stable,
`account_profile_confined`, qui ne disparaît ni au reset ni à la reprise du
primaire — seulement quand le profil devient utilisable sur un porteur, ou
par un retrait daté (`accounts_withdrawn`). Lecture en base seulement."""
from __future__ import annotations

import dataclasses
import time
import unittest
from unittest import mock

from ameesh import exploitation, notify, storage

from .support import PgTestCase

HOUR = 3600.0
VMS = ("vm-1", "vm-2", "vm-3")


class ProfilConfineTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        for table in ("account_identities", "credential_health"):
            self.db.execute("DELETE FROM %s" % table)
        self.now = float(int(time.time()))
        # la topologie du banc : les VM portent l'étiquette d'hôte d'agents,
        # pas le poste (un hôte complet, non intermittent)
        topo = mock.patch.object(exploitation, "_agent_hosts",
                                 lambda cfg: set(VMS))
        topo.start()
        self.addCleanup(topo.stop)

    def _declare(self, host, harness, account, ago_s=0.0, identity="x"):
        self.db.execute(
            "insert into account_identities (host, harness, account, identity, last_seen)"
            " values (%s, %s, %s, %s, to_timestamp(%s))"
            " on conflict (host, harness, account, identity)"
            " do update set last_seen = excluded.last_seen",
            (host, harness, account, identity, self.now - ago_s))

    def _sante(self, host, account, state="healthy"):
        storage.of(self.db).credentials.report(host, "codex", account, state,
                                               source="sentinel")

    def _mesh(self):
        """Le cas du 03/11 : le poste éteint depuis une semaine déclarait
        primaire + secondaire ; les trois VM ne déclarent que primaire."""
        self._declare("poste", "codex", "primaire", ago_s=7 * 24 * HOUR)
        self._declare("poste", "codex", "secondaire", ago_s=7 * 24 * HOUR + 1)
        for vm in VMS:
            self._declare(vm, "codex", "primaire", ago_s=60)
            self._sante(vm, "primaire")

    def _alertes(self, cfg=None):
        return [a for a in exploitation._confined_profiles(cfg or self.cfg, self.db,
                                                           self.now)]

    def test_profil_du_seul_poste_alerte_avec_le_geste(self):
        self._mesh()
        alertes = self._alertes()
        self.assertEqual([(a["harness"], a["account"]) for a in alertes],
                         [("codex", "secondaire")])
        alerte = alertes[0]
        self.assertEqual(alerte["declared_on"], ["poste"])
        self.assertEqual(alerte["carriers"], list(VMS))
        self.assertEqual(alerte["reason"], "provision")
        self.assertIsNone(alerte["since"])
        self.assertIn("never copy auth.json", alerte["detail"])
        self.assertIn("account_profile_confined", notify.DEFAULT_TYPES)
        # câblée dans les alertes du mesh
        self.assertIn(("codex", "secondaire"),
                      [(a.get("harness"), a.get("account"))
                       for a in exploitation.alerts(self.cfg, self.db, now=self.now)
                       if a["type"] == "account_profile_confined"])

    def test_le_reset_du_primaire_ne_change_pas_l_alerte(self):
        self._mesh()
        avant = [exploitation.alert_key(a) for a in self._alertes()]
        # le primaire repart (nouvelles publications, sonde saine) une heure après
        self.now += HOUR
        for vm in VMS:
            self._declare(vm, "codex", "primaire", ago_s=0)
            self._sante(vm, "primaire")
        self.assertEqual([exploitation.alert_key(a) for a in self._alertes()], avant)

    def test_declare_sur_une_vm_sans_sonde_puis_sain(self):
        self._mesh()
        self._declare("vm-2", "codex", "secondaire", ago_s=30)
        self.assertEqual([a["reason"] for a in self._alertes()], ["probe"])
        self._sante("vm-2", "secondaire", "failing")
        self.assertEqual([a["reason"] for a in self._alertes()], ["reconnect"])
        self._sante("vm-2", "secondaire", "healthy")
        self.assertEqual(self._alertes(), [])

    def test_retrait_date_seulement(self):
        self._mesh()
        sans_date = dataclasses.replace(
            self.cfg, accounts_withdrawn={"codex": {"secondaire": "plus utile"}})
        self.assertEqual(len(self._alertes(sans_date)), 1)
        impossible = dataclasses.replace(
            self.cfg, accounts_withdrawn={"codex": {"secondaire": "2026-99-99 rendu"}})
        self.assertEqual(len(self._alertes(impossible)), 1)
        date = dataclasses.replace(
            self.cfg, accounts_withdrawn={"codex": {"secondaire": "2026-11-03 rendu"}})
        self.assertEqual(self._alertes(date), [])

    def test_profil_retire_de_la_configuration_du_poste(self):
        """Le poste revenu ne déclare plus le profil : il n'en est plus connu."""
        self._mesh()
        self._declare("poste", "codex", "primaire", ago_s=0)
        self.assertEqual(self._alertes(), [])

    def test_poste_allume_et_sain_ne_couvre_pas_les_vm(self):
        """B1 (ameesh-cx1) : le poste allumé déclare primaire + secondaire, sain ;
        les VM n'ont toujours que primaire — l'alerte reste, même clé."""
        self._mesh()
        avant = [exploitation.alert_key(a) for a in self._alertes()]
        self._declare("poste", "codex", "primaire", ago_s=0)
        self._declare("poste", "codex", "secondaire", ago_s=0)
        self._sante("poste", "primaire")
        self._sante("poste", "secondaire")
        alertes = self._alertes()
        self.assertEqual([exploitation.alert_key(a) for a in alertes], avant)
        self.assertEqual(alertes[0]["carriers"], list(VMS))

    def test_poste_sain_et_vm_en_echec_demande_la_reconnexion(self):
        """Le cas de mty-amh-impl-03 : poste sain sur secondaire, AG2 déclare
        secondaire en échec — reconnexion sur la VM, pas de silence."""
        self._mesh()
        self._declare("poste", "codex", "secondaire", ago_s=0)
        self._sante("poste", "secondaire")
        self._declare("vm-2", "codex", "secondaire", ago_s=0)
        self._sante("vm-2", "secondaire", "failing")
        self.assertEqual([a["reason"] for a in self._alertes()], ["reconnect"])

    def test_topologie_lue_dans_les_etiquettes_des_fiches(self):
        """Le critère vient des fiches Host (`tags`), pas d'un nom ni de
        `policy.intermittent`."""
        mock.patch.stopall()
        hote = lambda titre, tags: mock.Mock(title=titre, tags=tags)
        canon = mock.Mock(hosts=[hote("poste", None), hote("vm-1", ["agents"]),
                                 hote("vm-2", ["agents", "gpu"])])
        with mock.patch("ameesh.canon.load_configured", return_value=[canon]):
            self.assertEqual(exploitation._agent_hosts(self.cfg), {"vm-1", "vm-2"})
        with mock.patch("ameesh.canon.load_configured",
                        return_value=[mock.Mock(hosts=[hote("vm-1", None)])]):
            self.assertIsNone(exploitation._agent_hosts(self.cfg))
        with mock.patch("ameesh.canon.load_configured", side_effect=OSError("canon")):
            self.assertIsNone(exploitation._agent_hosts(self.cfg))

    def test_sans_etiquette_le_poste_ne_blanchit_pas_les_vm(self):
        """B1 de ameesh-cx3 / B de mty-amh-impl-03 : topologie réelle sans
        étiquette, poste revenu et sain sur secondaire, VM sans profil ou en
        échec — l'alerte reste et dit la topologie manquante."""
        with mock.patch.object(exploitation, "_agent_hosts", lambda cfg: None):
            self._mesh()
            self._declare("poste", "codex", "primaire", ago_s=0)
            self._declare("poste", "codex", "secondaire", ago_s=0)
            self._sante("poste", "primaire")
            self._sante("poste", "secondaire")
            alertes = self._alertes()
            self.assertEqual([(a["reason"], a["topology"]) for a in alertes],
                             [("provision", "untagged")])
            self.assertIn("vm-1, vm-2, vm-3", alertes[0]["detail"])
            self._declare("vm-2", "codex", "secondaire", ago_s=0)
            self._sante("vm-2", "secondaire", "failing")
            self.assertEqual([a["reason"] for a in self._alertes()], ["reconnect"])
            # utilisable partout : plus d'alerte
            for vm in VMS:
                self._declare(vm, "codex", "secondaire", ago_s=0)
                self._sante(vm, "secondaire")
            self.assertEqual(self._alertes(), [])

    def test_topologie_sans_hote_d_agents_le_dit(self):
        self._mesh()
        with mock.patch.object(exploitation, "_agent_hosts", lambda cfg: None):
            alertes = self._alertes()
        self.assertEqual([a["topology"] for a in alertes], ["untagged"])
        self.assertIn("tags no agent host", alertes[0]["detail"])
        self.assertIn("no host vouches for the others", alertes[0]["detail"])
        self.assertEqual(self._alertes()[0]["topology"], "tagged")

    def test_harnais_que_personne_ne_fait_tourner(self):
        self._declare("poste", "codex", "secondaire", ago_s=7 * 24 * HOUR)
        self.assertEqual(self._alertes(), [])


if __name__ == "__main__":
    unittest.main()
