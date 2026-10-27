# SPDX-License-Identifier: AGPL-3.0-only
"""#3755 — forçage de compte : garde à la commande, échéance, alerte vue de
n'importe quel hôte.

Incident du 26/10/2026 : à ~11:45Z, `ameesh accounts use codex secondaire` a
été forcé sur pc-smichea parce que le primaire était en limite et codex3
bloqué en 429. Le forçage retire le choix automatique ; à 13:40Z le secondaire
affichait 100 % et « in overage (billed beyond the plan) », et il a fallu le
rendre à la main à l'automatique à 13:41Z. La passe d'alertes, chez le
détenteur du bail (agents-1/2), n'a pas vu ce dépassement : `account_overage`
n'était calculée que sur l'hôte qui voit le compte.

Témoins :
* un forçage sur un compte à 100 % (ou en overage) est refusé sans
  `--force "raison"` ; avec la raison, le forçage est posé et journalisé ;
* un forçage a une échéance et revient seul à l'automatique, journalisé
  `auto` ;
* les drapeaux d'overage publiés avec les jauges par l'hôte du compte lèvent
  `account_overage` dans `ameesh alerts` lu depuis un autre hôte.
"""
from __future__ import annotations

import dataclasses
import time
import unittest
from unittest import mock

from ameesh import accounts, cost, exploitation, mesh_cli, storage

from .test_1883_overage_boucle import CREDITS, DAY, HOUR, _journal
from .test_l30_accounts import _Base


class ForcageGardeTest(_Base):
    """`accounts.force` refuse un compte au-delà de son rythme ou en overage."""

    def _items(self):
        return accounts.parse({"codex": self._codex_comptes()})["codex"]

    def _book(self):
        return cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)

    def _active(self):
        return storage.of(self.db).accounts.active(self.cfg.host, "codex")

    def test_forcage_refuse_un_compte_en_depassement(self):
        items = self._items()
        now = float(int(time.time()))
        _journal(items[0].path, week=(34, now + 6 * DAY))
        _journal(items[1].path, short=(100, now + 2 * HOUR),
                 week=(47, now + 5 * DAY), credits=CREDITS)

        with self.assertRaises(accounts.AccountError) as ctx:
            accounts.force(self.db, self.cfg.host, "codex", items, "secondaire",
                           by="human:test", book=self._book(), now=now)
        message = str(ctx.exception)
        self.assertIn("in overage", message)
        self.assertIn("--force", message)
        self.assertFalse((self._active() or {}).get("forced"))

        # même avec --force, une raison est exigée (journalisée)
        with self.assertRaises(accounts.AccountError) as ctx:
            accounts.force(self.db, self.cfg.host, "codex", items, "secondaire",
                           by="human:test", book=self._book(), now=now,
                           allow=True, reason="  ")
        self.assertIn("reason", str(ctx.exception))

        until = now + 900.0
        self.assertTrue(accounts.force(
            self.db, self.cfg.host, "codex", items, "secondaire", by="human:test",
            book=self._book(), now=now, until=until, allow=True,
            reason="sonde de jauge du secondaire"))
        active = self._active()
        self.assertTrue(active["forced"])
        self.assertAlmostEqual(float(active["forced_until"]), until, delta=1.0)
        switch = self.db.query(
            "select kind, reason from account_switches order by id desc limit 1")[0]
        self.assertEqual(switch["kind"], "manuel")
        self.assertIn("sonde de jauge du secondaire", switch["reason"])

    def test_forcage_refuse_un_compte_au_dela_de_son_rythme(self):
        items = self._items()
        now = float(int(time.time()))
        _journal(items[0].path, week=(34, now + 6 * DAY))
        # 100 % de la fenêtre courte, sans crédits : au-delà du rythme, pas
        # encore en overage (le harnais ne dit pas qu'il peut facturer).
        _journal(items[1].path, short=(100, now + 2 * HOUR),
                 week=(47, now + 5 * DAY))
        with self.assertRaises(accounts.AccountError) as ctx:
            accounts.force(self.db, self.cfg.host, "codex", items, "secondaire",
                           by="human:test", book=self._book(), now=now)
        self.assertIn("100%", str(ctx.exception))
        self.assertIn("--force", str(ctx.exception))
        self.assertFalse((self._active() or {}).get("forced"))

    def test_un_forcage_refuse_une_retenue_d_overage_publiee_par_un_autre_runner(self):
        """#1883/#3755 : une retenue valide d'un autre runner est une preuve
        d'overage que la lecture locale peut ne pas voir ; `--force` la lève
        explicitement."""
        items = self._items()
        now = float(int(time.time()))
        _journal(items[0].path, week=(34, now + 6 * DAY))
        _journal(items[1].path, week=(5, now + 6 * DAY))
        store = storage.of(self.db).accounts
        store.hold(self.cfg.host, "codex", "secondaire", now + 600.0,
                   "codex plan: in overage (billed beyond the plan)")
        with self.assertRaises(accounts.AccountError) as ctx:
            accounts.force(self.db, self.cfg.host, "codex", items, "secondaire",
                           by="human:test", book=self._book(), now=now)
        self.assertIn("held", str(ctx.exception))
        self.assertIn("--force", str(ctx.exception))
        self.assertIn("secondaire", store.holds(self.cfg.host, "codex"))
        self.assertTrue(accounts.force(
            self.db, self.cfg.host, "codex", items, "secondaire", by="human:test",
            book=self._book(), now=now, until=now + 900.0, allow=True,
            reason="sonde de jauge"))
        self.assertEqual(store.holds(self.cfg.host, "codex"), {})

    def test_une_retenue_publiee_pendant_l_evaluation_refuse_le_forcage(self):
        """B/P2 des deux relecteurs : la retenue est relue sous le verrou de
        placement, dans la transaction de la bascule. Publiée entre le début
        de `force` et la bascule, elle refuse sans rien écrire."""
        items = self._items()
        now = float(int(time.time()))
        _journal(items[0].path, week=(34, now + 6 * DAY))
        _journal(items[1].path, week=(5, now + 6 * DAY))
        store = storage.of(self.db).accounts
        real_evaluate = accounts.evaluate

        def evaluate_then_publish(*args, **kwargs):
            result = real_evaluate(*args, **kwargs)
            # un autre runner publie la retenue pendant l'évaluation
            store.hold(self.cfg.host, "codex", "secondaire", now + 600.0,
                       "codex plan: in overage (billed beyond the plan)")
            return result

        with mock.patch.object(accounts, "evaluate",
                               side_effect=evaluate_then_publish):
            with self.assertRaises(accounts.AccountError) as ctx:
                accounts.force(self.db, self.cfg.host, "codex", items,
                               "secondaire", by="human:test",
                               book=self._book(), now=now)
        self.assertIn("held", str(ctx.exception))
        # refus sans écriture : ni forçage, ni levée de la retenue
        self.assertIsNone(store.active(self.cfg.host, "codex"))
        self.assertIn("secondaire", store.holds(self.cfg.host, "codex"))
        self.assertEqual(self._switches(), [])

        with mock.patch.object(accounts, "evaluate",
                               side_effect=evaluate_then_publish):
            self.assertTrue(accounts.force(
                self.db, self.cfg.host, "codex", items, "secondaire",
                by="human:test", book=self._book(), now=now,
                until=now + 900.0, allow=True, reason="sonde de jauge"))
        # le geste explicite lève la retenue
        self.assertEqual(store.holds(self.cfg.host, "codex"), {})
        self.assertTrue(store.active(self.cfg.host, "codex")["forced"])

    def _switches(self):
        return self.db.query(
            "select kind, from_account, to_account from account_switches"
            " order by id")


class ForcageEcheanceTest(_Base):
    """Un forçage revient seul à l'automatique au terme de `--for`."""

    def _items(self):
        return accounts.parse({"codex": self._codex_comptes()})["codex"]

    def _book(self):
        return cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)

    def _active(self):
        return storage.of(self.db).accounts.active(self.cfg.host, "codex")

    def _journal_comptes(self, items, now):
        _journal(items[0].path, week=(34, now + 6 * DAY))
        _journal(items[1].path, week=(5, now + 6 * DAY))

    def test_le_forcage_expire_et_journalise_auto(self):
        items = self._items()
        now = float(int(time.time()))
        self._journal_comptes(items, now)
        until = now + 60.0
        self.assertTrue(accounts.force(self.db, self.cfg.host, "codex", items,
                                       "secondaire", by="human:test", now=now,
                                       until=until))
        # avant l'échéance, le forçage tient
        choice = accounts.choose(self.db, self.cfg.host, "codex", items,
                                 self._book(), now=now + 30.0)
        self.assertTrue(choice.forced)
        self.assertEqual((self._active() or {}).get("account"), "secondaire")

        # au terme, le choix suivant le lit comme automatique ET le lève
        choice = accounts.choose(self.db, self.cfg.host, "codex", items,
                                 self._book(), now=now + 61.0)
        self.assertFalse(choice.forced)
        active = self._active()
        self.assertFalse(active["forced"])
        self.assertIsNone(active["forced_until"])
        switch = self.db.query(
            "select kind, reason from account_switches order by id desc limit 1")[0]
        self.assertEqual(switch["kind"], "auto")
        self.assertIn("forcing expired", switch["reason"])

    def test_l_expiration_ne_leve_pas_un_forcage_renouvele_du_meme_compte(self):
        """B1 de la relecture : un renouvellement du MÊME compte, lu avant la
        levée d'expiration, n'est pas effacé par cette levée."""
        items = self._items()
        now = float(int(time.time()))
        self._journal_comptes(items, now)
        accounts.force(self.db, self.cfg.host, "codex", items, "secondaire",
                       by="human:test", now=now, until=now + 60.0)
        store = storage.of(self.db).accounts
        stale = store.active(self.cfg.host, "codex")
        self.assertTrue(accounts.force_expired(stale, now + 120.0))
        renewed = now + 5000.0
        self.assertTrue(store.switch(
            self.cfg.host, "codex", expected=None, to="secondaire", kind="manuel",
            reason="renouvelé par un humain", agent=None, forced=True,
            forced_until=renewed))
        row = accounts._expire_force(store, self.cfg.host, "codex", stale,
                                     now + 120.0, readonly=False)
        self.assertTrue(row["forced"])
        self.assertAlmostEqual(float(row["forced_until"]), renewed, delta=1.0)
        # la levée refusée n'a rien journalisé
        kinds = [r["kind"] for r in self.db.query(
            "select kind from account_switches order by id")]
        self.assertEqual(kinds, ["manuel", "manuel"])

    def test_affichage_lit_le_forcage_echu_comme_automatique_sans_ecrire(self):
        comptes = self._codex_comptes()
        cfg = dataclasses.replace(self.cfg, accounts={"codex": comptes})
        items = accounts.parse(cfg.accounts)["codex"]
        now = float(int(time.time()))
        self._journal_comptes(items, now)
        accounts.force(self.db, cfg.host, "codex", items, "secondaire",
                       by="human:test", now=now, until=now + 60.0)
        rows = accounts.report(cfg, self.db, self._book(), now=now + 120.0,
                               record=False)
        row = [r for r in rows if r["account"] == "secondaire"][0]
        self.assertFalse(row["forced"])
        self.assertIsNone(row["forced_until"])
        # la lecture d'affichage n'a rien levé en base : le choix suivant le fera
        self.assertTrue((storage.of(self.db).accounts.active(
            cfg.host, "codex") or {}).get("forced"))


class ForcageCliTest(_Base):
    """`ameesh accounts use` : options `--for` et `--force` (témoin du lot)."""

    def _cfg_items(self):
        comptes = self._codex_comptes()
        cfg = dataclasses.replace(self.cfg, accounts={"codex": comptes})
        return cfg, accounts.parse(cfg.accounts)["codex"]

    def _args(self, *extra):
        return mesh_cli.build_parser().parse_args(
            ["accounts", "use", "codex", "secondaire"] + list(extra))

    def test_les_options_for_et_force_sont_analysees(self):
        self.assertEqual(self._args().duration, "1h")
        args = self._args("--force", "sonde de jauge", "--for", "15m")
        self.assertEqual(args.force, "sonde de jauge")
        self.assertEqual(args.duration, "15m")

    def test_use_refuse_sans_force_et_force_avec_echeance(self):
        cfg, items = self._cfg_items()
        now = float(int(time.time()))
        _journal(items[0].path, week=(34, now + 6 * DAY))
        _journal(items[1].path, short=(100, now + 2 * HOUR),
                 week=(47, now + 5 * DAY), credits=CREDITS)
        args = self._args("--for", "15m")
        with self.assertRaises(accounts.AccountError) as ctx:
            mesh_cli.cmd_accounts(cfg, args)
        self.assertIn("--force", str(ctx.exception))

        args = self._args("--force", "sonde de jauge", "--for", "15m")
        self.assertEqual(mesh_cli.cmd_accounts(cfg, args), 0)
        active = storage.of(self.db).accounts.active(cfg.host, "codex")
        self.assertTrue(active["forced"])
        remaining = float(active["forced_until"]) - time.time()
        self.assertGreater(remaining, 840.0)
        self.assertLessEqual(remaining, 900.0)
        switch = self.db.query(
            "select reason from account_switches order by id desc limit 1")[0]
        self.assertIn("sonde de jauge", switch["reason"])

    def test_use_refuse_une_echeance_nulle(self):
        cfg, _ = self._cfg_items()
        args = self._args("--for", "0")
        self.assertEqual(mesh_cli.cmd_accounts(cfg, args), 1)


class AlerteOverageHorsHoteTest(_Base):
    """#3755 : les drapeaux d'overage publiés en base lèvent l'alerte partout."""

    def _reading(self, *, used, resets_at, window_s, account="secondaire",
                 in_use=True, capable=True, observed_at=None, source="ameesh-cx3",
                 produced_at=None, key="codex-300min"):
        self.db.execute(
            "insert into quota_gauge_readings (harness, gauge_key, used, resets_at,"
            " window_s, account, produced_at, source, overage_capable,"
            " overage_in_use, observed_at)"
            " values ('codex', %s, %s, to_timestamp(%s), %s, %s,"
            " to_timestamp(%s), %s, %s, %s, coalesce(to_timestamp(%s), now()))",
            (key, used, resets_at, window_s, account, produced_at, source,
             capable, in_use, observed_at))

    def _alerts(self, now):
        cfg = dataclasses.replace(self.cfg, accounts={})
        return [a for a in exploitation.alerts(cfg, self.db, now=now)
                if a["type"] == "account_overage"]

    def test_un_compte_d_un_autre_hote_leve_account_overage(self):
        now = float(int(time.time()))
        self._reading(used=1.0, resets_at=now + 2 * HOUR, window_s=300 * 60)
        (alert,) = self._alerts(now)
        self.assertEqual(alert["harness"], "codex")
        self.assertEqual(alert["account"], "secondaire")
        self.assertIsNone(alert["host"])          # source absente du registre
        self.assertAlmostEqual(alert["value"], 1.0)
        self.assertAlmostEqual(alert["since"], now + 2 * HOUR - 300 * 60, delta=2.0)

    def _deux_hotes(self) -> None:
        """Deux hôtes homonymes : le même compte codex/secondaire chez chacun."""
        for name, host in (("runner-a", "host-a"), ("runner-b", "host-b")):
            self.db.execute(
                "INSERT INTO agent_registry (name, chantier, harness, host, status)"
                " VALUES (%s, '', 'codex', %s, 'idle')", (name, host))

    def test_deux_hotes_homonymes_meme_jauge_ne_s_effacent_pas(self):
        """Avis cx1 : à gauge_key égale, le relevé plus récent de l'hôte B ne
        doit pas effacer le dépassement publié par A avant le groupement."""
        now = float(int(time.time()))
        self._deux_hotes()
        self._reading(key="codex-300min", used=1.0, resets_at=now + 2 * HOUR,
                      window_s=300 * 60, in_use=True, source="runner-a",
                      produced_at=now - 10.0, observed_at=now - 5.0)
        self._reading(key="codex-300min", used=0.1, resets_at=now + 2 * HOUR,
                      window_s=300 * 60, in_use=False, source="runner-b",
                      produced_at=now, observed_at=now)
        (alert,) = self._alerts(now)
        self.assertEqual(alert["host"], "host-a")
        self.assertEqual(alert["gauge"], "codex-300min")
        self.assertAlmostEqual(alert["value"], 1.0)

    def test_deux_hotes_homonymes_fenetres_differentes_gardent_l_alerte(self):
        """Le relevé hebdomadaire faux et plus récent de B ne masque pas le
        dépassement de la fenêtre courte publié par A."""
        now = float(int(time.time()))
        self._deux_hotes()
        self._reading(key="codex-300min", used=1.0, resets_at=now + 2 * HOUR,
                      window_s=300 * 60, in_use=True, source="runner-a",
                      produced_at=now - 10.0, observed_at=now - 5.0)
        self._reading(key="codex-10080min", used=0.1,
                      resets_at=now + 6 * DAY, window_s=10080 * 60,
                      in_use=False, source="runner-b",
                      produced_at=now, observed_at=now)
        (alert,) = self._alerts(now)
        self.assertEqual(alert["host"], "host-a")
        self.assertEqual(alert["gauge"], "codex-300min")
        self.assertAlmostEqual(alert["value"], 1.0)

    def test_une_fenetre_remise_a_zero_resout_l_alerte(self):
        now = float(int(time.time()))
        self._reading(used=1.0, resets_at=now - 60.0, window_s=300 * 60)
        self.assertEqual(self._alerts(now), [])

    def test_un_releve_plus_recent_sans_overage_resout_l_alerte(self):
        now = float(int(time.time()))
        self._reading(used=1.0, resets_at=now + 2 * HOUR, window_s=300 * 60,
                      observed_at=now)
        self.assertEqual(len(self._alerts(now)), 1)
        self._reading(used=0.0, resets_at=now + 2 * HOUR, window_s=300 * 60,
                      in_use=False, observed_at=now + 1.0)
        self.assertEqual(self._alerts(now + 2.0), [])

    def test_le_drapeau_suit_la_production_pas_la_reinsertion(self):
        """B2 : une mesure ancienne réinsérée (observation récente) ne rajeunit
        pas son drapeau ; c'est la production qui départage, comme dans
        `latest_gauges`."""
        now = float(int(time.time()))
        # fenêtre courte : 100 %, overage vrai, produite récemment, observée
        # avant la réinsertion de la mesure hebdo
        self._reading(key="codex-300min", used=1.0, resets_at=now + 2 * HOUR,
                      window_s=300 * 60, in_use=True,
                      produced_at=now - 10.0, observed_at=now - 5.0)
        # fenêtre hebdo : 50 %, overage faux, produite anciennement mais
        # réinsérée après (un autre lecteur)
        self._reading(key="codex-10080min", used=0.5, resets_at=now + 6 * DAY,
                      window_s=10080 * 60, in_use=False,
                      produced_at=now - 200.0, observed_at=now - 1.0)
        (alert,) = self._alerts(now)
        self.assertEqual(alert["gauge"], "codex-300min")
        self.assertAlmostEqual(alert["value"], 1.0)

    def test_un_drapeau_sans_fenetre_saturee_expire_avec_la_remise_a_zero(self):
        """B3 : comme `Gauges.spent()`, un drapeau explicite sans fenêtre à
        100 % porte sur TOUTES les fenêtres ; il se résout à leur remise à
        zéro, pas huit jours plus tard."""
        now = float(int(time.time()))
        self._reading(used=0.8, resets_at=now + 60.0, window_s=300 * 60,
                      in_use=True, observed_at=now)
        self.assertEqual(len(self._alerts(now)), 1)
        self.db.execute(
            "update quota_gauge_readings set resets_at = to_timestamp(%s)",
            (now - 60.0,))
        self.assertEqual(self._alerts(now), [])

    def test_l_alerte_locale_ne_double_pas_la_lecture_partagee(self):
        now = float(int(time.time()))
        comptes = self._codex_comptes()
        cfg = dataclasses.replace(self.cfg, accounts={"codex": comptes})
        items = accounts.parse(cfg.accounts)["codex"]
        _journal(items[1].path, short=(100, now + 2 * HOUR),
                 week=(47, now + 5 * DAY), credits=CREDITS)
        # le relevé local de la passe d'alertes n'écrit pas l'historique : on
        # publie à la main celui que l'hôte du compte aurait écrit.
        self._reading(used=1.0, resets_at=now + 2 * HOUR, window_s=300 * 60,
                      observed_at=now)
        alerts = [a for a in exploitation.alerts(cfg, self.db, now=now)
                  if a["type"] == "account_overage"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["host"], self.cfg.host)


if __name__ == "__main__":
    unittest.main()
