# SPDX-License-Identifier: AGPL-3.0-only
"""L242c : un agent est critique par un rôle de sa fiche (manaty/home#25), pas
par son nom. Rôles critiques réglables par organisation (`critical_roles` de
federation.yaml ; défaut orchestrateur, conception, auditeur, relecture) ;
`critical_agents` reste la surcharge explicite ; dès qu'une fiche déclare des
rôles, plus de déduction « a confié des lots dans les 30 derniers jours »."""
from __future__ import annotations

import os
import types
import unittest
from unittest import mock

from ameesh import assignments, reprise, runner, sous_utilisation as su


class _Canon:
    """Canon minimal : possède les lignes du registre dont `canon` vaut
    `name` ; ses fiches portent les rôles donnés ; `federation` facultatif.
    `parsed` : les fiches portent aussi `roles` analysé (comme `canon`)."""

    def __init__(self, name, cards, federation=None, readable=True, parsed=None):
        self.name, self.federation, self.readable = name, federation, readable
        self.agents = []
        for agent, roles in cards.items():
            card = types.SimpleNamespace(title=agent,
                                         fiche=types.SimpleNamespace(data={"roles": roles}))
            if parsed is not None:
                card.roles = parsed.get(agent)
            self.agents.append(card)

    def owns(self, row_canon):
        return row_canon == self.name

    def agent(self, name):
        return next((a for a in self.agents if a.title == name), None)


CARTES = {"orchestrateur": ["orchestrateur"], "mesh-design": ["orchestrateur", "conception"],
          "auditeur": ["auditeur"], "codex1": ["implementation", "relecture"],
          "dev7": ["implementation"], "rev": ["reviewer"]}


def _rows(canon="A"):
    return {name: {"name": name, "canon": canon} for name in CARTES}


class RolesCritiquesTest(unittest.TestCase):
    def test_defaut(self):
        self.assertEqual(su.critical_roles(_Canon("A", {})),
                         ("orchestrateur", "conception", "auditeur", "relecture"))

    def test_reglage_de_l_organisation(self):
        canon = _Canon("A", {}, federation={"critical_roles": ["Orchestrateur", "qa"]})
        self.assertEqual(su.critical_roles(canon), ("orchestrateur", "qa"))
        self.assertEqual(su.critical_roles(_Canon("A", {}, {"critical_roles": ["design"]})),
                         ("conception",))
        self.assertEqual(su.critical_roles(_Canon("A", {}, {"critical_roles": []})), ())
        self.assertEqual(su.critical_roles(_Canon("A", {}, {"critical_roles": "qa"})), ("qa",))

    def test_roles_declares(self):
        self.assertFalse(su.roles_declared([_Canon("A", {"x": []})]))
        self.assertTrue(su.roles_declared([_Canon("A", {"x": []}), _Canon("B", {"y": ["qa"]})]))


class CritiqueTest(unittest.TestCase):
    def fake(self, canons, declared=()):
        return types.SimpleNamespace(
            cfg=types.SimpleNamespace(critical_agents=declared), db=None,
            _audit_canons=tuple(canons), _orchestrators_declared=frozenset())

    def critical(self, fake, name, rows):
        with mock.patch("ameesh.registry.get", lambda db, n: rows.get(n)):
            return runner.Runner.is_critical(fake, name)

    def test_par_role_au_defaut(self):
        fake, rows = self.fake([_Canon("A", CARTES)]), _rows()
        for name in ("orchestrateur", "mesh-design", "auditeur", "codex1", "rev"):
            self.assertTrue(self.critical(fake, name, rows), name)
        self.assertFalse(self.critical(fake, "dev7", rows))

    def test_par_role_de_l_organisation(self):
        canon = _Canon("A", CARTES, federation={"critical_roles": ["orchestrateur"]})
        fake, rows = self.fake([canon]), _rows()
        self.assertTrue(self.critical(fake, "mesh-design", rows))
        self.assertFalse(self.critical(fake, "codex1", rows))
        # la persona auditeur suit le réglage, elle aussi
        self.assertFalse(self.critical(fake, "auditeur", rows))

    def test_surcharge_explicite(self):
        fake, rows = self.fake([_Canon("A", CARTES)], declared=("dev7",)), _rows()
        self.assertTrue(self.critical(fake, "dev7", rows))

    def test_fiche_d_un_autre_canon_ignoree(self):
        # #324 r1 : seul le canon propriétaire de l'agent compte, avec ses
        # propres rôles critiques
        a = _Canon("A", {"dev7": ["implementation"]})
        b = _Canon("B", {"dev7": ["orchestrateur"]})
        rows = {"dev7": {"name": "dev7", "canon": "A"}}
        self.assertFalse(self.critical(self.fake([a, b]), "dev7", rows))

    def test_un_nom_hors_des_fiches_ne_lit_pas_le_registre(self):
        # #424 r6 : une fois son verdict lu (« non ») ; avant, il est inconnu
        fake = self.fake([_Canon("A", CARTES)])
        fake.db = object()
        self.assertFalse(runner.Runner.is_critical(fake, "dev7", {"name": "dev7",
                                                                  "canon": "A"}))
        with mock.patch("ameesh.registry.get", side_effect=AssertionError):
            self.assertFalse(runner.Runner.is_critical(fake, "dev7"))

    def test_persona_auditeur_sans_canon(self):
        self.assertTrue(self.critical(self.fake([]), "auditeur", {}))

    def test_reprise_juge_de_meme(self):
        canon = _Canon("A", CARTES)
        cfg = types.SimpleNamespace(critical_agents=(), canon="home")
        with mock.patch("ameesh.canon.load_configured", return_value=[canon]), \
                mock.patch.dict(os.environ, {"AMEESH_ALERT_ORCHESTRATORS": ""}):
            self.assertTrue(reprise._critical(cfg, _rows()["codex1"]))
            self.assertFalse(reprise._critical(cfg, _rows()["dev7"]))
            cfg.critical_agents = ("dev7",)
            self.assertTrue(reprise._critical(cfg, _rows()["dev7"]))


class SansDeductionTest(unittest.TestCase):
    """Dès qu'une fiche déclare des rôles, avoir confié des lots ne fait plus
    d'un agent un orchestrateur."""

    def storage(self):
        ops = types.SimpleNamespace(assigners=lambda since_s: ["codex3", "agent:orch"])
        return mock.patch("ameesh.storage.of",
                          return_value=types.SimpleNamespace(operations=ops))

    def test_orchestrateurs(self):
        listing = [{"name": n, "canon": "A"} for n in ("codex3", "orch", "mesh-design")]
        sans = _Canon("A", {"mesh-design": []})
        avec = _Canon("A", {"mesh-design": ["orchestrateur"], "codex3": ["relecture"]})
        with self.storage():
            self.assertEqual(su.orchestrators(None, None, listing, canons=[sans]),
                             ["codex3", "orch"])
            self.assertEqual(su.orchestrators(None, None, listing, canons=[avec]),
                             ["mesh-design"])

    def test_is_orchestrator(self):
        avec = _Canon(None, {"mesh-design": ["orchestrateur"], "codex3": ["relecture"]})
        sans = _Canon(None, {"mesh-design": []})
        cfg = types.SimpleNamespace()
        for canon, attendu in ((sans, True), (avec, False)):
            with self.storage(), \
                    mock.patch("ameesh.canon.load_configured", return_value=[canon]), \
                    mock.patch.dict(os.environ, {"AMEESH_ALERT_ORCHESTRATORS": ""}):
                self.assertEqual(assignments.is_orchestrator(cfg, None, "codex3"), attendu)
                self.assertEqual(assignments.is_orchestrator(cfg, None, "mesh-design"),
                                 canon is avec)



class Relecture424Test(unittest.TestCase):
    """#424 r1 : la P1 et les P2."""

    def fake(self, canons, configured=True):
        cfg = types.SimpleNamespace(critical_agents=(), canon="home" if configured else "")
        return types.SimpleNamespace(cfg=cfg, db=None, _audit_canons=tuple(canons),
                                     _orchestrators_declared=frozenset(),
                                     _canon_complete=True)

    def critical(self, fake, name, rows):
        with mock.patch("ameesh.registry.get", lambda db, n: rows.get(n)), \
                mock.patch("ameesh.runner.log_async"):
            return runner.Runner.is_critical(fake, name)

    def test_canon_illisible_le_verdict_connu_tient(self):
        rows = _rows()
        fake = self.fake([_Canon("A", CARTES)])
        self.assertTrue(self.critical(fake, "codex1", rows))
        self.assertFalse(self.critical(fake, "dev7", rows))
        # le canon devient illisible, puis n'est plus chargé : rien ne baisse
        for canons in ([_Canon("A", {}, readable=False)], []):
            fake._audit_canons = tuple(canons)
            self.assertTrue(self.critical(fake, "codex1", rows))
            self.assertFalse(self.critical(fake, "dev7", rows))
        # sans verdict connu : critique (#424 r6, fail-safe)
        neuf = self.fake([_Canon("A", {}, readable=False)])
        for name in ("orchestrateur", "mesh-design", "auditeur", "codex1", "dev7"):
            self.assertTrue(self.critical(neuf, name, rows), name)

    def test_reprise_canon_illisible_repli(self):
        cfg = types.SimpleNamespace(critical_agents=(), canon="home")
        with mock.patch("ameesh.canon.load_configured", side_effect=OSError("git")), \
                mock.patch.dict(os.environ, {"AMEESH_ALERT_ORCHESTRATORS": ""}):
            self.assertTrue(reprise._critical(cfg, {"name": "mesh-design", "canon": "A"}))
            self.assertTrue(reprise._critical(cfg, {"name": "dev7", "canon": "A"}))

    def test_alerte_canon_illisible(self):
        from ameesh import canon as canon_mod, exploitation, notify
        cfg = types.SimpleNamespace(host="h1", canon="home")
        with mock.patch.object(canon_mod, "from_config",
                               side_effect=canon_mod.CanonError("ref introuvable")):
            alertes = exploitation._critical_roles_unreadable(cfg, 100.0)
        self.assertEqual([a["type"] for a in alertes], ["critical_roles_unreadable"])
        self.assertIn("ref introuvable", alertes[0]["detail"])
        self.assertIn("without one: critical", alertes[0]["detail"])
        self.assertIn("critical_roles_unreadable", exploitation.ALERT_TYPES)
        self.assertIn("critical_roles_unreadable", notify.TYPE_LABELS)
        ok = types.SimpleNamespace(readable=True, id="manaty")
        with mock.patch.object(canon_mod, "from_config", return_value=ok):
            self.assertEqual(exploitation._critical_roles_unreadable(cfg, 100.0), [])

    def test_deduction_par_organisation(self):
        a = _Canon("A", {"mesh-design": ["orchestrateur"]})
        b = _Canon("B", {"chef-b": []})
        listing = [{"name": "chef-a", "canon": "A"}, {"name": "chef-b", "canon": "B"}]
        ops = types.SimpleNamespace(assigners=lambda since_s: ["chef-a", "chef-b"])
        with mock.patch("ameesh.storage.of",
                        return_value=types.SimpleNamespace(operations=ops)):
            self.assertEqual(su.orchestrators(None, None, listing, canons=[a, b]),
                             ["chef-b"])

    def test_roles_analyses_pas_le_frontmatter_brut(self):
        # `roles: {relecture: false}` : le canon le refuse (agent-roles-invalid)
        canon = _Canon("A", {"x": {"relecture": False}, "y": 42}, parsed={"x": None})
        rows = {"x": {"name": "x", "canon": "A"}, "y": {"name": "y", "canon": "A"}}
        fake = self.fake([canon])
        self.assertFalse(self.critical(fake, "x", rows))
        self.assertFalse(self.critical(fake, "y", rows))   # jamais de TypeError
        self.assertFalse(su.roles_declared([canon]))

    def test_synonymes_dans_les_deux_sens(self):
        canon = _Canon("A", {"r": ["relecture"], "c": ["conception"], "o": ["orchestrator"]},
                       federation={"critical_roles": ["review", "design"]})
        rows = {n: {"name": n, "canon": "A"} for n in ("r", "c", "o")}
        fake = self.fake([canon])
        self.assertTrue(self.critical(fake, "r", rows))
        self.assertTrue(self.critical(fake, "c", rows))
        self.assertFalse(self.critical(fake, "o", rows))
        self.assertEqual(su.role_orchestrators([canon]), {"o"})

    def test_homonyme_etranger_jamais_orchestrateur(self):
        a = _Canon("A", {"dev": ["implementation"]})
        b = _Canon("B", {"dev": ["orchestrateur"]})
        listing = [{"name": "dev", "canon": "A"}]
        with mock.patch("ameesh.storage.of", side_effect=AssertionError):
            self.assertEqual(su.orchestrators(None, None, listing, canons=[a, b]), [])
        with mock.patch("ameesh.canon.load_configured", return_value=[a, b]), \
                mock.patch("ameesh.registry.get", return_value={"name": "dev", "canon": "A"}), \
                mock.patch.dict(os.environ, {"AMEESH_ALERT_ORCHESTRATORS": ""}):
            self.assertFalse(assignments.is_orchestrator(types.SimpleNamespace(), object(),
                                                         "dev"))


class Relecture424r2Test(unittest.TestCase):
    """#424 r2 : canon lu en partie, mémoire des verdicts, clé de l'alerte."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def fake(self, canons):
        cfg = types.SimpleNamespace(critical_agents=(), canon="home", state_dir=self.tmp)
        return types.SimpleNamespace(cfg=cfg, db=None, _audit_canons=tuple(canons),
                                     _orchestrators_declared=frozenset(),
                                     _canon_complete=True)

    def critical(self, fake, name, rows):
        with mock.patch("ameesh.registry.get", lambda db, n: rows.get(n)), \
                mock.patch("ameesh.runner.log_async"):
            return runner.Runner.is_critical(fake, name)

    def test_canon_lu_en_partie_le_verdict_tient(self):
        from ameesh.canon import Finding
        rows = _rows()
        fake = self.fake([_Canon("A", CARTES)])
        self.assertTrue(self.critical(fake, "codex1", rows))
        # une erreur de lecture fait perdre la fiche de codex1, `readable` reste vrai
        partiel = _Canon("A", {k: v for k, v in CARTES.items() if k != "codex1"})
        partiel.load_findings = [Finding("canon-git-error", "error", "reading home: EIO")]
        fake._audit_canons = (partiel,)
        self.assertTrue(self.critical(fake, "codex1", rows))
        self.assertFalse(su.fully_read(partiel))

    def test_resume_relit_le_dernier_verdict(self):
        rows = _rows()
        self.assertTrue(self.critical(self.fake([_Canon("A", CARTES)]), "codex1", rows))
        cfg = types.SimpleNamespace(critical_agents=(), canon="home", state_dir=self.tmp)
        with mock.patch("ameesh.canon.load_configured", side_effect=OSError("git")), \
                mock.patch.dict(os.environ, {"AMEESH_ALERT_ORCHESTRATORS": ""}):
            self.assertTrue(reprise._critical(cfg, rows["codex1"]))
        # un exécuteur redémarré aussi, canon illisible
        neuf = self.fake([_Canon("A", {}, readable=False)])
        self.assertTrue(self.critical(neuf, "codex1", rows))

    def test_cle_d_alerte_stable_d_un_passage_au_suivant(self):
        from ameesh import canon as canon_mod, exploitation, notify
        cfg = types.SimpleNamespace(host="h1", canon="home")
        with mock.patch.object(canon_mod, "from_config",
                               side_effect=canon_mod.CanonError("ref introuvable")):
            un = exploitation._critical_roles_unreadable(cfg, 100.0)
            deux = exploitation._critical_roles_unreadable(cfg, 400.0)
        self.assertEqual(notify.key_of(un[0]), notify.key_of(deux[0]))


class _Cache(unittest.TestCase):
    """Le cache des verdicts dans un dossier d'état neuf."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.cfg = types.SimpleNamespace(critical_agents=(), canon="home", state_dir=self.tmp)
        self.dir = os.path.join(self.tmp, su.CRITICAL_DIR)
        self.path = os.path.join(self.dir, su.CRITICAL_DATA)
        os.makedirs(self.dir, 0o700, exist_ok=True)
        self.rows = _rows()

    def fake(self, canons):
        return types.SimpleNamespace(cfg=self.cfg, db=None, _audit_canons=tuple(canons),
                                     _orchestrators_declared=frozenset(),
                                     _canon_complete=True)

    def critical(self, fake, name):
        with mock.patch("ameesh.registry.get", lambda db, n: self.rows.get(n)), \
                mock.patch("ameesh.runner.log_async"):
            return runner.Runner.is_critical(fake, name)

    def outage(self, name):
        """Un exécuteur neuf (redémarré), canon illisible."""
        return self.critical(self.fake([_Canon("A", {}, readable=False)]), name)

    def resume(self, name, canons):
        with mock.patch("ameesh.canon.load_configured", **(
                {"return_value": canons} if canons is not None
                else {"side_effect": OSError("git")})), \
                mock.patch.dict(os.environ, {"AMEESH_ALERT_ORCHESTRATORS": ""}):
            return reprise._critical(self.cfg, self.rows[name])


class Relecture424r3Test(_Cache):
    """#424 r3 : le cache des verdicts, fail-closed (une épreuve par P1)."""

    def test_resume_rafraichit_le_cache(self):
        self.assertTrue(su.remember_critical(self.cfg, "codex1", False))
        self.assertTrue(self.resume("codex1", [_Canon("A", CARTES)]))
        self.assertTrue(su.critical_memory(self.cfg)["codex1"])
        self.assertTrue(self.resume("codex1", None))        # panne de canon ensuite

    def test_memoire_apres_ecriture_reussie_seulement(self):
        self.assertTrue(su.remember_critical(self.cfg, "codex1", False))
        fake = self.fake([_Canon("A", CARTES)])
        real = os.replace
        with mock.patch("os.replace", side_effect=OSError("EIO")):
            self.assertTrue(self.critical(fake, "codex1"))
        self.assertFalse(su.critical_memory(self.cfg)["codex1"])   # disque inchangé
        with mock.patch("os.replace", side_effect=real):
            self.assertTrue(self.critical(fake, "codex1"))         # nouvel essai
        self.assertTrue(su.critical_memory(self.cfg)["codex1"])
        self.assertTrue(self.outage("codex1"))

    def test_cache_corrompu_ou_invalide_vaut_critique(self):
        for contenu in ("{pas du json", "[1, 2]", ""):
            with self.subTest(contenu=contenu):
                with open(self.path, "w") as fh:
                    fh.write(contenu)
                os.chmod(self.path, 0o600)
                self.assertTrue(self.outage("codex1"))
                self.assertTrue(self.outage("dev7"))
        with open(self.path, "w") as fh:
            fh.write('{"codex1": null, "dev7": false}')
        os.chmod(self.path, 0o600)
        self.assertTrue(self.outage("codex1"))
        self.assertFalse(self.outage("dev7"))

    def test_cache_dont_on_ne_peut_se_fier_vaut_critique(self):
        self.assertTrue(su.remember_critical(self.cfg, "codex1", False))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertFalse(self.outage("codex1"))
        os.chmod(self.path, 0o644)                        # lisible par d'autres
        self.assertTrue(self.outage("codex1"))
        os.chmod(self.path, 0o600)
        ailleurs = os.path.join(self.tmp, "injecte.json")
        with open(ailleurs, "w") as fh:
            fh.write('{"codex1": false}')
        os.chmod(ailleurs, 0o600)
        os.unlink(self.path)
        os.symlink(ailleurs, self.path)                   # lien symbolique
        self.assertTrue(self.outage("codex1"))

    def test_ecritures_concurrentes_sans_perte(self):
        import threading
        noms = ["agent%02d" % i for i in range(24)]
        depart = threading.Barrier(len(noms))

        def ecrit(nom):
            depart.wait()
            self.assertTrue(su.remember_critical(self.cfg, nom, True))
        fils = [threading.Thread(target=ecrit, args=(n,)) for n in noms]
        for f in fils:
            f.start()
        for f in fils:
            f.join()
        self.assertEqual(sorted(su.critical_memory(self.cfg)), noms)
        self.assertEqual(os.listdir(self.dir), [su.CRITICAL_DATA])


class Relecture424r4Test(_Cache):
    """#424 r4 : une épreuve par P1 (corrompu jamais réécrit, absent vaut
    inconnu, comparaison au disque, dossier validé, verrou jamais remplacé)."""

    def write(self, text):
        with open(self.path, "w") as fh:
            fh.write(text)
        os.chmod(self.path, 0o600)

    def test_cache_corrompu_jamais_reecrit(self):
        self.write("{pas du json")
        self.assertFalse(su.remember_critical(self.cfg, "dev7", False))
        with open(self.path) as fh:
            self.assertEqual(fh.read(), "{pas du json")
        self.assertTrue(self.outage("codex1"))
        # une valeur invalide est gardée telle quelle : codex1 reste inconnu
        self.write('{"codex1": null}')
        self.assertTrue(su.remember_critical(self.cfg, "dev7", False))
        self.assertTrue(self.outage("codex1"))
        self.assertFalse(self.outage("dev7"))

    def test_cache_absent_vaut_inconnu(self):
        self.assertTrue(su.remember_critical(self.cfg, "codex1", True))
        os.unlink(self.path)                              # effacé, ou masqué
        self.assertTrue(self.outage("codex1"))
        self.assertTrue(self.outage("dev7"))
        import shutil
        shutil.rmtree(self.dir)
        self.assertTrue(self.outage("codex1"))

    def test_l_executeur_compare_au_disque(self):
        fake = self.fake([_Canon("A", CARTES)])
        self.assertTrue(self.critical(fake, "codex1"))
        # `resume` voit le rôle retiré et écrit False ; le rôle revient
        self.assertFalse(self.resume("codex1", [_Canon("A", dict(CARTES, codex1=["dev"]))]))
        self.assertFalse(su.critical_memory(self.cfg)["codex1"])
        self.assertTrue(self.critical(fake, "codex1"))
        self.assertTrue(su.critical_memory(self.cfg)["codex1"])
        self.assertTrue(self.outage("codex1"))            # redémarré, en panne

    def test_dossier_lie_ou_ouvert_aux_autres(self):
        import shutil
        ailleurs = os.path.join(self.tmp, "ailleurs")
        os.mkdir(ailleurs, 0o700)
        with open(os.path.join(ailleurs, su.CRITICAL_DATA), "w") as fh:
            fh.write('{"codex1": false}')
        os.chmod(os.path.join(ailleurs, su.CRITICAL_DATA), 0o600)
        shutil.rmtree(self.dir)
        os.symlink(ailleurs, self.dir)                    # parent lié : authentique, à nous
        self.assertTrue(self.outage("codex1"))
        self.assertFalse(su.remember_critical(self.cfg, "dev7", False))
        os.unlink(self.dir)
        os.rename(ailleurs, self.dir)
        self.assertFalse(self.outage("codex1"))
        os.chmod(self.dir, 0o755)                         # ouvert aux autres
        self.assertTrue(self.outage("codex1"))
        self.assertFalse(su.remember_critical(self.cfg, "dev7", False))

    def test_verrou_jamais_remplace(self):
        import json
        import threading
        self.assertTrue(su.remember_critical(self.cfg, "dev7", False))
        en_pause, reprends = threading.Event(), threading.Event()
        real = json.dump

        def lent(obj, fh, **kw):
            if "codex1" in obj and threading.current_thread().name == "A":
                en_pause.set()
                reprends.wait(5)
            return real(obj, fh, **kw)
        with mock.patch("json.dump", side_effect=lent):
            a = threading.Thread(target=su.remember_critical, name="A",
                                 args=(self.cfg, "codex1", True))
            a.start()
            self.assertTrue(en_pause.wait(5))
            for dossier in (self.tmp, self.dir):          # le chemin du verrou remplacé
                for nom in os.listdir(dossier):
                    if nom.endswith("lock"):
                        os.unlink(os.path.join(dossier, nom))
            b = threading.Thread(target=su.remember_critical, name="B",
                                 args=(self.cfg, "rev", True))
            b.start()
            b.join(1)
            reprends.set()
            a.join(5)
            b.join(5)
        memoire = su.critical_memory(self.cfg)
        self.assertEqual((memoire["codex1"], memoire["rev"]), (True, True))


class Relecture424r5Test(_Cache):
    """#424 r5 (décision de mesh-design) : un agent absent du cache, ou dont
    le canon est illisible, compte comme critique ; aucun « non » faute de
    lecture."""

    def test_cache_recree_par_un_autre_canon(self):
        self.rows["codex1"] = {"name": "codex1", "canon": "B"}
        self.assertTrue(su.remember_critical(self.cfg, "codex1", True))
        import shutil
        shutil.rmtree(self.dir)                           # cache effacé
        a = _Canon("A", CARTES)
        b = _Canon("B", {}, readable=False)               # canon de codex1 illisible
        # un agent lisible d'un autre canon recrée le cache sans codex1
        self.assertFalse(self.critical(self.fake([a, b]), "dev7"))
        self.assertEqual(su.critical_memory(self.cfg), {"dev7": False})
        self.assertTrue(self.critical(self.fake([a, b]), "codex1"))
        with mock.patch("ameesh.canon.load_configured", return_value=[a, b]), \
                mock.patch.dict(os.environ, {"AMEESH_ALERT_ORCHESTRATORS": ""}):
            self.assertTrue(reprise._critical(self.cfg, self.rows["codex1"]))
        self.assertTrue(self.resume("codex1", None))
        panne = self.fake([_Canon("A", {}, readable=False)])
        panne._canon_complete = False                     # B pas chargé (#424 r7)
        self.assertTrue(self.critical(panne, "codex1"))
        self.assertFalse(self.outage("dev7"))


class Relecture424r7Test(_Cache):
    """#424 r7 (mesh-design) : un canon configuré sur aucun canon de l'hôte est
    un état connu, pas une panne (non critique, un avertissement par
    processus) ; configuré mais illisible ou pas encore chargé : critique."""

    def setUp(self):
        super().setUp()
        self.addCleanup(su._FOREIGN_WARNED.clear)
        su._FOREIGN_WARNED.clear()
        self.rows["codex1"] = {"name": "codex1", "canon": "Z"}

    def test_canon_etranger_non_critique_un_avertissement(self):
        fake = self.fake([_Canon("A", CARTES)])
        with mock.patch("ameesh.registry.get", lambda db, n: self.rows.get(n)), \
                mock.patch("ameesh.runner.log_async") as log:
            self.assertFalse(runner.Runner.is_critical(fake, "codex1"))
            self.assertFalse(runner.Runner.is_critical(fake, "codex1"))
        self.assertEqual(len(log.call_args_list), 1)
        self.assertIn("codex1", log.call_args.args[0])
        self.assertIn("Z", log.call_args.args[0])
        self.assertNotIn("codex1", su.critical_memory(self.cfg) or {})  # jamais retenu
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(self.resume("codex1", [_Canon("A", CARTES)]))
        self.assertEqual(err.getvalue(), "")                 # déjà signalé ici

    def test_canon_configure_illisible_ou_pas_encore_charge_critique(self):
        # pas encore chargé : un canon configuré manque, il peut être le sien
        fake = self.fake([_Canon("A", CARTES)])
        fake._canon_complete = False
        self.assertTrue(self.critical(fake, "codex1"))
        self.cfg.canon_entries = ("a", "z")
        self.assertTrue(self.resume("codex1", [_Canon("A", CARTES)]))
        # configuré et chargé, mais illisible
        z = _Canon("Z", {}, readable=False)
        self.assertTrue(self.critical(self.fake([_Canon("A", CARTES), z]), "codex1"))
        self.assertTrue(self.resume("codex1", [_Canon("A", CARTES), z]))
        self.assertEqual(su._FOREIGN_WARNED, set())


if __name__ == "__main__":
    unittest.main()
