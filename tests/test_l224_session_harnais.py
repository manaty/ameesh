# SPDX-License-Identifier: AGPL-3.0-only
"""L224 : l'id de session d'un agent ne vient que de son propre harnais, et un
id inconnu du stockage du harnais n'est jamais repris.

Incident du 2026-10-11 : mesh-design (harnais claude) lance `codex exec` pour
une relecture ; le processus hérite de l'environnement du tour
(AGENT_MAIL_NAME, AMEESH_RUNNER_ID, AMEESH_LEASE_EPOCH…), et le hook
agent-mail de codex se présente comme mesh-design. ameesh retient l'id codex
(01a129ab…) comme session de mesh-design ; chaque tour suivant échoue
(`claude --resume` sur un id que Claude Code ne connaît pas), puis cinq échecs
rapides et l'arrêt. Postgres réel ; faux harnais `claude`.
"""
from __future__ import annotations

import dataclasses
import errno
import json
import os
import tempfile
import unittest
from unittest import mock

from ameesh import accounts, adapters, cli, mail, registry, reprise
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase

CODEX = "01a129ab-3aeb-76a2-beaf-c5829c01f373"
CLAUDE = "0ecd16ab-c729-44ef-80b4-5cfd20c789ad"


class HarnaisDuHookTest(unittest.TestCase):
    def test_harnais_etranger(self):
        with mock.patch.dict(os.environ, {"AMEESH_HARNESS": "claude"}):
            self.assertEqual(cli.hook_foreign_harness("codex"), "claude")
            self.assertEqual(cli.hook_foreign_harness("claude"), "")
        with mock.patch.dict(os.environ, {"AMEESH_HARNESS": ""}):
            # session hors exécuteur (ou exécuteur d'avant L224) : pas de contrôle
            self.assertEqual(cli.hook_foreign_harness("codex"), "")


class _Base(PgTestCase):
    def _worker(self, name: str, harness: str = "claude", comptes: dict | None = None):
        cfg = (dataclasses.replace(self.cfg, accounts=comptes) if comptes is not None
               else self.cfg)
        runner = Runner(cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, harness, cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return runner, worker


class HookEtrangerSousBailTest(_Base):
    """Le hook de codex lancé pendant un tour claude : ni inscription, ni
    courrier remis ; le refus est journalisé une fois."""

    def setUp(self) -> None:
        super().setUp()
        _runner, self.worker = self._worker("md")
        registry.set_session(self.db, "md", CLAUDE)
        mail.send(self.db, "orchestrateur", "md", "Pour la session claude.")

    def _hook(self, harness: str, event: str, session: str):
        env = self.env(AGENT_MAIL_NAME="md", AMEESH_RUNNER_ID=self.worker.runner.runner_id,
                       AMEESH_LEASE_EPOCH=str(self.worker.epoch), AMEESH_HARNESS="claude")
        return self.cli("hook", harness, env=env, stdin=json.dumps(
            {"hook_event_name": event, "session_id": session, "cwd": self.tmp}))

    def _non_remis(self) -> int:
        return self.db.query(
            "SELECT count(*)::int AS n FROM agent_mailbox "
            "WHERE recipient = 'md' AND delivered_at IS NULL")[0]["n"]

    def _refus_au_fil(self) -> int:
        marques = os.path.join(self.state, "hooks")
        return len([f for f in os.listdir(marques) if f.startswith("md.refus-")]
                   ) if os.path.isdir(marques) else 0

    def test_hook_codex_refuse(self):
        for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"):
            with self.subTest(event=event):
                proc = self._hook("codex", event, CODEX)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertIn("hook codex refused for md", proc.stderr)
                self.assertEqual(self._non_remis(), 1)
        self.assertEqual(registry.get(self.db, "md")["session_id"], CLAUDE)
        self.assertEqual(self._refus_au_fil(), 1)  # une fois par (harnais, session)

    def test_le_hook_du_harnais_de_l_agent_sert_toujours(self):
        self._hook("codex", "PostToolUse", CODEX)
        proc = self._hook("claude", "PostToolUse", CLAUDE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        contexte = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Pour la session claude.", contexte)
        self.assertEqual(self._non_remis(), 0)


class RepriseVerifieeTest(_Base):
    """Avant un --resume, l'id est cherché dans le stockage de sessions du
    harnais (Claude Code : `<CLAUDE_CONFIG_DIR>/projects/<dossier>/<id>.jsonl`)."""

    def setUp(self) -> None:
        super().setUp()
        self.home = os.path.join(self.tmp, "claude-home")
        os.makedirs(os.path.join(self.home, "projects", "-work-md"))

    def _session_sur_disque(self, session: str) -> None:
        with open(os.path.join(self.home, "projects", "-work-md", session + ".jsonl"),
                  "w", encoding="utf-8") as fh:
            fh.write("{}\n")

    def _env(self):
        return mock.patch.dict(os.environ, {
            "AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
            "CLAUDE_CONFIG_DIR": self.home, "AMEESH_SESSION_STORE_CHECK": "1"})

    def _tour(self, worker) -> bool:
        return worker.run_turn({"kind": "prompt", "prompt": "travail", "ids": []})

    def _reprises(self) -> list[str | None]:
        out = []
        for tour in self.turns():
            argv = tour["argv"]
            out.append(argv[argv.index("--resume") + 1] if "--resume" in argv else None)
        return out

    def test_id_inconnu_jamais_repris_brief_de_reprise(self):
        _runner, worker = self._worker("md-neuve")
        registry.set_session(self.db, "md-neuve", CODEX)
        worker.agent = registry.get(self.db, "md-neuve")
        with self._env():
            self.assertTrue(self._tour(worker))
        self.assertEqual(self._reprises(), [None])
        self.assertIn("session %s unknown to the harness" % CODEX,
                      " ".join(self.turns()[-1]["argv"]))
        self.assertEqual(worker.resume_origin, "ameesh")
        self.assertFalse(worker.fast_failure)
        # la session neuve du flux devient celle de l'agent
        self.assertEqual(registry.get(self.db, "md-neuve")["session_id"], "claude-sess-1")

    def test_brief_non_ecrit_sur_disque_il_ouvre_quand_meme_le_tour(self):
        # #389 r3 : la session refusée ne se garde pas ; un résumé en attente
        # non écrit (EACCES) reste en mémoire et ouvre le tour
        _runner, worker = self._worker("md-neuve")
        registry.set_session(self.db, "md-neuve", CODEX)
        worker.agent = registry.get(self.db, "md-neuve")
        reel = os.replace

        def replace(src, dst, *a, **kw):
            if str(dst) == worker._path(worker.RESUME_PENDING):
                raise OSError(errno.EACCES, "Permission denied")
            return reel(src, dst, *a, **kw)
        with self._env(), mock.patch("os.replace", side_effect=replace):
            self.assertTrue(self._tour(worker))
        self.assertEqual(self._reprises(), [None])
        self.assertIn("session %s unknown to the harness" % CODEX,
                      " ".join(self.turns()[-1]["argv"]))
        self.assertFalse(os.path.exists(worker._path(worker.RESUME_PENDING)))

    def test_retour_a_la_derniere_session_principale_connue(self):
        _runner, worker = self._worker("md-mere")
        self._session_sur_disque(CLAUDE)
        worker.write_session_file(CLAUDE)  # écrit depuis le flux du harnais
        registry.set_session(self.db, "md-mere", CODEX)
        worker.agent = registry.get(self.db, "md-mere")
        with self._env():
            self.assertTrue(self._tour(worker))
        self.assertEqual(self._reprises(), [CLAUDE])
        self.assertEqual(registry.get(self.db, "md-mere")["session_id"], CLAUDE)

    def test_session_locale_inconnue_elle_aussi(self):
        _runner, worker = self._worker("md-locale")
        worker.write_session_file("claude-perdue")
        registry.set_session(self.db, "md-locale", CODEX)
        worker.agent = registry.get(self.db, "md-locale")
        with self._env():
            self.assertTrue(self._tour(worker))
        self.assertEqual(self._reprises(), [None])

    def test_session_connue_reprise(self):
        _runner, worker = self._worker("md-ok")
        self._session_sur_disque(CLAUDE)
        registry.set_session(self.db, "md-ok", CLAUDE)
        worker.agent = registry.get(self.db, "md-ok")
        with self._env():
            self.assertTrue(self._tour(worker))
        self.assertEqual(self._reprises(), [CLAUDE])

    def test_le_tour_pose_le_harnais_pour_ses_hooks(self):
        _runner, worker = self._worker("md-env")
        with self._env():
            self.assertTrue(self._tour(worker))
        self.assertEqual(self.turns()[-1]["env"].get("AMEESH_HARNESS"), "claude")


def _dossier(chemin: str, creds: str) -> str:
    """Dossier de compte (L30) : fichier d'identifiants vide, seule sa
    présence est vérifiée."""
    os.makedirs(chemin, mode=0o700, exist_ok=True)
    os.chmod(chemin, 0o700)
    with open(os.path.join(chemin, creds), "w", encoding="utf-8"):
        pass
    os.chmod(os.path.join(chemin, creds), 0o600)
    return chemin


def _session_claude(home: str, session: str) -> None:
    os.makedirs(os.path.join(home, "projects", "-work"), exist_ok=True)
    with open(os.path.join(home, "projects", "-work", session + ".jsonl"), "w",
              encoding="utf-8") as fh:
        fh.write("{}\n")


def _session_codex(home: str, session: str) -> None:
    jour = os.path.join(home, "sessions", "2026", "10", "11")
    os.makedirs(jour, exist_ok=True)
    with open(os.path.join(jour, "rollout-2026-10-11T03-00-00-%s.jsonl" % session), "w",
              encoding="utf-8") as fh:
        fh.write("{}\n")


class _Verifie(_Base):
    """Contrôle actif (verdict codex #183, P2) : comptes, bascule, codex,
    dossier posé par le descripteur, stockage introuvable."""

    def _tour(self, worker, compte: str | None = None, **env) -> bool:
        if compte is not None:
            harness = worker.agent["harness"]
            worker._account_override = accounts.by_name(
                accounts.profiles(worker.cfg, harness), compte)
        base = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
                "AMEESH_SESSION_STORE_CHECK": "1"}
        base.update(env)
        with mock.patch.dict(os.environ, base):
            return worker.run_turn({"kind": "prompt", "prompt": "travail", "ids": []})

    def _reprise(self, flag: str = "--resume") -> str | None:
        argv = self.turns()[-1]["argv"]
        return argv[argv.index(flag) + 1] if flag in argv else None

    def _avec_session(self, worker, session: str) -> None:
        registry.set_session(self.db, worker.name, session)
        worker.agent = registry.get(self.db, worker.name)


class DossierDuTourTest(_Verifie):
    def test_dossier_pose_par_le_descripteur(self):
        # P1 : sans compte déclaré, le descripteur pose CLAUDE_CONFIG_DIR ; les
        # sessions sont là, pas dans le dossier de l'exécuteur
        vide = os.path.join(self.tmp, "claude-executeur")
        os.makedirs(os.path.join(vide, "projects"))
        descripteur = os.path.join(self.tmp, "claude-descripteur")
        _session_claude(descripteur, CLAUDE)
        _runner, worker = self._worker("md-desc")
        self._avec_session(worker, CLAUDE)
        env_harnais = adapters.HarnessAdapter.env
        with mock.patch.object(adapters.HarnessAdapter, "env", lambda a: dict(
                env_harnais(a), CLAUDE_CONFIG_DIR=descripteur)):
            self.assertTrue(self._tour(worker, CLAUDE_CONFIG_DIR=vide))
        self.assertEqual(self._reprise(), CLAUDE)
        self.assertEqual(self.turns()[-1]["compte"]["CLAUDE_CONFIG_DIR"], descripteur)

    def test_stockage_introuvable_session_gardee(self):
        # dans le doute, la session est gardée (reprise ratée rattrapée)
        sans_projets = os.path.join(self.tmp, "claude-sans-projets")
        os.makedirs(sans_projets)
        _runner, worker = self._worker("md-doute")
        self._avec_session(worker, CODEX)
        self.assertTrue(self._tour(worker, CLAUDE_CONFIG_DIR=sans_projets))
        self.assertEqual(self._reprise(), CODEX)


class HomeDuTourTest(_Verifie):
    def test_tilde_resolu_avec_le_home_du_tour(self):
        # verdict codex #183 (2e relecture, P1) : le descripteur pose HOME sans
        # CLAUDE_CONFIG_DIR ; `~/.claude` se lit sous ce HOME, pas sous celui de
        # l'exécuteur — même si ce dernier a un stockage (vide)
        executeur = os.path.join(self.tmp, "home-executeur")
        os.makedirs(os.path.join(executeur, ".claude", "projects"))
        tour = os.path.join(self.tmp, "home-tour")
        _session_claude(os.path.join(tour, ".claude"), CLAUDE)
        _runner, worker = self._worker("md-home")
        self._avec_session(worker, CLAUDE)
        env_harnais = adapters.HarnessAdapter.env
        with mock.patch.object(adapters.HarnessAdapter, "env",
                               lambda a: dict(env_harnais(a), HOME=tour)), \
                mock.patch.dict(os.environ, {"HOME": executeur, "AMEESH_BIN_DIR": FAKEBIN,
                                            "AMEESH_SESSION_STORE_CHECK": "1"}):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            prédicat = worker._session_connue("claude", None, worker._adapter())
            self.assertTrue(prédicat(CLAUDE))
            self.assertFalse(prédicat(CODEX))

    def test_expanduser_du_tour(self):
        from ameesh.runner import _expanduser_in
        self.assertEqual(_expanduser_in("~/.claude", {"HOME": "/tour"}), "/tour/.claude")
        self.assertEqual(_expanduser_in("~", {"HOME": "/tour/"}), "/tour")
        self.assertEqual(_expanduser_in("~/.dsh", {"HOME": "/"}), "/.dsh")
        self.assertEqual(_expanduser_in("/abs/.codex", {"HOME": "/tour"}), "/abs/.codex")


@unittest.skipIf(os.geteuid() == 0, "root lit un dossier 000")
class RechercheIncompleteTest(_Verifie):
    """Verdict codex #183 (2e relecture, P2) : un stockage illisible n'établit
    pas « introuvable » ; la session est gardée."""

    def _illisible(self, chemin: str) -> None:
        os.chmod(chemin, 0)
        self.addCleanup(os.chmod, chemin, 0o700)

    def test_find_session_strict(self):
        claude = os.path.join(self.tmp, "claude-illisible")
        _session_claude(claude, CLAUDE)
        self._illisible(os.path.join(claude, "projects", "-work"))
        self.assertIsNone(reprise.find_session("claude", claude, CLAUDE))
        with self.assertRaises(reprise.SessionSearchIncomplete):
            reprise.find_session("claude", claude, CLAUDE, strict=True)
        codex = os.path.join(self.tmp, "codex-illisible")
        _session_codex(codex, CodexTest.FIL)
        self._illisible(os.path.join(codex, "sessions", "2026"))
        self.assertIsNone(reprise.find_session("codex", codex, CodexTest.FIL))
        with self.assertRaises(reprise.SessionSearchIncomplete):
            reprise.find_session("codex", codex, CodexTest.FIL, strict=True)
        racine = os.path.join(self.tmp, "dsh-illisible")
        os.makedirs(os.path.join(racine, "sessions"))
        self._illisible(os.path.join(racine, "sessions"))
        with self.assertRaises(reprise.SessionSearchIncomplete):
            reprise.find_session("deepseek", racine, "abc", strict=True)

    def test_claude_illisible_session_gardee(self):
        home = os.path.join(self.tmp, "claude-tour-illisible")
        _session_claude(home, CLAUDE)
        self._illisible(os.path.join(home, "projects", "-work"))
        _runner, worker = self._worker("md-illisible")
        self._avec_session(worker, CLAUDE)
        self.assertTrue(self._tour(worker, CLAUDE_CONFIG_DIR=home))
        self.assertEqual(self._reprise(), CLAUDE)

    def test_codex_illisible_session_gardee(self):
        home = os.path.join(self.tmp, "codex-tour-illisible")
        _session_codex(home, CodexTest.FIL)
        self._illisible(os.path.join(home, "sessions", "2026"))
        _runner, worker = self._worker("cx-illisible", harness="codex")
        self._avec_session(worker, CodexTest.FIL)
        self.assertTrue(self._tour(worker, CODEX_HOME=home))
        self.assertEqual(self._reprise("resume"), CodexTest.FIL)


class _EntreeEIO:
    """Un `DirEntry` dont `is_dir()` échoue en EIO (métadonnées illisibles)."""

    def __init__(self, entry) -> None:
        self._entry = entry
        self.name, self.path = entry.name, entry.path

    def is_dir(self, **_kw) -> bool:
        raise OSError(errno.EIO, "Input/output error", self.path)

    def is_symlink(self) -> bool:
        return self._entry.is_symlink()


class _ScandirEIO:
    """`os.scandir` où l'entrée `cible` lève EIO sur `is_dir()`."""

    def __init__(self, cible: str) -> None:
        self.cible, self._scandir = cible, os.scandir

    def __call__(self, path):
        return _ScandirEIOIter(self._scandir(path), self.cible)


class _ScandirEIOIter:
    def __init__(self, it, cible: str) -> None:
        self._it, self.cible = it, cible

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self._it.close()

    def __iter__(self):
        for entry in self._it:
            yield _EntreeEIO(entry) if entry.path == self.cible else entry


def _eio_sur(chemin: str):
    return mock.patch.object(reprise.os, "scandir", _ScandirEIO(chemin))


class RechercheEIOTest(unittest.TestCase):
    """Verdict codex #183 (3e relecture, P2) : une erreur de `is_dir()` sur
    un dossier de date codex qui tient une session valide n'établit pas
    « introuvable »."""

    def test_codex_eio_sur_dossier_de_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            _session_codex(tmp, CodexTest.FIL)
            self.assertTrue(reprise.find_session("codex", tmp, CodexTest.FIL, strict=True))
            jour = os.path.join(tmp, "sessions", "2026", "10", "11")
            with _eio_sur(jour):
                with self.assertRaises(reprise.SessionSearchIncomplete):
                    reprise.find_session("codex", tmp, CodexTest.FIL, strict=True)
                self.assertIsNone(reprise.find_session("codex", tmp, CodexTest.FIL))


class CodexEIOSessionGardeeTest(_Verifie):
    def test_codex_eio_session_gardee(self):
        home = os.path.join(self.tmp, "codex-tour-eio")
        _session_codex(home, CodexTest.FIL)
        _runner, worker = self._worker("cx-eio", harness="codex")
        self._avec_session(worker, CodexTest.FIL)
        with _eio_sur(os.path.join(home, "sessions", "2026", "10", "11")):
            self.assertTrue(self._tour(worker, CODEX_HOME=home))
        self.assertEqual(self._reprise("resume"), CodexTest.FIL)


class ComptesClaudeTest(_Verifie):
    def setUp(self) -> None:
        super().setUp()
        self.a = _dossier(os.path.join(self.tmp, "comptes", "claude-1"), ".credentials.json")
        self.b = _dossier(os.path.join(self.tmp, "comptes", "claude-2"), ".credentials.json")
        for home in (self.a, self.b):
            os.makedirs(os.path.join(home, "projects"), exist_ok=True)
        self.comptes = {"claude": [{"name": "primaire", "path": self.a},
                                   {"name": "secondaire", "path": self.b}]}

    def test_compte_secondaire_session_connue(self):
        _session_claude(self.b, CLAUDE)
        _runner, worker = self._worker("md-sec", comptes=self.comptes)
        self._avec_session(worker, CLAUDE)
        self.assertTrue(self._tour(worker, "secondaire"))
        self.assertEqual(self._reprise(), CLAUDE)
        self.assertEqual(self.turns()[-1]["compte"]["CLAUDE_CONFIG_DIR"], self.b)

    def test_compte_secondaire_session_inconnue_partout(self):
        _session_claude(self.a, CLAUDE)
        _runner, worker = self._worker("md-sec-x", comptes=self.comptes)
        self._avec_session(worker, CODEX)
        self.assertTrue(self._tour(worker, "secondaire"))
        self.assertIsNone(self._reprise())
        self.assertEqual(registry.get(self.db, "md-sec-x")["session_id"], "claude-sess-1")

    def test_bascule_de_compte_session_de_l_autre_compte_gardee(self):
        # session ouverte sous le primaire, tour sous le secondaire : elle
        # existe sous un compte déclaré — jamais effacée (doute)
        _session_claude(self.a, CLAUDE)
        _runner, worker = self._worker("md-bascule", comptes=self.comptes)
        self._avec_session(worker, CLAUDE)
        self.assertTrue(self._tour(worker, "secondaire"))
        self.assertEqual(self._reprise(), CLAUDE)


class CodexTest(_Verifie):
    FIL = "019a0000-1111-7222-8333-444455556666"

    def test_fil_codex_connu_repris(self):
        home = os.path.join(self.tmp, "codex-home")
        _session_codex(home, self.FIL)
        _runner, worker = self._worker("cx-ok", harness="codex")
        self._avec_session(worker, self.FIL)
        self.assertTrue(self._tour(worker, CODEX_HOME=home))
        self.assertEqual(self._reprise("resume"), self.FIL)

    def test_fil_codex_inconnu_jamais_repris(self):
        home = os.path.join(self.tmp, "codex-home-2")
        _session_codex(home, self.FIL)
        _runner, worker = self._worker("cx-x", harness="codex")
        self._avec_session(worker, CLAUDE)  # un id claude retenu à tort
        self.assertTrue(self._tour(worker, CODEX_HOME=home))
        self.assertIsNone(self._reprise("resume"))

    def test_compte_codex_secondaire(self):
        a = _dossier(os.path.join(self.tmp, "comptes", "codex-1"), "auth.json")
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), "auth.json")
        os.makedirs(os.path.join(a, "sessions"))
        _session_codex(b, self.FIL)
        comptes = {"codex": [{"name": "primaire", "path": a},
                             {"name": "secondaire", "path": b}]}
        _runner, worker = self._worker("cx-sec", harness="codex", comptes=comptes)
        self._avec_session(worker, self.FIL)
        self.assertTrue(self._tour(worker, "secondaire"))
        self.assertEqual(self._reprise("resume"), self.FIL)
        # un id claude retenu à tort : écarté, retour au dernier fil principal
        # du flux (fichier local), qui existe sous le compte du tour
        self._avec_session(worker, CLAUDE)
        self.assertTrue(self._tour(worker, "secondaire"))
        self.assertEqual(self._reprise("resume"), self.FIL)
        self.assertEqual(registry.get(self.db, "cx-sec")["session_id"], self.FIL)


def _session_dsh(home: str, session: str) -> None:
    os.makedirs(os.path.join(home, "sessions", "-work", "session-" + session))


def _dossier_dsh(home: str, ident: str) -> None:
    """Le dossier tel que dsh le nomme pour un id REÇU, sans préfixe ajouté."""
    os.makedirs(os.path.join(home, "sessions", "-work", ident))


class DeepSeekTest(_Verifie):
    SESSION = "dsh-0a1b2c3d"
    #: L'id RÉEL du flux dsh porte déjà le préfixe `session-` (#724, #786).
    REEL = "session-b126155c-0d1e-4f2a-9b3c-5d6e7f809a1b"

    def _reprises(self) -> list[str | None]:
        out = []
        for tour in self.turns():
            argv = tour["argv"]
            out.append(argv[argv.index("--session-id") + 1]
                       if "--session-id" in argv else None)
        return out

    def test_session_dsh_connue_reprise(self):
        home = os.path.join(self.tmp, "dsh-home")
        _session_dsh(home, self.SESSION)
        _runner, worker = self._worker("ds-ok", harness="deepseek")
        self._avec_session(worker, self.SESSION)
        self.assertTrue(self._tour(worker, DSH_HOME=home))
        self.assertEqual(self._reprise("--session-id"), self.SESSION)

    def test_session_dsh_reelle_connue_reprise(self):
        """#786 : le dossier d'un id réel est retrouvé tel quel ; construit en
        `session-session-…`, il manquait et le tour repartait à vide."""
        home = os.path.join(self.tmp, "dsh-reel")
        _dossier_dsh(home, self.REEL)
        _runner, worker = self._worker("ds-reel", harness="deepseek")
        self._avec_session(worker, self.REEL)
        self.assertTrue(self._tour(worker, DSH_HOME=home))
        self.assertEqual(self._reprise("--session-id"), self.REEL)

    def test_le_tour_du_flux_reprend_l_id_reel(self):
        """#786 de bout en bout : l'id annoncé par le flux entre au registre,
        puis le tour suivant le reprend (dossier écrit par dsh pendant le
        tour) ; sans #724, le 2e tour perd la session et repart à vide."""
        home = os.path.join(self.tmp, "dsh-flux")
        _runner, worker = self._worker("ds-flux", harness="deepseek")
        with mock.patch.dict(os.environ, {"AMEESH_TEST_DSH_SESSION": self.REEL}):
            self.assertTrue(self._tour(worker, DSH_HOME=home))
            self.assertEqual(registry.get(self.db, "ds-flux")["session_id"], self.REEL)
            _dossier_dsh(home, self.REEL)
            worker.agent = registry.get(self.db, "ds-flux")
            self.assertTrue(self._tour(worker, DSH_HOME=home))
        self.assertEqual(self._reprises(), [None, self.REEL])

    def test_session_dsh_inconnue_jamais_reprise(self):
        home = os.path.join(self.tmp, "dsh-home-2")
        _session_dsh(home, self.SESSION)
        _runner, worker = self._worker("ds-x", harness="deepseek")
        self._avec_session(worker, CLAUDE)  # un id claude retenu à tort
        self.assertTrue(self._tour(worker, DSH_HOME=home))
        self.assertIsNone(self._reprise("--session-id"))


if __name__ == "__main__":
    unittest.main()
