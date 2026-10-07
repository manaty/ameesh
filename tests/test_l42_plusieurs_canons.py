# SPDX-License-Identifier: AGPL-3.0-only
"""Plusieurs canons sur un même hôte (lot L42, décision 0031).

Deux canons git construits sur disque, d'identifiants différents mais dont la
racine s'appelle `home` dans les deux (comme manaty et Acme) :

* synchroniser A puis B puis A n'arrête ni ne désadmet aucun agent ;
* un canon B invalide (ou illisible) ne ferme que les agents de B ;
* un nom déjà déclaré par l'autre canon : constat local, aucune écriture ;
* un nom libéré (agent retiré de son canon, arrêté) est repris ;
* configuration `canons`, `AMEESH_CANONS`, et compatibilité de `canon` seul ;
* l'exécuteur synchronise chaque canon (une erreur n'empêche pas les autres).
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import types
import unittest

from ameesh import canon, canon_sync, config as config_mod, registry, storage
from ameesh import runner as runner_mod

from .support import PgTestCase
from .test_canon import _TmpMixin, commit_all, git, publish, write

HOST = "pc"
ID_A = "manaty-essai"
ID_B = "acme-essai"


def federation(ident: str, workspace_path: str) -> str:
    return ("federation: \"0.1\"\nid: %s\nroot: home\nmembers:\n"
            "  - id: home\n    ref: main\n    bundle: .\n    entrypoint: index.md\n"
            "    workspace_path: %s\n    enforcement: required\n" % (ident, workspace_path))


def member(title: str) -> str:
    return ("---\ntype: Member\ntitle: %s\nroles: [project-lead]\nauthenticators: []\n---\n"
            "\n# %s\n" % (title, title))


def host(responsible: str, work_root: str) -> str:
    return ("---\ntype: Host\ntitle: %s\nresponsible: human:%s\npolicy:\n"
            "  harnesses: [claude, codex]\n  providers: [anthropic, openai]\n"
            "  credential_modes: [subscription, api-key]\n  work_root: %s\n"
            "  max_agents: 8\n---\n\n# %s\n" % (HOST, responsible, work_root, HOST))


def agent(title: str, responsible: str, team: str) -> str:
    return ("---\ntype: Agent\ntitle: %s\nresponsible: human:%s\nteam: %s\n"
            "capabilities: [read, propose]\nharness: claude\nmodel: claude-opus\n"
            "provider: anthropic\ncredential_mode: subscription\n---\n\n# %s\n"
            % (title, responsible, team, title))


def admission(title: str) -> str:
    return ("---\ntype: Placement\ntitle: %s@%s\nagent: %s\nhosts: [%s]\n"
            "credential_mode: subscription\n---\n" % (title, HOST, title, HOST))


def package(ident: str, responsible: str, team: str) -> str:
    return ("---\ntype: WorkPackage\ntitle: jalon %s\nkind: milestone\nresponsible: "
            "human:%s\nteam: %s\n---\n" % (ident, responsible, team))


class _DeuxCanons(_TmpMixin, PgTestCase):
    """Canon A (`manaty-essai`, humaine alice, agents a1 a2, paquet pa) et canon
    B (`acme-essai`, humain bob, agents b1 b2, paquet pb) ; même hôte `pc`."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM canon_state")
        self.db.execute("TRUNCATE authenticators, authenticator_syncs RESTART IDENTITY CASCADE")
        self.workspace = self.make_tmp()
        self.work = os.path.join(self.workspace, "travail")
        self.a = self.build("a", ID_A, "alice", "acme", ["a1", "a2"], "pa")
        self.b = self.build("b", ID_B, "bob", "acme", ["b1", "b2"], "pb")

    def build(self, name, ident, human, team, agents, pkg) -> str:
        _bare, clone = publish(self.workspace, name)
        write(clone, "federation.yaml", federation(ident, name))
        write(clone, "membres/%s.md" % human, member(human))
        write(clone, "hotes/%s.md" % HOST, host(human, self.work))
        for title in agents:
            write(clone, "agents/%s.md" % title, agent(title, human, team))
            write(clone, "placements/%s.md" % title, admission(title))
        write(clone, "plan/%s.md" % pkg, package(pkg, human, team))
        commit_all(clone, "canon %s" % ident)
        return clone

    # -- lecture et synchronisation ---------------------------------------
    def load_a(self) -> canon.Canon:
        return canon.load(self.a)

    def load_b(self) -> canon.Canon:
        return canon.load(self.b, default=False)

    def sync_a(self) -> canon_sync.SyncReport:
        return canon_sync.sync(self.db, self.load_a(), HOST)

    def sync_b(self) -> canon_sync.SyncReport:
        return canon_sync.sync(self.db, self.load_b(), HOST)

    def row(self, name: str) -> dict:
        rows = self.db.query("SELECT * FROM agent_registry WHERE name = %s", (name,))
        return rows[0] if rows else None

    def claimable(self) -> list[str]:
        return sorted(r["name"] for r in registry.claimable(self.db, HOST,
                                                            require_responsible=True))

    def actions(self, report) -> dict:
        return {a.agent: a.action for a in report.actions}


class SynchronisationBorneeTest(_DeuxCanons):
    def test_identite_et_references_qualifiees(self):
        a, b = self.load_a(), self.load_b()
        self.assertEqual((a.id, a.id_source, a.is_default), (ID_A, "federation", True))
        self.assertEqual((b.id, b.id_source, b.is_default), (ID_B, "federation", False))
        commit_a = git(self.a, "rev-parse", "origin/main")
        commit_b = git(self.b, "rev-parse", "origin/main")
        # canon par défaut : format historique ; autre canon : préfixé de son id
        self.assertEqual(a.agent("a1").fiche.ref, "home:agents/a1.md@%s" % commit_a)
        self.assertEqual(b.agent("b1").fiche.ref, "%s/home:agents/b1.md@%s" % (ID_B, commit_b))
        self.assertEqual(canon.split_ref(b.agent("b1").fiche.ref),
                         (ID_B, "home", "agents/b1.md", commit_b))
        self.assertEqual(b.member_of(b.agent("b1").fiche.ref), "home")
        self.assertIsNone(a.member_of(b.agent("b1").fiche.ref))
        self.assertIsNone(b.member_of(a.agent("a1").fiche.ref))

    def test_a_puis_b_puis_a_aucun_arret(self):
        first = self.sync_a()
        self.assertEqual(self.actions(first), {"a1": "créé", "a2": "créé"})
        second = self.sync_b()
        self.assertEqual(self.actions(second), {"b1": "créé", "b2": "créé"})
        third = self.sync_a()
        self.assertEqual(self.actions(third), {"a1": "inchangé", "a2": "inchangé"})
        self.assertEqual(self.actions(self.sync_b()), {"b1": "inchangé", "b2": "inchangé"})
        for name, owner, human in (("a1", None, "alice"), ("a2", None, "alice"),
                                   ("b1", ID_B, "bob"), ("b2", ID_B, "bob")):
            with self.subTest(agent=name):
                row = self.row(name)
                self.assertEqual(row["status"], "idle")
                self.assertEqual(row["canon"], owner)
                self.assertEqual(row["responsible"], "human:%s" % human)
                self.assertTrue(row["placement_ok"], row["placement_diagnostic"])
        self.assertEqual(self.claimable(), ["a1", "a2", "b1", "b2"])
        # l'état est tenu par (hôte, canon) : '' pour le canon par défaut
        states = {s["canon"]: s for s in canon_sync.states(self.db, HOST)}
        self.assertEqual(set(states), {"", ID_B})
        self.assertEqual((states[""]["status"], states[""]["canon_id"]), ("ok", ID_A))
        self.assertEqual((states[ID_B]["status"], states[ID_B]["canon_id"]), ("ok", ID_B))
        self.assertEqual(canon_sync.state(self.db, HOST, self.load_b())["status"], "ok")
        # le plan : chaque canon garde ses paquets, aucun n'est retiré par l'autre
        packages = {p["id"]: p for p in storage.of(self.db).packages.all(include_absent=True)}
        self.assertEqual({k: (v["present"], v["canon"]) for k, v in packages.items()},
                         {"pa": (True, None), "pb": (True, ID_B)})
        # L44 : le canon B synchronise SON registre des authentificateurs ; sans
        # branche de confiance configurée ni amorçage, refus limité à B
        done = self.sync_b().authenticators
        self.assertEqual((done.status, done.canon), (canon_sync.AUTH_REFUSED, ID_B))
        self.assertEqual(canon_sync.state(self.db, HOST, ID_B)["auth_status"], "error")

    def test_retrait_ne_vise_que_son_canon(self):
        self.sync_a()
        self.sync_b()
        write(self.b, "agents/b2.md", None)
        write(self.b, "placements/b2.md", None)
        write(self.b, "plan/pb.md", None)
        commit_all(self.b, "b2 retiré")
        report = self.sync_b()
        # b1 : référence au nouveau commit de B ; b2 : arrêté
        self.assertEqual(self.actions(report), {"b1": "mis à jour", "b2": "arrêté"})
        self.assertEqual(report.packages.retired, ["pb"])
        report = self.sync_a()
        self.assertEqual(self.actions(report), {"a1": "inchangé", "a2": "inchangé"})
        self.assertEqual(report.packages.retired, [])
        self.assertEqual(self.claimable(), ["a1", "a2", "b1"])

    def test_canon_b_invalide_ne_ferme_que_b(self):
        self.sync_a()
        self.sync_b()
        canon_sync.spawn(self.db, "aide-b", "b1", 3600)
        canon_sync.spawn(self.db, "aide-a", "a1", 3600)
        self.assertEqual(self.row("aide-b")["canon"], ID_B)    # canon du créateur
        self.assertIsNone(self.row("aide-a")["canon"])
        self.assertEqual(self.claimable(), ["a1", "a2", "aide-a", "aide-b", "b1", "b2"])
        write(self.b, "agents/casse.md", "---\ntype: Agent\ntitle: [x\n---\n")
        commit_all(self.b, "fiche cassée")
        self.assertEqual(self.sync_b().status, canon_sync.CANON_INVALID)
        self.assertEqual(self.sync_a().status, canon_sync.CANON_OK)
        self.assertEqual(self.claimable(), ["a1", "a2", "aide-a"])
        # B illisible (racine disparue) : sa propre clé est retrouvée par sa
        # racine, l'état fermé est le sien ; A reste ouvert
        write(self.b, "agents/casse.md", None)
        commit_all(self.b, "réparé")
        self.sync_b()
        self.assertEqual(self.claimable(), ["a1", "a2", "aide-a", "aide-b", "b1", "b2"])
        shutil.rmtree(self.b)
        with self.assertRaises(canon_sync.CanonUnreadable):
            self.sync_b()
        self.assertEqual(canon_sync.state(self.db, HOST, ID_B)["status"], "unreadable")
        self.assertEqual(canon_sync.state(self.db, HOST)["status"], "ok")
        self.sync_a()
        self.assertEqual(self.claimable(), ["a1", "a2", "aide-a"])
        overview = {r["name"]: r for r in registry.overview(self.db)}
        self.assertEqual((overview["b1"]["canon_status"], overview["b1"]["canon_claim_ok"]),
                         ("unreadable", False))
        self.assertEqual((overview["a1"]["canon_status"], overview["a1"]["canon_claim_ok"]),
                         ("ok", True))

    def test_conflit_de_nom_local_a_la_fiche(self):
        self.sync_a()
        # B déclare aussi a1 (et le paquet pa) : le premier déclarant garde le nom
        write(self.b, "agents/a1.md", agent("a1", "bob", "acme"))
        write(self.b, "placements/a1.md", admission("a1"))
        write(self.b, "plan/pa.md", package("pa", "bob", "acme"))
        commit_all(self.b, "a1 et pa aussi")
        avant = self.row("a1")
        report = self.sync_b()
        self.assertEqual(report.status, canon_sync.CANON_OK)      # pas d'erreur globale
        self.assertEqual(self.actions(report),
                         {"a1": "conflit", "b1": "créé", "b2": "créé"})
        conflicts = [f for f in report.findings if f.code == canon_sync.NAME_CONFLICT]
        self.assertEqual(sorted((f.agent, f.package) for f in conflicts),
                         [("", "pa"), ("a1", "")])
        self.assertIn("canon-name-conflict", report.diagnostic)
        self.assertEqual(report.packages.conflicts, ["pa"])
        apres = self.row("a1")
        for key in ("canon", "canon_ref", "responsible", "team", "status", "placement_ok"):
            self.assertEqual(apres[key], avant[key], key)
        self.assertIsNone(storage.of(self.db).packages.get("pa")["canon"])
        self.assertEqual(self.claimable(), ["a1", "a2", "b1", "b2"])
        # A, resynchronisé, garde a1 sans rien voir de B
        self.assertEqual(self.actions(self.sync_a()), {"a1": "inchangé", "a2": "inchangé"})

    def test_reprise_d_un_nom_libere(self):
        self.sync_a()
        self.sync_b()
        # a2 quitte A : arrêté par la synchronisation de A, nom libéré
        write(self.a, "agents/a2.md", None)
        write(self.a, "placements/a2.md", None)
        commit_all(self.a, "a2 part")
        self.assertEqual(self.actions(self.sync_a())["a2"], "arrêté")
        self.assertEqual(self.row("a2")["status"], "stopped")
        # B le déclare : il le reprend (la ligne change de canon et repart)
        write(self.b, "agents/a2.md", agent("a2", "bob", "acme"))
        write(self.b, "placements/a2.md", admission("a2"))
        commit_all(self.b, "a2 arrive")
        report = self.sync_b()
        self.assertEqual(self.actions(report)["a2"], "réintégré")
        row = self.row("a2")
        self.assertEqual((row["canon"], row["status"], row["responsible"]),
                         (ID_B, "idle", "human:bob"))
        self.assertTrue(row["canon_ref"].startswith("%s/home:agents/a2.md@" % ID_B))
        # A ne le touche plus
        self.assertNotIn("a2", self.actions(self.sync_a()))
        self.assertEqual(self.row("a2")["status"], "idle")
        self.assertEqual(self.claimable(), ["a1", "a2", "b1", "b2"])

    def test_changement_de_canon_par_defaut(self):
        self.sync_a()
        self.sync_b()
        # l'ordre de la configuration s'inverse : B devient le canon par défaut
        b_defaut = canon.load(self.b)
        a_second = canon.load(self.a, default=False)
        done = canon_sync.adopt_default(self.db, HOST, [b_defaut, a_second])
        self.assertEqual(done["agents"], ["a1", "a2"])
        report_b = canon_sync.sync(self.db, b_defaut, HOST)
        report_a = canon_sync.sync(self.db, a_second, HOST)
        self.assertEqual(self.actions(report_b), {"b1": "mis à jour", "b2": "mis à jour"})
        self.assertEqual(self.actions(report_a), {"a1": "mis à jour", "a2": "mis à jour"})
        self.assertEqual({n: self.row(n)["canon"] for n in ("a1", "a2", "b1", "b2")},
                         {"a1": ID_A, "a2": ID_A, "b1": None, "b2": None})
        self.assertEqual({n: self.row(n)["status"] for n in ("a1", "a2", "b1", "b2")},
                         dict.fromkeys(("a1", "a2", "b1", "b2"), "idle"))
        self.assertEqual(self.claimable(), ["a1", "a2", "b1", "b2"])
        self.assertIsNone(canon_sync.adopt_default(self.db, HOST, [b_defaut, a_second]))

    def test_identifiants_en_double_refuses(self):
        write(self.b, "federation.yaml", federation(ID_A, "b"))
        commit_all(self.b, "même id")
        with self.assertRaises(canon.CanonError) as ctx:
            canon.check_identities([self.load_a(), self.load_b()])
        self.assertIn(ID_A, str(ctx.exception))


class ExecuteurTest(_DeuxCanons):
    def runner_pass(self, cfg) -> bool:
        fake = types.SimpleNamespace(cfg=cfg, host=HOST)
        return runner_mod.Runner.canon_sync_once(fake)

    def cfg2(self, *, second=None):
        return dataclasses.replace(
            self.cfg, canon=self.a, canon_ref="",
            extra_canons=(config_mod.CanonEntry(second or self.b),))

    def test_chaque_canon_a_chaque_passe(self):
        self.assertTrue(self.runner_pass(self.cfg2()))
        self.assertTrue(self.runner_pass(self.cfg2()))
        self.assertEqual(self.claimable(), ["a1", "a2", "b1", "b2"])
        self.assertEqual({n: self.row(n)["canon"] for n in ("a1", "b1")},
                         {"a1": None, "b1": ID_B})

    def test_une_erreur_n_empeche_pas_les_autres(self):
        self.assertTrue(self.runner_pass(self.cfg2()))
        shutil.rmtree(self.b)
        self.assertFalse(self.runner_pass(self.cfg2()))           # B illisible
        self.assertEqual(self.claimable(), ["a1", "a2"])
        state = canon_sync.state(self.db, HOST)
        self.assertEqual(state["status"], "ok")

    def test_identifiant_en_double_ecarte(self):
        write(self.b, "federation.yaml", federation(ID_A, "b"))
        commit_all(self.b, "même id")
        self.assertFalse(self.runner_pass(self.cfg2()))
        self.assertEqual(self.claimable(), ["a1", "a2"])          # A synchronisé quand même
        self.assertIsNone(self.row("b1"))

    def test_canon_retire_de_la_configuration_ferme(self):
        self.assertTrue(self.runner_pass(self.cfg2()))
        self.assertEqual(self.claimable(), ["a1", "a2", "b1", "b2"])
        seul = dataclasses.replace(self.cfg, canon=self.a, canon_ref="", extra_canons=())
        self.assertTrue(self.runner_pass(seul))
        self.assertEqual(canon_sync.state(self.db, HOST, ID_B)["status"], "invalid")
        self.assertEqual(self.claimable(), ["a1", "a2"])
        self.assertEqual(self.row("b1")["status"], "idle")        # rien n'est arrêté
        self.assertTrue(self.runner_pass(self.cfg2()))            # remis : rouvert
        self.assertEqual(self.claimable(), ["a1", "a2", "b1", "b2"])

    def test_seuils_de_l_hote_depuis_les_canons(self):
        runner = types.SimpleNamespace(host=HOST, _host_limits=None)
        a, b = self.load_a(), self.load_b()
        runner_mod.Runner.refresh_host_limits(runner, b, a)
        self.assertIsNotNone(runner._host_limits)


class CliTest(_DeuxCanons):
    def cli_env(self, **extra) -> dict:
        # AMEESH_CANON_REF : branche de confiance du canon par défaut (registre
        # des authentificateurs), comme sur un hôte réel
        return self.env(AMEESH_CANONS="%s:%s" % (self.a, self.b), AMEESH_HOST=HOST,
                        AMEESH_CANON_REF="origin/main", **extra)

    def test_sync_check_show_tous_les_canons(self):
        # L44 : B (sans `ref` dans AMEESH_CANONS) amorce son propre registre des
        # authentificateurs ; ignoré pour A, dont la configuration fixe la branche
        proc = self.mesh("canon", "sync", "--host", HOST, "--bootstrap-ref", "main",
                         env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("== canon %s" % ID_A, proc.stdout)
        self.assertIn("== canon %s" % ID_B, proc.stdout)
        self.assertEqual(self.claimable(), ["a1", "a2", "b1", "b2"])
        data = json.loads(self.mesh("canon", "check", "--host", HOST, "--json",
                                    env=self.cli_env()).stdout)
        self.assertEqual([c["canon_id"] for c in data["canons"]], [ID_A, ID_B])
        self.assertEqual([c["registry_state"]["status"] for c in data["canons"]],
                         ["ok", "ok"])
        data = json.loads(self.mesh("canon", "show", "--json", env=self.cli_env()).stdout)
        self.assertEqual([(c["id"], c["default"]) for c in data["canons"]],
                         [(ID_A, True), (ID_B, False)])

    def test_option_canon_par_identifiant(self):
        proc = self.mesh("canon", "sync", "--host", HOST, "--canon", ID_B, "--json",
                         "--bootstrap-ref", "main", env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(sorted(a["agent"] for a in report["actions"]), ["b1", "b2"])
        self.assertIsNone(self.row("a1"))
        proc = self.mesh("canon", "check", "--host", HOST, "--canon", ID_A, env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("== canon", proc.stdout)

    def test_spawn_cherche_dans_tous_les_canons(self):
        self.mesh("canon", "sync", "--host", HOST, env=self.cli_env())
        proc = self.mesh("agent", "spawn", "aide", "--by", "b1", "--ttl", "1h",
                         env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("responsable human:bob", proc.stdout)
        self.assertEqual(self.row("aide")["canon"], ID_B)
        # un nom déclaré par le canon B n'est pas un éphémère
        proc = self.mesh("agent", "spawn", "b2", "--by", "a1", "--ttl", "1h",
                         env=self.cli_env())
        self.assertEqual(proc.returncode, 1)


class ConfigurationTest(_TmpMixin, unittest.TestCase):
    def load(self, config: dict | None = None, **env) -> config_mod.Config:
        path = os.path.join(self.make_tmp(), "config.json")
        if config is not None:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(config, fh)
        return config_mod.load(dict(env, AMEESH_CONFIG=path))

    def test_canons_du_fichier(self):
        cfg = self.load({"canons": [{"path": "/srv/a", "ref": "origin/main"},
                                    {"path": "/srv/b", "untrusted": True}, "/srv/c"]})
        self.assertEqual((cfg.canon, cfg.canon_ref, cfg.canon_untrusted),
                         ("/srv/a", "origin/main", False))
        self.assertEqual(cfg.canon_entries, (
            config_mod.CanonEntry("/srv/a", "origin/main", False),
            config_mod.CanonEntry("/srv/b", "", True),
            config_mod.CanonEntry("/srv/c", "", False)))
        self.assertTrue(cfg.responsible_required)

    def test_ameesh_canons(self):
        cfg = self.load({"canons": ["/srv/x"]}, AMEESH_CANONS="/srv/a:/srv/b",
                        AMEESH_CANON_REF="origin/prod")
        self.assertEqual([e.path for e in cfg.canon_entries], ["/srv/a", "/srv/b"])
        self.assertEqual([e.ref for e in cfg.canon_entries], ["origin/prod", ""])
        cfg = self.load(None, AMEESH_CANONS="/srv/a:/srv/b", AMEESH_CANON="/srv/a")
        self.assertEqual(len(cfg.canon_entries), 2)
        with self.assertRaises(SystemExit):
            self.load(None, AMEESH_CANONS="/srv/a:/srv/b", AMEESH_CANON="/srv/b")
        # la liste de l'environnement remplace celle du fichier
        cfg = self.load({"canons": ["/srv/a", "/srv/b"]}, AMEESH_CANON="/srv/z")
        self.assertEqual([e.path for e in cfg.canon_entries], ["/srv/z"])

    def test_canon_seul_compatible(self):
        cfg = self.load({"canon": "/srv/a", "canon_ref": "origin/main"})
        self.assertEqual(cfg.canon_entries, (config_mod.CanonEntry("/srv/a", "origin/main"),))
        self.assertEqual(cfg.extra_canons, ())
        self.assertTrue(cfg.responsible_required)
        cfg = self.load(None)
        self.assertEqual((cfg.canon_entries, cfg.responsible_required), ((), False))

    def test_erreurs_de_configuration(self):
        for bad in ({"canon": "/srv/a", "canons": ["/srv/b"]},
                    {"canons": "/srv/a"},
                    {"canons": [{"path": ""}]},
                    {"canons": [{"path": "/srv/a", "zut": 1}]},
                    {"canons": ["/srv/a", "/srv/a"]}):
            with self.subTest(config=bad), self.assertRaises(SystemExit):
                self.load(bad)


class CompatibiliteUnCanonTest(_DeuxCanons):
    """Un seul canon : lignes et état exactement comme avant 0032."""

    def test_un_canon_rien_ne_change(self):
        cfg = dataclasses.replace(self.cfg, canon=self.a, extra_canons=())
        self.assertTrue(runner_mod.Runner.canon_sync_once(
            types.SimpleNamespace(cfg=cfg, host=HOST)))
        self.assertEqual([(s["canon"], s["status"]) for s in canon_sync.states(self.db, HOST)],
                         [("", "ok")])
        self.assertEqual({r["canon"] for r in self.db.query("SELECT canon FROM agent_registry")},
                         {None})
        self.assertTrue(self.row("a1")["canon_ref"].startswith("home:agents/a1.md@"))
        loaded = canon.from_config(cfg)
        self.assertFalse([f for f in loaded.load_findings if f.code.startswith("canon-id")])


if __name__ == "__main__":
    unittest.main()
