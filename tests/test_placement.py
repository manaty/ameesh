# SPDX-License-Identifier: AGPL-3.0-only
"""Placement gouverné (L3, C4, R18) : verdict écrit par `canon sync`, condition
de réclamation, `ameesh placement check`, diagnostics."""
from __future__ import annotations

import json
import os
import unittest

from ameesh import canon, canon_sync, placement, registry

from . import test_canon
from .test_canon import EXAMPLE, commit_all, fiche, git, publish, write

ATELIER = dict(harnesses=["claude", "codex", "deepseek"],
               providers=["anthropic", "openai", "deepseek"],
               credential_modes=["api-key", "subscription"], max_agents="4")


def host_card(title: str, responsible: str = "human:bruno", **policy) -> str:
    """Fiche Host ; la politique est un mapping en ligne (clé absente = tout admis)."""
    items = ["%s: %s" % (key, "[%s]" % ", ".join(value) if isinstance(value, list) else value)
             for key, value in policy.items()]
    return fiche(type="Host", title=title, responsible=responsible,
                 policy="{%s}" % ", ".join(items))


# ==========================================================================
# évaluation (sans base)
# ==========================================================================

class EvaluateTest(test_canon._TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.example_copy()

    def load(self) -> canon.Canon:
        return canon.load(self.root, untrusted=True)

    def test_admis(self):
        verdict = placement.evaluate(self.load(), "orchestre", "atelier")
        self.assertEqual((verdict.ok, verdict.diagnostic, verdict.credential_mode),
                         (True, "", "subscription"))
        self.assertTrue(verdict.ref.startswith("canon:placements/orchestre-atelier.md@"))

    def test_refus_fail_closed(self):
        write(self.root, "placements/orchestre-banc.md", fiche(
            type="Placement", agent="orchestre", host="banc", credential_mode="api-key"))
        write(self.root, "placements/x.md", fiche(type="Placement", agent="fantome",
                                                  host="atelier"))
        write(self.root, "placements/relecteur-atelier.md", fiche(
            type="Placement", agent="relecteur", host="nulle-part"))
        loaded = self.load()
        cas = {
            ("orchestre", "atelier"): "placement ambigu : orchestre placé 2 fois",
            ("ouvrier", "atelier"): "aucun placement de ouvrier sur atelier (placé sur : banc)",
            ("fantome", "atelier"): "fiche Agent fantome introuvable",
            ("relecteur", "nulle-part"): "hôte nulle-part sans fiche Host",
        }
        for (agent, host), attendu in cas.items():
            with self.subTest(agent=agent, host=host):
                verdict = placement.evaluate(loaded, agent, host)
                self.assertIs(verdict.ok, False)
                self.assertIn(attendu, verdict.diagnostic)

    def test_politique_violee(self):
        write(self.root, "hotes/atelier.md", host_card(
            "atelier", **dict(ATELIER, harnesses=["claude", "deepseek"],
                              credential_modes=["api-key"])))
        loaded = self.load()
        verdict = placement.evaluate(loaded, "relecteur", "atelier")
        self.assertIs(verdict.ok, False)
        self.assertIn("harnais codex non admis par l'hôte atelier", verdict.diagnostic)
        verdict = placement.evaluate(loaded, "orchestre", "atelier")
        self.assertIn("mode d'identifiants subscription non admis", verdict.diagnostic)

    def test_propositions(self):
        loaded = self.load()
        choix = {c["host"]: c for c in placement.proposals(loaded, "orchestre")}
        self.assertTrue(choix["atelier"]["admissible"])
        self.assertTrue(choix["atelier"]["current"])
        self.assertEqual(choix["atelier"]["credential_modes"], ["api-key", "subscription"])
        self.assertEqual(choix["atelier"]["credential_mode"], "subscription")
        self.assertFalse(choix["banc"]["admissible"])
        textes = " ".join(choix["banc"]["reasons"])
        self.assertIn("harnais claude", textes)
        self.assertIn("fournisseur anthropic", textes)
        # le mode n'est pas un motif de refus : le placement peut le fixer
        choix = {c["host"]: c for c in placement.proposals(loaded, "relecteur")}
        self.assertTrue(choix["banc"]["admissible"])
        self.assertEqual(choix["banc"]["credential_mode"], "api-key")
        # hôte complet (max_agents, sans compter l'agent lui-même)
        write(self.root, "hotes/banc.md", host_card(
            "banc", "human:alice", harnesses=["deepseek", "codex"],
            providers=["deepseek", "openai"], credential_modes=["api-key"], max_agents="1"))
        choix = {c["host"]: c for c in placement.proposals(self.load(), "relecteur")}
        self.assertFalse(choix["banc"]["admissible"])
        self.assertIn("complet", " ".join(choix["banc"]["reasons"]))
        choix = {c["host"]: c for c in placement.proposals(self.load(), "ouvrier")}
        self.assertTrue(choix["banc"]["admissible"])


# ==========================================================================
# sync et réclamation (base réelle)
# ==========================================================================

class PlacementTest(test_canon._CanonDbCase):
    def claimable(self, host: str = "atelier", **kwargs) -> list[str]:
        kwargs.setdefault("require_responsible", False)
        return sorted(r["name"] for r in registry.claimable(self.db, host, **kwargs))

    def set_atelier(self, **changes) -> None:
        write(self.root, "hotes/atelier.md", host_card("atelier", **dict(ATELIER, **changes)))

    def test_placement_admis_reclamable(self):
        report = self.sync()
        placements = {a.agent: a.to_dict() for a in report.actions}
        self.assertEqual({n: p["placement_ok"] for n, p in placements.items()},
                         {"orchestre": True, "relecteur": True})
        for name in ("orchestre", "relecteur"):
            row = self.db.query("SELECT placement_ok, placement_diagnostic, placement_ref "
                                "FROM agent_registry WHERE name = %s", (name,))[0]
            self.assertEqual((row["placement_ok"], row["placement_diagnostic"]), (True, ""))
            self.assertTrue(row["placement_ref"].startswith(
                "canon:placements/%s-atelier.md@" % name))
        self.assertEqual(self.claimable(), ["orchestre", "relecteur"])
        self.assertEqual(self.claimable(require_responsible=True), ["orchestre", "relecteur"])
        self.assertIsNotNone(registry.claim(self.db, "relecteur", "runner-x", 600))
        # inchangé au second passage : le verdict est une colonne déclarative comme les autres
        self.assertEqual({a.agent: a.action for a in self.sync().actions},
                         {"orchestre": "inchangé", "relecteur": "inchangé"})

    def test_harnais_non_admis_par_l_hote(self):
        self.sync()
        self.set_atelier(harnesses=["claude", "deepseek"])
        report = self.sync()
        relecteur = {a.agent: a for a in report.actions}["relecteur"]
        self.assertEqual(relecteur.action, "mis à jour")
        self.assertIn("placement refusé", relecteur.detail)
        self.assertIn("harnais codex non admis par l'hôte atelier", relecteur.detail)
        self.assertIs(relecteur.to_dict()["placement_ok"], False)
        # erreur propre à l'agent : l'hôte reste `ok`, seul relecteur est fermé…
        self.assertEqual(report.status, "ok")
        self.assertEqual(canon_sync.state(self.db, "atelier")["status"], "ok")
        # … même sans exiger le responsable (le placement suffit à refuser)
        self.assertEqual(self.claimable(require_responsible=False), ["orchestre"])
        self.assertIsNone(registry.claim(self.db, "relecteur", "runner-x", 600))
        row = self.db.query("SELECT placement_ok, placement_diagnostic FROM agent_registry "
                            "WHERE name = 'relecteur'")[0]
        self.assertIs(row["placement_ok"], False)
        self.assertIn("harnais codex non admis", row["placement_diagnostic"])

    def test_mode_d_identifiants_non_admis(self):
        self.set_atelier(credential_modes=["api-key"])
        report = self.sync()
        orchestre = {a.agent: a for a in report.actions}["orchestre"]
        self.assertIs(orchestre.placement.ok, False)
        self.assertIn("mode d'identifiants subscription non admis par l'hôte atelier",
                      orchestre.placement.diagnostic)
        self.assertEqual(self.claimable(), ["relecteur"])
        # le responsable du projet change le mode du placement : de nouveau admis
        write(self.root, "placements/orchestre-atelier.md", fiche(
            type="Placement", title="orchestre@atelier", agent="orchestre", host="atelier",
            credential_mode="api-key", cwd="~/acme/acme-web"))
        self.sync()
        self.assertEqual(self.claimable(), ["orchestre", "relecteur"])
        self.assertEqual(self.row("orchestre")["credential_mode"], "api-key")

    def test_agent_du_canon_sans_placement_sur_l_hote(self):
        self.sync()
        lease = registry.claim(self.db, "orchestre", "runner-x", 600)
        epoch = int(lease["lease_epoch"])
        self.assertTrue(registry.begin_turn(self.db, "orchestre", "runner-x", epoch, "tour"))
        write(self.root, "placements/orchestre-atelier.md", None)   # fiche Agent gardée
        report = self.sync()
        self.assertEqual({a.agent: a.action for a in report.actions}["orchestre"],
                         "arrêt demandé")
        row = self.db.query("SELECT placement_ok, placement_diagnostic, lease_owner, "
                            "lease_epoch, status FROM agent_registry "
                            "WHERE name = 'orchestre'")[0]
        self.assertIs(row["placement_ok"], False)
        self.assertIn("aucun placement de orchestre sur atelier", row["placement_diagnostic"])
        self.assertEqual((row["lease_owner"], int(row["lease_epoch"]), row["status"]),
                         ("runner-x", epoch, "running"))
        # fin du tour : avant la synchronisation suivante, plus réclamable sur cet hôte
        self.assertTrue(registry.end_turn(self.db, "orchestre", "runner-x", epoch,
                                          status="idle", status_text="tour fini"))
        self.assertTrue(registry.release(self.db, "orchestre", "runner-x", epoch))
        self.assertEqual(self.claimable(), ["relecteur"])
        self.assertIsNone(registry.claim(self.db, "orchestre", "runner-y", 600))

    def test_changement_d_hote_hors_sync(self):
        """`ameesh run register` sur une autre machine n'emporte pas le verdict."""
        self.sync("banc")
        self.sync()
        self.assertEqual(self.claimable("banc"), ["ouvrier"])
        registry.upsert(self.db, "ouvrier", host="atelier")
        row = self.db.query("SELECT placement_ok, placement_diagnostic FROM agent_registry "
                            "WHERE name = 'ouvrier'")[0]
        self.assertIsNone(row["placement_ok"])
        self.assertIn("hôte changé hors de « ameesh canon sync »", row["placement_diagnostic"])
        self.assertEqual(self.claimable(), ["orchestre", "relecteur"])     # canon ok sur atelier
        # sync suit le canon : ouvrier retourne sur le banc, évalué là-bas
        self.assertEqual({a.agent: a.action for a in self.sync().actions}["ouvrier"], "déplacé")
        self.assertEqual(self.claimable("banc"), [])
        self.sync("banc")
        self.assertEqual(self.claimable("banc"), ["ouvrier"])

    def test_politique_modifiee_puis_sync(self):
        """Par le canon approuvé (git) : la politique fusionnée s'applique au sync."""
        workspace = self.make_tmp()
        _bare, clone = publish(workspace, "acme", EXAMPLE)
        load = lambda: canon.load(clone)  # noqa: E731
        canon_sync.sync(self.db, load(), "atelier")
        self.assertEqual(self.claimable(), ["orchestre", "relecteur"])
        write(clone, "hotes/atelier.md", host_card("atelier", **dict(
            ATELIER, harnesses=["claude", "deepseek"])))
        canon_sync.sync(self.db, load(), "atelier")                # non commité : ignoré
        self.assertEqual(self.claimable(), ["orchestre", "relecteur"])
        head = commit_all(clone, "atelier : plus de codex")
        canon_sync.sync(self.db, load(), "atelier")
        self.assertEqual(self.claimable(), ["orchestre"])
        self.assertTrue(self.row("relecteur")["canon_ref"].endswith("@" + head))
        git(clone, "revert", "--no-edit", "HEAD")
        git(clone, "push", "-q", "origin", "HEAD")
        git(clone, "fetch", "-q", "origin")
        canon_sync.sync(self.db, load(), "atelier")
        self.assertEqual(self.claimable(), ["orchestre", "relecteur"])
        self.assertEqual(self.claimable(require_responsible=True), ["orchestre", "relecteur"])

    def test_agent_manuel_non_affecte(self):
        registry.upsert(self.db, "manuel", harness="codex", host="atelier")
        self.assertEqual(self.claimable(), ["manuel"])               # aucun état, aucun verdict
        self.set_atelier(harnesses=["deepseek"], providers=["deepseek"])
        report = self.sync()
        self.assertEqual({a.agent: a.action for a in report.actions}["manuel"], "hors canon")
        self.assertEqual(self.claimable(), ["manuel"])
        row = self.db.query("SELECT placement_ok, placement_diagnostic, placement_ref "
                            "FROM agent_registry WHERE name = 'manuel'")[0]
        self.assertEqual(row, {"placement_ok": None, "placement_diagnostic": None,
                               "placement_ref": None})
        registry.upsert(self.db, "manuel", host="banc")             # rien à réévaluer
        self.assertIsNone(self.db.query("SELECT placement_diagnostic FROM agent_registry "
                                        "WHERE name = 'manuel'")[0]["placement_diagnostic"])
        self.assertEqual(self.claimable("banc"), ["manuel"])
        self.assertIsNotNone(registry.claim(self.db, "manuel", "runner-x", 600))

    def test_bail_en_cours_jamais_touche(self):
        self.sync()
        lease = registry.claim(self.db, "relecteur", "runner-x", 600)
        epoch = int(lease["lease_epoch"])
        registry.set_session(self.db, "relecteur", "sess-relecteur")
        self.assertTrue(registry.begin_turn(self.db, "relecteur", "runner-x", epoch, "tour"))
        colonnes = ("lease_owner", "lease_epoch", "lease_expires_ts", "status", "status_text",
                    "session_id", "pending_prompt", "current_prompt", "spent_usd", "turns")
        avant = {k: self.row("relecteur")[k] for k in colonnes}
        self.set_atelier(harnesses=["claude"])
        self.sync()
        self.assertEqual({k: self.row("relecteur")[k] for k in colonnes}, avant)
        self.assertIs(self.db.query("SELECT placement_ok FROM agent_registry "
                                    "WHERE name = 'relecteur'")[0]["placement_ok"], False)
        # le bail vit sa vie : il se vérifie, se renouvelle, clôt son tour et se rend
        self.assertEqual(registry.lease_matches(self.db, "relecteur", "runner-x", epoch),
                         (True, ""))
        self.assertIsNotNone(registry.renew(self.db, "relecteur", "runner-x", epoch, 600))
        self.assertTrue(registry.end_turn(self.db, "relecteur", "runner-x", epoch,
                                          status="idle", status_text="tour fini"))
        self.assertTrue(registry.release(self.db, "relecteur", "runner-x", epoch))
        # mais aucune nouvelle réclamation
        self.assertNotIn("relecteur", self.claimable())
        self.assertIsNone(registry.claim(self.db, "relecteur", "runner-y", 600))
        self.assertEqual(self.row("relecteur")["session_id"], "sess-relecteur")

    def test_verdict_absent_pas_de_reclamation(self):
        """Agent du canon synchronisé avant 0022 (verdict NULL) : fermé jusqu'au sync."""
        self.sync()
        self.db.execute("UPDATE agent_registry SET placement_ok = NULL, "
                        "placement_diagnostic = NULL, placement_ref = NULL")
        self.assertEqual(self.claimable(), [])
        overview = {r["name"]: r for r in registry.overview(self.db)}
        self.assertFalse(overview["orchestre"]["canon_claim_ok"])
        self.assertEqual({a.agent: a.action for a in self.sync().actions},
                         {"orchestre": "mis à jour", "relecteur": "mis à jour"})
        self.assertEqual(self.claimable(), ["orchestre", "relecteur"])

    def test_ephemeres_heritent_du_placement(self):
        self.sync()
        registry.upsert(self.db, "manuel", harness="claude", host="atelier")
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice' "
                        "WHERE name = 'manuel'")
        canon_sync.spawn(self.db, "aide", "orchestre", 3600)
        canon_sync.spawn(self.db, "petit", "aide", 3600)
        canon_sync.spawn(self.db, "aidem", "manuel", 3600)
        self.assertEqual(self.claimable(),
                         ["aide", "aidem", "manuel", "orchestre", "petit", "relecteur"])
        self.assertEqual(self.row("aide")["canon_ref"], None)
        self.set_atelier(harnesses=["codex", "deepseek"])            # plus de claude
        report = self.sync()
        self.assertNotIn("aide", {a.agent for a in report.actions})   # pas d'action
        self.assertEqual(self.claimable(), ["aidem", "manuel", "relecteur"])
        row = self.db.query("SELECT placement_ok, placement_diagnostic FROM agent_registry "
                            "WHERE name = 'petit'")[0]
        self.assertIs(row["placement_ok"], False)
        self.assertIn("harnais claude non admis", row["placement_diagnostic"])
        # R14 empêche déjà un agent refusé par le canon (responsable vidé) de créer
        with self.assertRaises(canon_sync.SpawnError):
            canon_sync.spawn(self.db, "tardif", "orchestre", 3600)
        self.set_atelier()
        self.sync()
        self.assertEqual(self.claimable(),
                         ["aide", "aidem", "manuel", "orchestre", "petit", "relecteur"])
        # le verdict est recopié à la création : refusé chez le créateur, refusé chez l'enfant
        self.db.execute("UPDATE agent_registry SET placement_ok = false "
                        "WHERE name = 'orchestre'")
        canon_sync.spawn(self.db, "tardif", "orchestre", 3600)
        self.assertNotIn("tardif", self.claimable())
        self.sync()
        self.assertEqual(self.claimable(), ["aide", "aidem", "manuel", "orchestre", "petit",
                                            "relecteur", "tardif"])
        # lignée rompue : tenue pour gouvernée, verdict refusé au sync
        self.db.execute("UPDATE agent_registry SET created_by = 'disparu' WHERE name = 'petit'")
        self.sync()
        self.assertNotIn("petit", self.claimable())
        self.assertIn("lignée invérifiable", self.db.query(
            "SELECT placement_diagnostic FROM agent_registry WHERE name = 'petit'"
        )[0]["placement_diagnostic"])

    def test_l_executeur_respecte_le_placement(self):
        """De bout en bout, sans toucher à runner.py : claimable filtre."""
        cwd = os.path.join(self.tmp, "work")
        os.makedirs(cwd, exist_ok=True)
        env = self.env(AMEESH_HOST="h-place")
        proc = self.runner("register", "place", "claude", "--cwd", cwd, "--prompt", "tour",
                           env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.db.execute("UPDATE agent_registry SET canon_ref = 'canon:agents/p.md@abc', "
                        "responsible = 'human:alice', placement_ok = false, "
                        "placement_diagnostic = 'harnais claude non admis' "
                        "WHERE name = 'place'")
        canon_sync.record_state(self.db, "h-place", canon_sync.CANON_OK, "")
        self.runner("--once", "--agents", "place", env=env)
        self.assertEqual(self.turns(), [])
        self.db.execute("UPDATE agent_registry SET placement_ok = true, "
                        "placement_diagnostic = '' WHERE name = 'place'")
        proc = self.runner("--once", "--agents", "place", env=env)
        self.assertEqual([t["harness"] for t in self.turns()], ["claude"], proc.stdout)


# ==========================================================================
# CLI : placement check (lecture seule), canon check, canon sync, list --json
# ==========================================================================

class PlacementCliTest(test_canon._CanonDbCase):
    def cli_env(self, **extra) -> dict:
        return self.env(AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1", **extra)

    def snapshot(self) -> list[dict]:
        return self.db.query("SELECT * FROM agent_registry ORDER BY name")

    def test_placement_check_propose_les_hotes_admissibles(self):
        self.sync()
        avant = self.snapshot()
        proc = self.mesh("placement", "check", "--agent", "relecteur", "--json",
                         env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        data = json.loads(proc.stdout)
        (entry,) = data["agents"]
        self.assertEqual([p["host"] for p in entry["placements"]], ["atelier"])
        self.assertTrue(entry["placements"][0]["placement_ok"])
        admis = {c["host"]: c for c in entry["admissible"]}
        self.assertEqual(sorted(admis), ["atelier", "banc"])
        self.assertEqual(admis["banc"]["credential_modes"], ["api-key"])
        self.assertTrue(admis["atelier"]["current"])
        # orchestre placé sur le banc : refusé, expliqué, et atelier proposé
        write(self.root, "placements/orchestre-atelier.md", fiche(
            type="Placement", agent="orchestre", host="banc", credential_mode="subscription"))
        proc = self.mesh("placement", "check", "--agent", "orchestre", "--json",
                         env=self.cli_env())
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        (entry,) = json.loads(proc.stdout)["agents"]
        (actuel,) = entry["placements"]
        self.assertEqual((actuel["host"], actuel["placement_ok"]), ("banc", False))
        for attendu in ("harnais claude non admis par l'hôte banc",
                        "fournisseur anthropic non admis",
                        "mode d'identifiants subscription non admis"):
            self.assertIn(attendu, actuel["placement_diagnostic"])
        self.assertEqual([c["host"] for c in entry["admissible"]], ["atelier"])
        self.assertEqual(entry["admissible"][0]["credential_mode"], "subscription")
        self.assertEqual([c["host"] for c in entry["refused"]], ["banc"])
        proc = self.mesh("placement", "check", "--agent", "orchestre", env=self.cli_env())
        self.assertEqual(proc.returncode, 1)
        for attendu in ("REFUSÉ", "non réclamable sur banc", "admissibles :", "atelier",
                        "mode subscription", "ameesh ne déplace aucun agent", "[NON APPROUVÉ]"):
            self.assertIn(attendu, proc.stdout)
        # tous les agents ; un agent inconnu est une erreur
        data = json.loads(self.mesh("placement", "check", "--json", env=self.cli_env()).stdout)
        self.assertEqual([e["agent"] for e in data["agents"]],
                         ["orchestre", "ouvrier", "relecteur"])
        self.assertEqual(data["refused"], 1)
        proc = self.mesh("placement", "check", "--agent", "fantome", env=self.cli_env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("aucune fiche Agent", proc.stdout)
        # lecture seule : rien n'a bougé au registre, aucun agent déplacé
        self.assertEqual(self.snapshot(), avant)
        self.assertEqual(self.row("orchestre")["host"], "atelier")

    def test_diagnostics_check_sync_list(self):
        write(self.root, "hotes/atelier.md", host_card("atelier", **dict(
            ATELIER, harnesses=["claude", "deepseek"])))
        registry.upsert(self.db, "manuel", harness="codex", host="atelier")
        proc = self.mesh("canon", "check", "--host", "atelier", env=self.cli_env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("placements sur atelier : 1 admis, 1 refusé(s)", proc.stdout)
        self.assertIn("relecteur", proc.stdout)
        self.assertIn("REFUSÉ — harnais codex non admis", proc.stdout)
        data = json.loads(self.mesh("canon", "check", "--host", "atelier", "--json",
                                    env=self.cli_env()).stdout)
        verdicts = {(p["agent"], p["host"]): p for p in data["placements"]}
        self.assertIs(verdicts[("relecteur", "atelier")]["placement_ok"], False)
        self.assertIs(verdicts[("orchestre", "atelier")]["placement_ok"], True)
        self.assertIs(verdicts[("ouvrier", "banc")]["placement_ok"], True)
        proc = self.mesh("canon", "sync", "--host", "atelier", env=self.cli_env())
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("placement refusé sur atelier : relecteur", proc.stdout)
        data = json.loads(self.mesh("canon", "sync", "--host", "atelier", "--json",
                                    env=self.cli_env()).stdout)
        actions = {a["agent"]: a for a in data["actions"]}
        self.assertIs(actions["relecteur"]["placement_ok"], False)
        self.assertIn("harnais codex", actions["relecteur"]["placement_diagnostic"])
        self.assertIs(actions["orchestre"]["placement_ok"], True)
        self.assertNotIn("placement_ok", actions["manuel"])
        rows = {r["name"]: r for r in json.loads(self.mesh("list", "--json").stdout)}
        self.assertEqual((rows["relecteur"]["placement_ok"], rows["relecteur"]["canon_claim_ok"]),
                         (False, False))
        self.assertIn("harnais codex non admis", rows["relecteur"]["placement_diagnostic"])
        self.assertEqual((rows["orchestre"]["placement_ok"], rows["orchestre"]["canon_claim_ok"],
                          rows["orchestre"]["placement_diagnostic"]), (True, True, ""))
        self.assertEqual((rows["manuel"]["placement_ok"], rows["manuel"]["canon_claim_ok"]),
                         (None, True))


if __name__ == "__main__":
    unittest.main()
