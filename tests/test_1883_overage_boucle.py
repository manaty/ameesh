# SPDX-License-Identifier: AGPL-3.0-only
"""#1883 — un compte en dépassement n'est jamais candidat, et le choix ne
bascule plus en boucle.

Incident du 16/10/2026 (pc-smichea, comptes Codex) : le même nom de compte
« secondaire » était déclaré dans plusieurs configurations de l'hôte, vers des
dossiers différents. Un runner y lisait ses vraies jauges (fenêtre 5 h à
100 %, « in overage, billed beyond the plan »), un autre y lisait celles d'un
compte à 34 % ; le choix partagé (`account_active`) basculait donc
primaire ↔ secondaire toutes les quelques secondes, et chaque bascule pouvait
lancer un tour facturé au-delà du forfait.

La règle « un compte en dépassement n'est jamais choisi » (`evaluate` →
`in_overage` → `hard`) n'était pas fautive : c'est la LECTURE du compte qui
n'était pas la sienne par moments, dans un autre processus. Témoins ici :

* deux comptes déclarés qui résolvent vers le MÊME dossier (chemin réel, liens
  compris) sont refusés à la lecture de la configuration — la dérive devient
  une erreur bruyante, jamais un choix payant ;
* l'overage vu par UN processus est publié pour l'hôte (`account_holds`) quand
  une bascule quitte le compte : les autres runners l'écartent même si leur
  lecture locale le dit sain, tant que la retenue court — la boucle s'arrête,
  et la rotation normale reprend dès que la retenue expire ;
* la capacité d'overage inconnue garde le plafond de base (#1176, inchangé) :
  seule une capacité explicite applique `overage_cap` ;
* une alerte `account_overage` part dès qu'une lecture dit le compte facturé
  au-delà de son plan, avec un `since` stable tant que la fenêtre court.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from ameesh import accounts, cost, exploitation, storage

from .test_l30_accounts import _Base, _dossier

HOUR = 3600.0
DAY = 86400.0
WEEK = 7 * DAY


def _journal(home: str, *, short=None, week=None, credits=None) -> None:
    """Un journal de session Codex enregistré : `short`/`week` sont des
    `(used_percent, resets_at)`. `credits` = l'objet `credits` du harnais
    (`has_credits` vrai + une fenêtre à 100 % ⇒ « en dépassement »)."""
    folder = os.path.join(home, "sessions", "2026", "10", "16")
    os.makedirs(folder, exist_ok=True)
    limits = {"plan_type": "pro", "primary": None, "secondary": None}
    if short is not None:
        limits["primary"] = {"used_percent": short[0], "window_minutes": 300,
                             "resets_at": short[1]}
    if week is not None:
        limits["secondary"] = {"used_percent": week[0], "window_minutes": 10080,
                               "resets_at": week[1]}
    if credits is not None:
        limits["credits"] = credits
    with open(os.path.join(folder, "rollout-test.jsonl"), "w",
              encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": {"id": "x"}}) + "\n")
        fh.write(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": limits}}) + "\n")


#: capacité de dépassement : le compte peut être facturé au-delà du forfait
CREDITS = {"has_credits": True, "unlimited": False}


class DossiersPartagesTest(unittest.TestCase):
    """`parse` refuse deux comptes qui lisent le même dossier."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-1883-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_deux_comptes_meme_dossier_refuses(self):
        dossier = _dossier(os.path.join(self.tmp, "codex-1"), creds="auth.json")
        with self.assertRaises(accounts.AccountError) as ctx:
            accounts.parse({"codex": [{"name": "primaire", "path": dossier},
                                      {"name": "secondaire", "path": dossier}]})
        message = str(ctx.exception)
        self.assertIn("primaire", message)
        self.assertIn("secondaire", message)

    def test_lien_symbolique_vers_le_meme_dossier_refuse(self):
        """La comparaison est faite sur le chemin **résolu** : un lien vers le
        même dossier est le même compte."""
        dossier = _dossier(os.path.join(self.tmp, "codex-1"), creds="auth.json")
        lien = os.path.join(self.tmp, "codex-2")
        os.symlink(dossier, lien)
        with self.assertRaises(accounts.AccountError):
            accounts.parse({"codex": [{"name": "primaire", "path": dossier},
                                      {"name": "secondaire", "path": lien}]})

    def test_compte_sans_dossier_et_defaut_explicite_refuses(self):
        """Un compte déclaré sans `path` lit le dossier par défaut du harnais :
        le redéclarer explicitement est le même dossier."""
        defaut = os.path.join(os.path.expanduser("~"), ".codex")
        with self.assertRaises(accounts.AccountError):
            accounts.parse({"codex": [{"name": "primaire"},
                                      {"name": "secondaire", "path": defaut}]})

    def test_dossiers_distincts_acceptes(self):
        a = _dossier(os.path.join(self.tmp, "codex-1"), creds="auth.json")
        b = _dossier(os.path.join(self.tmp, "codex-2"), creds="auth.json")
        profils = accounts.parse({"codex": [{"name": "primaire", "path": a},
                                            {"name": "secondaire", "path": b}]})["codex"]
        self.assertEqual([p.name for p in profils], ["primaire", "secondaire"])


class CapaciteTest(unittest.TestCase):
    """#1176, inchangé : une capacité inconnue ne prouve pas la gratuité du
    dépassement — mais elle ne l'applique pas non plus. Le plafond de base
    (80 % par défaut, `PACE_CEILING`) tient ; seule une capacité **explicite**
    ajoute le plafond de dépassement de 80 %."""

    NOW = 1_800_000_000.0

    def _profil(self):
        return accounts.Profile("codex", "secondaire", path="/x/codex-2",
                                pace_multiplier=10.0, pace_ceiling=0.95)

    def test_capacite_inconnue_garde_le_plafond_de_base(self):
        jauge = cost.Gauge("codex", "10080min", 0.85, self.NOW + 0.9 * WEEK, WEEK)
        inconnue = cost.Gauges([jauge], cost.Overage())
        caps = accounts.limits(self._profil(), inconnue, self.NOW)
        self.assertEqual(caps["10080min"][1], "pace cap")
        self.assertAlmostEqual(caps["10080min"][0], 0.95)
        # capacité explicite : le plafond de dépassement s'applique
        capable = cost.Gauges([jauge], cost.Overage(capable=True))
        caps = accounts.limits(self._profil(), capable, self.NOW)
        self.assertEqual(caps["10080min"][1], "overage cap")
        self.assertAlmostEqual(caps["10080min"][0], accounts.OVERAGE_CAP)

    def test_capacite_et_incapacite(self):
        profil = accounts.Profile("codex", "secondaire")
        self.assertTrue(accounts.overage_capable(profil, cost.Overage(capable=True)))
        self.assertFalse(accounts.overage_capable(profil, cost.Overage(capable=False)))
        self.assertFalse(accounts.overage_capable(profil, cost.Overage()))
        self.assertFalse(accounts.overage_capable(profil, None))

    def test_le_reglage_declare_prime(self):
        self.assertFalse(accounts.overage_capable(
            accounts.Profile("codex", "a", overage="none"), cost.Overage(capable=True)))
        self.assertTrue(accounts.overage_capable(
            accounts.Profile("codex", "a", overage="allowed"), None))


class OveragePartageTest(_Base):
    """L'overage vu par un runner écarte le compte pour les autres, sans
    retarder la rotation normale (le classement L117 reste intact)."""

    def setUp(self) -> None:
        super().setUp()
        reserve = mock.patch.object(accounts, "RESERVE", 0.0)
        reserve.start()
        self.addCleanup(reserve.stop)

    def _items(self, secondaire: str):
        a = _dossier(os.path.join(self.tmp, "comptes", "codex-1"), creds="auth.json")
        self.declared = [
            {"name": "primaire", "path": a, "pace_multiplier": 10},
            {"name": "secondaire", "path": secondaire,
             "pace_multiplier": 10, "priority": 20},
        ]
        return accounts.parse({"codex": self.declared})["codex"]

    def _book(self):
        return cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)

    def _depassement(self, items, now):
        """Le runner A : sa lecture du secondaire est en dépassement, sa
        priorité 20 le mettrait premier ; le choix part au primaire."""
        storage.of(self.db).accounts.init_active(self.cfg.host, "codex", "secondaire")
        _journal(items[1].path, short=(100, now + 2 * HOUR),
                 week=(47, now + 5 * DAY), credits=CREDITS)
        _journal(items[0].path, week=(34, now + 6 * DAY))
        choix = accounts.choose(self.db, self.cfg.host, "codex", items,
                                self._book(), now=now, agent="runner-a")
        self.assertEqual(choix.profile.name, "primaire")
        self.assertEqual(choix.switched["to"], "primaire")

    def test_un_runner_ecarte_l_overage_pour_les_autres(self):
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        items = self._items(b)
        now = float(int(time.time()))
        self._depassement(items, now)

        # l'overage est publié pour l'hôte (retenue datée)
        retenues = storage.of(self.db).accounts.holds(self.cfg.host, "codex")
        self.assertIn("secondaire", retenues)
        self.assertGreater(float(retenues["secondaire"]["until_ts"]), now)

        # runner B : une AUTRE configuration du même nom lit un compte sain
        # (5 %, priorité 20) — la retenue l'emporte sur sa lecture locale
        c = _dossier(os.path.join(self.tmp, "comptes", "codex-3"), creds="auth.json")
        _journal(c, week=(5, now + 6 * DAY))
        autres = self._items(c)
        bascules = len(self._bascules())
        choix = accounts.choose(self.db, self.cfg.host, "codex", autres,
                                self._book(), now=now + 1.0, agent="runner-b")
        self.assertEqual(choix.profile.name, "primaire", choix.why)
        self.assertEqual(len(self._bascules()), bascules)

        # la retenue expirée, la rotation normale reprend (aucune lecture ne
        # voit plus de dépassement)
        self.db.execute("UPDATE account_holds"
                        " SET until_ts = extract(epoch from now()) - 1")
        choix = accounts.choose(self.db, self.cfg.host, "codex", autres,
                                self._book(), now=time.time(), agent="runner-b")
        self.assertEqual(choix.profile.name, "secondaire", choix.why)
        self.assertEqual(choix.switched["to"], "secondaire")

    def test_un_forcage_humain_ne_fait_pas_tourner_un_compte_en_depassement(self):
        """`ameesh accounts use` ne fait pas tourner un compte en dépassement :
        la garde absolue prime, le forçage est levé pour un compte sain (et
        sans autre compte, il tient l'agent en pause, jamais un tour payé)."""
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        items = self._items(b)
        now = float(int(time.time()))
        self._depassement(items, now)
        self.assertTrue(accounts.force(self.db, self.cfg.host, "codex", items,
                                       "secondaire", by="test"))
        choix = accounts.choose(self.db, self.cfg.host, "codex", items,
                                self._book(), now=now + 1.0)
        self.assertEqual(choix.profile.name, "primaire", choix.why)
        self.assertFalse(choix.forced)

    def test_la_pause_dit_la_retenue(self):
        """Tous les comptes retenus : la pause le dit, elle ne prétend pas
        qu'ils sont tous au seuil."""
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        items = self._items(b)
        now = float(int(time.time()))
        self._depassement(items, now)
        # le primaire passe lui aussi en dépassement (lecture du runner B) :
        # plus aucun compte plaçable, et les deux sont retenus
        _journal(items[0].path, short=(100, now + 2 * HOUR),
                 week=(47, now + 5 * DAY), credits=CREDITS)
        choix = accounts.choose(self.db, self.cfg.host, "codex", items,
                                self._book(), now=now + 1.0, agent="runner-b")
        self.assertIsNone(choix.profile)
        self.assertIn("held for overage", choix.reason)


class AlerteOverageTest(_Base):
    """L'alerte `account_overage` (attendu 4 du lot)."""

    def _cfg(self):
        a = _dossier(os.path.join(self.tmp, "comptes", "codex-1"), creds="auth.json")
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        self.declared = [
            {"name": "primaire", "path": a, "pace_multiplier": 10},
            {"name": "secondaire", "path": b, "pace_multiplier": 10, "priority": 20},
        ]
        return dataclasses.replace(self.cfg, accounts={"codex": self.declared})

    def _alertes(self, cfg, now):
        return [a for a in exploitation.alerts(cfg, self.db, now=now)
                if a["type"] == "account_overage"]

    def test_alerte_des_la_lecture_en_depassement(self):
        cfg = self._cfg()
        now = float(int(time.time()))
        _journal(self.declared[1]["path"], short=(100, now + 2 * HOUR),
                 week=(47, now + 5 * DAY), credits=CREDITS)
        _journal(self.declared[0]["path"], week=(34, now + 6 * DAY))
        alertes = self._alertes(cfg, now)
        self.assertEqual(len(alertes), 1)
        alerte = alertes[0]
        self.assertEqual(alerte["account"], "secondaire")
        self.assertEqual(alerte["harness"], "codex")
        self.assertEqual(alerte["host"], cfg.host)
        # `since` = le début de la fenêtre qui a débordé : stable tant qu'elle
        # court, l'alerte n'est pas renvoyée à chaque passage
        self.assertAlmostEqual(alerte["since"], now + 2 * HOUR - 300 * 60, places=3)
        self.assertAlmostEqual(alerte["value"], 1.0)
        self.assertEqual([a["since"] for a in self._alertes(cfg, now + 5.0)],
                         [alerte["since"]])

    def test_aucune_alerte_sans_depassement(self):
        cfg = self._cfg()
        now = float(int(time.time()))
        _journal(self.declared[1]["path"], short=(10, now + 2 * HOUR),
                 week=(47, now + 5 * DAY), credits=CREDITS)
        _journal(self.declared[0]["path"], week=(34, now + 6 * DAY))
        self.assertEqual(self._alertes(cfg, now), [])


if __name__ == "__main__":
    unittest.main()
