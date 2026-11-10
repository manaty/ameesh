# SPDX-License-Identifier: AGPL-3.0-only
"""#6606 : retenir les instantanés `src-<sha7>` du layout d'hôte.

Chaque mise à jour crée un worktree exact du commit dans
`~/.local/share/ameesh/src-<sha7>` (40 Mo environ) et rien ne le retirait :
91 instantanés sur agents-1, environ 3 par jour. Le ménage de `bascule.py`
taille aussi ces instantanés, avec les mêmes gardes que les venvs (#3792) :

* le courant est la source du venv vivant (`direct_url.json`), jamais un motif ;
* le précédent est `venv.precedent`, avec `previous-sha` en repli sur les VM ;
* le journal `installation-en-cours` protège la cible de `retour` ;
* tout instantané qu'un processus vivant nomme par un chemin absolu est gardé ;
* le clone `src`, le lien `src-current` et les noms qui ne sont pas exactement
  `src-` + 7 hex ne sont jamais candidats ;
* rien n'est retiré quand l'état ne permet pas de conclure.

Témoin du corps : cinq instantanés, dont le courant (en usage) et le
précédent → trois retirés seulement.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy/mise-a-jour"
SPEC = importlib.util.spec_from_file_location("bascule_6606", DEPLOY / "bascule.py")
bascule = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bascule)


def cmdline(*arguments: str) -> bytes:
    return b"".join(os.fsencode(argument) + b"\0" for argument in arguments)


class MenageInstantanesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="menage-6606-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "venv").mkdir()
        self.proc = self.base / "proc"
        self.proc.mkdir()
        self.sha_courant = "aaaaaaa"
        self.sha_precedent = "bbbbbbb"
        self.anciens = ["1111111", "2222222", "3333333"]

    # -- helpers ------------------------------------------------------------

    def instantane(self, court: str) -> Path:
        chemin = self.base / ("src-%s" % court)
        chemin.mkdir()
        (chemin / "README.md").write_text("instantané %s\n" % court)
        return chemin

    def venv(self, courant: str | None = None, precedent: str | None = None,
             previous_sha: str | None = None, direct_url: bool = True) -> Path:
        courant = courant or self.sha_courant
        version = self.base / ("venv-%s" % courant)
        info = version / "lib/python3.12/site-packages/ameesh-1.0.dist-info"
        info.mkdir(parents=True, exist_ok=True)
        if direct_url:
            (info / "direct_url.json").write_text(json.dumps(
                {"url": (self.base / ("src-%s" % courant)).as_uri()}))
        (self.base / bascule.CURRENT).symlink_to(version.name)
        if precedent is not None:
            (self.base / bascule.REPORT).write_text(precedent + "\n")
        if previous_sha is not None:
            (self.base / bascule.PREVIOUS).write_text(previous_sha + "\n")
        return version

    def processus(self, pid: int, *arguments: str) -> None:
        (self.proc / str(pid)).mkdir()
        (self.proc / str(pid) / "cmdline").write_bytes(cmdline(*arguments))

    def menager(self, *, appliquer: bool):
        return bascule._menager(str(self.base / "venv"), appliquer=appliquer,
                               racine=str(self.proc))

    # -- témoin du corps -----------------------------------------------------

    def test_cinq_instantanes_trois_retires(self):
        courant = self.instantane(self.sha_courant)
        precedent = self.instantane(self.sha_precedent)
        anciens = [self.instantane(court) for court in self.anciens]
        self.venv(precedent=self.sha_precedent,
                  previous_sha=self.sha_precedent + "0" * 33)
        # le courant est celui qu'un processus vivant nomme
        self.processus(4242, str(courant / "README.md"))
        # le clone et le lien du poste ne sont pas des candidats
        (self.base / "src").mkdir()
        (self.base / "src" / "README.md").write_text("clone\n")
        (self.base / "src-current").symlink_to("src-%s" % self.sha_courant)

        rapport = self.menager(appliquer=True)

        self.assertIsNone(rapport["probleme"])
        instantanes = rapport["instantanes"]
        self.assertIsNone(instantanes["probleme"])
        self.assertEqual(["src-%s" % c for c in self.anciens],
                         sorted(instantanes["retires"]))
        for ancien in anciens:
            self.assertFalse(ancien.exists(), "un ancien instantané est resté")
        self.assertTrue(courant.is_dir(), "l'instantané courant a été retiré")
        self.assertTrue(precedent.is_dir(), "l'instantané précédent a été retiré")
        self.assertTrue((self.base / "src").is_dir(), "le clone a été touché")
        self.assertTrue((self.base / "src-current").is_symlink())
        raisons = dict(instantanes["gardes"])
        self.assertEqual("courant", raisons["src-%s" % self.sha_courant])
        self.assertEqual("précédent", raisons["src-%s" % self.sha_precedent])

    # -- à blanc -------------------------------------------------------------

    def test_a_blanc_ne_retire_rien(self):
        self.instantane(self.sha_courant)
        ancien = self.instantane(self.anciens[0])
        self.venv()
        rapport = self.menager(appliquer=False)
        self.assertEqual(["src-%s" % self.anciens[0]],
                         rapport["instantanes"]["retirables"])
        self.assertEqual([], rapport["instantanes"]["retires"])
        self.assertTrue(ancien.is_dir())

    # -- précédent -----------------------------------------------------------

    def test_previous_sha_sert_de_repli_au_rapport(self):
        self.instantane(self.sha_courant)
        precedent = self.instantane(self.sha_precedent)
        ancien = self.instantane(self.anciens[0])
        self.venv(previous_sha=self.sha_precedent + "0" * 33)
        rapport = self.menager(appliquer=True)
        self.assertTrue(precedent.is_dir())
        self.assertFalse(ancien.exists())
        self.assertEqual(str(self.base / ("src-%s" % self.sha_precedent)),
                         rapport["instantanes"]["precedent"])

    def test_previous_sha_invalide_ne_retire_rien(self):
        self.instantane(self.sha_courant)
        ancien = self.instantane(self.anciens[0])
        self.venv(previous_sha="pas-un-sha")
        rapport = self.menager(appliquer=True)
        self.assertIn("previous-sha", rapport["instantanes"]["probleme"] or "")
        self.assertTrue(ancien.is_dir())

    def test_rapport_precedent_illisible_ne_retire_rien(self):
        self.instantane(self.sha_courant)
        ancien = self.instantane(self.anciens[0])
        self.venv()
        (self.base / bascule.REPORT).write_bytes(b"\xff\xfe\n")
        rapport = self.menager(appliquer=True)
        self.assertIn("rapport", rapport["instantanes"]["probleme"] or "")
        self.assertTrue(ancien.is_dir())

    # -- journal de récupération --------------------------------------------

    def test_le_journal_de_recuperation_garde_sa_cible(self):
        self.instantane(self.sha_courant)
        self.instantane(self.sha_precedent)
        cible = self.instantane("ccccccc")
        ancien = self.instantane(self.anciens[0])
        self.venv(precedent=self.sha_precedent)
        (self.base / bascule.JOURNAL).write_text(
            "sha=%s\ninstantane=src-%s\ncible=%s\n"
            % ("c" * 40, "ccccccc", "d" * 40))
        rapport = self.menager(appliquer=True)
        self.assertIsNone(rapport["instantanes"]["probleme"])
        self.assertTrue(cible.is_dir(), "la cible du journal a été retirée")
        self.assertFalse(ancien.exists())

    def test_journal_illisible_ne_retire_rien(self):
        self.instantane(self.sha_courant)
        ancien = self.instantane(self.anciens[0])
        self.venv()
        (self.base / bascule.JOURNAL).mkdir()  # un répertoire : lecture impossible
        rapport = self.menager(appliquer=True)
        self.assertIn("journal", rapport["instantanes"]["probleme"] or "")
        self.assertTrue(ancien.is_dir())

    # -- processus vivants ---------------------------------------------------

    def test_un_instantane_nomme_par_un_processus_est_garde(self):
        self.instantane(self.sha_courant)
        self.instantane(self.sha_precedent)
        usage = self.instantane("ccccccc")
        ancien = self.instantane(self.anciens[0])
        self.venv(precedent=self.sha_precedent)
        self.processus(4242, str(usage / "README.md"))
        rapport = self.menager(appliquer=True)
        self.assertTrue(usage.is_dir(), "l'instantané d'un processus vivant a été retiré")
        self.assertFalse(ancien.exists())
        self.assertIn(("src-ccccccc", "utilisé par un processus vivant"),
                      rapport["instantanes"]["gardes"])

    def test_processus_illisibles_rien_n_est_retire(self):
        self.instantane(self.sha_courant)
        ancien = self.instantane(self.anciens[0])
        self.venv()
        (self.proc / "4242").mkdir()
        (self.proc / "4242" / "cmdline").mkdir()
        rapport = self.menager(appliquer=True)
        self.assertIn("processus", rapport["probleme"] or "")
        self.assertTrue(ancien.is_dir())

    # -- état non concluant --------------------------------------------------

    def test_courant_introuvable_ne_retire_rien(self):
        ancien = self.instantane(self.anciens[0])
        self.venv(direct_url=False)
        rapport = self.menager(appliquer=True)
        self.assertIn("courant", rapport["instantanes"]["probleme"] or "")
        self.assertTrue(ancien.is_dir())

    def test_ni_le_clone_ni_un_nom_approchant_ne_sont_candidats(self):
        self.instantane(self.sha_courant)
        self.venv()
        (self.base / "src").mkdir()
        (self.base / "src-abc").mkdir()
        (self.base / "src-12345678").mkdir()
        fichier = self.base / "src-4444444"
        fichier.write_text("pas un instantané")
        rapport = self.menager(appliquer=True)
        self.assertIsNone(rapport["instantanes"]["probleme"])
        self.assertEqual([], rapport["instantanes"]["retires"])
        self.assertTrue((self.base / "src").is_dir())
        self.assertTrue((self.base / "src-abc").is_dir())
        self.assertTrue((self.base / "src-12345678").is_dir())
        self.assertTrue(fichier.is_file())

    # -- worktrees -----------------------------------------------------------

    def test_le_retrait_elague_les_worktrees_du_clone(self):
        self.instantane(self.sha_courant)
        ancien = self.instantane(self.anciens[0])
        self.venv()
        (self.base / "src" / ".git").mkdir(parents=True)
        resultat = SimpleNamespace(returncode=0, stderr="")
        with mock.patch.object(bascule.subprocess, "run",
                               return_value=resultat) as lancer:
            rapport = self.menager(appliquer=True)
        self.assertFalse(ancien.exists())
        self.assertEqual([], rapport["instantanes"]["erreurs"])
        lancer.assert_called_once_with(
            ["git", "-C", str(self.base / "src"), "worktree", "prune"],
            capture_output=True, text=True, timeout=60)

    def test_un_worktree_prune_en_echec_est_signale_sans_bloquer_le_retrait(self):
        self.instantane(self.sha_courant)
        ancien = self.instantane(self.anciens[0])
        self.venv()
        (self.base / "src" / ".git").mkdir(parents=True)
        resultat = SimpleNamespace(returncode=1, stderr="prune cassé")
        with mock.patch.object(bascule.subprocess, "run", return_value=resultat):
            rapport = self.menager(appliquer=True)
        self.assertFalse(ancien.exists())
        self.assertIn(("worktree prune", "prune cassé"),
                      rapport["instantanes"]["erreurs"])

    # -- câblage dans activer ------------------------------------------------

    def test_une_erreur_du_menage_des_instantanes_ne_fait_pas_echouer_la_bascule(self):
        self.instantane(self.sha_courant)
        self.venv()
        with mock.patch.object(bascule, "_menager_instantanes",
                               side_effect=RuntimeError("inattendu")), \
             mock.patch.object(bascule, "_chemins_des_processus", return_value=set()), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            bascule._menager_apres_bascule(str(self.base / "venv"))
        self.assertIn("impossible", err.getvalue())


if __name__ == "__main__":
    unittest.main()
