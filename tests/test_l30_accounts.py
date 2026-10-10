# SPDX-License-Identifier: AGPL-3.0-only
"""L30 — comptes multiples par fournisseur (décision 0027).

Aucun vrai harnais, aucun vrai compte : les dossiers de configuration sont
des dossiers du bac à sable (avec un fichier d'identifiants VIDE, seule sa
présence compte), les clés sont factices, les jauges sont des journaux
enregistrés rejoués tels quels (rate_limit_event Claude, rate_limits Codex).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import time
import unittest
from unittest import mock

from ameesh import account_turn, accounts, balance, cost, registry, storage
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase


def _empreinte(cle: str) -> str:
    return hashlib.sha256(cle.encode()).hexdigest()[:12]


def _dossier(chemin: str, *, creds: str | None = None, mode: int = 0o700) -> str:
    os.makedirs(chemin, exist_ok=True)
    os.chmod(chemin, mode)
    if creds:
        # fichier d'identifiants VIDE : seule sa présence est vérifiée
        with open(os.path.join(chemin, creds), "w", encoding="utf-8"):
            pass
        os.chmod(os.path.join(chemin, creds), 0o600)
    return chemin


def _claude_rate_limit(used: float, resets_at: float) -> dict:
    return {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": used, "resetsAt": resets_at}}}}


def _codex_rollout(home: str, used_percent: float, resets_at: float) -> None:
    """Un journal de session Codex enregistré (forme des vrais `token_count`)."""
    dossier = os.path.join(home, "sessions", "2026", "10", "05")
    os.makedirs(dossier, exist_ok=True)
    with open(os.path.join(dossier, "rollout-test.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": {"id": "x"}}) + "\n")
        fh.write(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": {
                "plan_type": "pro",
                "primary": {"used_percent": used_percent, "window_minutes": 300,
                            "resets_at": resets_at},
                "secondary": None}}}) + "\n")


class ProfilsTest(unittest.TestCase):
    """Configuration, validation, environnement, continuité (sans base)."""

    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l30-")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_configuration_valide_et_ordre_garde(self):
        profils = accounts.parse({"claude": [
            {"name": "primaire"},
            {"name": "secondaire", "type": "config_dir", "path": "/x/claude-2"}]})
        self.assertEqual([p.name for p in profils["claude"]], ["primaire", "secondaire"])
        self.assertTrue(profils["claude"][0].default)
        self.assertEqual(profils["claude"][1].path, "/x/claude-2")

    def test_configuration_invalide_refusee(self):
        for brut in (
            {"inconnu": [{"name": "a"}]},
            {"claude": []},
            {"claude": [{"name": "a"}, {"name": "a"}]},
            {"claude": [{"name": "a", "type": "magie"}]},
            {"claude": [{"name": "a", "path": "relatif"}]},
            {"deepseek": [{"name": "a", "type": "api_key_env"}]},
            {"deepseek": [{"name": "a", "type": "api_key_env", "key_env": "X",
                           "key_file": "/k"}]},
            {"claude": [{"name": "../evil"}]},
        ):
            with self.subTest(brut=brut):
                with self.assertRaises(accounts.AccountError):
                    accounts.parse(brut)

    def test_dossier_prive_et_identifiants_presents(self):
        ouvert = _dossier(os.path.join(self.tmp, "ouvert"), mode=0o755,
                          creds=".credentials.json")
        sans = _dossier(os.path.join(self.tmp, "sans"))
        bon = _dossier(os.path.join(self.tmp, "bon"), creds=".credentials.json")
        p = lambda chemin: accounts.Profile("claude", "x", path=chemin)  # noqa: E731
        self.assertTrue(any("non privé" in m for m in accounts.check(p(ouvert))))
        self.assertTrue(any("identifiants absents" in m for m in accounts.check(p(sans))))
        self.assertEqual(accounts.check(p(bon)), [])
        self.assertTrue(accounts.check(p(os.path.join(self.tmp, "absent"))))
        codex = accounts.Profile("codex", "c", path=_dossier(
            os.path.join(self.tmp, "codex"), creds="auth.json"))
        self.assertEqual(accounts.check(codex), [])

    def test_fichier_de_cle_0600_et_variable(self):
        cle = os.path.join(self.tmp, "cle")
        with open(cle, "w", encoding="utf-8") as fh:
            fh.write("cle-factice-a\n")
        os.chmod(cle, 0o644)
        prof = accounts.Profile("deepseek", "a", kind="api_key_env",
                                env="DEEPSEEK_API_KEY", key_file=cle)
        self.assertTrue(any("trop ouvert" in m for m in accounts.check(prof)))
        with self.assertRaises(accounts.AccountError) as ctx:
            accounts.apply_env({}, prof)
        self.assertNotIn("cle-factice-a", str(ctx.exception))
        os.chmod(cle, 0o600)
        env: dict = {}
        accounts.apply_env(env, prof)
        self.assertEqual(env["DEEPSEEK_API_KEY"], "cle-factice-a")
        var = accounts.Profile("deepseek", "b", kind="api_key_env",
                               env="DEEPSEEK_API_KEY", key_env="AMEESH_TEST_CLE_B")
        self.assertTrue(accounts.check(var, environ={}))
        env = {}
        accounts.apply_env(env, var, environ={"AMEESH_TEST_CLE_B": "cle-factice-b"})
        self.assertEqual(env["DEEPSEEK_API_KEY"], "cle-factice-b")

    def test_environnement_du_dossier(self):
        # compte par défaut : le dossier hérité de l'exécuteur pour CE harnais,
        # jamais celui d'un autre harnais
        declare = {"claude": [accounts.Profile("claude", "p")]}
        env = {"CLAUDE_CONFIG_DIR": "/herite", "CODEX_HOME": "/autre"}
        accounts.launch_env(env, declare, declare["claude"][0],
                            environ={"CLAUDE_CONFIG_DIR": "/herite", "CODEX_HOME": "/autre"})
        self.assertEqual(env, {"CLAUDE_CONFIG_DIR": "/herite"})
        env = {}
        accounts.launch_env(env, declare, declare["claude"][0], environ={})
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)  # rien d'hérité : défaut du harnais
        self.assertEqual(accounts.Profile("codex", "d").home({"CODEX_HOME": "/h"}), "/h")
        with self.assertRaises(accounts.AccountError):  # validé au lancement
            accounts.apply_env(env, accounts.Profile("codex", "s", path="/x/codex-2"))
        dossier = _dossier(os.path.join(self.tmp, "codex-2"), creds="auth.json")
        accounts.apply_env(env, accounts.Profile("codex", "s", path=dossier))
        self.assertEqual(env["CODEX_HOME"], dossier)

    def test_continuite_selon_le_harnais(self):
        a = _dossier(os.path.join(self.tmp, "a"))
        b = _dossier(os.path.join(self.tmp, "b"))
        c = _dossier(os.path.join(self.tmp, "c"))
        os.makedirs(os.path.join(a, "projects"))
        os.symlink(os.path.join(a, "projects"), os.path.join(b, "projects"))
        os.makedirs(os.path.join(c, "projects"))
        pa, pb, pc = (accounts.Profile("claude", n, path=d)
                      for n, d in (("a", a), ("b", b), ("c", c)))
        self.assertTrue(accounts.session_portable("claude", pa, pb))   # projects/ partagé
        self.assertFalse(accounts.session_portable("claude", pa, pc))  # distinct
        self.assertFalse(accounts.session_portable("claude", None, pa))
        self.assertFalse(accounts.session_portable(
            "codex", accounts.Profile("codex", "a", path=a),
            accounts.Profile("codex", "b", path=b)))
        k1 = accounts.Profile("deepseek", "k1", kind="api_key_env", env="DEEPSEEK_API_KEY",
                              key_env="X")
        k2 = accounts.Profile("deepseek", "k2", kind="api_key_env", env="DEEPSEEK_API_KEY",
                              key_env="Y")
        self.assertTrue(accounts.session_portable("deepseek", k1, k2))  # même DSH_HOME

    def test_jauges_claude_attribuees_par_le_marqueur(self):
        etat = os.path.join(self.tmp, "etat")
        os.makedirs(os.path.join(etat, "ag"))
        with open(os.path.join(etat, "ag", "tool"), "w", encoding="utf-8") as fh:
            fh.write("claude\n")
        now = time.time()
        lignes = [
            _claude_rate_limit(0.30, now + 3600),  # avant tout marqueur : le primaire
            {"type": cost.ACCOUNT_MARKER, "harness": "claude", "account": "secondaire",
             "ts": now - 10},
            _claude_rate_limit(0.70, now + 3600),
            {"type": cost.ACCOUNT_MARKER, "harness": "claude", "account": "primaire",
             "ts": now - 5},
            _claude_rate_limit(0.40, now + 3600),
        ]
        with open(os.path.join(etat, "ag", "events.jsonl"), "w", encoding="utf-8") as fh:
            for ligne in lignes:
                fh.write(json.dumps(ligne) + "\n")
        book = cost.CostBook(state_dir=etat, db=None, codex_sessions=self.tmp)
        self.assertAlmostEqual(book.claude_gauges("primaire", "primaire")[0].used, 0.40)
        self.assertAlmostEqual(book.claude_gauges("secondaire", "primaire")[0].used, 0.70)
        self.assertEqual(book.claude_gauges("autre", "primaire"), [])
        self.assertEqual(book.account_at("ag", 3, "claude"), "secondaire")
        self.assertIsNone(book.account_at("ag", 1, "claude"))

    def test_jauges_codex_lues_dans_le_dossier_du_compte(self):
        now = time.time()
        a, b = os.path.join(self.tmp, "ca"), os.path.join(self.tmp, "cb")
        _codex_rollout(a, 95, now + 3600)
        _codex_rollout(b, 10, now + 3600)
        book = cost.CostBook(state_dir=self.tmp, db=None, codex_sessions="/nulle-part")
        items = [accounts.Profile("codex", "a", path=a), accounts.Profile("codex", "b", path=b)]
        self.assertAlmostEqual(accounts.gauges_of(book, items[0], items)[0].used, 0.95)
        self.assertAlmostEqual(accounts.gauges_of(book, items[1], items)[0].used, 0.10)


class _Base(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        for table in ("account_active", "account_holds", "account_switches", "turn_costs",
                      "spend_pending", "quota_gauge_readings", "provider_balances"):
            self.db.execute("DELETE FROM %s" % table)

    def _claude_comptes(self, partage: bool = True) -> list:
        a = _dossier(os.path.join(self.tmp, "comptes", "claude-1"), creds=".credentials.json")
        b = _dossier(os.path.join(self.tmp, "comptes", "claude-2"), creds=".credentials.json")
        os.makedirs(os.path.join(a, "projects"), exist_ok=True)
        if partage:
            os.symlink(os.path.join(a, "projects"), os.path.join(b, "projects"))
        return [{"name": "primaire", "path": a}, {"name": "secondaire", "path": b}]

    def _codex_comptes(self) -> list:
        a = _dossier(os.path.join(self.tmp, "comptes", "codex-1"), creds="auth.json")
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        return [{"name": "primaire", "path": a}, {"name": "secondaire", "path": b}]

    def _worker(self, name: str, harness: str, comptes: dict, **cfg_extra):
        cfg = dataclasses.replace(self.cfg, accounts=comptes, budget_usd_per_hour=10.0,
                                  budget_check_interval=0.0, **cfg_extra)
        runner = Runner(cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, harness, cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        return runner, AgentWorker(runner, registry.get(self.db, name), lease)

    def _tour(self, worker, prompt: str = "travail", **env_extra) -> dict | None:
        registry.set_pending_prompt(self.db, worker.name, prompt)
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        env.update(env_extra)
        with mock.patch.dict(os.environ, env, clear=False):
            for name in ("AMEESH_TEST_RATE_LIMIT",):
                if name not in env_extra:
                    os.environ.pop(name, None)
            spec = worker.pick()
            if spec is None:
                return None
            worker.run_turn(spec)
            return spec

    def _fil(self) -> str:
        texte = []
        for racine, _d, fichiers in os.walk(self.cfg.threads_root):
            for nom in fichiers:
                with open(os.path.join(racine, nom), encoding="utf-8", errors="ignore") as fh:
                    texte.append(fh.read())
        return "\n".join(texte)

    def _bascules(self) -> list:
        return self.db.query("SELECT from_account, to_account, kind, agent FROM account_switches"
                             " ORDER BY id")


class ChoixTest(_Base):
    """`accounts.choose` sur Postgres réel : bascule, continuité, pause, course."""

    def _items(self):
        return accounts.parse({"codex": self._codex_comptes()})["codex"]

    def test_bascule_au_seuil_puis_retour_par_ordre_declare(self):
        """0034 (amende 0027 §2–3) : plus de retenue ; à la remise à zéro, les
        deux relevés échus comptent pour 0 % et l'ordre déclaré départage."""
        items = self._items()
        now = time.time()
        _codex_rollout(items[0].path, 95, now + 3600)   # primaire au seuil
        _codex_rollout(items[1].path, 5, now + 3600)
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now)
        self.assertEqual(choix.profile.name, "secondaire")
        self.assertEqual(choix.switched["kind"], "bascule")
        self.assertIn("primaire au seuil", choix.switched["reason"])
        self.assertEqual(storage.of(self.db).accounts.holds(self.cfg.host, "codex"), {})
        # avant la remise à zéro : le primaire est toujours au seuil
        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now + 60)
        self.assertEqual(choix.profile.name, "secondaire")
        self.assertIsNone(choix.switched)
        # fenêtres remises à zéro (resets_at passé) : égalité, ordre déclaré
        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now + 3700)
        self.assertEqual(choix.profile.name, "primaire")
        self.assertEqual(choix.switched["kind"], "bascule")
        self.assertEqual([(b["from_account"], b["to_account"], b["kind"])
                          for b in self._bascules()],
                         [("primaire", "secondaire", "bascule"),
                          ("secondaire", "primaire", "bascule")])
        # jauges par compte dans l'historique de L26 (colonne de compte, 0028)
        comptes = {r["account"] for r in self.db.query(
            "SELECT DISTINCT account FROM quota_gauge_readings")}
        self.assertEqual(comptes, {"primaire", "secondaire"})

    def test_la_session_garde_son_compte_sous_son_seuil(self):
        """0034 §4 (remplace la retenue de 0027 §3) : le primaire repasse sous
        son plafond de rythme, mais la session ouverte sur le secondaire y
        reste ; sans session, le choix va au plus en retard sur son rythme
        (amendement 0034 du 2026-10-10, L117) : encore le secondaire."""
        items = self._items()
        now = time.time()
        _codex_rollout(items[0].path, 95, now + 3600)
        _codex_rollout(items[1].path, 5, now + 3600)
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        self.assertEqual(accounts.choose(self.db, self.cfg.host, "codex", items, book,
                                         now=now).profile.name, "secondaire")
        _codex_rollout(items[0].path, 85, now + 3600)  # écoulé 80 % → plafond 90 %
        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book,
                                now=now + 600, session_account="secondaire")
        self.assertEqual(choix.profile.name, "secondaire")
        self.assertTrue(choix.kept)
        self.assertIsNone(choix.switched)
        self.assertEqual(accounts.choose(self.db, self.cfg.host, "codex", items, book,
                                         now=now + 600).profile.name, "secondaire")
        # le primaire redevient le plus en retard : une nouvelle session y va
        _codex_rollout(items[1].path, 89, now + 3600)   # 89 % contre 85 %
        self.assertEqual(accounts.choose(self.db, self.cfg.host, "codex", items, book,
                                         now=now + 600).profile.name, "primaire")

    def test_tous_au_seuil_pause(self):
        items = self._items()
        now = time.time()
        _codex_rollout(items[0].path, 95, now + 3600)
        _codex_rollout(items[1].path, 97, now + 3600)
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now)
        self.assertIsNone(choix.profile)
        self.assertIn("tous les comptes codex au seuil", choix.reason)
        self.assertIn("primaire", choix.reason)
        self.assertIn("secondaire", choix.reason)

    def test_course_une_seule_bascule_journalisee(self):
        items = self._items()
        store = storage.of(self.db).accounts
        store.init_active(self.cfg.host, "codex", "primaire")
        self.assertTrue(store.switch(self.cfg.host, "codex", expected="primaire",
                                     to="secondaire", kind="bascule", reason="r", agent="a"))
        self.assertFalse(store.switch(self.cfg.host, "codex", expected="primaire",
                                      to="secondaire", kind="bascule", reason="r", agent="b"))
        self.assertEqual(len(self._bascules()), 1)
        self.assertEqual(accounts.current(self.db, self.cfg.host, "codex", items).name,
                         "secondaire")

    def test_forcage_manuel_puis_automatique(self):
        items = self._items()
        now = time.time()
        _codex_rollout(items[0].path, 5, now + 3600)
        _codex_rollout(items[1].path, 97, now + 3600)
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        self.assertTrue(accounts.force(self.db, self.cfg.host, "codex", items, "secondaire",
                                       by="human:test"))
        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now)
        self.assertIsNone(choix.profile)  # forcé ET au seuil : pause, pas de bascule
        self.assertIn("forcé", choix.reason)
        self.assertTrue(accounts.automatic(self.db, self.cfg.host, "codex", by="human:test"))
        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now)
        self.assertEqual(choix.profile.name, "primaire")
        with self.assertRaises(accounts.AccountError):
            accounts.force(self.db, self.cfg.host, "codex", items, "inconnu", by="human:test")
        self.assertEqual([b["kind"] for b in self._bascules()], ["manuel", "auto", "bascule"])

    def test_profil_invalide_jamais_choisi(self):
        comptes = self._codex_comptes()
        os.chmod(comptes[1]["path"], 0o755)
        items = accounts.parse({"codex": comptes})["codex"]
        now = time.time()
        _codex_rollout(items[0].path, 95, now + 3600)
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now)
        self.assertIsNone(choix.profile)
        self.assertIn("non privé", choix.reason)


class ExecuteurTest(_Base):
    """La bascule avant chaque tour, dans l'exécuteur, avec les faux harnais."""

    def test_claude_bascule_et_reprend_la_meme_session(self):
        comptes = self._claude_comptes(partage=True)
        runner, worker = self._worker("cl", "claude", {"claude": comptes})
        try:
            # 1er tour, primaire ; le faux harnais publie une jauge au seuil
            haut = json.dumps({"five_hour": {"utilization": 0.95,
                                             "resetsAt": time.time() + 3600}})
            self.assertIsNotNone(self._tour(worker, AMEESH_TEST_RATE_LIMIT=haut))
            self.assertEqual(self.turns()[-1]["compte"]["CLAUDE_CONFIG_DIR"], comptes[0]["path"])
            self.assertEqual(registry.get(self.db, "cl")["session_id"], "claude-sess-1")
            # 2e tour : bascule AVANT le tour, même session reprise (projects/ partagé)
            self.assertIsNotNone(self._tour(worker, "suite"))
            dernier = self.turns()[-1]
            self.assertEqual(dernier["compte"]["CLAUDE_CONFIG_DIR"], comptes[1]["path"])
            self.assertIn("--resume", dernier["argv"])
            self.assertIn("claude-sess-1", dernier["argv"])
            self.assertEqual([(b["from_account"], b["to_account"], b["kind"], b["agent"])
                              for b in self._bascules()],
                             [("primaire", "secondaire", "bascule", "cl")])
            fil = self._fil()
            self.assertIn("Bascule de compte claude : primaire → secondaire", fil)
            self.assertIn("reprise telle quelle", fil)
            # le grand livre attribue chaque tour à son compte
            lignes = self.db.query("SELECT account FROM turn_costs WHERE agent = 'cl'"
                                   " ORDER BY id")
            self.assertEqual([r["account"] for r in lignes], ["primaire", "secondaire"])
        finally:
            worker.watchdog_stop.set()

    def test_claude_sans_partage_rotation_avec_resume(self):
        comptes = self._claude_comptes(partage=False)
        runner, worker = self._worker("cl2", "claude", {"claude": comptes})
        try:
            haut = json.dumps({"five_hour": {"utilization": 0.95,
                                             "resetsAt": time.time() + 3600}})
            self._tour(worker, AMEESH_TEST_RATE_LIMIT=haut)
            n = len(self.turns())
            self._tour(worker, "suite")
            tours = self.turns()[n:]
            # le résumé sous l'ANCIEN compte, sur l'ancienne session…
            self.assertEqual(tours[0]["compte"]["CLAUDE_CONFIG_DIR"], comptes[0]["path"])
            self.assertIn("claude-sess-1", tours[0]["argv"])
            # … puis le tour sous le nouveau, session neuve ouverte par le résumé
            self.assertEqual(tours[1]["compte"]["CLAUDE_CONFIG_DIR"], comptes[1]["path"])
            self.assertNotIn("--resume", tours[1]["argv"])
            self.assertIn("Reprise de session après rotation", tours[1]["argv"][-1])
            self.assertIn("rotation avec résumé", self._fil().lower())
        finally:
            worker.watchdog_stop.set()

    def test_codex_rotation_avec_resume_sous_l_ancien_codex_home(self):
        comptes = self._codex_comptes()
        runner, worker = self._worker("cx", "codex", {"codex": comptes})
        try:
            self._tour(worker)
            self.assertEqual(self.turns()[-1]["compte"]["CODEX_HOME"], comptes[0]["path"])
            _codex_rollout(comptes[0]["path"], 95, time.time() + 3600)  # journal enregistré
            n = len(self.turns())
            self._tour(worker, "suite")
            tours = self.turns()[n:]
            self.assertEqual(len(tours), 2)
            self.assertEqual(tours[0]["compte"]["CODEX_HOME"], comptes[0]["path"])
            self.assertIn("resume", tours[0]["argv"])          # résumé sur l'ancien fil
            self.assertEqual(tours[1]["compte"]["CODEX_HOME"], comptes[1]["path"])
            self.assertNotIn("resume", tours[1]["argv"])       # fil neuf, sous le secondaire
            fil = self._fil()
            self.assertIn("ne peut pas être reprise sous le compte secondaire", fil)
        finally:
            worker.watchdog_stop.set()

    def test_codex_compte_par_defaut_un_seul_mecanisme(self):
        """Compte primaire sans dossier : jauges ET harnais sur le CODEX_HOME de
        l'exécuteur (mécanisme de cost.CostBook) ; compte déclaré : le sien."""
        herite = _dossier(os.path.join(self.tmp, "codex-herite"), creds="auth.json")
        secondaire = self._codex_comptes()[1]
        comptes = {"codex": [{"name": "primaire"}, secondaire]}
        runner, worker = self._worker("cxd", "codex", comptes)
        try:
            with mock.patch.dict(os.environ, {"CODEX_HOME": herite}):
                self._tour(worker)
                self.assertEqual(self.turns()[-1]["compte"]["CODEX_HOME"], herite)
                _codex_rollout(herite, 95, time.time() + 3600)  # lu par le mécanisme
                n = len(self.turns())
                self._tour(worker, "suite")
            dernier = self.turns()[-1]
            self.assertGreater(len(self.turns()), n)
            self.assertEqual(dernier["compte"]["CODEX_HOME"], secondaire["path"])
            self.assertEqual([(b["from_account"], b["to_account"]) for b in self._bascules()],
                             [("primaire", "secondaire")])
        finally:
            worker.watchdog_stop.set()

    def test_tous_au_seuil_l_agent_est_en_pause(self):
        comptes = self._codex_comptes()
        runner, worker = self._worker("cx2", "codex", {"codex": comptes})
        try:
            _codex_rollout(comptes[0]["path"], 95, time.time() + 3600)
            _codex_rollout(comptes[1]["path"], 96, time.time() + 3600)
            self.assertIsNone(self._tour(worker))
            ligne = registry.get(self.db, "cx2")
            self.assertEqual(ligne["status"], "blocked")
            self.assertIn("tous les comptes codex au seuil", ligne["status_text"])
            self.assertEqual(self.turns(), [])
        finally:
            worker.watchdog_stop.set()

    def test_deepseek_cle_par_compte_et_plafond_du_compte(self):
        cle_a = os.path.join(self.tmp, "deepseek-a.key")
        with open(cle_a, "w", encoding="utf-8") as fh:
            fh.write("cle-factice-a\n")
        os.chmod(cle_a, 0o600)
        comptes = {"deepseek": [
            {"name": "cle-a", "type": "api_key_env", "key_file": cle_a, "hourly_usd": 1.0},
            {"name": "cle-b", "type": "api_key_env", "key_env": "AMEESH_TEST_CLE_B"}]}
        runner, worker = self._worker("ds", "deepseek", comptes)
        try:
            with mock.patch.dict(os.environ, {"AMEESH_TEST_CLE_B": "cle-factice-b"}):
                self._tour(worker)
                self.assertEqual(self.turns()[-1]["compte"]["cle"], _empreinte("cle-factice-a"))
                self.db.execute("INSERT INTO turn_costs (agent, harness, model, usd, account)"
                                " VALUES ('ds', 'deepseek', 'deepseek-flash', 2.0, 'cle-a')")
                self._tour(worker, "suite")
                dernier = self.turns()[-1]
                self.assertEqual(dernier["compte"]["cle"], _empreinte("cle-factice-b"))
                self.assertIn("--session-id", dernier["argv"])  # même DSH_HOME : reprise
            # aucune clé dans le journal des faux harnais ni dans le fil
            with open(self.turns_log, encoding="utf-8") as fh:
                journal = fh.read()
            self.assertNotIn("cle-factice", journal)
            self.assertNotIn("cle-factice", self._fil())
        finally:
            worker.watchdog_stop.set()

    def test_garde_desactivee_bascule_sans_pause(self):
        comptes = self._codex_comptes()
        runner, worker = self._worker("cx3", "codex", {"codex": comptes})
        runner.budget_usd_per_hour = 0.0
        try:
            _codex_rollout(comptes[0]["path"], 95, time.time() + 3600)
            _codex_rollout(comptes[1]["path"], 96, time.time() + 3600)
            self.assertIsNotNone(self._tour(worker))  # tous au seuil, mais pas de pause
            self.assertEqual(self.turns()[-1]["compte"]["CODEX_HOME"], comptes[0]["path"])
        finally:
            worker.watchdog_stop.set()

    def test_sans_comptes_rien_ne_change(self):
        runner, worker = self._worker("nu", "claude", {})
        try:
            self._tour(worker)
            self.assertIsNone(self.turns()[-1]["compte"]["CLAUDE_CONFIG_DIR"])
            with open(os.path.join(self.cfg.state_dir, "nu", "events.jsonl"),
                      encoding="utf-8") as fh:
                self.assertNotIn(cost.ACCOUNT_MARKER, fh.read())
            self.assertIsNone(self.db.query("SELECT account FROM turn_costs")[0]["account"])
        finally:
            worker.watchdog_stop.set()

    def test_profil_inutilisable_le_tour_ne_part_pas(self):
        comptes = {"deepseek": [{"name": "k", "type": "api_key_env",
                                 "key_env": "AMEESH_TEST_CLE_ABSENTE"}]}
        runner, worker = self._worker("ds2", "deepseek", comptes)
        runner.budget_usd_per_hour = 0.0  # pas de garde : le tour lui-même refuse
        try:
            os.environ.pop("AMEESH_TEST_CLE_ABSENTE", None)
            self._tour(worker)
            self.assertEqual(self.turns(), [])
            ligne = registry.get(self.db, "ds2")
            self.assertEqual(ligne["status"], "blocked")
            self.assertEqual(ligne["pending_prompt"], "travail")  # consigne remise
        finally:
            worker.watchdog_stop.set()


class IsolementTest(_Base):
    """Revue codex1 de eed8927 : P1 (isolement des identifiants), P2 (profil
    invalide refusé au lancement, garde de budget active ou non). Valeurs
    synthétiques seulement ; on ne regarde que les variables d'identifiants
    de l'environnement du fils, jamais l'environnement entier."""

    AUTH = ("CODEX1_FAKE_SOURCE_A", "CODEX1_FAKE_SOURCE_B", "DEEPSEEK_API_KEY",
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
            "DSH_HOME")

    def _capture(self, worker, binaire: str, **env_extra) -> list:
        import subprocess
        vrai = subprocess.Popen
        fils = []

        def espion(argv, *args, **kwargs):
            if any(("fakebin/%s" % binaire) in str(part) for part in argv):
                env = kwargs.get("env") or {}
                fils.append({k: env.get(k) for k in self.AUTH})
            return vrai(argv, *args, **kwargs)

        with mock.patch("subprocess.Popen", side_effect=espion):
            self._tour(worker, **env_extra)
        return fils

    def test_sonde_codex1_le_fils_ne_recoit_que_la_cle_choisie(self):
        runner, worker = self._worker("isolation", "deepseek", {"deepseek": [
            {"name": "a", "type": "api_key_env", "key_env": "CODEX1_FAKE_SOURCE_A"},
            {"name": "b", "type": "api_key_env", "key_env": "CODEX1_FAKE_SOURCE_B"}]})
        try:
            fils = self._capture(worker, "dsh", CODEX1_FAKE_SOURCE_A="synthetic-a",
                                 CODEX1_FAKE_SOURCE_B="synthetic-b",
                                 ANTHROPIC_API_KEY="herite-x", OPENAI_API_KEY="herite-y")
            self.assertEqual(len(fils), 1)
            self.assertEqual(fils[0]["DEEPSEEK_API_KEY"], "synthetic-a")
            for nom in ("CODEX1_FAKE_SOURCE_A", "CODEX1_FAKE_SOURCE_B",
                        "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
                self.assertIsNone(fils[0][nom], nom)
        finally:
            worker.watchdog_stop.set()

    def test_config_dir_sans_cle_d_api_heritee(self):
        comptes = self._claude_comptes()
        runner, worker = self._worker("isol-cl", "claude", {"claude": comptes, "deepseek": [
            {"name": "k", "type": "api_key_env", "key_env": "CODEX1_FAKE_SOURCE_A"}]})
        try:
            fils = self._capture(worker, "claude", ANTHROPIC_API_KEY="herite-x",
                                 CODEX_HOME="/herite", CODEX1_FAKE_SOURCE_A="synthetic-a")
            self.assertEqual(len(fils), 1)
            self.assertEqual(fils[0]["CLAUDE_CONFIG_DIR"], comptes[0]["path"])
            for nom in ("ANTHROPIC_API_KEY", "CODEX_HOME", "CODEX1_FAKE_SOURCE_A",
                        "DEEPSEEK_API_KEY"):
                self.assertIsNone(fils[0][nom], nom)
        finally:
            worker.watchdog_stop.set()

    def test_harnais_sans_comptes_ne_recoit_pas_les_cles_des_autres(self):
        runner, worker = self._worker("isol-cx", "codex", {"deepseek": [
            {"name": "k", "type": "api_key_env", "key_env": "CODEX1_FAKE_SOURCE_A"}]})
        try:
            fils = self._capture(worker, "codex", CODEX1_FAKE_SOURCE_A="synthetic-a",
                                 OPENAI_API_KEY="garde-y")
            self.assertEqual(len(fils), 1)
            self.assertIsNone(fils[0]["CODEX1_FAKE_SOURCE_A"])
            self.assertEqual(fils[0]["OPENAI_API_KEY"], "garde-y")  # pas de compte : inchangé
        finally:
            worker.watchdog_stop.set()

    def test_sonde_codex1_dossier_invalide_garde_desactivee_aucun_lancement(self):
        runner, worker = self._worker("invalid-dir", "claude", {"claude": [
            {"name": "absent", "path": os.path.join(self.tmp, "absent-config")}]})
        runner.budget_usd_per_hour = 0
        try:
            self._tour(worker)
            self.assertEqual(len(self.turns()), 0, "un dossier invalide a lancé un harnais")
            ligne = registry.get(self.db, "invalid-dir")
            self.assertEqual(ligne["status"], "blocked")
            self.assertIn("dossier absent", ligne["last_error"] or "")
            self.assertEqual(ligne["pending_prompt"], "travail")
        finally:
            worker.watchdog_stop.set()

    def test_profil_devenu_invalide_apres_le_choix(self):
        comptes = self._claude_comptes()
        runner, worker = self._worker("apres-choix", "claude", {"claude": comptes})
        try:
            registry.set_pending_prompt(self.db, "apres-choix", "travail")
            with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN,
                                              "AMEESH_TEST_LOG": self.turns_log}):
                spec = worker.pick()
                self.assertIsNotNone(spec)
                os.chmod(comptes[0]["path"], 0o755)  # entre le choix et le lancement
                self.assertFalse(worker.run_turn(spec))
            self.assertEqual(self.turns(), [])
            self.assertIn("non privé", registry.get(self.db, "apres-choix")["last_error"])
        finally:
            worker.watchdog_stop.set()

    def test_attach_meme_nettoyage_et_validation(self):
        from ameesh import account_turn
        comptes = self._codex_comptes()
        cfg = dataclasses.replace(self.cfg, accounts={"codex": comptes, "deepseek": [
            {"name": "k", "type": "api_key_env", "key_env": "CODEX1_FAKE_SOURCE_A"}]})
        ok, profil = account_turn.attach_profile(cfg, self.db, "codex")
        self.assertTrue(ok)
        env = {"CODEX1_FAKE_SOURCE_A": "synthetic-a", "OPENAI_API_KEY": "herite-y",
               "CLAUDE_CONFIG_DIR": "/herite", "PATH": "/usr/bin"}
        self.assertTrue(account_turn.apply_env_attach(env, profil, cfg))
        self.assertEqual(env["CODEX_HOME"], comptes[0]["path"])
        self.assertEqual(env["PATH"], "/usr/bin")  # le reste est préservé
        for nom in ("CODEX1_FAKE_SOURCE_A", "OPENAI_API_KEY", "CLAUDE_CONFIG_DIR"):
            self.assertNotIn(nom, env)
        # dossier devenu invalide : attach refuse, sans rien lancer
        os.chmod(comptes[0]["path"], 0o755)
        self.assertFalse(account_turn.apply_env_attach(dict(env), profil, cfg))
        ok, _ = account_turn.attach_profile(cfg, self.db, "codex")
        self.assertFalse(ok)


class SoldesEtCliTest(_Base):
    def _config(self, comptes: dict) -> str:
        chemin = os.path.join(self.tmp, "config-hote.json")
        with open(chemin, "w", encoding="utf-8") as fh:
            json.dump({"accounts": comptes}, fh)
        return chemin

    def test_solde_par_compte_de_cle(self):
        cle_a = os.path.join(self.tmp, "a.key")
        with open(cle_a, "w", encoding="utf-8") as fh:
            fh.write("cle-factice-a")
        os.chmod(cle_a, 0o600)
        cfg = dataclasses.replace(self.cfg, accounts={"deepseek": [
            {"name": "cle-a", "type": "api_key_env", "key_file": cle_a, "min_balance": 1.0},
            {"name": "cle-b", "type": "api_key_env", "key_env": "AMEESH_TEST_CLE_B"}]})
        vus = []
        soldes = {"cle-factice-a": "0.50", "cle-factice-b": "20.00"}

        def transport(url, headers, timeout):
            cle = headers["Authorization"].split(" ", 1)[1]
            vus.append(_empreinte(cle))
            # forme de `discovery.http.fetch_json` : (charge JSON, nom d'échec)
            return {"is_available": True, "balance_infos": [
                {"currency": "USD", "total_balance": soldes[cle]}]}, ""

        paires = balance.account_sources(cfg, env={"AMEESH_TEST_CLE_B": "cle-factice-b"},
                                         transport=transport)
        self.assertEqual([c for _s, c in paires], ["cle-a", "cle-b"])
        for source, compte in paires:
            balance.record(self.db, source, compte)
        self.assertEqual(vus, [_empreinte("cle-factice-a"), _empreinte("cle-factice-b")])
        rows = storage.of(self.db).operations.balances(provider="deepseek", since_s=3600)
        self.assertEqual({r["account"] for r in rows}, {"cle-a", "cle-b"})
        # le solde sous le seuil met le compte au seuil : bascule vers cle-b
        items = accounts.parse(cfg.accounts)["deepseek"]
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        with mock.patch.dict(os.environ, {"AMEESH_TEST_CLE_B": "cle-factice-b"}):
            choix = accounts.choose(self.db, self.cfg.host, "deepseek", items, book)
        self.assertEqual(choix.profile.name, "cle-b")
        self.assertIn("solde du compte", choix.switched["reason"])
        # deux séries distinctes : pas de « dépense » entre deux clés
        self.assertEqual(balance.spend(rows)["hourly"], [])

    def test_solde_par_compte_passe_par_le_transport_partage(self):
        """Aucun autre client HTTP : sans transport injecté, chaque source de
        compte passe par `discovery.http.fetch_json` (ouvreur local, aucune
        redirection) — la clé du compte dans l'en-tête, jamais ailleurs."""
        from ameesh.discovery import http as discovery_http
        cfg = dataclasses.replace(self.cfg, accounts={"deepseek": [
            {"name": "cle-b", "type": "api_key_env", "key_env": "AMEESH_TEST_CLE_B"}]})
        vus = []

        def faux_fetch(url, headers=None, timeout=20.0, **_kw):
            vus.append((url, _empreinte(headers["Authorization"].split(" ", 1)[1])))
            return {"is_available": True, "balance_infos": [
                {"currency": "USD", "total_balance": "3.00"}]}, ""

        paires = balance.account_sources(cfg, env={"AMEESH_TEST_CLE_B": "cle-factice-b"})
        self.assertEqual(len(paires), 1)
        self.assertIs(paires[0][0].transport, balance._https_transport)
        with mock.patch.object(discovery_http, "fetch_json", faux_fetch):
            balance.record(self.db, paires[0][0], paires[0][1])
        self.assertEqual(vus, [("https://api.deepseek.com/user/balance",
                                _empreinte("cle-factice-b"))])

    def test_cli_accounts_list_use_auto_et_cost(self):
        comptes = self._codex_comptes()
        env = self.env(AMEESH_CONFIG=self._config({"codex": comptes}))
        now = time.time()
        _codex_rollout(comptes[0]["path"], 40, now + 3600)
        proc = self.mesh("accounts", "list", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("primaire", proc.stdout)
        self.assertIn("secondaire", proc.stdout)
        proc = self.mesh("accounts", "use", "codex", "secondaire", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("accounts", "list", "--json", env=env)
        data = json.loads(proc.stdout)
        actif = [r for r in data["accounts"] if r["active"]][0]
        self.assertEqual((actif["account"], actif["forced"]), ("secondaire", True))
        self.assertEqual(data["switches"][0]["kind"], "manuel")
        self.assertEqual(self.mesh("accounts", "use", "codex", "inconnu",
                                   env=env).returncode, 1)
        proc = self.mesh("accounts", "auto", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("automatique", proc.stdout)
        # `ameesh cost` : compte actif et jauges par compte
        os.makedirs(os.path.join(self.cfg.state_dir, "cxa"), exist_ok=True)
        with open(os.path.join(self.cfg.state_dir, "cxa", "tool"), "w",
                  encoding="utf-8") as fh:
            fh.write("codex\n")
        proc = self.mesh("cost", "report", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("codex/secondaire", proc.stdout)
        self.assertIn("comptes (hôte", proc.stdout)
        proc = self.mesh("cost", "report", "--json", env=env)
        ligne = json.loads(proc.stdout)[0]
        self.assertEqual(ligne["account"], "secondaire")
        self.assertEqual({c["account"] for c in ligne["accounts"]}, {"primaire", "secondaire"})
        primaire = [c for c in ligne["accounts"] if c["account"] == "primaire"][0]
        self.assertAlmostEqual(primaire["gauges"][0]["used"], 0.40)

    def test_cli_configuration_invalide(self):
        env = self.env(AMEESH_CONFIG=self._config({"claude": [{"name": "a"}, {"name": "a"}]}))
        proc = self.mesh("accounts", "list", env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("en double", proc.stderr)


if __name__ == "__main__":
    unittest.main()
