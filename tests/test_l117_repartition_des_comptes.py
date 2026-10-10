# SPDX-License-Identifier: AGPL-3.0-only
"""L117 — répartir les sessions entre tous les comptes au forfait
(amendement du 2026-10-10 à la décision 0034).

Constat : le compte primaire, toujours en fenêtre ouverte et sous son rythme,
gagnait toujours le choix « ce qui expire le plus tôt » ; les deux autres
comptes payés n'avaient jamais servi, leur forfait hebdomadaire était perdu.
Règle amendée : pour une nouvelle session, parmi les comptes sous leur seuil
dans toutes leurs fenêtres, celui qui a le plus de retard sur son rythme
(plus petit `utilisé / autorisé` sur sa fenêtre la plus contraignante) ; un
compte sans relevé compte pour 0 % ; à égalité, l'échéance la plus proche,
puis l'ordre déclaré. Forçage, continuité et pause inchangés.
"""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from ameesh import accounts, cost

from .test_l30_accounts import _Base, _codex_rollout, _dossier

HEURE = 3600.0
JOUR = 86400.0
SEMAINE = 7 * JOUR


def _codex_hebdo(home: str, used_percent: float, resets_at: float,
                 court: tuple | None = None) -> None:
    """Un journal Codex avec la fenêtre de 7 jours (`secondary`) et, si
    `court` est donné, celle de 5 h (`primary`) : `(pourcentage, resets_at)`."""
    dossier = os.path.join(home, "sessions", "2026", "10", "10")
    os.makedirs(dossier, exist_ok=True)
    limits = {"plan_type": "pro",
              "primary": ({"used_percent": court[0], "window_minutes": 300,
                           "resets_at": court[1]} if court else None),
              "secondary": {"used_percent": used_percent, "window_minutes": 10080,
                            "resets_at": resets_at}}
    with open(os.path.join(dossier, "rollout-test.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": {"id": "x"}}) + "\n")
        fh.write(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": limits}}) + "\n")


class RepartitionTest(_Base):
    """`accounts.choose` : le compte le plus en retard sur son rythme."""

    def _items(self):
        comptes = self._codex_comptes()
        comptes.append({"name": "tertiaire", "path": _dossier(
            os.path.join(self.tmp, "comptes", "codex-3"), creds="auth.json")})
        self.comptes = comptes
        return accounts.parse({"codex": comptes})["codex"]

    def _book(self):
        return cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)

    def _choisir(self, items, now, **kw):
        return accounts.choose(self.db, self.cfg.host, "codex", items, self._book(),
                               now=now, **kw)

    def test_trois_comptes_dont_deux_sans_releve_le_choix_va_a_un_compte_neuf(self):
        """Le constat du 2026-10-10 : primaire à 35 % de sa semaine (rythme
        55 %), les deux autres jamais utilisés. Le choix ne reste plus au
        primaire ; entre les deux comptes neufs, l'ordre déclaré."""
        items = self._items()
        now = float(int(time.time()))
        # 45 % de la semaine écoulée : rythme 55 %
        _codex_hebdo(items[0].path, 35, now + 0.55 * SEMAINE, court=(37, now + 2 * HEURE))
        choix = self._choisir(items, now)
        self.assertEqual(choix.profile.name, "secondaire")
        self.assertEqual(choix.switched["from"], "primaire")
        self.assertEqual(choix.why,
                         "sans relevé : 0 % utilisé, le plus en retard sur son rythme ; "
                         "avant : tertiaire sans relevé : 0 % utilisé, primaire "
                         "codex-10080min 35 % utilisé (rythme 55 %)")
        # la bascule en base porte la même raison
        self.assertEqual(self.db.query("SELECT reason FROM account_switches")[0]["reason"],
                         choix.why)
        # `ameesh accounts list` : même prochain choix, même raison
        cfg = mock.Mock(host=self.cfg.host, accounts={"codex": self.comptes})
        lignes = accounts.report(cfg, self.db, self._book(), now=now, record=False)
        self.assertEqual([r["account"] for r in lignes if r["next"]], ["secondaire"])
        self.assertIn("↳ prochain choix pour une nouvelle session : sans relevé : 0 % "
                      "utilisé, le plus en retard sur son rythme ; avant : tertiaire",
                      accounts.format_rows(lignes))

    def test_repartition_sur_des_nouvelles_sessions_successives(self):
        """Chaque nouvelle session va au compte le plus en retard ; l'usage
        qu'elle produit le fait reculer dans le classement : les trois comptes
        servent, aucun n'est laissé de côté."""
        items = self._items()
        now = float(int(time.time()))
        usage = {"primaire": 35.0}
        echeance = {"primaire": now + 0.55 * SEMAINE}
        _codex_hebdo(items[0].path, 35, echeance["primaire"])
        suite = []
        for _ in range(6):
            choix = self._choisir(items, now)            # nouvelle session
            nom = choix.profile.name
            suite.append(nom)
            # la session consomme 4 % de la semaine du compte ; un compte neuf
            # ouvre sa fenêtre de 7 jours à ce premier usage
            usage[nom] = usage.get(nom, 0.0) + 4
            echeance.setdefault(nom, now + SEMAINE - HEURE)
            _codex_hebdo(accounts.by_name(items, nom).path, usage[nom], echeance[nom])
        self.assertEqual(suite[:2], ["secondaire", "tertiaire"])
        self.assertEqual(set(suite), {"primaire", "secondaire", "tertiaire"})
        self.assertEqual(suite, ["secondaire", "tertiaire", "secondaire", "tertiaire",
                                 "primaire", "primaire"])
        self.assertEqual(len(self._bascules()), 5)

    def test_un_compte_au_seuil_est_exclu(self):
        """Le compte le plus en retard sur 7 jours, mais au seuil sur 5 h, n'est
        pas pris : le seuil vaut dans TOUTES les fenêtres."""
        items = self._items()
        now = float(int(time.time()))
        _codex_hebdo(items[0].path, 35, now + 0.55 * SEMAINE)
        # secondaire : 1 % de sa semaine, mais 95 % de sa fenêtre de 5 h
        _codex_hebdo(items[1].path, 1, now + 0.55 * SEMAINE, court=(95, now + HEURE))
        _codex_hebdo(items[2].path, 20, now + 0.55 * SEMAINE)
        choix = self._choisir(items, now)
        self.assertEqual(choix.profile.name, "tertiaire")
        self.assertNotIn("secondaire", choix.why)
        # tous au seuil : pause, comme avant
        _codex_hebdo(items[0].path, 60, now + 0.55 * SEMAINE)
        _codex_hebdo(items[2].path, 60, now + 0.55 * SEMAINE)
        choix = self._choisir(items, now)
        self.assertIsNone(choix.profile)
        self.assertIn("tous les comptes codex au seuil", choix.reason)

    def test_forcage_prime_sur_le_retard(self):
        items = self._items()
        now = float(int(time.time()))
        _codex_hebdo(items[0].path, 35, now + 0.55 * SEMAINE)
        self.assertTrue(accounts.force(self.db, self.cfg.host, "codex", items, "primaire",
                                       by="human:test"))
        choix = self._choisir(items, now)
        self.assertEqual(choix.profile.name, "primaire")
        self.assertTrue(choix.forced)
        cfg = mock.Mock(host=self.cfg.host, accounts={"codex": self.comptes})
        lignes = accounts.report(cfg, self.db, self._book(), now=now, record=False)
        self.assertEqual([(r["account"], r["why"]) for r in lignes if r["next"]],
                         [("primaire", "forcé (ameesh accounts use)")])
        self.assertTrue(accounts.automatic(self.db, self.cfg.host, "codex", by="human:test"))
        self.assertEqual(self._choisir(items, now).profile.name, "secondaire")

    def test_continuite_la_session_garde_son_compte(self):
        """Une session ouverte sur le primaire y reste tant qu'il est sous son
        seuil, même si deux comptes neufs sont plus en retard ; une nouvelle
        session, elle, va au plus en retard."""
        items = self._items()
        now = float(int(time.time()))
        _codex_hebdo(items[0].path, 35, now + 0.55 * SEMAINE)
        choix = self._choisir(items, now, session_account="primaire")
        self.assertEqual(choix.profile.name, "primaire")
        self.assertTrue(choix.kept)
        self.assertEqual(self._bascules(), [])
        self.assertEqual(self._choisir(items, now).profile.name, "secondaire")
        # session sur le primaire passé au seuil : le plus en retard
        _codex_hebdo(items[0].path, 60, now + 0.55 * SEMAINE)
        choix = self._choisir(items, now, session_account="primaire")
        self.assertEqual(choix.profile.name, "secondaire")
        self.assertFalse(choix.kept)
        self.assertIn("primaire au seuil", choix.why)

    def test_egalite_echeance_puis_ordre_declare(self):
        """Même retard : l'échéance la plus proche (règle 0034 du matin), puis
        l'ordre déclaré."""
        items = self._items()
        now = float(int(time.time()))
        # 5 h : primaire 20 % à 40 % écoulés (rythme 50 %) → 0,4 ;
        # secondaire et tertiaire 30 % à 65 % écoulés (rythme 75 %) → 0,4
        _codex_rollout(items[0].path, 20, now + 3 * HEURE)
        _codex_rollout(items[1].path, 30, now + 1.75 * HEURE)
        _codex_rollout(items[2].path, 30, now + 1.75 * HEURE)
        choix = self._choisir(items, now)
        self.assertEqual(choix.profile.name, "secondaire")       # échéance, puis ordre
        self.assertIn("avant : tertiaire codex-300min 30 % utilisé (rythme 75 %), "
                      "primaire codex-300min 20 % utilisé (rythme 50 %)", choix.why)


class RangSansBaseTest(unittest.TestCase):
    """`accounts.rank` et `accounts.lag` sur des jauges Claude construites."""

    def _ev(self, name, gauges):
        return accounts.Evaluation(accounts.Profile("claude", name), gauges=gauges)

    def test_cas_de_production_claude(self):
        """`ameesh accounts list` du 2026-10-10 : primaire five_hour 37 %
        (rythme 83 %), seven_day 35 % (rythme 55 %) ; secondaire et tertiaire
        « — ». La fenêtre la plus contraignante du primaire est seven_day."""
        now = 1_800_000_000.0
        primaire = [
            cost.Gauge("claude", "five_hour", 0.37, now + (1 - 0.73) * 5 * HEURE, 5 * HEURE),
            cost.Gauge("claude", "seven_day", 0.35, now + 0.55 * SEMAINE, SEMAINE),
        ]
        items = [accounts.Profile("claude", n) for n in ("primaire", "secondaire", "tertiaire")]
        evs = {"primaire": self._ev("primaire", primaire),
               "secondaire": self._ev("secondaire", []),
               "tertiaire": self._ev("tertiaire", [])}
        ratio, jauge = accounts.lag(primaire, now)
        self.assertEqual(jauge.key, "seven_day")
        self.assertAlmostEqual(ratio, 0.35 / 0.55, places=6)
        self.assertEqual([p.name for p in accounts.rank(items, evs, now)],
                         ["secondaire", "tertiaire", "primaire"])
        self.assertEqual(accounts.reason_of(accounts.rank(items, evs, now), evs, now),
                         "sans relevé : 0 % utilisé, le plus en retard sur son rythme ; "
                         "avant : tertiaire sans relevé : 0 % utilisé, primaire seven_day "
                         "35 % utilisé (rythme 55 %)")

    def test_releve_echu_vaut_zero(self):
        now = 1_800_000_000.0
        echu = [cost.Gauge("claude", "seven_day", 0.80, now - 60, SEMAINE)]
        self.assertEqual(accounts.lag(echu, now)[0], 0.0)
        self.assertEqual(accounts.explain(self._ev("x", echu), now),
                         "relevé échu (fenêtre remise à zéro) : 0 % utilisé")


if __name__ == "__main__":
    unittest.main()
