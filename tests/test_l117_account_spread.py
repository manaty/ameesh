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

HOUR = 3600.0
DAY = 86400.0
WEEK = 7 * DAY


def _codex_weekly(home: str, used_percent: float, resets_at: float,
                 short: tuple | None = None) -> None:
    """Un journal Codex avec la fenêtre de 7 jours (`secondary`) et, si
    `short` est donné, celle de 5 h (`primary`) : `(pourcentage, resets_at)`."""
    folder = os.path.join(home, "sessions", "2026", "10", "10")
    os.makedirs(folder, exist_ok=True)
    limits = {"plan_type": "pro",
              "primary": ({"used_percent": short[0], "window_minutes": 300,
                           "resets_at": short[1]} if short else None),
              "secondary": {"used_percent": used_percent, "window_minutes": 10080,
                            "resets_at": resets_at}}
    with open(os.path.join(folder, "rollout-test.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": {"id": "x"}}) + "\n")
        fh.write(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": limits}}) + "\n")


class RepartitionTest(_Base):
    """`accounts.choose` : le compte le plus en retard sur son rythme."""

    def _items(self):
        declared = self._codex_comptes()
        declared.append({"name": "tertiaire", "path": _dossier(
            os.path.join(self.tmp, "declared", "codex-3"), creds="auth.json")})
        self.declared = declared
        return accounts.parse({"codex": declared})["codex"]

    def _book(self):
        return cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)

    def _choose(self, items, now, **kw):
        return accounts.choose(self.db, self.cfg.host, "codex", items, self._book(),
                               now=now, **kw)

    def test_three_accounts_two_without_reading_choice_goes_to_unused(self):
        """Le constat du 2026-10-10 : primaire à 35 % de sa semaine (rythme
        55 %), les deux autres jamais utilisés. Le choix ne reste plus au
        primaire ; entre les deux comptes neufs, l'ordre déclaré."""
        items = self._items()
        now = float(int(time.time()))
        # 45 % de la semaine écoulée : rythme 55 %
        _codex_weekly(items[0].path, 35, now + 0.55 * WEEK, short=(37, now + 2 * HOUR))
        choice = self._choose(items, now)
        self.assertEqual(choice.profile.name, "secondaire")
        self.assertEqual(choice.switched["from"], "primaire")
        self.assertEqual(choice.why,
                         "sans relevé : 0 % utilisé, le plus en retard sur son rythme ; "
                         "avant : tertiaire sans relevé : 0 % utilisé, primaire "
                         "codex-10080min 35 % utilisé (rythme 55 %)")
        # la bascule en base porte la même raison
        self.assertEqual(self.db.query("SELECT reason FROM account_switches")[0]["reason"],
                         choice.why)
        # `ameesh accounts list` : même prochain choix, même raison
        cfg = mock.Mock(host=self.cfg.host, accounts={"codex": self.declared})
        rows = accounts.report(cfg, self.db, self._book(), now=now, record=False)
        self.assertEqual([r["account"] for r in rows if r["next"]], ["secondaire"])
        self.assertIn("↳ prochain choix pour une nouvelle session : sans relevé : 0 % "
                      "utilisé, le plus en retard sur son rythme ; avant : tertiaire",
                      accounts.format_rows(rows))

    def test_spread_over_successive_new_sessions(self):
        """Chaque nouvelle session va au compte le plus en retard ; l'usage
        qu'elle produit le fait reculer dans le classement : les trois comptes
        servent, aucun n'est laissé de côté."""
        items = self._items()
        now = float(int(time.time()))
        usage = {"primaire": 35.0}
        resets = {"primaire": now + 0.55 * WEEK}
        _codex_weekly(items[0].path, 35, resets["primaire"])
        sequence = []
        for _ in range(6):
            choice = self._choose(items, now)            # nouvelle session
            name = choice.profile.name
            sequence.append(name)
            # la session consomme 4 % de la semaine du compte ; un compte neuf
            # ouvre sa fenêtre de 7 jours à ce premier usage
            usage[name] = usage.get(name, 0.0) + 4
            resets.setdefault(name, now + WEEK - HOUR)
            _codex_weekly(accounts.by_name(items, name).path, usage[name], resets[name])
        self.assertEqual(sequence[:2], ["secondaire", "tertiaire"])
        self.assertEqual(set(sequence), {"primaire", "secondaire", "tertiaire"})
        self.assertEqual(sequence, ["secondaire", "tertiaire", "secondaire", "tertiaire",
                                    "primaire", "primaire"])
        self.assertEqual(len(self._bascules()), 5)

    def test_account_at_threshold_is_excluded(self):
        """Le compte le plus en retard sur 7 jours, mais au seuil sur 5 h, n'est
        pas pris : le seuil vaut dans TOUTES les fenêtres."""
        items = self._items()
        now = float(int(time.time()))
        _codex_weekly(items[0].path, 35, now + 0.55 * WEEK)
        # secondaire : 1 % de sa semaine, mais 95 % de sa fenêtre de 5 h
        _codex_weekly(items[1].path, 1, now + 0.55 * WEEK, short=(95, now + HOUR))
        _codex_weekly(items[2].path, 20, now + 0.55 * WEEK)
        choice = self._choose(items, now)
        self.assertEqual(choice.profile.name, "tertiaire")
        self.assertNotIn("secondaire", choice.why)
        # tous au seuil : pause, comme avant
        _codex_weekly(items[0].path, 60, now + 0.55 * WEEK)
        _codex_weekly(items[2].path, 60, now + 0.55 * WEEK)
        choice = self._choose(items, now)
        self.assertIsNone(choice.profile)
        self.assertIn("tous les comptes codex au seuil", choice.reason)

    def test_forcing_wins_over_lag(self):
        items = self._items()
        now = float(int(time.time()))
        _codex_weekly(items[0].path, 35, now + 0.55 * WEEK)
        self.assertTrue(accounts.force(self.db, self.cfg.host, "codex", items, "primaire",
                                       by="human:test"))
        choice = self._choose(items, now)
        self.assertEqual(choice.profile.name, "primaire")
        self.assertTrue(choice.forced)
        cfg = mock.Mock(host=self.cfg.host, accounts={"codex": self.declared})
        rows = accounts.report(cfg, self.db, self._book(), now=now, record=False)
        self.assertEqual([(r["account"], r["why"]) for r in rows if r["next"]],
                         [("primaire", "forcé (ameesh accounts use)")])
        self.assertTrue(accounts.automatic(self.db, self.cfg.host, "codex", by="human:test"))
        self.assertEqual(self._choose(items, now).profile.name, "secondaire")

    def test_session_keeps_its_account(self):
        """Une session ouverte sur le primaire y reste tant qu'il est sous son
        seuil, même si deux comptes neufs sont plus en retard ; une nouvelle
        session, elle, va au plus en retard."""
        items = self._items()
        now = float(int(time.time()))
        _codex_weekly(items[0].path, 35, now + 0.55 * WEEK)
        choice = self._choose(items, now, session_account="primaire")
        self.assertEqual(choice.profile.name, "primaire")
        self.assertTrue(choice.kept)
        self.assertEqual(self._bascules(), [])
        self.assertEqual(self._choose(items, now).profile.name, "secondaire")
        # session sur le primaire passé au seuil : le plus en retard
        _codex_weekly(items[0].path, 60, now + 0.55 * WEEK)
        choice = self._choose(items, now, session_account="primaire")
        self.assertEqual(choice.profile.name, "secondaire")
        self.assertFalse(choice.kept)
        self.assertIn("primaire au seuil", choice.why)

    def test_tie_broken_by_expiry_then_declared_order(self):
        """Même retard : l'échéance la plus proche (règle 0034 du matin), puis
        l'ordre déclaré."""
        items = self._items()
        now = float(int(time.time()))
        # 5 h : primaire 20 % à 40 % écoulés (rythme 50 %) → 0,4 ;
        # secondaire et tertiaire 30 % à 65 % écoulés (rythme 75 %) → 0,4
        _codex_rollout(items[0].path, 20, now + 3 * HOUR)
        _codex_rollout(items[1].path, 30, now + 1.75 * HOUR)
        _codex_rollout(items[2].path, 30, now + 1.75 * HOUR)
        choice = self._choose(items, now)
        self.assertEqual(choice.profile.name, "secondaire")       # échéance, puis ordre
        self.assertIn("avant : tertiaire codex-300min 30 % utilisé (rythme 75 %), "
                      "primaire codex-300min 20 % utilisé (rythme 50 %)", choice.why)


class RangSansBaseTest(unittest.TestCase):
    """`accounts.rank` et `accounts.lag` sur des jauges Claude construites."""

    def _ev(self, name, gauges):
        return accounts.Evaluation(accounts.Profile("claude", name), gauges=gauges)

    def test_production_case_claude(self):
        """`ameesh accounts list` du 2026-10-10 : primaire five_hour 37 %
        (rythme 83 %), seven_day 35 % (rythme 55 %) ; secondaire et tertiaire
        « — ». La fenêtre la plus contraignante du primaire est seven_day."""
        now = 1_800_000_000.0
        primary = [
            cost.Gauge("claude", "five_hour", 0.37, now + (1 - 0.73) * 5 * HOUR, 5 * HOUR),
            cost.Gauge("claude", "seven_day", 0.35, now + 0.55 * WEEK, WEEK),
        ]
        items = [accounts.Profile("claude", n) for n in ("primaire", "secondaire", "tertiaire")]
        evaluations = {"primaire": self._ev("primaire", primary),
                       "secondaire": self._ev("secondaire", []),
                       "tertiaire": self._ev("tertiaire", [])}
        ratio, gauge = accounts.lag(primary, now)
        self.assertEqual(gauge.key, "seven_day")
        self.assertAlmostEqual(ratio, 0.35 / 0.55, places=6)
        self.assertEqual([p.name for p in accounts.rank(items, evaluations, now)],
                         ["secondaire", "tertiaire", "primaire"])
        self.assertEqual(accounts.reason_of(accounts.rank(items, evaluations, now), evaluations, now),
                         "sans relevé : 0 % utilisé, le plus en retard sur son rythme ; "
                         "avant : tertiaire sans relevé : 0 % utilisé, primaire seven_day "
                         "35 % utilisé (rythme 55 %)")

    def test_expired_reading_counts_as_zero(self):
        now = 1_800_000_000.0
        expired = [cost.Gauge("claude", "seven_day", 0.80, now - 60, WEEK)]
        self.assertEqual(accounts.lag(expired, now)[0], 0.0)
        self.assertEqual(accounts.explain(self._ev("x", expired), now),
                         "relevé échu (fenêtre remise à zéro) : 0 % utilisé")


if __name__ == "__main__":
    unittest.main()
