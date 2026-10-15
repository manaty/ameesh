# SPDX-License-Identifier: AGPL-3.0-only
"""L233b : les lots `intake` et `build` d'un agent Claude non critique en
pause budget passent à un agent DeepSeek libre du même projet."""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import budget, handoff, notify, storage, work

from .test_l119_backlog import HEURE, _Base, _seuils

GIT_OK = {"ok": True, "reason": "", "paths": []}


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


class LotGitTest(unittest.TestCase):
    """L'état git d'un lot, sur un vrai dépôt et son dépôt distant."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="l233b-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        origin = os.path.join(self.tmp, "origin.git")
        self.repo = os.path.join(self.tmp, "work")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
        subprocess.run(["git", "clone", "-q", origin, self.repo], check=True,
                       capture_output=True)
        self.write("README.md", "x\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        _git(self.repo, "push", "-q", "origin", "HEAD:main")
        _git(self.repo, "fetch", "-q", "origin")
        _git(self.repo, "checkout", "-q", "-b", "lot/7")
        self.write("src/a.py", "a = 1\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "lot 7")
        _git(self.repo, "push", "-q", "origin", "lot/7")
        self.source = {"name": "claude1", "host": "h1", "cwd": self.repo}

    def write(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def check(self, state="build", branch="lot/7", host="h1", source=None):
        item = {"id": 7, "state": state, "branch": branch}
        return handoff.lot_git(item, source or self.source, host=host, worktrees=[])

    def test_branche_poussee_et_worktree_propre(self):
        self.assertEqual(self.check(), {"ok": True, "reason": "", "paths": ["src/a.py"]})

    def test_changements_non_commites(self):
        self.write("src/a.py", "a = 2\n")
        out = self.check()
        self.assertFalse(out["ok"])
        self.assertIn("uncommitted changes", out["reason"])
        # un lot intake se reprend toujours
        self.assertTrue(self.check(state="intake")["ok"])

    def test_commits_non_pousses(self):
        self.write("src/b.py", "b = 1\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "local")
        out = self.check()
        self.assertFalse(out["ok"])
        self.assertIn("commits not pushed", out["reason"])

    def test_pousse_ailleurs_que_sur_la_branche_du_lot(self):
        # codex r1 (#409) : HEAD poussé sur une autre branche distante, celle
        # du lot plus ancienne : le preneur repartirait d'une pointe périmée
        self.write("src/b.py", "b = 1\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "local")
        _git(self.repo, "push", "-q", "origin", "HEAD:backup")
        _git(self.repo, "fetch", "-q", "origin")
        out = self.check()
        self.assertFalse(out["ok"])
        self.assertIn("not on origin/lot/7", out["reason"])
        # poussé sur la branche du lot : repris
        _git(self.repo, "push", "-q", "origin", "lot/7")
        self.assertTrue(self.check()["ok"])
        # la branche locale du lot compte aussi quand HEAD est ailleurs
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "branch", "-f", "lot/7", "HEAD@{1}")
        _git(self.repo, "checkout", "-q", "-b", "autre")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "x")
        _git(self.repo, "branch", "-f", "lot/7", "autre")
        _git(self.repo, "push", "-q", "origin", "autre")
        _git(self.repo, "fetch", "-q", "origin")
        out = self.check()
        self.assertIn("branch lot/7 in %s not on origin/lot/7" % self.repo, out["reason"])

    def test_worktree_du_lot_detache(self):
        # codex r2 (#409) : un worktree du lot détaché sur B, poussé ailleurs,
        # alors que lot/7 (local et distant) reste sur A
        other = os.path.join(self.tmp, "wt")
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "worktree", "add", "-q", "--detach", other, "lot/7")
        with open(os.path.join(other, "b.py"), "w") as fh:
            fh.write("b = 1\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-q", "-m", "B")
        _git(other, "push", "-q", "origin", "HEAD:refs/heads/backup")
        _git(self.repo, "fetch", "-q", "origin")
        item = {"id": 7, "state": "build", "branch": "lot/7"}
        tracked = [{"agent": "claude1", "lot": "7", "branch": "", "path": other}]
        out = handoff.lot_git(item, self.source, host="h1", worktrees=tracked)
        self.assertEqual(out["reason"], "HEAD in %s not on origin/lot/7" % other)
        _git(other, "push", "-q", "origin", "HEAD:refs/heads/lot/7")
        _git(self.repo, "fetch", "-q", "origin")
        self.assertTrue(handoff.lot_git(item, self.source, host="h1", worktrees=tracked)["ok"])
        # un worktree sur la branche du lot, connu de git seul : son HEAD aussi
        _git(other, "checkout", "-q", "-B", "lot/7", "origin/lot/7")
        _git(other, "commit", "-q", "--allow-empty", "-m", "C")
        _git(other, "push", "-q", "origin", "HEAD:refs/heads/backup2")
        _git(self.repo, "fetch", "-q", "origin")
        out = handoff.lot_git(item, self.source, host="h1", worktrees=[])
        self.assertIn("not on origin/lot/7", out["reason"])

    def test_branche_non_poussee_ou_absente(self):
        out = self.check(branch="lot/8")
        self.assertEqual((out["ok"], out["reason"]), (False, "branch lot/8 not pushed"))
        self.assertEqual(self.check(state="intake", branch="lot/8"),
                         {"ok": True, "reason": "", "paths": []})
        self.assertEqual(self.check(branch="")["reason"], "no branch")
        self.assertTrue(self.check(state="intake", branch="")["ok"])

    def test_worktree_suivi_du_lot(self):
        # le dossier de travail est propre, le worktree du lot ne l'est pas
        other = os.path.join(self.tmp, "wt")
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "worktree", "add", "-q", other, "lot/7")
        with open(os.path.join(other, "src", "a.py"), "w") as fh:
            fh.write("a = 3\n")
        item = {"id": 7, "state": "build", "branch": "lot/7"}
        tracked = [{"agent": "claude1", "lot": "7", "branch": "lot/7", "path": other}]
        out = handoff.lot_git(item, self.source, host="h1", worktrees=tracked)
        self.assertIn("uncommitted changes in %s" % other, out["reason"])
        # le worktree d'un autre agent ne compte pas
        tracked[0]["agent"] = "dev2"
        self.assertTrue(handoff.lot_git(item, self.source, host="h1", worktrees=tracked)["ok"])

    def test_autre_hote(self):
        out = self.check(host="h2")
        self.assertFalse(out["ok"])
        self.assertIn("not readable from h2", out["reason"])

    def test_classe_de_revue(self):
        canon = types.SimpleNamespace(federation={"review_policies": {"risk_classes": {
            "default": "normal",
            "rules": [{"paths": ["src/ameesh/migrations/**"], "class": "sensitive"}]}}})
        self.assertEqual(handoff.sensitive([], [canon]), "")
        self.assertEqual(handoff.sensitive(["src/a.py"], [canon]), "")
        self.assertIn("src/ameesh/migrations/0099_x.sql",
                      handoff.sensitive(["src/a.py", "src/ameesh/migrations/0099_x.sql"],
                                        [canon]))
        bad = types.SimpleNamespace(federation={"review_policies": {"risk_classes": {
            "default": "énorme"}}})
        self.assertEqual(handoff.sensitive(["src/a.py"], [bad]), "review policy unreadable")

    def test_classe_de_revue_de_chaque_canon(self):
        # codex r1 (#409) : un chemin sensible pour un canon secondaire l'est
        def canon(rules):
            return types.SimpleNamespace(federation={"review_policies": {"risk_classes": {
                "default": "normal", "rules": rules}}})
        principal = canon([])
        secondaire = canon([{"paths": ["secrets/**"], "class": "sensitive"}])
        self.assertEqual(handoff.sensitive(["secrets/k"], [principal]), "")
        self.assertIn("secrets/k", handoff.sensitive(["secrets/k"], [principal, secondaire]))


class _Handoff(_Base):
    def setUp(self):
        super().setUp()
        self.orch = "mesh-design"

    def pause(self, name):
        self.db.execute("UPDATE agent_registry SET status = 'blocked', status_text = %s,"
                        " status_since = now() - interval '10 minutes' WHERE name = %s",
                        (budget.PAUSE_PREFIX + "mesh cap reached", name))

    def lot(self, title, assignee="claude1", *, state="intake", app="nexlink", actor=None,
            **kw):
        kw.setdefault("force", "test: several lots for one agent (#540)")
        # #1021 : l'API refuse désormais d'attribuer un lot à un agent en tenue
        # budget/rythme. Ces témoins L233b portent sur un lot attribué AVANT la
        # pause de son agent — l'état que la reprise doit justement traiter :
        # l'assigné s'écrit donc directement, comme la création le faisait.
        item = work.add(self.db, title=title, app=app, actor=actor or self.orch, **kw)
        if assignee:
            self.db.execute("UPDATE work_items SET assignee = %s WHERE id = %s",
                            (assignee, item["id"]))
            item = work.get(self.db, item["id"])
        if state != "intake":
            self.db.execute("UPDATE work_items SET state = %s WHERE id = %s",
                            (state, item["id"]))
        return item

    def force_assign(self, item_id, assignee, *, actor=""):
        """Réattribution hors garde #1021, pour le même motif : une attribution
        antérieure à la pause de l'agent (l'API la refuse désormais)."""
        item = work.get(self.db, item_id)
        return storage.of(self.db).work.assign(
            item_id, assignee, current=item.get("assignee"),
            note="assigned to %s (before: %s)" % (assignee,
                                                  item.get("assignee") or "nobody"),
            actor=actor)

    def hand(self, **kw):
        kw.setdefault("now", self.now)
        kw.setdefault("canons", [])
        kw.setdefault("declared", ())
        kw.setdefault("orchestras", [self.orch])
        kw.setdefault("budget_check", lambda agent: "")
        return handoff.budget_handoff(self.cfg, self.db, **kw)

    def assignee(self, item):
        return work.get(self.db, item["id"])["assignee"]

    def mails(self):
        return self.db.query("SELECT sender, recipient, kind, work_item_id, body, payload"
                             " FROM agent_mailbox ORDER BY id")

    def team(self):
        self.agent("claude1", team="nexlink")
        self.pause("claude1")
        self.agent("deepseek1", "deepseek", team="nexlink")


class HandoffTest(_Handoff):
    def test_lots_repris_avec_note_et_courriers(self):
        self.team()
        self.agent("deepseek2", "deepseek", team="nexlink", since_s=HEURE)
        intake = self.lot("export", priority=1)
        build = self.lot("import", state="build", branch="lot/import")
        with mock.patch.object(handoff, "lot_git", return_value=GIT_OK) as checked:
            out = self.hand()
        # le plus long repos d'abord, un lot par preneur
        self.assertEqual([(m["agent"], m["item"]["id"], m["previous"]) for m in out],
                         [("deepseek1", intake["id"], "claude1"),
                          ("deepseek2", build["id"], "claude1")])
        self.assertEqual(checked.call_args.kwargs["host"], self.cfg.host)
        self.assertEqual((self.assignee(intake), self.assignee(build)),
                         ("deepseek1", "deepseek2"))
        note = work.events(self.db, build["id"])[0]
        self.assertTrue(note["note"].startswith(handoff.HANDOFF_NOTE))
        self.assertIn("rien ne revient seul", note["note"])
        self.assertIn("branche lot/import", note["note"])
        self.assertEqual(note["actor"], "ameesh")
        mails = self.mails()
        self.assertEqual([(m["recipient"], m["kind"], m["work_item_id"]) for m in mails],
                         [("deepseek1", "event", str(intake["id"])),
                          ("claude1", "event", str(intake["id"])),
                          ("deepseek2", "event", str(build["id"])),
                          ("claude1", "event", str(build["id"]))])
        self.assertIn("codex review stays required", mails[2]["body"])
        self.assertIn("pushed branch lot/import", mails[2]["body"])
        self.assertIn("Do not push to lot/import", mails[3]["body"])
        payload = mails[0]["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        self.assertEqual(payload["budget_handoff"],
                         {"lot": intake["id"], "from": "claude1", "to": "deepseek1"})
        # les preneurs ne sont plus libres : plus rien au passage suivant
        self.assertEqual(self.hand(), [])

    def test_a_blanc(self):
        self.team()
        intake = self.lot("export")
        out = self.hand(dry_run=True)
        self.assertEqual([(m["agent"], m["item"]["id"]) for m in out],
                         [("deepseek1", intake["id"])])
        self.assertEqual(self.assignee(intake), "claude1")
        self.assertEqual(self.mails(), [])

    def test_agent_critique_ou_pas_en_pause(self):
        self.team()
        intake = self.lot("export")
        self.assertEqual(self.hand(declared=("claude1",)), [])
        # déclaré critique par la configuration
        cfg = dataclasses.replace(self.cfg, critical_agents=("claude1",))
        self.assertEqual(handoff.budget_handoff(
            cfg, self.db, now=self.now, canons=[], declared=(), orchestras=[],
            budget_check=lambda a: ""), [])
        # en pause pour une autre raison, ou pas en pause
        self.db.execute("UPDATE agent_registry SET status_text = 'waiting' WHERE name = %s",
                        ("claude1",))
        self.assertEqual(self.hand(), [])
        self.db.execute("UPDATE agent_registry SET status = 'idle' WHERE name = %s",
                        ("claude1",))
        self.assertEqual(self.hand(), [])
        self.assertEqual(self.assignee(intake), "claude1")

    def test_agent_critique_d_un_canon_secondaire(self):
        # codex r1 (#409) : le canon propriétaire est lu au registre, pas dans
        # la ligne du listing (sans `canon`, donc jugée au canon par défaut)
        def canon(name, default, cards):
            return types.SimpleNamespace(
                readable=True, is_default=default, scope_unread=False, load_findings=[],
                federation={}, agents=[types.SimpleNamespace(
                    title=agent, fiche=types.SimpleNamespace(data={"roles": roles},
                                                             path="", ref=""))
                    for agent, roles in cards.items()],
                owns=lambda c, name=name, default=default: (c or None) == name
                or (default and not c),
                agent=lambda n, cards=cards: None)
        principal = canon("A", True, {})
        secondaire = canon("B", False, {"claude1": ["design"]})
        secondaire.agent = lambda n: next((a for a in secondaire.agents if a.title == n),
                                          None)
        self.team()
        self.db.execute("UPDATE agent_registry SET canon = 'B' WHERE name = 'claude1'")
        intake = self.lot("export")
        self.assertEqual(self.hand(canons=[principal, secondaire]), [])
        self.assertEqual(self.assignee(intake), "claude1")
        # la même fiche dans un canon qui ne le possède pas ne compte pas
        self.db.execute("UPDATE agent_registry SET canon = NULL WHERE name = 'claude1'")
        out = self.hand(canons=[principal, secondaire])
        self.assertEqual([m["item"]["id"] for m in out], [intake["id"]])

    def test_seulement_un_agent_claude_vers_un_agent_deepseek(self):
        self.agent("codex1", "codex", team="nexlink")
        self.pause("codex1")
        self.agent("claude2", team="nexlink")
        self.agent("deepseek1", "deepseek", team="nexlink")
        lot = self.lot("export", assignee="codex1")
        self.assertEqual(self.hand(), [])
        self.assertEqual(self.assignee(lot), "codex1")
        # un agent Claude libre ne prend pas : seul DeepSeek reprend
        self.agent("claude1", team="nexlink")
        self.pause("claude1")
        self.db.execute("UPDATE agent_registry SET status = 'running' WHERE name = %s",
                        ("deepseek1",))
        mine = self.lot("import")
        self.assertEqual(self.hand(), [])
        self.assertEqual(self.assignee(mine), "claude1")

    def test_jamais_en_revue_gele_sensible_ou_en_decision(self):
        self.team()
        self.lot("en revue", state="qa")
        gele = self.lot("gelé", state="build", branch="lot/gele")
        self.db.execute("INSERT INTO work_item_milestones (work_item_id, kind, sha)"
                        " VALUES (%s, 'frozen', 'abc')", (gele["id"],))
        prod = self.lot("production")
        self.db.execute("UPDATE work_items SET required_capabilities = '{production}'"
                        " WHERE id = %s", (prod["id"],))
        decision = self.lot("décision")
        self.db.execute("INSERT INTO commitments (what, kind, work_item_id)"
                        " VALUES ('choisir', 'decision', %s)", (decision["id"],))
        with mock.patch.object(handoff, "lot_git", return_value=GIT_OK):
            self.assertEqual(self.hand(), [])
        # une branche qui touche un chemin sensible
        sensible = self.lot("migration", state="build", branch="lot/migration")
        canon = types.SimpleNamespace(federation={"review_policies": {"risk_classes": {
            "default": "normal",
            "rules": [{"paths": ["src/ameesh/migrations/**"], "class": "sensitive"}]}}},
            readable=True, agents=[], owns=lambda c: True, agent=lambda n: None,
            scope_unread=False, load_findings=[])
        touched = dict(GIT_OK, paths=["src/ameesh/migrations/0099_x.sql"])
        with mock.patch.object(handoff, "lot_git", return_value=touched):
            self.assertEqual(self.hand(canons=[canon]), [])
        self.assertEqual(self.assignee(sensible), "claude1")
        with mock.patch.object(handoff, "lot_git", return_value=GIT_OK):
            out = self.hand(canons=[canon])
        self.assertEqual([m["item"]["id"] for m in out], [sensible["id"]])

    def test_autre_projet_ou_hors_budget(self):
        self.team()
        self.db.execute("UPDATE agent_registry SET team = 'web' WHERE name = 'deepseek1'")
        lot = self.lot("export")
        self.assertEqual(self.hand(), [])
        self.db.execute("UPDATE agent_registry SET team = 'nexlink' WHERE name = 'deepseek1'")
        self.assertEqual(self.hand(budget_check=lambda agent: "cap reached"), [])
        # un orchestrateur DeepSeek ne se sert pas
        self.assertEqual(self.hand(orchestras=["deepseek1"]), [])
        # un preneur occupé ou au repos trop court
        self.assertEqual(self.hand(idle_s=3 * HEURE), [])
        self.assertEqual(self.assignee(lot), "claude1")
        self.assertEqual(len(self.hand()), 1)

    def test_derniere_attribution_humaine(self):
        self.team()
        par_humain = self.lot("choisi par le propriétaire", actor="human:proprio")
        self.assertEqual(self.hand(), [])
        self.assertEqual(self.assignee(par_humain), "claude1")
        # réattribué ensuite par l'orchestrateur : la dernière attribution compte
        self.agent("claude2", team="nexlink")
        work.assign(self.db, par_humain["id"], "claude2", actor="human:proprio")
        self.force_assign(par_humain["id"], "claude1", actor=self.orch)
        self.pause("claude1")
        out = self.hand()
        self.assertEqual([m["item"]["id"] for m in out], [par_humain["id"]])

    def test_build_saute_avec_une_note_par_pause(self):
        self.team()
        build = self.lot("import", state="build", branch="lot/import")
        dirty = {"ok": False, "reason": "uncommitted changes in /w", "paths": None}
        with mock.patch.object(handoff, "lot_git", return_value=dirty):
            self.assertEqual(self.hand(), [])
            self.assertEqual(self.hand(), [])
        notes = [e["note"] for e in work.events(self.db, build["id"])
                 if e["note"].startswith(handoff.SKIP_NOTE)]
        self.assertEqual(notes, ["%s (vers deepseek1) : uncommitted changes in /w"
                                 % handoff.SKIP_NOTE])
        self.assertEqual(self.assignee(build), "claude1")
        # un intake sauté pour son git (branche illisible) ne reçoit pas de note
        intake = self.lot("export", branch="lot/export")
        with mock.patch.object(handoff, "lot_git", return_value=dirty):
            self.hand()
        self.assertFalse(any(e["note"].startswith(handoff.SKIP_NOTE)
                             for e in work.events(self.db, intake["id"])))
        # nouvelle pause : une nouvelle note
        self.db.execute("UPDATE agent_registry SET status_since = now() + interval '1 second'"
                        " WHERE name = 'claude1'")
        with mock.patch.object(handoff, "lot_git", return_value=dirty):
            self.hand()
        self.assertEqual(len([e for e in work.events(self.db, build["id"])
                              if e["note"].startswith(handoff.SKIP_NOTE)]), 2)

    def test_plafond_horaire(self):
        self.team()
        self.agent("deepseek2", "deepseek", team="nexlink")
        self.lot("a")
        self.lot("b")
        self.assertEqual(len(self.hand(max_per_hour=1)), 1)
        self.assertEqual(self.hand(max_per_hour=1), [])
        self.assertEqual(self.hand(max_per_hour=0), [])

    def take(self, lot, *, current="claude1", state="intake", assignee="deepseek1",
             max_per_window=4):
        return storage.of(self.db).work.handoff(
            lot["id"], assignee, current=current, state=state,
            paused_prefix=budget.PAUSE_PREFIX, note_prefix=handoff.HANDOFF_NOTE,
            window_s=3600, max_per_window=max_per_window,
            note=handoff.HANDOFF_NOTE + " x", actor="ameesh")

    def test_lot_change_entre_temps(self):
        # codex r1 (#409) : tout est relu à l'écriture, sous verrou
        self.team()
        lot = self.lot("export")
        self.assertIsNone(self.take(lot, state="build"))      # pas l'état contrôlé
        self.assertIsNone(self.take(lot, current="dev9"))      # plus son assigné
        self.assertIsNone(self.take(lot, max_per_window=0))    # plafond atteint
        autre = self.lot("autre", assignee="deepseek1")
        self.assertIsNone(self.take(lot))                      # preneur plus libre
        self.db.execute("UPDATE work_items SET assignee = NULL WHERE id = %s",
                        (autre["id"],))
        self.db.execute("UPDATE agent_registry SET status = 'idle' WHERE name = 'claude1'")
        self.assertIsNone(self.take(lot))                      # source sortie de pause
        self.pause("claude1")
        self.assertEqual(self.take(lot)["assignee"], "deepseek1")
        self.assertIsNone(self.take(lot, current="deepseek1", assignee="deepseek2",
                                    max_per_window=1))         # la reprise compte

    def test_sortie_de_pause_pendant_le_controle(self):
        # codex r1 (#409) : la source reprend pendant le contrôle git
        self.team()
        lot = self.lot("export")

        def resumed(*args, **kwargs):
            self.db.execute("UPDATE agent_registry SET status = 'idle', status_text = ''"
                            " WHERE name = 'claude1'")
            return GIT_OK

        with mock.patch.object(handoff, "lot_git", side_effect=resumed):
            self.assertEqual(self.hand(), [])
        self.assertEqual(self.assignee(lot), "claude1")
        self.assertEqual(self.mails(), [])

    def test_attribution_humaine_au_dela_du_journal_lu(self):
        # codex r1 (#409) : plus de EVENTS_READ notes après l'attribution humaine
        self.team()
        lot = self.lot("choisi", actor="human:proprio")
        self.db.execute("INSERT INTO work_item_events (work_item_id, state, note, actor)"
                        " SELECT %s, 'intake', 'avancement ' || n, 'dev' || n"
                        "   FROM generate_series(1, %s) n",
                        (lot["id"], handoff.EVENTS_READ + 5))
        self.assertEqual(self.hand(), [])
        self.assertEqual(self.assignee(lot), "claude1")

    def test_etat_valide_seulement(self):
        # codex r1 (#409) : la réattribution exige l'état contrôlé, pas l'un
        # des STATES : un lot passé d'intake à build entre-temps n'a pas eu
        # son contrôle git
        self.team()
        lot = self.lot("export")

        def build_meanwhile(*args, **kwargs):
            self.db.execute("UPDATE work_items SET state = 'build' WHERE id = %s",
                            (lot["id"],))
            return GIT_OK

        with mock.patch.object(handoff, "lot_git", side_effect=build_meanwhile):
            self.assertEqual(self.hand(), [])
        self.assertEqual(self.assignee(lot), "claude1")
        self.assertEqual(self.mails(), [])

    def test_passage_de_notify(self):
        self.team()
        lot = self.lot("export")
        records = []
        ncfg = notify.parse_config({})
        logs = []
        notifier = notify.Notifier(self.cfg, ncfg, log=logs.append, emit=records.append,
                                   clock=lambda: self.now,
                                   handoff={"max_per_hour": 4, "idle_s": 300.0})
        real = handoff.budget_handoff
        seen = {}

        def fake(cfg, db, **kw):
            seen.update(kw)
            return real(cfg, db, canons=[], declared=(), orchestras=[self.orch],
                        budget_check=lambda agent: "", **kw)

        with mock.patch.object(handoff, "budget_handoff", fake):
            notifier.run_pass(self.db, thresholds=_seuils())
        records = [r for r in records if r["event"] == "budget_handoff"]
        self.assertFalse([t for t in logs if "budget handoff" in t])
        self.assertEqual(seen, {"now": self.now, "dry_run": False, "max_per_hour": 4,
                                "idle_s": 300.0})
        self.assertEqual([(r["event"], r["agent"], r["lot"], r["delivery"]) for r in records],
                         [("budget_handoff", "deepseek1", lot["id"], "prise")])
        self.assertIn("budget handoff", notify._format(records[0]))
        args = notify.build_parser().parse_args(["--handoff-max-per-hour", "0"])
        self.assertIsNone(notify.handoff_settings(args))
        args = notify.build_parser().parse_args([])
        self.assertEqual(notify.handoff_settings(args),
                         {"max_per_hour": handoff.DEFAULT_MAX_PER_HOUR,
                          "idle_s": handoff.DEFAULT_IDLE_S})


class DernierAttributeurTest(unittest.TestCase):
    def test_last_assigner(self):
        events = [{"note": "commentaire", "actor": "dev1"},
                  {"note": "assigned to dev1 (before: nobody)", "actor": "human:proprio"},
                  {"note": "créé", "actor": "mesh-design"}]
        self.assertEqual(handoff.last_assigner(events), "human:proprio")
        self.assertEqual(handoff.last_assigner(events[2:]), "mesh-design")
        self.assertEqual(handoff.last_assigner([]), "")
        moved = [{"note": handoff.HANDOFF_NOTE + " par deepseek1", "actor": "ameesh"}] + events
        self.assertEqual(handoff.last_assigner(moved), "ameesh")


if __name__ == "__main__":
    unittest.main()
