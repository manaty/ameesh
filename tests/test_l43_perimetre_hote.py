# SPDX-License-Identifier: AGPL-3.0-only
"""Périmètre ameesh et hôte décrit par chaque canon (lot L43, décision 0031).

* un canon partagé imitant Acme — des `type: Agent` sans `title`
  (sous-agents Claude Code) dans `org/agent-harness/agents/`, les fiches
  ameesh sous `ameesh/` — devient valide avec `ameesh: {scope: ameesh}` ; sans
  la clé il reste invalide (non-régression) ;
* un périmètre invalide ou introuvable : erreur, membre non lu, rien retiré ;
* l'admission d'un agent se juge avec la fiche Host de SON canon ;
* les limites physiques de l'hôte sont les plus strictes des fiches Host ;
* les humains se résolvent dans le canon de l'agent ;
* `canon check` et `placement check` ne montrent dans le bloc d'un canon que
  les agents de ce canon.
"""
from __future__ import annotations

import json
import os
import threading
import types
import unittest

from ameesh import canon, canon_sync, placement, registry, resources, visibility
from ameesh import runner as runner_mod

from .support import PgTestCase
from .test_canon import _TmpMixin, commit_all, publish, write

HOST = "pc"
ID_A = "manaty-essai"
ID_T = "acme-essai"
GIB = 1024 ** 3


def federation(ident: str, workspace_path: str, extra: str = "",
               members: str = "") -> str:
    return ("federation: \"0.1\"\nid: %s\nroot: home\n%smembers:\n"
            "  - id: home\n    ref: main\n    bundle: .\n    entrypoint: index.md\n"
            "    workspace_path: %s\n    enforcement: required\n%s"
            % (ident, extra, workspace_path, members))


def member(title: str, login: str = "") -> str:
    return ("---\ntype: Member\ntitle: %s\nroles: [project-lead]\nauthenticators: []\n%s"
            "---\n" % (title, "github: %s\n" % login if login else ""))


def host(responsible: str, *, harnesses=("claude", "codex"), max_agents: int | None = 8,
         resources_yaml: str = "", work_root: str = "/tmp/travail") -> str:
    return ("---\ntype: Host\ntitle: %s\nresponsible: human:%s\npolicy:\n"
            "  harnesses: [%s]\n  providers: [anthropic, openai]\n"
            "  credential_modes: [subscription, api-key]\n  work_root: %s\n%s%s---\n"
            % (HOST, responsible, ", ".join(harnesses), work_root,
               "  max_agents: %d\n" % max_agents if max_agents is not None else "",
               resources_yaml))


def agent(title: str, responsible: str, *, harness: str = "claude",
          memory: str = "") -> str:
    provider = "openai" if harness == "codex" else "anthropic"
    return ("---\ntype: Agent\ntitle: %s\nresponsible: human:%s\nteam: equipe\n"
            "capabilities: [read, propose]\nharness: %s\nprovider: %s\n"
            "credential_mode: subscription\n%s---\n"
            % (title, responsible, harness, provider,
               "memory:\n  repository: %s\n" % memory if memory else ""))


def admission(title: str) -> str:
    return ("---\ntype: Placement\ntitle: %s@%s\nagent: %s\nhosts: [%s]\n"
            "credential_mode: subscription\n---\n" % (title, HOST, title, HOST))


def package(ident: str, responsible: str) -> str:
    return ("---\ntype: WorkPackage\ntitle: jalon %s\nkind: milestone\nresponsible: "
            "human:%s\nteam: equipe\n---\n" % (ident, responsible))


#: sous-agent Claude Code tel que Acme les range dans son canon :
#: `type: Agent`, `name`, `tools`, pas de `title`
SOUS_AGENT = ("---\ntype: Agent\nname: %s\ndescription: \"sous-agent Claude Code\"\n"
              "tools: Read, Grep, Bash\nmodel: haiku\n---\n\nTu relis les PR.\n")


def codes(findings, severity=None) -> set:
    return {f.code for f in findings if severity is None or f.severity == severity}


class _Canons(_TmpMixin):
    """Construit des canons git sur disque (racine `home` dans chacun)."""

    def setUp(self) -> None:
        super().setUp()
        self.workspace = self.make_tmp()

    def acme(self, *, scope: str | None = "ameesh", name: str = "t", human: str = "bob",
            agents=("t1",), harness: str = "codex", host_text: str | None = None,
            extra_members: str = "") -> str:
        """Canon imitant Acme : sous-agents Claude Code hors périmètre,
        fiches ameesh sous `ameesh/`."""
        _bare, clone = publish(self.workspace, name)
        extra = "ameesh:\n  scope: %s\n" % scope if scope is not None else ""
        write(clone, "federation.yaml", federation(ID_T, name, extra, extra_members))
        for sub in ("pr-reviewer", "qa-api-tester", "deployment-message"):
            write(clone, "org/agent-harness/agents/%s.md" % sub, SOUS_AGENT % sub)
        write(clone, "org/index.md", "---\ntype: Index\ntitle: org\n---\n")
        write(clone, "ameesh/membres/%s.md" % human, member(human))
        write(clone, "ameesh/hotes/%s.md" % HOST,
              host_text if host_text is not None else host(human, harnesses=("codex",)))
        for title in agents:
            write(clone, "ameesh/agents/%s.md" % title, agent(title, human, harness=harness))
            write(clone, "ameesh/placements/%s.md" % title, admission(title))
        write(clone, "ameesh/plan/pt.md", package("pt", human))
        commit_all(clone, "canon %s" % ID_T)
        return clone

    def manaty(self, *, name: str = "a", human: str = "alice", agents=("a1",),
               host_text: str | None = None) -> str:
        """Canon sans périmètre (comme manaty) : tout le canon est lu."""
        _bare, clone = publish(self.workspace, name)
        write(clone, "federation.yaml", federation(ID_A, name))
        write(clone, "membres/%s.md" % human, member(human))
        write(clone, "hotes/%s.md" % HOST,
              host_text if host_text is not None else host(human, harnesses=("claude",)))
        for title in agents:
            write(clone, "agents/%s.md" % title, agent(title, human))
            write(clone, "placements/%s.md" % title, admission(title))
        commit_all(clone, "canon %s" % ID_A)
        return clone


# ==========================================================================
# 1. Périmètre (sans base)
# ==========================================================================

class PerimetreTest(_Canons, unittest.TestCase):
    def test_sans_perimetre_le_canon_partage_reste_invalide(self):
        loaded = canon.load(self.acme(scope=None), default=False)
        found = canon.validate(loaded)
        self.assertIn("fiche-title-missing", codes(found, canon.ERROR))
        self.assertEqual(canon_sync.assess(loaded, HOST, found)[0], canon_sync.CANON_INVALID)
        self.assertEqual(loaded.scopes, {})

    def test_avec_perimetre_le_canon_partage_devient_valide(self):
        loaded = canon.load(self.acme(), default=False)
        found = canon.validate(loaded)
        self.assertEqual(canon.errors(found), [], [f.to_dict() for f in found])
        self.assertEqual(canon_sync.assess(loaded, HOST, found)[0], canon_sync.CANON_OK)
        self.assertEqual([a.title for a in loaded.agents], ["t1"])
        self.assertEqual([m.title for m in loaded.members], ["bob"])
        self.assertEqual([p.id for p in loaded.packages], ["pt"])
        self.assertEqual(loaded.scopes, {"home": ["ameesh"]})
        # un seul constat d'information, agrégé
        info = [f for f in found if f.code == "ameesh-scope-ignored"]
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0].severity, canon.INFO)
        self.assertIn("3 fiche(s)", info[0].message)
        self.assertEqual(loaded.out_of_scope, {"home": 3})
        self.assertIn("home", loaded.loaded_members())
        # références qualifiées (canon non par défaut), chemin complet
        self.assertTrue(loaded.agent("t1").fiche.ref.startswith(
            "%s/home:ameesh/agents/t1.md@" % ID_T))

    def test_une_fiche_ameesh_hors_perimetre_est_ignoree(self):
        clone = self.acme()
        write(clone, "ailleurs/intrus.md", agent("intrus", "bob"))
        commit_all(clone)
        loaded = canon.load(clone, default=False)
        self.assertIsNone(loaded.agent("intrus"))
        self.assertEqual(loaded.out_of_scope, {"home": 4})
        self.assertEqual(canon.errors(canon.validate(loaded)), [])

    def test_perimetre_en_liste_et_frontmatter_casse_hors_perimetre(self):
        clone = self.acme(scope="[ameesh, equipe-b]")
        write(clone, "equipe-b/agents/t9.md", agent("t9", "bob", harness="codex"))
        write(clone, "equipe-b/placements/t9.md", admission("t9"))
        write(clone, "brouillons/casse.md", "---\ntype: Agent\ntitle: [non ferme\n---\n")
        commit_all(clone)
        loaded = canon.load(clone, default=False)
        self.assertEqual(sorted(a.title for a in loaded.agents), ["t1", "t9"])
        self.assertEqual(loaded.scopes, {"home": ["ameesh", "equipe-b"]})
        self.assertEqual(canon.errors(canon.validate(loaded)), [])

    def test_perimetre_invalide_rien_n_est_lu(self):
        for bad in ("../ailleurs", "/abs", "\"[]\"", "\"a*\"", ".cache"):
            with self.subTest(scope=bad):
                clone = self.acme(scope=bad, name="t-%d" % abs(hash(bad)))
                loaded = canon.load(clone, default=False)
                found = canon.validate(loaded)
                self.assertIn("ameesh-scope-invalid", codes(found, canon.ERROR))
                self.assertEqual(loaded.agents, [])
                self.assertNotIn("home", loaded.loaded_members())
                self.assertEqual(canon_sync.assess(loaded, HOST, found)[0],
                                 canon_sync.CANON_INVALID)

    def test_perimetre_introuvable(self):
        loaded = canon.load(self.acme(scope="fiches-ameesh"), default=False)
        found = canon.validate(loaded)
        self.assertIn("ameesh-scope-missing", codes(found, canon.ERROR))
        self.assertEqual(loaded.agents, [])
        self.assertNotIn("home", loaded.loaded_members())

    def test_cle_inconnue_avertie(self):
        clone = self.acme()
        write(clone, "federation.yaml", federation(
            ID_T, "t", "ameesh:\n  scope: ameesh\n  portee: tout\n"))
        commit_all(clone)
        found = canon.validate(canon.load(clone, default=False))
        self.assertIn("ameesh-key-unknown", codes(found, canon.WARNING))
        self.assertEqual(canon.errors(found), [])

    def test_perimetre_d_un_autre_membre(self):
        """`members[].ameesh.scope` borne le bundle de CE membre ; un membre
        sans la clé est lu en entier (comportement historique)."""
        _bare, outil = publish(self.workspace, "outil")
        write(outil, "knowledge/index.md", "---\ntype: Index\ntitle: outil\n---\n")
        write(outil, "knowledge/agents/sous-agent.md", SOUS_AGENT % "sous-agent")
        write(outil, "knowledge/ameesh/agents/t5.md", agent("t5", "bob", harness="codex"))
        write(outil, "knowledge/ameesh/placements/t5.md", admission("t5"))
        commit_all(outil, "membre outil")
        entry = ("  - id: outil\n    ref: main\n    bundle: knowledge\n"
                 "    entrypoint: index.md\n    workspace_path: outil\n"
                 "    enforcement: migrating\n    ameesh:\n      scope: ameesh\n")
        loaded = canon.load(self.acme(extra_members=entry), default=False)
        found = canon.validate(loaded)
        self.assertEqual(canon.errors(found), [], [f.to_dict() for f in found])
        self.assertEqual(sorted(a.title for a in loaded.agents), ["t1", "t5"])
        self.assertEqual(loaded.scopes, {"home": ["ameesh"], "outil": ["ameesh"]})
        self.assertEqual(loaded.out_of_scope, {"home": 3, "outil": 1})
        # sans périmètre déclaré pour le membre : lu en entier, le sous-agent
        # sans title redevient une erreur
        entry_libre = entry.replace("    ameesh:\n      scope: ameesh\n", "")
        loaded = canon.load(self.acme(name="t2", extra_members=entry_libre), default=False)
        self.assertIn("fiche-title-missing", codes(canon.validate(loaded), canon.ERROR))

    def test_sans_cle_rien_ne_change(self):
        loaded = canon.load(self.manaty())
        found = canon.validate(loaded)
        self.assertEqual(canon.errors(found), [])
        self.assertEqual((loaded.scopes, loaded.out_of_scope), ({}, {}))
        self.assertFalse([f for f in found if f.code.startswith("ameesh-")])


# ==========================================================================
# 2. Hôte par canon : limites physiques (sans base)
# ==========================================================================

class LimitesPhysiquesTest(_Canons, unittest.TestCase):
    def deux(self):
        a = canon.load(self.manaty(host_text=host(
            "alice", harnesses=("claude",), max_agents=8,
            resources_yaml="  resources:\n    max_swap_used: 16GiB\n"
                           "    min_mem_available: 1GiB\n")))
        t = canon.load(self.acme(host_text=host(
            "bob", harnesses=("codex",), max_agents=3,
            resources_yaml="  resources:\n    max_swap_used: 4GiB\n"
                           "    min_disk_free: 10GiB\n    min_mem_available: 512MiB\n")),
            default=False)
        return a, t

    def test_les_plus_strictes_avec_provenance(self):
        a, t = self.deux()
        got = resources.host_limits([a, t], HOST)
        defaut = resources.thresholds(None)
        self.assertEqual(got["limits"], {
            "min_mem_available": GIB,                  # A (plus haut plancher)
            "max_swap_used": 4 * GIB,                  # T (plus bas plafond)
            "max_load": defaut["max_load"],            # personne ne le déclare
            "min_disk_free": 10 * GIB,                 # T
        })
        self.assertEqual(got["max_agents"], 3)
        self.assertEqual(got["origin"], {
            "min_mem_available": ID_A, "max_swap_used": ID_T,
            "max_load": resources.DEFAULT_ORIGIN, "min_disk_free": ID_T,
            "max_agents": ID_T})
        self.assertEqual(got["fiches"], [ID_A, ID_T])
        # une fiche qui se tait n'impose pas le défaut à celle qui déclare
        seul = resources.host_limits([a], HOST)
        self.assertEqual(seul["limits"]["max_swap_used"], 16 * GIB)
        # hôte inconnu : valeurs par défaut prudentes
        self.assertEqual(resources.host_limits([a, t], "autre")["limits"], defaut)
        self.assertIsNone(resources.host_limits([a, t], "autre")["max_agents"])

    def test_l_executeur_applique_les_plus_strictes(self):
        a, t = self.deux()
        runner = types.SimpleNamespace(host=HOST, _host_limits=None)
        runner_mod.Runner.refresh_host_limits(runner, a, t)
        self.assertEqual(runner._host_limits["max_swap_used"], 4 * GIB)
        self.assertEqual(runner._host_limits["min_mem_available"], GIB)
        self.assertEqual(runner._host_max_agents, 3)
        self.assertEqual(runner._host_limits_origin["max_agents"], ID_T)

    def test_max_agents_physique_borne_les_personas(self):
        class Fil:
            def __init__(self, ephemeral=False):
                self.agent = {"ephemeral": ephemeral}

            def is_alive(self):
                return True

        runner = types.SimpleNamespace(lock=threading.Lock(), _host_max_agents=2,
                                       workers={"x": Fil(), "e": Fil(ephemeral=True)})
        admits = lambda row: runner_mod.Runner.admits_new_worker(runner, row)  # noqa: E731
        self.assertTrue(admits({"name": "y"}))                  # 1 persona < 2
        runner.workers["y"] = Fil()
        self.assertFalse(admits({"name": "z"}))                 # 2 personas : plein
        self.assertTrue(admits({"name": "z", "ephemeral": True}))  # éphémère : hors compte
        runner._host_max_agents = None
        self.assertTrue(admits({"name": "z"}))                  # pas de maximum déclaré

    def test_max_agents_de_chaque_canon_compte_ses_admissions(self):
        """Chaque canon juge `max_agents` sur SES admissions : 2 + 2 agents
        sur un hôte à max 2 dans chaque canon ne déclenche rien."""
        a = canon.load(self.manaty(agents=("a1", "a2"), host_text=host(
            "alice", harnesses=("claude",), max_agents=2)))
        t = canon.load(self.acme(agents=("t1", "t2"), host_text=host(
            "bob", harnesses=("codex",), max_agents=2)), default=False)
        for one in (a, t):
            self.assertNotIn("host-max-agents", codes(canon.validate(one)))
        t3 = canon.load(self.acme(name="t3", agents=("t1", "t2", "t3"), host_text=host(
            "bob", harnesses=("codex",), max_agents=2)), default=False)
        self.assertIn("host-max-agents", codes(canon.validate(t3), canon.ERROR))


# ==========================================================================
# 3. Avec la base : admission, humains, affichage par canon
# ==========================================================================

class _AvecBase(_Canons, PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM canon_state")
        self.db.execute("TRUNCATE authenticators, authenticator_syncs RESTART IDENTITY CASCADE")

    def row(self, name: str) -> dict | None:
        rows = self.db.query("SELECT * FROM agent_registry WHERE name = %s", (name,))
        return rows[0] if rows else None

    def claimable(self) -> list[str]:
        return sorted(r["name"] for r in registry.claimable(self.db, HOST,
                                                            require_responsible=True))


class AdmissionParCanonTest(_AvecBase):
    def test_admission_avec_la_fiche_host_de_son_canon(self):
        # A n'admet que claude sur pc, T que codex : chaque agent est jugé par
        # la fiche de SON canon
        a = canon.load(self.manaty())
        t = canon.load(self.acme(agents=("t1",)), default=False)
        self.assertTrue(placement.evaluate(t, "t1", HOST).ok)
        self.assertFalse(placement.evaluate(a, "t1", HOST).ok)   # A ne le connaît pas
        canon_sync.sync(self.db, a, HOST)
        canon_sync.sync(self.db, t, HOST)
        self.assertEqual((self.row("a1")["placement_ok"], self.row("t1")["placement_ok"]),
                         (True, True))
        self.assertEqual(self.claimable(), ["a1", "t1"])

    def test_refus_par_la_fiche_de_son_canon(self):
        # t1 en claude : admis par la fiche Host de A, mais pas par celle de T
        t = canon.load(self.acme(harness="claude"), default=False)
        found = canon.validate(t)
        self.assertIn("placement-policy-violation", codes(found, canon.ERROR))
        canon_sync.sync(self.db, canon.load(self.manaty()), HOST)
        report = canon_sync.sync(self.db, t, HOST, found)
        self.assertFalse(self.row("t1")["placement_ok"])
        self.assertIn("non admis par l'hôte", self.row("t1")["placement_diagnostic"])
        self.assertEqual(self.claimable(), ["a1"])
        self.assertEqual(report.status, canon_sync.CANON_OK)

    def test_perimetre_introuvable_ne_retire_rien(self):
        clone = self.acme()
        t = canon.load(clone, default=False)
        canon_sync.sync(self.db, t, HOST)
        self.assertEqual(self.claimable(), ["t1"])
        self.assertTrue(self.db.query("SELECT 1 FROM work_packages WHERE id = 'pt' AND present"))
        # une faute de frappe dans le périmètre : rien n'est lu, rien n'est
        # retiré, mais plus rien n'est réclamable (fail closed)
        write(clone, "federation.yaml", federation(ID_T, "t", "ameesh:\n  scope: amesh\n"))
        commit_all(clone)
        report = canon_sync.sync(self.db, canon.load(clone, default=False), HOST)
        self.assertEqual(report.status, canon_sync.CANON_INVALID)
        self.assertNotEqual(self.row("t1")["status"], "stopped")
        self.assertEqual(self.claimable(), [])
        self.assertTrue(self.db.query("SELECT 1 FROM work_packages WHERE id = 'pt' AND present"))


class HumainsParCanonTest(_AvecBase):
    def test_humain_resolu_dans_le_canon_de_l_agent(self):
        a = canon.load(self.manaty())               # Member alice
        clone = self.acme()                          # Member bob
        write(clone, "ameesh/agents/t2.md", agent("t2", "alice", harness="codex"))
        write(clone, "ameesh/placements/t2.md", admission("t2"))
        write(clone, "ameesh/plan/pa.md", package("pa", "alice"))
        commit_all(clone)
        t = canon.load(clone, default=False)
        self.assertEqual(t.resolve_human("human:bob"), "human:bob")
        self.assertIsNone(t.resolve_human("human:alice"))       # Member d'un AUTRE canon
        self.assertEqual(a.resolve_human("human:alice"), "human:alice")
        found = canon.validate(t)
        self.assertIn("agent-responsible-unresolved",
                      {f.code for f in found if f.agent == "t2"})
        self.assertIn("package-responsible-unresolved",
                      {f.code for f in found if f.package == "pa"})
        canon_sync.sync(self.db, a, HOST)
        canon_sync.sync(self.db, t, HOST, found)
        self.assertEqual(self.row("t1")["responsible"], "human:bob")
        self.assertIsNone(self.row("t2")["responsible"])
        self.assertEqual(self.claimable(), ["a1", "t1"])

    def test_visibilite_humains_et_comptes_du_canon_de_la_persona(self):
        a = canon.load(self.manaty(host_text=host("alice", harnesses=("claude",))))
        clone = self.acme()
        write(clone, "ameesh/membres/bob.md", member("bob", login="bob-gh"))
        commit_all(clone)
        t = canon.load(clone, default=False)
        self.assertEqual(visibility.host_humans(t, HOST), ["human:bob"])
        self.assertEqual(visibility.host_humans(a, HOST), ["human:alice"])
        self.assertEqual(visibility.human_login(t, "human:bob"), "bob-gh")
        self.assertIsNone(visibility.human_login(a, "human:bob"))


class AffichageParCanonTest(_AvecBase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.manaty(agents=("a1", "a2"))
        self.t = self.acme(agents=("t1",))

    def cli_env(self, **extra) -> dict:
        return self.env(AMEESH_CANONS="%s:%s" % (self.a, self.t), AMEESH_HOST=HOST,
                        AMEESH_CANON_REF="origin/main", **extra)

    def test_canon_check_par_canon(self):
        # L44 : un canon non par défaut sans `ref` de confiance s'amorce une fois
        proc = self.mesh("canon", "sync", "--host", HOST, "--bootstrap-ref", "main",
                         env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # un verdict enregistré qui ne vaut plus, pour chaque canon
        self.db.execute("UPDATE agent_registry SET placement_profile = 'ancien' "
                        "WHERE name IN ('a1', 't1')")
        data = json.loads(self.mesh("canon", "check", "--host", HOST, "--json",
                                    env=self.cli_env()).stdout)
        blocs = {c["canon_id"]: c for c in data["canons"]}
        self.assertEqual(sorted(r["agent"] for r in blocs[ID_A]["registry_placements"]),
                         ["a1", "a2"])
        self.assertEqual([r["agent"] for r in blocs[ID_T]["registry_placements"]], ["t1"])
        self.assertEqual(sorted(v["agent"] for v in blocs[ID_A]["placements"]), ["a1", "a2"])
        self.assertEqual(blocs[ID_T]["infos"], 1)
        self.assertEqual(blocs[ID_T]["errors"], 0)
        texte = self.mesh("canon", "check", "--host", HOST, env=self.cli_env()).stdout
        bloc_a, bloc_t = texte.split("== canon %s" % ID_T)
        self.assertRegex(bloc_a, r"\n  a1 +admis")
        self.assertIn("registre  : a1", bloc_a)
        self.assertNotRegex(bloc_a, r"\n  t1 +|registre  : t1")
        self.assertNotRegex(bloc_t, r"\n  a[12] +|registre  : a[12]")
        self.assertIn("registre  : t1", bloc_t)
        self.assertIn("ameesh-scope-ignored", bloc_t)
        self.assertIn("1 information(s)", bloc_t)

    def test_placement_check_par_canon(self):
        data = json.loads(self.mesh("placement", "check", "--json", env=self.cli_env()).stdout)
        self.assertEqual([c["canon_id"] for c in data["canons"]], [ID_A, ID_T])
        self.assertEqual([sorted(e["agent"] for e in c["agents"]) for c in data["canons"]],
                         [["a1", "a2"], ["t1"]])
        texte = self.mesh("placement", "check", env=self.cli_env())
        self.assertEqual(texte.returncode, 0, texte.stdout + texte.stderr)
        bloc_a, bloc_t = texte.stdout.split("== canon %s" % ID_T)
        self.assertNotIn("agent t1", bloc_a)
        self.assertIn("agent t1", bloc_t)
        self.assertNotIn("agent a1", bloc_t)
        # --agent : seul le canon qui le déclare
        data = json.loads(self.mesh("placement", "check", "--agent", "t1", "--json",
                                    env=self.cli_env()).stdout)
        self.assertEqual([e["agent"] for e in data["agents"]], ["t1"])
        self.assertTrue(data["agents"][0]["placements"][0]["placement_ok"])
        # --canon <id>
        data = json.loads(self.mesh("placement", "check", "--canon", ID_T, "--json",
                                    env=self.cli_env()).stdout)
        self.assertEqual([e["agent"] for e in data["agents"]], ["t1"])

    def test_canon_show_perimetre(self):
        proc = self.mesh("canon", "show", "--canon", ID_T, env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("périmètre  : home → ameesh (3 fiche(s)", proc.stdout)
        data = json.loads(self.mesh("canon", "show", "--canon", ID_T, "--json",
                                    env=self.cli_env()).stdout)
        self.assertEqual(data["ameesh_scope"], {"home": ["ameesh"]})
        self.assertEqual(data["out_of_scope"], {"home": 3})

    def test_hosts_montre_la_provenance(self):
        resources_t = ("  resources:\n    max_swap_used: 4GiB\n")
        write(self.t, "ameesh/hotes/%s.md" % HOST,
              host("bob", harnesses=("codex",), max_agents=3, resources_yaml=resources_t))
        commit_all(self.t)
        proc = self.mesh("hosts", HOST, "--json", env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        objet = json.loads(proc.stdout.splitlines()[0])
        self.assertEqual(objet["max_agents"], 3)
        self.assertEqual(objet["limits"]["max_swap_used"], 4 * GIB)
        self.assertEqual(objet["limits_origin"]["max_swap_used"], ID_T)
        self.assertEqual(objet["limits_origin"]["max_agents"], ID_T)
        self.assertEqual(objet["limits_origin"]["max_load"], resources.DEFAULT_ORIGIN)
        self.assertEqual([(f["canon"], f["admissions"]) for f in objet["fiches"]],
                         [(ID_A, 2), (ID_T, 1)])
        texte = self.mesh("hosts", HOST, env=self.cli_env()).stdout
        self.assertIn("max_agents 3 [%s]" % ID_T, texte)
        self.assertIn("3 admission(s)", texte)
        self.assertIn("fiches     %s" % ID_A, texte)


if __name__ == "__main__":
    unittest.main()
