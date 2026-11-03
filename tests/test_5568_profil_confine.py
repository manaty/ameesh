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
        # la topologie du banc : « poste » est intermittent (policy.intermittent)
        postes = mock.patch.object(exploitation, "_intermittent_hosts",
                                   lambda cfg, hosts: set(hosts) & {"poste"})
        postes.start()
        self.addCleanup(postes.stop)

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

    def test_hote_intermittent_lu_dans_la_topologie(self):
        """Le critère vient des fiches Host (`policy.intermittent`), pas d'un nom."""
        mock.patch.stopall()
        limites = {"poste": {"intermittent": True}, "vm-1": {"intermittent": False}}
        with mock.patch("ameesh.canon.load_configured", return_value=["c"]), \
                mock.patch("ameesh.resources.host_limits",
                           side_effect=lambda canons, h: limites[h]):
            self.assertEqual(exploitation._intermittent_hosts(self.cfg, {"poste", "vm-1"}),
                             {"poste"})
        with mock.patch("ameesh.canon.load_configured", side_effect=OSError("canon")):
            self.assertEqual(exploitation._intermittent_hosts(self.cfg, {"poste"}), set())

    def test_harnais_que_personne_ne_fait_tourner(self):
        self._declare("poste", "codex", "secondaire", ago_s=7 * 24 * HOUR)
        self.assertEqual(self._alertes(), [])


if __name__ == "__main__":
    unittest.main()
