# SPDX-License-Identifier: AGPL-3.0-only
"""L122 : numéros de migration uniques, réservés par les lots.

Le premier test ne demande pas de base : il refuse deux migrations de même
numéro dans `src/ameesh/migrations` (le 2026-10-10, quatre lots ont pris
0048 à 0050) et un fichier `.sql` que `migrations.discover` ignorerait.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from ameesh import db as db_mod, migrations, storage, work
from ameesh.db import DbError
from ameesh.storage.postgres import work as pg_work

from .support import PgTestCase


class NumerosUniquesTest(unittest.TestCase):
    def test_aucun_numero_en_double(self):
        names = os.listdir(migrations.MIGRATIONS_DIR)
        doubles = {"%04d" % n: files for n, files in migrations.numbered(names).items()
                   if len(files) > 1}
        self.assertEqual(doubles, {}, "migrations de même numéro : renuméroter la dernière "
                                      "venue (`ameesh work migration <lot>`, L122)")

    def test_aucun_fichier_sql_hors_format(self):
        names = [n for n in os.listdir(migrations.MIGRATIONS_DIR) if n.endswith(".sql")]
        ignored = sorted(set(names) - {f for files in migrations.numbered(names).values()
                                       for f in files})
        self.assertEqual(ignored, [], "fichier de migration hors format NNNN_nom.sql")

    def test_numbered_voit_les_doublons(self):
        self.assertEqual(migrations.numbered(["0048_a.sql", "0048_b.sql", "README", "x/0049_c.sql"]),
                         {48: ["0048_a.sql", "0048_b.sql"], 49: ["0049_c.sql"]})


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x"))


def _commit(repo, directory, *names):
    os.makedirs(os.path.join(repo, directory), exist_ok=True)
    for name in names:
        with open(os.path.join(repo, directory, name), "w") as fh:
            fh.write("--")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", " ".join(names) or "vide", "--allow-empty")


def _depot(path, slug, directory, base=(), branches=None):
    """Un clone de `slug` (origin GitHub, sans réseau) : `base` sur la branche
    principale, puis chaque branche `{nom: fichiers}` ; reste sur la principale."""
    os.makedirs(path)
    _git(path, "init", "-q", "-b", "develop")
    _git(path, "remote", "add", "origin", "git@github.com:%s.git" % slug)
    _commit(path, directory, *base)
    for branch, names in (branches or {}).items():
        _git(path, "checkout", "-qb", branch)
        _commit(path, directory, *names)
        _git(path, "checkout", "-q", "develop")
    return path


NEXLINK = migrations.LAYOUTS["manaty/nexlink"]
AMEESH = migrations.LAYOUTS[migrations.AMEESH_REPO]


class NumerosDuDepotTest(unittest.TestCase):
    """#443 : les numéros se lisent dans le dépôt du lot, avec son dossier."""

    def test_nexlink_trois_chiffres_toutes_les_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _depot(os.path.join(tmp, "nexlink"), "manaty/nexlink", NEXLINK.directory,
                          ["375_a.sql", "376_b.sql", "376_restore.sql", "RESERVATIONS.md"],
                          {"agent/x-405": ["377_receipt.sql"]})
            # une branche distante compte aussi, sans fetch
            _git(repo, "update-ref", "refs/remotes/origin/agent/y-417",
                 "refs/heads/agent/x-405")
            found = migrations.repo_numbers(repo, NEXLINK)
            self.assertEqual(sorted(found), [375, 376, 377])
            self.assertIn("origin/agent/y-417:377_receipt.sql", found[377])
            # la branche du lot ne compte pas (locale ni distante)
            mine = migrations.repo_numbers(repo, NEXLINK, skip_branch="agent/x-405")
            self.assertEqual(sorted(mine), [375, 376, 377])
            _git(repo, "update-ref", "-d", "refs/remotes/origin/agent/y-417")
            mine = migrations.repo_numbers(repo, NEXLINK, skip_branch="agent/x-405")
            self.assertEqual(sorted(mine), [375, 376])
            self.assertEqual(migrations.origin_repo(repo), "manaty/nexlink")

    def test_une_branche_du_paquet_s_ignore_explicitement(self):
        """#1603 : la branche d'intégration d'un paquet en composition s'ignore
        sur demande, en plus de celle du lot (réf locale et distante)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _depot(os.path.join(tmp, "nexlink"), "manaty/nexlink", NEXLINK.directory,
                          ["375_a.sql"], {"integration/paquet-5": ["453_demo.sql"]})
            self.assertIn(453, migrations.repo_numbers(repo, NEXLINK))
            skipped = migrations.repo_numbers(repo, NEXLINK,
                                              also_skip=("integration/paquet-5",))
            self.assertEqual(sorted(skipped), [375])
            _git(repo, "update-ref", "refs/remotes/origin/integration/paquet-5",
                 "refs/heads/integration/paquet-5")
            _git(repo, "update-ref", "-d", "refs/heads/integration/paquet-5")
            self.assertIn(453, migrations.repo_numbers(repo, NEXLINK))
            skipped = migrations.repo_numbers(repo, NEXLINK,
                                              also_skip=("integration/paquet-5",))
            self.assertEqual(sorted(skipped), [375])

    def test_une_branche_entree_dans_le_tronc_ne_compte_pas(self):
        """#4097 : un paquet renumérote 602 → 599 ; la branche d'agent restée
        sur la forge (ancêtre de `develop`) porte encore 602 et ne doit pas
        tenir ce numéro. Une branche non fusionnée compte toujours, le tronc
        aussi, même en retard sur `origin/`."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _depot(os.path.join(tmp, "nexlink"), "manaty/nexlink", NEXLINK.directory,
                          ["598_a.sql"], {"agent/x-4079": ["602_key.sql"],
                                          "agent/y-4078": ["603_open.sql"]})
            _git(repo, "update-ref", "refs/remotes/origin/develop", "refs/heads/develop")
            self.assertEqual(sorted(migrations.repo_numbers(repo, NEXLINK)), [598, 602, 603])
            # le paquet fusionne la branche puis renumérote sur le tronc
            _git(repo, "merge", "-q", "--no-ff", "-m", "paquet 212", "agent/x-4079")
            _git(repo, "mv", NEXLINK.directory + "/602_key.sql",
                 NEXLINK.directory + "/599_key.sql")
            _git(repo, "commit", "-qm", "602 -> 599")
            _git(repo, "update-ref", "refs/remotes/origin/agent/x-4079",
                 "refs/heads/agent/x-4079")
            _git(repo, "update-ref", "refs/remotes/origin/develop", "refs/heads/develop")
            _git(repo, "update-ref", "refs/heads/develop", "HEAD~1")
            found = migrations.repo_numbers(repo, NEXLINK, worktree=False)
            self.assertEqual(sorted(found), [598, 599, 602, 603])
            self.assertEqual(found[602], ["develop:602_key.sql"])
            _git(repo, "update-ref", "refs/heads/develop", "refs/remotes/origin/develop")
            found = migrations.repo_numbers(repo, NEXLINK, worktree=False)
            self.assertEqual(sorted(found), [598, 599, 603])
            self.assertNotIn("origin/agent/x-4079:602_key.sql", sum(found.values(), []))
            # le clone de lecture est resté sur la branche fusionnée : son
            # fichier commité ne compte pas, un ajout local si (revue cx3)
            _git(repo, "checkout", "-q", "agent/x-4079")
            with open(os.path.join(repo, NEXLINK.directory, "604_local.sql"), "w") as fh:
                fh.write("--")
            found = migrations.repo_numbers(repo, NEXLINK)
            self.assertEqual(sorted(found), [598, 599, 603, 604])
            self.assertEqual(found[604], ["worktree:604_local.sql"])
            # HEAD détaché sur un commit déjà contenu dans le tronc : idem
            _git(repo, "checkout", "-q", "--detach", "agent/x-4079")
            self.assertEqual(sorted(migrations.repo_numbers(repo, NEXLINK)),
                             [598, 599, 603, 604])
            # `agent/main` n'est pas un tronc : fusionnée, elle ne compte pas
            _git(repo, "update-ref", "refs/heads/agent/main", "refs/heads/agent/x-4079")
            self.assertNotIn(602, migrations.repo_numbers(repo, NEXLINK))
            # une branche non fusionnée garde son arbre de travail entier
            _git(repo, "checkout", "-q", "agent/y-4078")
            self.assertIn("worktree:603_open.sql",
                          migrations.repo_numbers(repo, NEXLINK)[603])

    def test_la_cible_seule_est_lue(self):
        """#2620 : un numéro exact ne lit que la branche cible — ni l'arbre de
        travail, ni le paquet, ni les branches non fusionnées ; `origin/`
        publiée l'emporte sur la branche locale (#1333)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _depot(os.path.join(tmp, "nexlink"), "manaty/nexlink", NEXLINK.directory,
                          ["375_a.sql", "376_b.sql", "377_receipt+fix.sql"],
                          {"agent/x-417": ["378_autre.sql"]})
            with open(os.path.join(repo, NEXLINK.directory, "379_wt.sql"), "w") as fh:
                fh.write("--")
            found = migrations.target_numbers(repo, NEXLINK, "develop")
            # tout nom numéroté de la cible compte, même hors du format
            self.assertEqual(sorted(found), [375, 376, 377])
            self.assertEqual(found[377], ["develop:377_receipt+fix.sql"])
            self.assertEqual(migrations.target_numbers(repo, NEXLINK, "origin/develop"), found)
            self.assertIsNone(migrations.target_numbers(repo, NEXLINK, "main"))
            self.assertIsNone(migrations.target_numbers(repo, NEXLINK, ""))
            # la ref publiée `origin/develop` fait foi sur la locale figée
            os.remove(os.path.join(repo, NEXLINK.directory, "379_wt.sql"))
            _git(repo, "checkout", "-q", "--orphan", "publie")
            _git(repo, "rm", "-rqf", ".")
            os.makedirs(os.path.join(repo, NEXLINK.directory))
            with open(os.path.join(repo, NEXLINK.directory, "390_publie.sql"), "w") as fh:
                fh.write("--")
            _git(repo, "add", ".")
            _git(repo, "commit", "-qm", "390 publie")
            sha = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"],
                                          text=True).strip()
            _git(repo, "checkout", "-q", "develop")
            _git(repo, "update-ref", "refs/remotes/origin/develop", sha)
            found = migrations.target_numbers(repo, NEXLINK, "develop")
            self.assertEqual(sorted(found), [390])
            self.assertIn("origin/develop:390_publie.sql", found[390])

    def test_l_arbre_de_travail_peut_etre_ignore(self):
        """#2400 : le train compose la migration qu'il réserve ; le scan de
        réservation ignore alors l'arbre de travail, jamais les branches."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _depot(os.path.join(tmp, "nexlink"), "manaty/nexlink", NEXLINK.directory,
                          ["375_a.sql"], {"agent/y-417": ["377_c.sql"]})
            with open(os.path.join(repo, NEXLINK.directory, "378_wt.sql"), "w") as fh:
                fh.write("--")
            self.assertEqual(sorted(migrations.repo_numbers(repo, NEXLINK)), [375, 377, 378])
            self.assertEqual(sorted(migrations.repo_numbers(repo, NEXLINK, worktree=False)),
                             [375, 377])

    def test_le_paquet_ameesh_ne_compte_que_pour_ameesh(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = os.path.join(tmp, "paquet")
            os.makedirs(package)
            open(os.path.join(package, "0903_c.sql"), "w").close()
            ameesh = _depot(os.path.join(tmp, "ameesh"), "manaty/ameesh", AMEESH.directory,
                            ["0005_e.sql"])
            nexlink = _depot(os.path.join(tmp, "nexlink"), "manaty/nexlink",
                             NEXLINK.directory, ["012_x.sql"])
            self.assertEqual(sorted(migrations.repo_numbers(ameesh, AMEESH, package=package)),
                             [5, 903])
            self.assertEqual(sorted(migrations.repo_numbers(nexlink, NEXLINK, package=package)),
                             [12])

    def test_tout_nom_numerote_compte(self):
        """#443 r1 P2 : `377_receipt+fix.sql` occupe 377, même hors du format."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _depot(os.path.join(tmp, "nexlink"), "manaty/nexlink", NEXLINK.directory,
                          ["376_a.sql", "377_receipt+fix.sql", "378_x.SQL", "README.md",
                           "v379_pas_un_numero.sql"])
            self.assertEqual(sorted(migrations.repo_numbers(repo, NEXLINK)), [376, 377, 378])

    def test_origin_hote_exact(self):
        """#443 r1 P2 : l'hôte doit être github.com, pas une sous-chaîne."""
        for url in ("https://github.com/manaty/nexlink.git", "https://github.com/Manaty/Nexlink",
                    "https://user@github.com/manaty/nexlink.git/", "git@github.com:manaty/nexlink.git",
                    "ssh://git@github.com/manaty/nexlink", "github.com:manaty/nexlink"):
            with self.subTest(url=url):
                self.assertEqual(migrations.github_repo(url), "manaty/nexlink")
        for url in ("https://notgithub.com/manaty/nexlink.git",
                    "https://github.com.evil.example/manaty/nexlink.git",
                    "git@gitlab.com:manaty/nexlink.git", "https://example.com/github.com/manaty/nexlink",
                    "https://github.com/manaty/nexlink/tree/x", "file:///srv/github.com/manaty/nexlink",
                    "/srv/github.com:manaty/nexlink", ""):
            with self.subTest(url=url):
                self.assertIsNone(migrations.github_repo(url))

    def test_disposition(self):
        self.assertEqual(NEXLINK.file(378), "supabase/migrations/378_<name>.sql")
        self.assertEqual(AMEESH.file(378), "src/ameesh/migrations/0378_<name>.sql")
        self.assertIsNone(migrations.layout_for("manaty/inconnu"))

    def test_largeur_declaree(self):
        """#4750 : un dépôt déclaré à la configuration (pas en dur) porte sa
        largeur ; `014_x.sql` se lit sur 3 chiffres, défaut 4, valeur vérifiée."""
        from ameesh import radar, train_migrations

        def cfg(**extra):
            return types.SimpleNamespace(github=None, radar={"projects": {"liquichain": dict(
                {"repo": "/x", "github": "manaty/liquichain",
                 "migrations": "supabase/migrations"}, **extra)}})

        layout = migrations.layout_for("manaty/liquichain", cfg(migrations_width=3))
        self.assertEqual(layout.file(16), "supabase/migrations/016_<name>.sql")
        self.assertEqual(migrations.layout_for("manaty/liquichain", cfg()).width, 4)
        project = radar.settings_of(cfg(migrations_width=3)).projects["liquichain"]
        self.assertEqual(train_migrations.layout(project).label(16), "016")
        self.assertEqual(radar.settings_of(cfg(migrations_width=0))
                         .projects["liquichain"].migrations_width, 0)
        for bad in ("3", 10, -1, True, False, "", [], {}):
            with self.subTest(bad=bad), self.assertRaises(radar.SettingsError):
                radar.settings_of(cfg(migrations_width=bad))


class ReservationTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.nexlink = _depot(os.path.join(self.tmp, "nexlink"), "manaty/nexlink",
                              NEXLINK.directory, ["375_a.sql", "376_b.sql"],
                              {"agent/autre": ["377_c.sql"]})
        self.ameesh = _depot(os.path.join(self.tmp, "ameesh"), "manaty/ameesh",
                             AMEESH.directory, ["0005_e.sql"])
        # #2620 : un numéro exact lit la cible du lot ; ces dépôts n'ont que
        # `develop` (pas d'`origin/HEAD`, pas de main), la cible est déclarée.
        _git(self.nexlink, "config", "ameesh.target", "develop")
        _git(self.ameesh, "config", "ameesh.target", "develop")
        self.floor = max(migrations.repo_numbers(self.ameesh, AMEESH)) + 1

    def _lot(self, title="un lot", repo="manaty/ameesh", n=1):
        return work.add(self.db, title=title, issue_ref="%s#%d" % (repo, n), actor="mesh-design")

    def _nx(self, lot, number=None, **kw):
        return work.reserve_migration(self.db, lot["id"], number, repo=self.nexlink, **kw)

    def _am(self, lot, number=None, **kw):
        return work.reserve_migration(self.db, lot["id"], number, repo=self.ameesh, **kw)

    def test_prochain_numero_libre_du_depot_puis_idempotent(self):
        a, b = self._lot("a"), self._lot("b")
        ra = self._am(a, actor="dev1")
        self.assertEqual((ra["migration"], ra["migration_repo"]), (self.floor, "manaty/ameesh"))
        self.assertEqual(ra["migration_file"],
                         "src/ameesh/migrations/%04d_<name>.sql" % self.floor)
        # le lot garde sa réservation ; le suivant prend le numéro d'après
        self.assertEqual(self._am(a)["migration"], self.floor)
        self.assertEqual(self._am(b, actor="dev2")["migration"], self.floor + 1)
        notes = [e["note"] for e in work.events(self.db, a["id"])]
        self.assertIn("migration %04d reserved (manaty/ameesh)" % self.floor, notes)
        proc = self.mesh("work", "show", str(b["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("migration: %04d reserved in manaty/ameesh" % (self.floor + 1),
                      proc.stdout)

    def test_branche_du_paquet_ignoree_hors_cible(self):
        """#1603 × #2620 : à la composition, la branche d'intégration porte déjà
        les fichiers renumérotés ; un numéro exact ne lit que la cible et n'a
        plus besoin de `--skip-branch` ; l'allocation du prochain libre, elle,
        l'ignore sur demande, sans exempter de l'unicité entre lots."""
        _git(self.nexlink, "checkout", "-qb", "integration/paquet-5")
        _commit(self.nexlink, NEXLINK.directory, "453_demo.sql", "455_autre.sql")
        _git(self.nexlink, "checkout", "-q", "develop")
        # prochain libre : la branche d'intégration compte, sauf --skip-branch
        skipped = self._lot("nx", "manaty/nexlink")
        self.assertEqual(self._nx(skipped, skip_branches=("integration/paquet-5",))["migration"],
                         378)      # 375, 376, 377 (agent/autre), sans 453/455
        other = self._lot("nx2", "manaty/nexlink", 2)
        self.assertEqual(self._nx(other)["migration"], 456)   # au-delà de 453/455
        # un numéro exact lit la cible seule : 453 et 455 sont libres
        lot = self._lot("nx3", "manaty/nexlink", 3)
        self.assertEqual(self._nx(lot, 453)["migration"], 453)
        proc = self.mesh("work", "migration", str(self._lot("nx4", "manaty/nexlink", 4)["id"]),
                         "--number", "455", "--repo", self.nexlink)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("migration 455 reserved in manaty/nexlink", proc.stdout)
        # l'option ne dispense pas de l'unicité (dépôt, numéro) entre lots
        holder = self._nx(self._lot("nx5", "manaty/nexlink", 5), 454)
        with self.assertRaises(work.WorkError) as ctx:
            self._nx(lot, 454)
        self.assertIn("lot #%d" % holder["id"], str(ctx.exception))
        # le tronc ne s'ignore jamais : il porte les migrations fusionnées
        for trunk in ("develop", "main"):
            with self.assertRaises(work.WorkError) as ctx:
                self._nx(other, 376, skip_branches=(trunk,))
            self.assertIn("trunk branch", str(ctx.exception))
        self.assertEqual(work.get(self.db, other["id"])["migration"], 456)

    def test_lot_nexlink_dossier_et_numerotation_de_nexlink(self):
        lot = self._lot("nx", "manaty/nexlink", 169)
        # 377 n'existe que sur une autre branche : il compte quand même
        got = self._nx(lot)
        self.assertEqual((got["migration"], got["migration_repo"]), (378, "manaty/nexlink"))
        self.assertEqual(got["migration_file"], "supabase/migrations/378_<name>.sql")
        proc = self.mesh("work", "migration", str(self._lot("nx2", "manaty/nexlink", 170)["id"]),
                         "--repo", self.nexlink)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("migration 379 reserved in manaty/nexlink — file "
                      "supabase/migrations/379_<name>.sql", proc.stdout)
        self.assertNotIn("src/ameesh", proc.stdout)

    def test_suites_independantes_par_depot(self):
        """Le cas 0377 : même numéro, deux dépôts, deux réservations légitimes ;
        dans un même dépôt, la seconde est refusée."""
        am, nx = self._lot("am"), self._lot("nx", "manaty/nexlink")
        self.assertEqual(self._am(am, 9000)["migration"], 9000)
        self.assertEqual(self._nx(nx, 9000)["migration"], 9000)
        other = self._lot("nx bis", "manaty/nexlink", 2)
        with self.assertRaises(work.WorkError) as ctx:
            self._nx(other, 9000)
        self.assertIn("lot #%d" % nx["id"], str(ctx.exception))
        self.assertIn("manaty/nexlink", str(ctx.exception))
        # le prochain libre de nexlink passe au-delà de ses réservations, pas de celles d'ameesh
        self.assertEqual(self._nx(other)["migration"], 9001)
        self.assertEqual(self._am(self._lot("am2"))["migration"], 9001)

    def test_un_numero_d_une_branche_non_fusionnee_ne_refuse_plus(self):
        """#2620 : le témoin du défaut — `377` n'existe que sur la branche non
        fusionnée `agent/autre` ; une réservation à numéro exact l'accepte,
        alors qu'un fichier de la cible refuse toujours."""
        self.assertEqual(sorted(migrations.target_numbers(self.nexlink, NEXLINK, "develop")),
                         [375, 376])
        lot = self._lot("nx", "manaty/nexlink")
        with self.assertRaises(work.WorkError) as ctx:
            self._nx(lot, 376)          # la cible, sous son propre nom
        self.assertIn("already in manaty/nexlink", str(ctx.exception))
        self.assertIn("develop:376_b.sql", str(ctx.exception))
        got = self._nx(lot, 377)        # la branche non fusionnée ne compte pas
        self.assertEqual(got["migration"], 377)
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertIn("migration 377 reserved (manaty/nexlink; before: none)", notes)
        # le même numéro est libre dans ameesh (0005 seulement sur sa cible)
        self.assertEqual(self._am(self._lot("am"), 376)["migration"], 376)

    def test_la_cible_du_lot_prime_sur_celle_du_depot(self):
        """#2620 : `branch_target` fait foi (celle que lisent la file de fusion
        et la classe de revue), avant la cible déclarée du clone."""
        lot = self._lot("nx", "manaty/nexlink")
        work.set_branch(self.db, lot["id"], "lot/2620", "agent/autre")
        with self.assertRaises(work.WorkError) as ctx:
            self._nx(lot, 377)
        self.assertIn("agent/autre:377_c.sql", str(ctx.exception))

    def test_numero_exact_sans_cible_connue_refuse(self):
        """#2620 : sans cible connue, refuser plutôt que de retomber sur le
        scan de toutes les branches (le défaut corrigé)."""
        _git(self.nexlink, "config", "--unset", "ameesh.target")
        lot = self._lot("nx", "manaty/nexlink")
        with self.assertRaises(work.WorkError) as ctx:
            self._nx(lot, 377)
        self.assertIn("no target branch", str(ctx.exception))
        self.assertIn("#2620", str(ctx.exception))
        self.assertIsNone(work.get(self.db, lot["id"])["migration"])

    def test_numero_exact_ne_lit_pas_origin_head(self):
        """#2660 : sans cible déclarée, un numéro exact ne lit plus
        `origin/HEAD` — main sur un clone nexlink dont le projet intègre sur
        develop — et refuse. La cible du projet et `ameesh.target` restent
        lues : ce sont des cibles déclarées."""
        _git(self.nexlink, "config", "--unset", "ameesh.target")
        # `origin/HEAD` dit main, et main porte 377 : le repli silencieux
        # vérifierait un lot qui vise develop contre main.
        _git(self.nexlink, "checkout", "-qb", "main")
        _commit(self.nexlink, NEXLINK.directory, "377_main.sql")
        _git(self.nexlink, "checkout", "-q", "develop")
        _git(self.nexlink, "update-ref", "refs/remotes/origin/main", "refs/heads/main")
        _git(self.nexlink, "symbolic-ref", "refs/remotes/origin/HEAD",
             "refs/remotes/origin/main")
        lot = work.add(self.db, title="nx", app="nexlink",
                       issue_ref="manaty/nexlink#7", actor="mesh-design")
        with self.assertRaises(work.WorkError) as ctx:
            self._nx(lot, 377)
        self.assertIn("no target branch", str(ctx.exception))
        self.assertIn("#2620", str(ctx.exception))
        self.assertIn("#2660", str(ctx.exception))
        self.assertNotIn("already in", str(ctx.exception))
        self.assertIsNone(work.get(self.db, lot["id"])["migration"])
        # la cible du projet, déclarée à l'hôte, suffit et fait foi
        cfg = types.SimpleNamespace(github=None, radar={"projects": {"nexlink": {
            "repo": self.nexlink, "github": "manaty/nexlink",
            "migrations": NEXLINK.directory, "target": "develop"}}})
        self.assertEqual(self._nx(lot, 377, cfg=cfg)["migration"], 377)
        # `ameesh.target` du clone reste une cible déclarée
        work.reserve_migration(self.db, lot["id"], release=True)
        _git(self.nexlink, "config", "ameesh.target", "develop")
        self.assertEqual(self._nx(lot, 377)["migration"], 377)

    def test_clone_d_un_autre_depot_ou_lot_sans_depot_refuse(self):
        nx = self._lot("nx", "manaty/nexlink")
        with self.assertRaises(work.WorkError) as ctx:
            work.reserve_migration(self.db, nx["id"], repo=self.ameesh)
        self.assertIn("clone of manaty/ameesh, not of manaty/nexlink", str(ctx.exception))
        # #443 r1 P2 : un hôte qui contient « github.com » n'est pas github.com
        _git(self.nexlink, "remote", "set-url", "origin", "https://notgithub.com/manaty/nexlink.git")
        with self.assertRaises(work.WorkError) as ctx:
            self._nx(nx)
        self.assertIn("no GitHub origin", str(ctx.exception))
        bare = work.add(self.db, title="sans issue", actor="mesh-design")
        with self.assertRaises(work.WorkError) as ctx:
            work.reserve_migration(self.db, bare["id"], repo=self.ameesh)
        self.assertIn("no repository", str(ctx.exception))
        unknown = self._lot("x", "manaty/inconnu")
        with self.assertRaises(work.WorkError) as ctx:
            work.reserve_migration(self.db, unknown["id"], repo=self.ameesh)
        self.assertIn("migrations directory unknown", str(ctx.exception))
        self.assertIsNone(work.get(self.db, nx["id"])["migration"])

    def test_cle_de_depot_en_minuscules(self):
        """#443 r1 P2 : `Manaty/Nexlink` et `manaty/nexlink` sont une seule suite,
        avec la disposition de nexlink, même déclarée en majuscules à l'hôte."""
        cfg = types.SimpleNamespace(github=None, radar={"projects": {"nx": {
            "repo": self.nexlink, "github": "Manaty/Nexlink",
            "migrations": NEXLINK.directory}}})
        upper = self._lot("nx", "Manaty/Nexlink", 3)
        got = work.reserve_migration(self.db, upper["id"], cfg=cfg)
        self.assertEqual((got["migration"], got["migration_repo"]), (378, "manaty/nexlink"))
        self.assertEqual(got["migration_file"], "supabase/migrations/378_<name>.sql")
        lower = self._lot("nx2", "manaty/nexlink", 4)
        with self.assertRaises(work.WorkError) as ctx:
            work.reserve_migration(self.db, lower["id"], 378, cfg=cfg)
        self.assertIn("lot #%d" % upper["id"], str(ctx.exception))
        self.assertEqual(work.reserve_migration(self.db, lower["id"], cfg=cfg)["migration"], 379)
        # en base aussi : un dépôt écrit en majuscules tombe dans la même suite
        with self.assertRaises(DbError):
            self.db.execute("UPDATE work_items SET migration = 379,"
                            " migration_repo = 'MANATY/nexlink' WHERE id = %d"
                            % self._lot("nx3", n=5)["id"])

    def test_pas_d_espace_de_noms_invente(self):
        """#443 r1 P2 : sans dépôt d'issue, un client d'avant #443 est refusé
        (pas de `unknown/<app>`) ; une issue entourée d'espaces garde son dépôt."""
        held = work.add(self.db, title="nx", app="nexlink", issue_ref=" manaty/nexlink#9 ",
                        actor="mesh-design")
        self.db.execute("UPDATE work_items SET migration = 389 WHERE id = %d" % held["id"])
        self.assertEqual(work.get(self.db, held["id"])["migration_repo"], "manaty/nexlink")
        with self.assertRaises(work.WorkError) as ctx:
            self._nx(self._lot("nx2", "manaty/nexlink", 10), 389)
        self.assertIn("lot #%d" % held["id"], str(ctx.exception))
        bare = work.add(self.db, title="sans issue", app="nexlink", actor="mesh-design")
        with self.assertRaises(DbError) as ctx:
            self.db.execute("UPDATE work_items SET migration = 390 WHERE id = %d" % bare["id"])
        self.assertIn("without a repository", str(ctx.exception))
        rows = self.db.query("SELECT work_items_issue_repo(%s) AS a, work_items_issue_repo(%s) AS b,"
                             " work_items_issue_repo(%s) AS c",
                             (" Manaty/Nexlink#3\n", "", "manaty/nexlink"))
        self.assertEqual((rows[0]["a"], rows[0]["b"], rows[0]["c"]),
                         ("manaty/nexlink", None, None))

    def _rejoue_0390(self, *rows):
        """Les réservations `rows` (migration, dépôt, issue) posées avant 0390,
        puis 0390 rejouée : son déclencheur, sa contrainte et son index sont
        retirés d'abord, ainsi que le trigger de projection #711 qui
        n'existait pas avant 0390 ; la colonne reste (un dépôt déjà donné). Le schéma de la
        classe est rendu tel quel ensuite, réservations libérées."""
        path = os.path.join(migrations.MIGRATIONS_DIR, "0390_work_item_migration_repo.sql")
        with open(path, encoding="utf-8") as fh:
            sql = fh.read()
        undo = ("DROP TRIGGER IF EXISTS work_items_migration_slot_sync ON work_items;"
                " DROP TRIGGER IF EXISTS work_items_migration_repo_fill ON work_items;"
                " ALTER TABLE work_items DROP CONSTRAINT IF EXISTS work_items_migration_repo_check;"
                " DROP INDEX IF EXISTS work_items_migration_repo_idx;")
        ids = []

        def restore():
            self.db.script(undo)
            for i in ids:
                self.db.execute("UPDATE work_items SET migration = NULL, migration_repo = NULL"
                                " WHERE id = %s", (i,))
            self.db.script(sql)
            self.db.script("CREATE TRIGGER work_items_migration_slot_sync"
                           " AFTER INSERT OR UPDATE OF migration, migration_repo, app, branch_target, state"
                           " ON work_items FOR EACH ROW"
                           " EXECUTE FUNCTION work_items_migration_slot_sync();")

        self.addCleanup(restore)
        self.db.script(undo)
        for number, repo, issue in rows:
            lot = work.add(self.db, title="avant 0390", app="nexlink", issue_ref=issue,
                           actor="mesh-design")
            ids.append(lot["id"])
            self.db.execute("UPDATE work_items SET migration = %s, migration_repo = %s"
                            " WHERE id = %s", (number, repo, lot["id"]))
        self.db.script(sql)
        return [work.get(self.db, i)["migration_repo"] for i in ids]

    def test_0390_garde_un_depot_deja_donne(self):
        """#443 r2 P2 : un dépôt déjà donné sans issue ne fait pas échouer 0390,
        un dépôt en majuscules est ramené en minuscules (la contrainte l'accepte),
        un dépôt vide prend celui de l'issue."""
        self.assertEqual(
            self._rejoue_0390((389, "manaty/nexlink", None),
                              (390, "Manaty/Nexlink", "manaty/nexlink#7"),
                              (391, " ", "Manaty/Nexlink#8"),
                              (None, "manaty/nexlink", None)),
            ["manaty/nexlink", "manaty/nexlink", "manaty/nexlink", None])
        with self.assertRaises(DbError):
            self.db.execute("UPDATE work_items SET migration = 390,"
                            " migration_repo = 'MANATY/NEXLINK' WHERE id = %d"
                            % self._lot("nx", "manaty/nexlink", 9)["id"])

    def test_0390_refuse_une_reservation_sans_depot(self):
        with self.assertRaises(DbError) as ctx:
            self._rejoue_0390((389, None, None))
        self.assertIn("without a repository", str(ctx.exception))

    def test_la_base_garantit_l_unicite(self):
        """Hors de l'outil, la contrainte refuse un doublon (dépôt, numéro)."""
        a, b = self._lot("a"), self._lot("b")
        self.db.execute("UPDATE work_items SET migration = 42, migration_repo = 'manaty/ameesh'"
                        " WHERE id = %d" % a["id"])
        with self.assertRaises(DbError) as ctx:
            self.db.execute("UPDATE work_items SET migration = 42,"
                            " migration_repo = 'manaty/ameesh' WHERE id = %d" % b["id"])
        self.assertIn("work_items_migration_repo_idx", str(ctx.exception))

    def test_client_d_avant_443(self):
        """Un poste pas encore à jour pose le seul numéro : le déclencheur
        prend le dépôt de l'issue, et la libération efface les deux."""
        nx, am = self._lot("nx", "manaty/nexlink"), self._lot("am")
        for lot in (nx, am):
            self.db.execute("UPDATE work_items SET migration = 500 WHERE id = %d" % lot["id"])
        self.assertEqual(work.get(self.db, nx["id"])["migration_repo"], "manaty/nexlink")
        self.assertEqual(work.get(self.db, am["id"])["migration_repo"], "manaty/ameesh")
        with self.assertRaises(DbError):
            self.db.execute("UPDATE work_items SET migration = 500 WHERE id = %d"
                            % self._lot("am2", n=2)["id"])
        self.db.execute("UPDATE work_items SET migration = NULL WHERE id = %d" % nx["id"])
        self.assertIsNone(work.get(self.db, nx["id"])["migration_repo"])

    def test_liberer_et_lot_ferme(self):
        a = self._lot()
        self._am(a, 9100)
        released = work.reserve_migration(self.db, a["id"], release=True)
        self.assertEqual((released["migration"], released["migration_repo"]), (None, None))
        self.assertIsNone(work.reserve_migration(self.db, a["id"], release=True)["migration"])
        work.close(self.db, a["id"], abandoned=True)
        with self.assertRaises(work.WorkError):
            self._am(a)

    def test_la_fusion_libere_la_reservation(self):
        """#956 : le lot fusionné ne tient plus son numéro ; le suivant peut le
        reprendre (le fichier, lui, le protège toujours)."""
        a, b = self._lot("a"), self._lot("b", n=2)
        number = self._am(a, 9101)["migration"]
        got = work.close_merged(self.db, a["id"], sha="deadbeef", actor="dev1",
                                source="test")
        self.assertEqual(got["result"], "merged")
        self.assertIsNone(got["item"]["migration"])
        item = work.get(self.db, a["id"])
        self.assertEqual((item["migration"], item["migration_repo"]), (None, None))
        self.assertIn("migration 9101 released (fusion, #956)",
                      [e["note"] for e in work.events(self.db, a["id"])])
        self.assertEqual(self._am(b, number)["migration"], number)

    def test_le_passage_terminal_par_move_libere_la_reservation(self):
        """#956 : le chemin `work move` vers merged (et promoted) libère aussi."""
        a = self._lot("move", n=3)
        number = self._am(a, 9102)["migration"]
        for state in ("build", "qa", "merged"):
            work.move(self.db, a["id"], state, actor="dev1")
        self.assertIsNone(work.get(self.db, a["id"])["migration"])
        self.assertEqual(self._am(self._lot("suivant", n=4), number)["migration"], number)

    def test_la_cloture_libere_la_reservation(self):
        a = self._lot("ferme", n=5)
        number = self._am(a, 9103)["migration"]
        work.close(self.db, a["id"], abandoned=True, actor="dev1")
        item = work.get(self.db, a["id"])
        self.assertEqual((item["migration"], item["migration_repo"]), (None, None))
        self.assertEqual(self._am(self._lot("apres", n=6), number)["migration"], number)

    def test_reservation_heritee_d_un_lot_terminal(self):
        """#956 : une réservation posée avant le correctif sur un lot terminal
        (cas #850) ne bloque plus : la réservation suivante la libère dans sa
        transaction, avec sa ligne de journal."""
        old = self._lot("ancien", "manaty/nexlink", 169)
        work.close(self.db, old["id"], abandoned=True, actor="dev1")
        # état d'avant le correctif : le lot terminal garde son numéro
        self.db.execute("UPDATE work_items SET migration = 9401 WHERE id = %d" % old["id"])
        self.assertEqual(work.get(self.db, old["id"])["migration_repo"], "manaty/nexlink")
        self.assertIsNone(pg_work.WorkItems(self.db).migration_holder(9401, "manaty/nexlink"))
        new = self._lot("nouveau", "manaty/nexlink", 170)
        self.assertEqual(self._nx(new, 9401)["migration"], 9401)
        self.assertIsNone(work.get(self.db, old["id"])["migration"])
        notes = " ".join(e["note"] for e in work.events(self.db, old["id"]))
        self.assertIn("migration 9401 released (lot %d closed, #956)" % old["id"], notes)

    def _attend_la_reprise(self, xid, timeout=30.0):
        """Vrai quand une transaction attend le verrou de ligne de `xid` : le
        témoin désigne la transaction, pas une requête qui y ressemble (le banc
        est partagé)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self.db.query(
                "SELECT count(*) AS n FROM pg_locks"
                " WHERE locktype = 'transactionid' AND transactionid = %s"
                "   AND NOT granted", (int(xid),))
            if rows[0]["n"]:
                return True
            time.sleep(0.05)
        return False

    def _ligne_verrouillee(self, lot_id, driver) -> bool:
        """Vrai si la ligne est tenue par une autre transaction (NOWAIT)."""
        conn = db_mod.connect(self.cfg, driver=driver)
        try:
            with conn.transaction() as tx:
                tx.query("SELECT id FROM work_items WHERE id = %s FOR UPDATE NOWAIT",
                         (int(lot_id),))
            return False
        except DbError as exc:
            if exc.sqlstate != "55P03":     # lock_not_available, pas autre chose
                raise
            return True
        finally:
            conn.close()

    def test_ordre_de_verrouillage_des_reservations_heritees(self):
        """#967 : les réservations héritées du même dépôt se verrouillent par id
        croissant — l'ordre canonique du module. Témoin à deux transactions : T
        tient la ligne d'id le plus GRAND (insérée la première : le scan comme
        l'index (dépôt, numéro) la voient d'abord) ; la reprise doit avoir
        verrouillé la plus PETITE avant de s'arrêter sur T. Sans ORDER BY, elle
        attendrait T sans rien tenir et les deux transactions partiraient en
        sens inverse (interblocage possible)."""
        for k, driver in enumerate(_drivers()):
            with self.subTest(driver=driver):
                high, low = 9902 + 10 * k, 9901 + 10 * k
                # insertion (et donc scan physique) : `high` d'abord ; index
                # (dépôt, numéro) : `high` aussi (9501 < 9502) ; id : `low`.
                for lot_id, number in ((high, 9501 + 10 * k), (low, 9502 + 10 * k)):
                    self.db.execute(
                        "INSERT INTO work_items (id, title, state, close_reason,"
                        "                        migration, migration_repo)"
                        " VALUES (%s, 'hérité', 'closed', 'abandoned', %s, 'manaty/nexlink')",
                        (lot_id, number))
                new = self._lot("nouveau", "manaty/nexlink", 171 + k)
                held, release, out = threading.Event(), threading.Event(), {}

                def hold():
                    conn = db_mod.connect(self.cfg, driver=driver)
                    try:
                        with conn.transaction() as tx:
                            tx.query("SELECT id FROM work_items WHERE id = %s FOR UPDATE",
                                     (high,))
                            out["xid"] = tx.query("SELECT txid_current() AS xid")[0]["xid"]
                            held.set()
                            release.wait(60)
                    finally:
                        conn.close()

                def reserve():
                    conn = db_mod.connect(self.cfg, driver=driver)
                    try:
                        out["row"] = pg_work.WorkItems(conn).reserve_migration(
                            new["id"], "manaty/nexlink", None, 9503 + 10 * k,
                            note="migration %(number)s reserved (témoin)", actor="t",
                            issue_ref="manaty/nexlink#%d" % (171 + k))
                    except Exception as exc:   # noqa: BLE001 - rapporté par l'assertion
                        out["row"] = exc
                    finally:
                        conn.close()

                ta = threading.Thread(target=hold)
                ta.start()
                self.assertTrue(held.wait(30), "le verrou témoin n'a pas été posé")
                tb = threading.Thread(target=reserve)
                tb.start()
                try:
                    self.assertTrue(self._attend_la_reprise(out["xid"]),
                                    "la reprise n'a jamais attendu le verrou témoin")
                    self.assertTrue(
                        self._ligne_verrouillee(low, driver),
                        "la reprise attend sans tenir la plus petite ligne : "
                        "verrouillage dans l'ordre du scan, pas par id (#967)")
                finally:
                    release.set()
                    ta.join(30)
                    tb.join(60)
                self.assertIsInstance(out.get("row"), dict, out.get("row"))
                self.assertEqual(out["row"]["migration"], 9503 + 10 * k)
                self.assertEqual(
                    self.db.query("SELECT count(*) AS n FROM work_items"
                                  " WHERE migration IS NOT NULL AND id IN (%s, %s)",
                                  (low, high))[0]["n"], 0)

    def test_liberer_la_reservation_d_un_lot_terminal(self):
        """#956 : `--release` marche encore sur un lot déjà terminal (héritage)."""
        old = self._lot("tardif", n=7)
        self._am(old, 9104)
        work.close(self.db, old["id"], abandoned=True, actor="dev1")
        self.db.execute("UPDATE work_items SET migration = 9104 WHERE id = %d" % old["id"])
        released = work.reserve_migration(self.db, old["id"], release=True, actor="dev1")
        self.assertIsNone(released["migration"])

    def test_cli(self):
        a = self._lot()
        proc = self.mesh("work", "migration", str(a["id"]), "--number", "9200",
                         "--repo", self.ameesh)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("migration 9200 reserved in manaty/ameesh", proc.stdout)
        proc = self.mesh("work", "migration", str(a["id"]), "--release")
        self.assertIn("no migration reserved", proc.stdout)

    def test_reference_changee_pendant_le_scan_refusee(self):
        """#1830 : la référence qui décide du dépôt est lue avant le scan ;
        une correction concurrente pendant ce scan ne doit pas laisser
        issue_ref d'un dépôt avec migration_repo d'un autre."""
        lot = self._lot("fenêtre", "manaty/ameesh", 1)
        observed = work.get(self.db, lot["id"])
        work.set_issue_ref(self.db, lot["id"], "manaty/nexlink#1567", actor="human:owner")
        live = work.get(self.db, lot["id"])
        with mock.patch.object(work, "get", side_effect=[observed, live]):
            with self.assertRaises(work.WorkError) as ctx:
                work.reserve_migration(self.db, lot["id"], repo=self.ameesh)
        self.assertIn("issue reference changed meanwhile", str(ctx.exception))
        got = work.get(self.db, lot["id"])
        self.assertIsNone(got["migration"])
        self.assertIsNone(got["migration_repo"])
        self.assertEqual(got["issue_ref"], "manaty/nexlink#1567")

    def test_cas_storage_refuse_une_reference_observee_perimee(self):
        """#1830 : le storage n'écrit pas sous une référence observée qui
        n'est plus celle du lot ; la référence exacte passe."""
        lot = self._lot("cas", "manaty/ameesh", 2)
        observed = work.get(self.db, lot["id"])
        work.set_issue_ref(self.db, lot["id"], "manaty/nexlink#1567", actor="human:owner")
        store = storage.of(self.db).work
        self.assertIsNone(store.reserve_migration(
            lot["id"], "manaty/ameesh", 9703, 0,
            note="migration %(number)s reserved (témoin)", actor="t",
            issue_ref=observed["issue_ref"]))
        self.assertIsNone(work.get(self.db, lot["id"])["migration"])
        row = store.reserve_migration(
            lot["id"], "manaty/nexlink", 9701, 0,
            note="migration %(number)s reserved (témoin)", actor="t",
            issue_ref="manaty/nexlink#1567")
        self.assertEqual((row["migration"], row["migration_repo"]), (9701, "manaty/nexlink"))


class CorrectionIssueRefTest(PgTestCase):
    """#1603 : `work issue-ref` corrige la référence qui décide du dépôt de la
    réservation (`lot_repository`), réservé aux correcteurs et journalisé."""

    def _lot(self):
        return work.add(self.db, title="mauvais dépôt", issue_ref="manaty/nexlink#1567",
                        actor="mesh-design")

    def test_correcteur_corrige_et_journalise_puis_idempotent(self):
        lot = self._lot()
        proc = self.mesh("work", "issue-ref", str(lot["id"]), "manaty/ameesh#1603",
                         "--actor", "human:owner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["issue_ref"], "manaty/ameesh#1603")
        events = work.events(self.db, lot["id"])
        self.assertEqual(events[0]["actor"], "human:owner")
        self.assertIn("manaty/nexlink#1567", events[0]["note"])
        self.assertIn("manaty/ameesh#1603", events[0]["note"])
        proc = self.mesh("work", "issue-ref", str(lot["id"]), "manaty/ameesh#1603",
                         "--actor", "human:owner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.events(self.db, lot["id"]), events)

    def test_none_efface_et_format_refuse(self):
        lot = self._lot()
        proc = self.mesh("work", "issue-ref", str(lot["id"]), "pas-une-ref",
                         "--actor", "human:owner")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("owner/repo#N", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["issue_ref"], "manaty/nexlink#1567")
        proc = self.mesh("work", "issue-ref", str(lot["id"]), "--none",
                         "--actor", "human:owner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(work.get(self.db, lot["id"])["issue_ref"])
        proc = self.mesh("work", "issue-ref", str(lot["id"]), "manaty/ameesh#1", "--none",
                         "--actor", "human:owner")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--none", proc.stderr)

    def test_reservation_dans_l_ancien_depot_a_liberer_d_abord(self):
        """Une réservation prise sous l'ancienne référence ne suit pas la
        correction : refus tant qu'elle n'est pas libérée (sinon `work
        migration` rendrait en silence le numéro de l'autre dépôt)."""
        lot = self._lot()
        nexlink = _depot(os.path.join(self.tmp, "nexlink"), "manaty/nexlink",
                         NEXLINK.directory, ["376_b.sql"])
        _git(nexlink, "config", "ameesh.target", "develop")   # #2620 : cible lue
        work.reserve_migration(self.db, lot["id"], 453, repo=nexlink)
        with self.assertRaises(work.WorkError) as ctx:
            work.set_issue_ref(self.db, lot["id"], "manaty/ameesh#1603", actor="human:owner")
        self.assertIn("--release", str(ctx.exception))
        self.assertEqual(work.get(self.db, lot["id"])["issue_ref"], "manaty/nexlink#1567")
        # même dépôt : la réservation reste juste, la correction passe
        work.set_issue_ref(self.db, lot["id"], "manaty/nexlink#1600", actor="human:owner")
        work.reserve_migration(self.db, lot["id"], release=True)
        got = work.set_issue_ref(self.db, lot["id"], "manaty/ameesh#1603", actor="human:owner")
        self.assertEqual(got["issue_ref"], "manaty/ameesh#1603")

    def test_agent_lie_ne_corrige_pas_a_la_place_d_un_humain(self):
        lot = self._lot()
        env = self.env(AGENT_MAIL_NAME="dev1", AMEESH_ALERT_ORCHESTRATORS="orch")
        proc = self.mesh("work", "issue-ref", str(lot["id"]), "manaty/ameesh#1",
                         "--actor", "human:owner", env=env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("correction refused", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["issue_ref"], "manaty/nexlink#1567")

    def test_reservation_arrivee_entre_lecture_et_ecriture(self):
        """#1654 : la lecture (garde du dépôt) et le CAS ne sont pas atomiques ;
        une réservation prise entre les deux ne doit pas passer en silence. Le
        CAS couvre la réservation lue : refus explicite, issue intacte."""
        lot = self._lot()
        observed = work.get(self.db, lot["id"])   # lue AVANT la réservation
        nexlink = _depot(os.path.join(self.tmp, "nexlink"), "manaty/nexlink",
                         NEXLINK.directory, ["376_b.sql"])
        _git(nexlink, "config", "ameesh.target", "develop")   # #2620 : cible lue
        work.reserve_migration(self.db, lot["id"], 453, repo=nexlink)
        live = work.get(self.db, lot["id"])
        with mock.patch.object(work, "get", side_effect=[observed, live]):
            with self.assertRaises(work.WorkError) as ctx:
                work.set_issue_ref(self.db, lot["id"], "manaty/ameesh#1603",
                                   actor="human:owner")
        self.assertIn("reservation changed meanwhile", str(ctx.exception))
        got = work.get(self.db, lot["id"])
        self.assertEqual(got["issue_ref"], "manaty/nexlink#1567")
        self.assertEqual((got["migration"], got["migration_repo"]), (453, "manaty/nexlink"))

    def test_cas_storage_couvre_la_reservation_observee(self):
        """#1654 : le storage resserre le CAS quand l'appelant fournit la
        réservation lue ; sans observation, le CAS d'origine est conservé
        (les appelants qui ne lisent pas la réservation ne changent pas)."""
        lot = self._lot()
        observed = work.get(self.db, lot["id"])
        nexlink = _depot(os.path.join(self.tmp, "nexlink"), "manaty/nexlink",
                         NEXLINK.directory, ["376_b.sql"])
        _git(nexlink, "config", "ameesh.target", "develop")   # #2620 : cible lue
        work.reserve_migration(self.db, lot["id"], 453, repo=nexlink)
        store = storage.of(self.db).work
        self.assertFalse(store.set_issue_ref(lot["id"], "manaty/ameesh#1603",
                                             current=observed["issue_ref"],
                                             migration_repo=observed.get("migration_repo")))
        self.assertTrue(store.set_issue_ref(lot["id"], "manaty/ameesh#1603",
                                            current=observed["issue_ref"]))


def _drivers():
    """Les pilotes présents : la passe psql de la CI tourne sans psycopg."""
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return ("psql",)
    return ("psql", "psycopg")


class ReservationsSimultaneesTest(PgTestCase):
    """#443 : deux réservations au même instant, sur les deux pilotes."""

    def setUp(self):
        super().setUp()
        self.nexlink = _depot(os.path.join(self.tmp, "nexlink"), "manaty/nexlink",
                              NEXLINK.directory, ["376_b.sql"])

    def _lots(self, n):
        return [work.add(self.db, title="lot %d" % i, issue_ref="manaty/nexlink#%d" % i,
                         actor="mesh-design")["id"] for i in range(n)]

    def _blocked(self, timeout=60.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self.db.query(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                " AND datname = current_database()"
                " AND (query LIKE '%UPDATE work_items SET migration = greatest(%'"
                " OR query LIKE '%:migration-repo:%')")
            if rows[0]["n"]:
                return True
            time.sleep(0.05)
        return False

    def test_la_perdante_recalcule(self):
        """A tient 377 sans avoir validé ; B attend sur la réservation
        (verrou dépôt #711, ou index), puis recalcule et prend 378."""
        for holder, other in [(h, o) for h in _drivers() for o in _drivers()]:
            with self.subTest(holder=holder, other=other):
                self.db.execute("UPDATE work_items SET migration = NULL, migration_repo = NULL")
                a, b = self._lots(2)
                held, done, out = threading.Event(), threading.Event(), {}

                def hold():
                    conn = db_mod.connect(self.cfg, driver=holder)
                    try:
                        with conn.transaction() as tx:
                            tx.query("UPDATE work_items SET migration = 377,"
                                     " migration_repo = 'manaty/nexlink' WHERE id = %s"
                                     " RETURNING id", (a,))
                            held.set()
                            done.wait(60)   # jusqu'à ce que B attende sur l'index
                    finally:
                        conn.close()

                def reserve():
                    conn = db_mod.connect(self.cfg, driver=other)
                    try:
                        out["b"] = work.reserve_migration(conn, b, repo=self.nexlink)
                    except Exception as exc:  # noqa: BLE001 - rapporté par l'assertion
                        out["b"] = exc
                    finally:
                        conn.close()

                ta = threading.Thread(target=hold)
                ta.start()
                self.assertTrue(held.wait(30))
                tb = threading.Thread(target=reserve)
                tb.start()
                # B attend réellement la réservation de A avant son commit
                self.assertTrue(self._blocked(), "B never waited on A's reservation")
                done.set()
                ta.join(30)
                tb.join(60)
                self.assertIsInstance(out.get("b"), dict, out.get("b"))
                self.assertEqual(out["b"]["migration"], 378)
                self.assertEqual(work.get(self.db, a)["migration"], 377)

    def test_rafale_de_20_sur_les_deux_pilotes(self):
        """#478 : 20 réservations simultanées, 20 numéros distincts — la
        rafale de qa-443 épuisait les 8 essais de _RESERVE_ATTEMPTS."""
        ids = self._lots(20)
        start, out = threading.Barrier(len(ids)), {}

        def reserve(item_id, driver):
            conn = db_mod.connect(self.cfg, driver=driver)
            try:
                start.wait(30)
                out[item_id] = work.reserve_migration(conn, item_id,
                                                      repo=self.nexlink)["migration"]
            except Exception as exc:  # noqa: BLE001 - rapporté par l'assertion
                out[item_id] = exc
            finally:
                conn.close()

        drivers = _drivers()
        threads = [threading.Thread(target=reserve, args=(i, drivers[k % len(drivers)]))
                   for k, i in enumerate(ids)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(180)
        self.assertEqual(sorted(out.values()), list(range(377, 377 + len(ids))), out)


#: l'index unique (dépôt, numéro) de L122 : lui seul fait une contention
_INDEX = "work_items_migration_repo_idx"


def _violation(*, constraint=_INDEX, state="23505") -> DbError:
    """La violation d'unicité de `constraint` telle qu'un pilote la rend : le
    code SQLSTATE 23505 et le nom exact de la contrainte — ceux de psycopg
    (`diag.constraint_name`) comme ceux de psql (`VERBOSITY=verbose`). Le
    message n'entre pas dans la classification (codex #478 r2)."""
    return DbError('psql: ERROR:  duplicate key value violates unique constraint "%s"'
                   % constraint, sqlstate=state, constraint=constraint)


def _psql_verbose(constraint=_INDEX, *, severity="ERROR", state="23505",
                  message=None) -> str:
    """Le rapport d'erreur de psql en `VERBOSITY=verbose` : le code SQLSTATE
    sur la ligne d'erreur, le nom exact dans son champ. `severity` et
    `message` suivent `lc_messages` du serveur (« ERREUR », message
    français) ; le code et le champ CONSTRAINT NAME, non."""
    if message is None:
        message = 'duplicate key value violates unique constraint "%s"' % constraint
    return ("%s:  %s: %s\n"
            "DETAIL:  Key (migration_repo, migration)=(manaty/nexlink, 377)"
            " already exists.\n"
            "SCHEMA NAME:  public\n"
            "TABLE NAME:  work_items\n"
            "CONSTRAINT NAME:  %s\n"
            "LOCATION:  _bt_check_unique, nbtinsert.c:677\n"
            % (severity, state, message, constraint))


def _failed(stderr: str) -> subprocess.CompletedProcess:
    """Un psql en échec (code 3, ON_ERROR_STOP) dont le `_fail` lit stderr."""
    return subprocess.CompletedProcess([], 3, "", stderr)


class _FakeTime:
    """Horloge fausse d'un test de budget : `monotonic()` ne bouge que par
    `sleep()`, qui est enregistré au lieu d'attendre (codex #478 r1)."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class RetryReservationTest(PgTestCase):
    """#478 (codex r1) : le budget de réessais de la réservation, sans
    ordonnancement réel ni verrou : les violations d'unicité sont injectées
    et l'horloge est fausse."""

    def setUp(self):
        super().setUp()
        self.nexlink = _depot(os.path.join(self.tmp, "nexlink"), "manaty/nexlink",
                              NEXLINK.directory, ["376_b.sql"])

    def _lot(self):
        return work.add(self.db, title="lot", issue_ref="manaty/nexlink#1",
                        actor="mesh-design")["id"]

    def _reserve(self, exc, *, budget):
        """`reserve_migration` sur un lot neuf, chaque essai levant `exc` :
        rend (résultat ou exception, horloge, essais, tirages d'attente)."""
        lot = self._lot()
        clock = _FakeTime()
        attempts, draws = [], []

        def transaction():
            attempts.append(clock.now)
            raise exc

        def uniform(low, high):
            draws.append((low, high))
            return high

        with mock.patch.object(pg_work, "time", clock), \
                mock.patch.object(pg_work, "_RESERVE_BUDGET_S", budget), \
                mock.patch.object(pg_work, "random",
                                  types.SimpleNamespace(uniform=uniform)), \
                mock.patch.object(self.db, "transaction", side_effect=transaction):
            try:
                out = work.reserve_migration(self.db, lot, repo=self.nexlink)
            except Exception as got:  # noqa: BLE001 - rapporté par l'assertion
                out = got
        return out, clock, attempts, draws

    def test_l_attente_est_bornee_par_le_budget_restant(self):
        # codex #478 r1 : avec 1 ms de budget, l'ancienne attente de 5 ms
        # entamait une transaction après l'échéance ; l'attente est bornée au
        # restant, et rien n'est retenté une fois l'échéance passée
        got, clock, attempts, _ = self._reserve(_violation(), budget=0.001)
        self.assertIsInstance(got, work.WorkError, got)
        self.assertEqual(str(got), "reservation contention, retry")
        self.assertEqual(attempts, [0.0], "une seule transaction, avant l'échéance")
        self.assertEqual(clock.slept, [0.001], "l'attente tient dans le budget")

    def test_budget_nul_ne_retente_pas(self):
        # codex #478 r1 : budget épuisé, la première violation rend la
        # contention sans deuxième transaction ni attente
        got, clock, attempts, _ = self._reserve(_violation(), budget=0.0)
        self.assertIsInstance(got, work.WorkError, got)
        self.assertEqual(attempts, [0.0])
        self.assertEqual(clock.slept, [])

    def test_une_erreur_qui_nomme_l_index_sans_unicite_remonte(self):
        # codex #478 r1 : « permission denied for relation
        # work_items_migration_repo_idx » n'est pas une violation d'unicité :
        # elle remonte telle quelle, jamais en WorkError de contention
        denied = DbError("psql: ERROR:  permission denied for relation %s" % _INDEX)
        got, _, _, _ = self._reserve(denied, budget=0.05)
        self.assertIs(got, denied, got)

    def test_la_violation_se_reconnait_au_code_sqlstate(self):
        # codex #478 r1/r2 : psycopg donne le code 23505 et le nom exact par
        # ses diagnostics ; c'est la contention de l'index
        got, _, _, _ = self._reserve(_violation(), budget=0.0)
        self.assertIsInstance(got, work.WorkError, got)
        self.assertEqual(str(got), "reservation contention, retry")

    def test_une_autre_contrainte_au_nom_proche_ne_retente_pas(self):
        # codex #478 r2 : `work_items_migration_repo_idx_other` contient le
        # nom de l'index sans être lui ; sa violation remonte telle quelle et
        # n'entame aucune deuxième transaction (l'ancien test par sous-chaîne
        # la retentait)
        other = _violation(constraint=_INDEX + "_other")
        got, _, attempts, _ = self._reserve(other, budget=10.0)
        self.assertIs(got, other, got)
        self.assertEqual(attempts, [0.0], "aucune deuxième transaction")

    def test_psql_verbose_reconnait_la_violation_meme_en_francais(self):
        # codex #478 r2 : un serveur en `lc_messages=fr` écrit « ERREUR » et
        # un message français ; le code SQLSTATE et le champ CONSTRAINT NAME
        # ne sont pas traduits, et c'est tout ce que la classification lit
        exc = db_mod.PsqlDriver._fail(None, _failed(_psql_verbose(
            severity="ERREUR",
            message="la valeur d'une clé dupliquée rompt la contrainte unique"
                    " « %s »" % _INDEX)))
        self.assertEqual(exc.sqlstate, "23505")
        self.assertEqual(exc.constraint, _INDEX)
        got, _, _, _ = self._reserve(exc, budget=0.0)
        self.assertIsInstance(got, work.WorkError, got)
        self.assertEqual(str(got), "reservation contention, retry")

    def test_psql_verbose_autre_contrainte_ne_retente_pas(self):
        # codex #478 r2 : le champ CONSTRAINT NAME tranche exactement, même
        # quand le message (traduit) nomme l'index par sous-chaîne
        exc = db_mod.PsqlDriver._fail(None, _failed(_psql_verbose(
            constraint=_INDEX + "_other",
            message='duplicate key value violates unique constraint "%s"' % _INDEX)))
        self.assertEqual(exc.constraint, _INDEX + "_other")
        got, _, attempts, _ = self._reserve(exc, budget=10.0)
        self.assertIs(got, exc, got)
        self.assertEqual(attempts, [0.0], "aucune deuxième transaction")

    def test_psql_verbose_ignore_les_notices_avant_l_erreur(self):
        # codex #478 r3 : psql imprime les NOTICE et WARNING traversés avant
        # l'erreur ; leur classe (00, 01, 02) n'est pas une erreur et ne doit
        # pas donner le code qui classe l'échec. Le libellé de sévérité peut
        # être traduit : c'est la classe du code qui tranche.
        for severity, state in (("NOTICE", "00000"), ("AVERTISSEMENT", "01000"),
                                ("INFO", "02000")):
            with self.subTest(severity=severity, state=state):
                stderr = ("%s:  %s: table \"work_items\" does not exist, skipping\n"
                          % (severity, state)) + _psql_verbose()
                exc = db_mod.PsqlDriver._fail(None, _failed(stderr))
                self.assertEqual(exc.sqlstate, "23505")
                self.assertEqual(exc.constraint, _INDEX)
                got, _, _, _ = self._reserve(exc, budget=0.0)
                self.assertIsInstance(got, work.WorkError, got)

    def test_psql_verbose_le_message_ne_pretend_pas_le_champ_contrainte(self):
        # codex #478 r3 : un message multiligne qui recopie « CONSTRAINT
        # NAME: » ne doit pas se faire passer pour le champ ; psql écrit ses
        # champs structurés APRÈS le message — le dernier est le vrai
        exc = db_mod.PsqlDriver._fail(None, _failed(_psql_verbose(
            constraint=_INDEX,
            message='duplicate key value violates unique constraint "%s"\n'
                    "CONSTRAINT NAME:  %s" % (_INDEX, _INDEX + "_other"))))
        self.assertEqual(exc.constraint, _INDEX)
        got, _, _, _ = self._reserve(exc, budget=0.0)
        self.assertIsInstance(got, work.WorkError, got)

    def test_le_code_et_la_contrainte_du_pilote_survivent_a_la_dberror(self):
        # codex #478 r1/r2 : le code SQLSTATE et le nom de contrainte du
        # pilote (psycopg) survivent à la traduction en DbError — seuls eux
        # distinguent 23505 d'une autre erreur qui nomme le même index
        class Diag:
            constraint_name = _INDEX

        class DriverError(Exception):
            sqlstate = "23505"
            diag = Diag()

        mapped = db_mod._map_error(DriverError("duplicate key value"))
        self.assertEqual(mapped.sqlstate, "23505")
        self.assertEqual(mapped.constraint, _INDEX)
        self.assertIsNone(db_mod._map_error(Exception("boom")).sqlstate)
        self.assertIsNone(db_mod._map_error(Exception("boom")).constraint)

    def test_l_attente_plein_jitter_part_de_zero(self):
        # codex #478 r1 (P3) : plein jitter — uniform(0, min(cap, base*2^n)) —
        # et non uniform(base, wait) : le premier tirage part de 0, les
        # suivants doublent jusqu'au plafond
        got, _, _, draws = self._reserve(_violation(), budget=10.0)
        self.assertIsInstance(got, work.WorkError, got)
        self.assertEqual(draws[:3], [(0.0, 0.005), (0.0, 0.01), (0.0, 0.02)])

    def test_budget_epuise_rend_workerror_lisible(self):
        """#478 : le budget de réessais épuisé rend un WorkError lisible,
        jamais la DbError brute de l'index. Déterministe (codex #478 r1) :
        violation injectée, horloge fausse — plus de verrou réel ni
        d'attente de 60 s."""
        got, clock, attempts, _ = self._reserve(_violation(), budget=0.0)
        self.assertIsInstance(got, work.WorkError, got)
        self.assertEqual(str(got), "reservation contention, retry")
        self.assertNotIsInstance(got, DbError)
        self.assertEqual(attempts, [0.0])
        self.assertEqual(clock.slept, [])


class NumeroAtterriTest(PgTestCase):
    """#1567 : le numéro **atterri** est enregistré et nommé par `work show`,
    pas seulement la réservation — un paquet renumérote au max+1 de la cible
    (#1240 réservait 457, atterri 452)."""

    def setUp(self):
        super().setUp()
        self.nexlink = _depot(os.path.join(self.tmp, "nexlink"), "manaty/nexlink",
                              NEXLINK.directory, ["375_a.sql", "376_b.sql"])
        _git(self.nexlink, "config", "ameesh.target", "develop")
        self.ameesh = _depot(os.path.join(self.tmp, "ameesh"), "manaty/ameesh",
                             AMEESH.directory, ["0005_e.sql"])

    def _lot(self, repo="manaty/nexlink", n=1):
        return work.add(self.db, title="lot", issue_ref="%s#%d" % (repo, n),
                        actor="mesh-design")

    def _nx(self, lot, number=None, **kw):
        return work.reserve_migration(self.db, lot["id"], number, repo=self.nexlink, **kw)

    def _merged(self, lot, **kwargs):
        # #1286 × #1567: keep real Git evidence while testing landed numbers.
        _git(self.nexlink, "commit", "--allow-empty", "-qm",
             "Deliver\n\nameesh-work: %d" % lot["id"])
        sha = subprocess.check_output(
            ["git", "-C", self.nexlink, "rev-parse", "HEAD"], text=True).strip()
        return work.merged(self.db, lot["id"], sha=sha, repo=self.nexlink, **kwargs)

    def test_la_fusion_nomme_le_numero_atterri(self):
        lot = self._lot()
        self.assertEqual(self._nx(lot)["migration"], 377)
        done = self._merged(lot, actor="dev1", migration_landed=452)
        self.assertEqual(done["result"], "merged")
        item = work.get(self.db, lot["id"])
        # #956 (main) : la fusion libère la réservation dans la transaction de
        # `close_merged` ; le numéro atterri est un fait à part, et le journal
        # garde les deux (l'atterri ET la réservation d'origine)
        self.assertEqual((item["migration"], item["migration_repo"]), (None, None))
        self.assertEqual((item["migration_landed"], item["migration_landed_repo"]),
                         (452, "manaty/nexlink"))
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertTrue(any("migration 452 landed (reserved 377)" in n for n in notes), notes)
        self.assertTrue(any("migration 377 released (fusion, #956)" in n for n in notes), notes)
        self.assertEqual(
            work.migration_line(item),
            "452 landed in manaty/nexlink (L122) — file"
            " supabase/migrations/452_<name>.sql")
        proc = self.mesh("work", "show", str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("migration: 452 landed in manaty/nexlink (L122) — file"
                      " supabase/migrations/452_<name>.sql", proc.stdout)
        self.assertNotIn("file supabase/migrations/377", proc.stdout)
        proc = self.mesh("work", "show", str(lot["id"]), "--json")
        self.assertIn('"migration_landed": 452', proc.stdout)
        self.assertIn('"migration_landed_repo": "manaty/nexlink"', proc.stdout)

    def test_sans_enregistrement_la_vue_ne_donne_pas_la_reservation_pour_atterrie(self):
        lot = self._lot()
        self._nx(lot)
        self._merged(lot, actor="dev1")
        item = work.get(self.db, lot["id"])
        self.assertIsNone(item["migration_landed"])
        # #956 (main) : la fusion a libéré la réservation ; sans numéro atterri
        # enregistré, la vue ne nomme aucune migration — jamais 377 comme
        # atterrie. La branche « landed number not recorded » reste celle des
        # lignes héritées d'avant #956, où `migration` survit à l'état terminal.
        self.assertEqual((item["migration"], item["migration_repo"]), (None, None))
        self.assertIsNone(work.migration_line(item))
        proc = self.mesh("work", "show", str(lot["id"]))
        self.assertNotIn("migration:", proc.stdout)
        self.assertNotIn("377 landed", proc.stdout)
        self.assertNotIn("supabase/migrations/377", proc.stdout)
        # ligne héritée : la réservation est encore là, sans numéro atterri
        self.db.execute("UPDATE work_items SET migration = 377,"
                        " migration_repo = 'manaty/nexlink' WHERE id = %d" % lot["id"])
        item = work.get(self.db, lot["id"])
        self.assertEqual(work.migration_line(item),
                         "377 reserved in manaty/nexlink (L122)"
                         " — landed number not recorded")
        proc = self.mesh("work", "show", str(lot["id"]))
        self.assertIn("migration: 377 reserved in manaty/nexlink (L122)"
                      " — landed number not recorded", proc.stdout)
        self.assertNotIn("377 landed", proc.stdout)
        self.assertNotIn("supabase/migrations/377", proc.stdout)

    def _slot(self, lot):
        return self.db.query(
            "SELECT number, status FROM work_migration_reservations"
            " WHERE work_item_id = %s AND slot = 'default'", (lot["id"],))[0]

    def test_le_creneau_atterri_suit_le_numero_pose(self):
        """#4097 : la fusion déclarée avec le numéro réellement posé lande le
        créneau `default` à ce numéro, plus au numéro réservé ; la
        déclaration après coup (paquet fermé par le balayage) le corrige."""
        lot = self._lot()
        self.assertEqual(self._nx(lot)["migration"], 377)
        self._merged(lot, actor="dev1", migration_landed=376)
        self.assertEqual(self._slot(lot), {"number": 376, "status": "landed"})
        later = self._lot(n=2)
        self.assertEqual(self._nx(later)["migration"], 377)
        self._merged(later, actor="sweeper")
        # #4100 : aucun fichier 377 ni dans le commit ni sur la cible : relâché
        self.assertEqual(self._slot(later), {"number": 377, "status": "released"})
        self._merged(later, actor="pilote", migration_landed=375)
        self.assertEqual(self._slot(later), {"number": 375, "status": "landed"})

    # -- #4100 : le relevé suit le numéro posé par un paquet ------------------

    def _write(self, name, content):
        path = os.path.join(self.nexlink, NEXLINK.directory, name)
        with open(path, "w") as fh:
            fh.write(content)

    def _head(self):
        return subprocess.check_output(
            ["git", "-C", self.nexlink, "rev-parse", "HEAD"], text=True).strip()

    def _packet(self, name, *renamed, content="--"):
        """La branche du lot porte `name` ; le paquet la fusionne sur develop,
        puis renumérote vers `renamed` (le premier par `git mv`, les suivants
        sont des copies). Rend le commit de la branche."""
        directory = NEXLINK.directory
        _git(self.nexlink, "checkout", "-qb", "agent/z-%s" % name)
        self._write(name, content)
        _git(self.nexlink, "add", ".")
        _git(self.nexlink, "commit", "-qm", name)
        sha = self._head()
        _git(self.nexlink, "checkout", "-q", "develop")
        _git(self.nexlink, "merge", "-q", "--no-ff", "-m", "paquet", "agent/z-%s" % name)
        if renamed:
            _git(self.nexlink, "mv", "%s/%s" % (directory, name),
                 "%s/%s" % (directory, renamed[0]))
            for other in renamed[1:]:
                self._write(other, content)
            _git(self.nexlink, "add", ".")
            _git(self.nexlink, "commit", "-qm", "renumérotation")
        return sha

    def _notes(self, lot):
        return [e["note"] for e in work.events(self.db, lot["id"])]

    def test_le_paquet_ferme_suit_la_renumerotation(self):
        """#4100 : une renumérotation 377 → 378 dans un paquet fermé par le
        relevé (sans `migration_landed` déclaré) donne landed=378 ; 377 n'est
        plus tenu et l'allocateur propose le premier libre au-dessus de la tête
        réelle de la cible."""
        lot = self._lot()
        self.assertEqual(self._nx(lot)["migration"], 377)
        _commit(self.nexlink, NEXLINK.directory, "377_other.sql")
        sha = self._packet("377_key.sql", "378_key.sql")
        done = work.close_merged(self.db, lot["id"], sha=sha, actor="packet:develop",
                                 source="packet manifest", repo=self.nexlink)
        self.assertEqual(done["result"], "merged", done)
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["migration_landed"], item["migration_landed_repo"]),
                         (378, "manaty/nexlink"))
        self.assertEqual(self._slot(lot), {"number": 378, "status": "landed"})
        self.assertTrue(any(
            "migration 378 landed (reserved 377, renumbered: 377_key.sql → 378 (by name))"
            in n for n in self._notes(lot)), self._notes(lot))
        _git(self.nexlink, "branch", "-qD", "agent/z-377_key.sql")
        self.assertEqual(self._nx(self._lot(n=2))["migration"], 379)

    def test_le_contenu_identifie_un_fichier_renomme(self):
        """#4100 : nom changé avec le numéro, contenu identique : l'empreinte
        du contenu désigne le numéro posé."""
        lot = self._lot()
        self._nx(lot)
        _commit(self.nexlink, NEXLINK.directory, "377_other.sql")
        sha = self._packet("377_key.sql", "378_renamed.sql", content="create table k();")
        work.close_merged(self.db, lot["id"], sha=sha, actor="packet:develop",
                          source="packet manifest", repo=self.nexlink)
        self.assertEqual(work.get(self.db, lot["id"])["migration_landed"], 378)
        self.assertEqual(self._slot(lot), {"number": 378, "status": "landed"})

    def test_deux_candidats_relachent_sans_deviner(self):
        """#4100 : deux candidats (378_key, 379_key) : rien n'est deviné, la
        réservation 377 est relâchée et le journal dit pourquoi."""
        lot = self._lot()
        self._nx(lot)
        _commit(self.nexlink, NEXLINK.directory, "377_other.sql")
        sha = self._packet("377_key.sql", "378_key.sql", "379_key.sql")
        done = work.close_merged(self.db, lot["id"], sha=sha, actor="packet:develop",
                                 source="packet manifest", repo=self.nexlink)
        self.assertEqual(done["result"], "merged", done)
        self.assertIsNone(work.get(self.db, lot["id"])["migration_landed"])
        self.assertEqual(self._slot(lot), {"number": 377, "status": "released"})
        self.assertTrue(any(
            "migration 377 released (not landed: 377_key.sql absent from develop,"
            " ambiguous matches 378, 379)" in n for n in self._notes(lot)),
            self._notes(lot))

    def test_un_fichier_pose_tel_quel_ne_change_rien(self):
        lot = self._lot()
        self._nx(lot)
        sha = self._packet("377_key.sql")
        work.close_merged(self.db, lot["id"], sha=sha, actor="packet:develop",
                          source="packet manifest", repo=self.nexlink)
        self.assertIsNone(work.get(self.db, lot["id"])["migration_landed"])
        self.assertEqual(self._slot(lot), {"number": 377, "status": "landed"})
        self.assertFalse(any("renumbered" in n or "not landed" in n
                             for n in self._notes(lot)), self._notes(lot))

    def test_un_numero_reoccupe_par_un_autre_fichier_est_suivi(self):
        """#4100 : nexlink 600 — le numéro réservé porte depuis un AUTRE
        fichier sur la cible ; la migration du lot est suivie sous son numéro
        posé, pas tenue pour posée."""
        lot = self._lot()
        self._nx(lot)
        sha = self._packet("377_key.sql", "376_key.sql")
        _commit(self.nexlink, NEXLINK.directory, "377_autre_lot.sql")
        # 376 est déjà occupé par 376_b.sql : le paquet a posé 376_key à côté
        work.close_merged(self.db, lot["id"], sha=sha, actor="packet:develop",
                          source="packet manifest", repo=self.nexlink)
        self.assertEqual(work.get(self.db, lot["id"])["migration_landed"], 376)

    def _unit(self, lot, name, content="--"):
        """Une unité de composition sur develop (un parent, trailer du lot) qui
        pose `name`, déjà renuméroté par le compositeur."""
        self._write(name, content)
        _git(self.nexlink, "add", ".")
        _git(self.nexlink, "commit", "-qm", "composition : #%d\n\nameesh-work: %d"
             % (lot["id"], lot["id"]))
        return self._head()

    def test_la_composition_ferme_sur_l_unite_renumerotee(self):
        """#4100 (revue cx3) : la composition ferme le lot sur l'unité du
        paquet, déjà renumérotée (602 → 599 : 377 → 378 ici), pas sur sa
        source. Le fichier du lot est lu à son dernier gel ; et 377, réoccupé
        depuis par un autre fichier, ne le fait pas tenir pour posé."""
        lot = self._lot()
        self._nx(lot)
        _git(self.nexlink, "checkout", "-qb", "agent/z-src")
        self._write("377_key.sql", "create table k();")
        _git(self.nexlink, "add", ".")
        _git(self.nexlink, "commit", "-qm", "source")
        source = self._head()
        work.milestone(self.db, lot["id"], "frozen", sha=source, actor="ouvrier")
        _git(self.nexlink, "checkout", "-q", "develop")
        unit = self._unit(lot, "378_key.sql", "create table k();")
        _commit(self.nexlink, NEXLINK.directory, "377_autre_lot.sql")
        done = work.close_merged(self.db, lot["id"], sha=unit, actor="composition:9",
                                 source="package composition #9", repo=self.nexlink)
        self.assertEqual(done["result"], "merged", done)
        self.assertEqual(work.get(self.db, lot["id"])["migration_landed"], 378)
        self.assertEqual(self._slot(lot), {"number": 378, "status": "landed"})
        self.assertTrue(any("377_key.sql → 378 (by name)" in n for n in self._notes(lot)),
                        self._notes(lot))

    def test_sync_packets_sans_gel_suit_l_unite(self):
        """#4100 (revue cx3) : `sync-packets` sur un lot SANS gel ferme sur le
        commit de composition (trailer) ; l'unité ajoute un seul fichier
        numéroté, présent sur la cible : c'est le numéro posé."""
        from ameesh import packet_close

        lot = self._lot()
        self._nx(lot)
        unit = self._unit(lot, "378_key.sql")
        os.makedirs(os.path.join(self.nexlink, "reviews"), exist_ok=True)
        with open(os.path.join(self.nexlink, "reviews", "integration-paquet-9.md"),
                  "w") as fh:
            fh.write("# Paquet 9\n\n## Composition (1 unité)\n\n"
                     "| unité | lot | source | commit | note |\n|---|---|---|---|---|\n"
                     "| 1 | #%d | `%s` | `%s` | renumérotée 377 → 378 |\n"
                     % (lot["id"], unit, unit))
        _git(self.nexlink, "add", ".")
        _git(self.nexlink, "commit", "-qm", "manifeste")
        report = packet_close.sync(self.db, self.nexlink, target="develop",
                                   actor="packet:develop")
        self.assertEqual([r["result"] for r in report["results"]], ["merged"], report)
        self.assertEqual(work.get(self.db, lot["id"])["migration_landed"], 378)
        self.assertEqual(self._slot(lot), {"number": 378, "status": "landed"})
        self.assertTrue(any("378_key.sql added by unit" in n for n in self._notes(lot)),
                        self._notes(lot))

    def _merge_unit(self, lot, branch, resolution):
        """Une unité `paquet-compose.py` : `git merge --no-ff` de la branche
        du lot, en conflit sur `seed.txt` ; la résolution, DANS le commit
        d'unité, renumérote (`resolution` : [(ancien, nouveau, contenu)])."""
        directory = NEXLINK.directory
        with open(os.path.join(self.nexlink, "seed.txt"), "w") as fh:
            fh.write("develop\n")
        _git(self.nexlink, "add", ".")
        _git(self.nexlink, "commit", "-qm", "seed develop")
        proc = subprocess.run(["git", "-C", self.nexlink, "merge", "--no-ff", "--no-commit",
                               branch], capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0, "le conflit attendu n'a pas eu lieu")
        with open(os.path.join(self.nexlink, "seed.txt"), "w") as fh:
            fh.write("résolu\n")
        for old, new, content in resolution:
            _git(self.nexlink, "mv", "%s/%s" % (directory, old), "%s/%s" % (directory, new))
            if content is not None:
                self._write(new, content)
        _git(self.nexlink, "add", ".")
        _git(self.nexlink, "commit", "-qm", "vehicule : #%d\n\nameesh-work: %d"
             % (lot["id"], lot["id"]))
        return self._head()

    def _lot_branch(self, name, content):
        _git(self.nexlink, "checkout", "-qb", "agent/m-%s" % name)
        with open(os.path.join(self.nexlink, "seed.txt"), "w") as fh:
            fh.write("lot\n")
        self._write(name, content)
        _git(self.nexlink, "add", ".")
        _git(self.nexlink, "commit", "-qm", name)
        _git(self.nexlink, "checkout", "-q", "develop")
        return "agent/m-%s" % name

    def _manifest(self, lot, unit):
        os.makedirs(os.path.join(self.nexlink, "reviews"), exist_ok=True)
        with open(os.path.join(self.nexlink, "reviews", "integration-paquet-9.md"),
                  "w") as fh:
            fh.write("# Paquet 9\n\n## Composition (1 unité)\n\n"
                     "| unité | lot | source | commit | note |\n|---|---|---|---|---|\n"
                     "| 1 | #%d | `%s` | `%s` | renumérotée |\n" % (lot["id"], unit, unit))
        _git(self.nexlink, "add", ".")
        _git(self.nexlink, "commit", "-qm", "manifeste")

    def test_sync_packets_sans_gel_unite_de_fusion_renumerotee(self):
        """#4100 (revue dev3) : l'unité réelle est un `merge --no-ff` dont la
        résolution de conflit renumérote 377 → 378 ; lot sans gel, fermé par
        `sync-packets` sur ce commit à deux parents : landed=378."""
        from ameesh import packet_close

        lot = self._lot()
        self._nx(lot)
        branch = self._lot_branch("377_key.sql", "create table k();")
        unit = self._merge_unit(lot, branch, [("377_key.sql", "378_key.sql", None)])
        self._manifest(lot, unit)
        report = packet_close.sync(self.db, self.nexlink, target="develop",
                                   actor="packet:develop")
        self.assertEqual([r["result"] for r in report["results"]], ["merged"], report)
        self.assertEqual(work.get(self.db, lot["id"])["migration_landed"], 378)
        self.assertEqual(self._slot(lot), {"number": 378, "status": "landed"})

    def test_unite_de_fusion_qui_renomme_et_reecrit(self):
        """#4100 (revue dev3) : la résolution change nom ET contenu ; ni le
        nom ni l'empreinte ne correspondent, l'unique fichier numéroté
        qu'ajoute l'unité (comparée à sa première parente) est le posé."""
        from ameesh import packet_close

        lot = self._lot()
        self._nx(lot)
        branch = self._lot_branch("377_key.sql", "create table k();")
        unit = self._merge_unit(lot, branch,
                                [("377_key.sql", "378_cle.sql", "create table k2();")])
        self._manifest(lot, unit)
        packet_close.sync(self.db, self.nexlink, target="develop", actor="packet:develop")
        self.assertEqual(work.get(self.db, lot["id"])["migration_landed"], 378)
        self.assertTrue(any("378_cle.sql added by unit" in n for n in self._notes(lot)),
                        self._notes(lot))

    def test_avance_rapide_puis_renumerotation(self):
        """#4100 (revue cx2) : la source S est sur la ligne de premier parent
        de la cible (avance rapide), puis R renomme 377 → 378 ; gel et preuve
        = S. La base calculée égale S : le fichier du lot est celui que la
        cible n'a plus, suivi par son nom (landed=378, pas de relâchement)."""
        lot = self._lot()
        self._nx(lot)
        self._write("377_key.sql", "create table k();")
        _git(self.nexlink, "add", ".")
        _git(self.nexlink, "commit", "-qm", "S")
        source = self._head()
        work.milestone(self.db, lot["id"], "frozen", sha=source, actor="ouvrier")
        _git(self.nexlink, "mv", NEXLINK.directory + "/377_key.sql",
             NEXLINK.directory + "/378_key.sql")
        _git(self.nexlink, "commit", "-qm", "R")
        work.close_merged(self.db, lot["id"], sha=source, actor="git:develop",
                          source="branch", repo=self.nexlink)
        self.assertEqual(work.get(self.db, lot["id"])["migration_landed"], 378)
        self.assertEqual(self._slot(lot), {"number": 378, "status": "landed"})
        # revue dev3 (b) : le fichier vient d'un commit ANTÉRIEUR de la
        # branche (S1) et la tête gelée S2 le porte seulement
        later = self._lot(n=3)
        self.assertEqual(self._nx(later)["migration"], 379)
        self._write("379_early.sql", "create table e();")
        _git(self.nexlink, "add", ".")
        _git(self.nexlink, "commit", "-qm", "S1")
        _commit(self.nexlink, "docs", "s2.md")
        frozen = self._head()
        work.milestone(self.db, later["id"], "frozen", sha=frozen, actor="ouvrier")
        _git(self.nexlink, "mv", NEXLINK.directory + "/379_early.sql",
             NEXLINK.directory + "/380_early.sql")
        _git(self.nexlink, "commit", "-qm", "R")
        work.close_merged(self.db, later["id"], sha=frozen, actor="git:develop",
                          source="branch", repo=self.nexlink)
        self.assertEqual(work.get(self.db, later["id"])["migration_landed"], 380)
        self.assertEqual(self._slot(later), {"number": 380, "status": "landed"})
        # le même chemin sans renumérotation : posé tel quel, rien à dire
        other = self._lot(n=2)
        self.assertEqual(self._nx(other)["migration"], 381)
        _commit(self.nexlink, NEXLINK.directory, "381_plain.sql")
        work.close_merged(self.db, other["id"], sha=self._head(), actor="git:develop",
                          source="branch", repo=self.nexlink)
        self.assertIsNone(work.get(self.db, other["id"])["migration_landed"])
        self.assertEqual(self._slot(other), {"number": 381, "status": "landed"})

    def test_la_reparation_d_un_lot_deja_fusionne_passe_par_le_meme_chemin(self):
        """#4100 : un lot fusionné avant le suivi (créneau atterri au numéro
        réservé) est réparé par `ameesh work merged <id> --sha S --repo R`,
        journalisé ; sans candidat unique, l'ancien numéro est relâché."""
        lot, other = self._lot(), self._lot(n=2)
        self._nx(lot)
        self._nx(other)
        self.assertEqual(self._nx(other)["migration"], 378)
        _commit(self.nexlink, NEXLINK.directory, "377_other.sql")
        sha = self._packet("377_key.sql", "380_key.sql")
        work.close_merged(self.db, lot["id"], sha=sha, actor="packet:develop",
                          source="packet manifest")   # sans clone : rien de suivi
        self.assertEqual(self._slot(lot), {"number": 377, "status": "landed"})
        sha2 = self._packet("378_x.sql", "381_x.sql", "382_x.sql")
        work.close_merged(self.db, other["id"], sha=sha2, actor="packet:develop",
                          source="packet manifest")
        proc = self.mesh("work", "merged", str(lot["id"]), "--sha", sha,
                         "--repo", self.nexlink, "--actor", "bureau")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("landed migration 380 recorded (manaty/nexlink) — reserved 377",
                      proc.stdout)
        self.assertEqual(work.get(self.db, lot["id"])["migration_landed"], 380)
        self.assertEqual(self._slot(lot), {"number": 380, "status": "landed"})
        done = work.merged(self.db, other["id"], sha=sha2, repo=self.nexlink, actor="bureau")
        self.assertEqual(done["result"], "landed", done)
        self.assertEqual(self._slot(other), {"number": 378, "status": "released"})
        self.assertTrue(any("migration 378 released (not landed: 378_x.sql absent from"
                            " develop, ambiguous matches 381, 382)" in n
                            for n in self._notes(other)), self._notes(other))
        # rejouée, la réparation ne réécrit rien
        again = work.merged(self.db, lot["id"], sha=sha, repo=self.nexlink, actor="bureau")
        self.assertEqual(again["result"], "already", again)

    def test_rattrapage_et_correction_d_un_lot_deja_fusionne(self):
        lot = self._lot()
        self._nx(lot)
        sha = "b" * 40
        self._merged(lot, actor="dev1")
        done = self._merged(lot, actor="dev2", migration_landed=452)
        self.assertEqual(done["result"], "landed")
        self.assertIn("landed migration 452 recorded (manaty/nexlink)", done["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["migration_landed"], 452)
        # idempotent : le même numéro ne réécrit rien
        again = self._merged(lot, actor="dev2", migration_landed=452)
        self.assertEqual(again["result"], "already")
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertEqual(sum("landed migration" in n for n in notes), 1, notes)
        # correction : l'ancien numéro est rendu, le nouveau est tenu
        fixed = self._merged(lot, actor="dev2", migration_landed=453)
        self.assertEqual(fixed["result"], "landed")
        self.assertIn("landed migration 452 → 453 corrected (manaty/nexlink)",
                      fixed["detail"])
        item = work.get(self.db, lot["id"])
        self.assertEqual(item["migration_landed"], 453)
        registry = storage.of(self.db).work
        self.assertIsNone(registry.migration_landed_holder(452, "manaty/nexlink"))
        self.assertEqual(registry.migration_landed_holder(453, "manaty/nexlink")["id"],
                         lot["id"])
        proc = self.mesh("work", "merged", str(lot["id"]), "--sha", sha,
                         "--migration-landed", "454")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("lot #%d already delivered:" % lot["id"], proc.stdout)
        self.assertEqual(work.get(self.db, lot["id"])["migration_landed"], 454)

    def test_le_sweep_concurrent_ne_perd_pas_le_numero_declare(self):
        lot = self._lot()
        self._nx(lot)
        close_merged = work.close_merged

        def sweep_then_close(db, item_id, **kwargs):
            close_merged(db, item_id, sha="a" * 40, actor="sweeper", source="git")
            return close_merged(db, item_id, **kwargs)

        with mock.patch.object(work, "close_merged", side_effect=sweep_then_close):
            done = self._merged(lot, actor="dev1", migration_landed=452)
        self.assertEqual(done["result"], "landed")
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["state"], item["migration_landed"],
                          item["migration_landed_repo"]),
                         ("delivered", 452, "manaty/nexlink"))
        self.assertTrue(any("landed migration 452 recorded" in e["note"]
                            for e in work.events(self.db, lot["id"])))

    def test_le_sweep_concurrent_avec_numero_deja_tenu_le_corrige(self):
        # #1605 : la fusion concurrente a déjà enregistré 452 ; notre
        # déclaration de 453 rencontre `already` et corrige le numéro tenu
        lot = self._lot()
        self._nx(lot)
        close_merged = work.close_merged

        def sweep_then_close(db, item_id, **kwargs):
            close_merged(db, item_id, sha="a" * 40, actor="sweeper", source="sweeper",
                         declared=True, landed=(452, "manaty/nexlink"))
            return close_merged(db, item_id, **kwargs)

        with mock.patch.object(work, "close_merged", side_effect=sweep_then_close):
            # #1286 : `merged` exige une preuve Git réelle, lue dans le dépôt
            # du lot ; le helper `_merged` la commit dans le clone nexlink, le
            # balayage concurrent restant simulé par la doublure ci-dessus.
            done = self._merged(lot, actor="dev1", migration_landed=453)
        self.assertEqual(done["result"], "landed")
        self.assertIn("landed migration 452 → 453 corrected (manaty/nexlink)",
                      done["detail"])
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["state"], item["migration_landed"],
                          item["migration_landed_repo"]),
                         ("delivered", 453, "manaty/nexlink"))
        registry = storage.of(self.db).work
        self.assertIsNone(registry.migration_landed_holder(452, "manaty/nexlink"))

    def test_deux_lots_ne_nomment_pas_le_meme_numero_atterri(self):
        first = self._lot(n=1)
        self._nx(first)
        self._merged(first, actor="dev1", migration_landed=452)
        second = self._lot(n=2)
        self._nx(second)
        with self.assertRaises(work.WorkError) as ctx:
            self._merged(second, actor="dev2", migration_landed=452)
        self.assertIn("lot #%d" % first["id"], str(ctx.exception))
        self.assertIn("manaty/nexlink", str(ctx.exception))
        # la déclaration refusée n'a rien fermé ni écrit
        item = work.get(self.db, second["id"])
        self.assertEqual((item["state"], item["migration_landed"]), ("intake", None))
        done = self._merged(second, actor="dev2", migration_landed=453)
        self.assertEqual(done["result"], "merged")

    def test_numero_hors_bornes_et_lot_ferme(self):
        lot = self._lot()
        self._nx(lot)
        for number in (0, 10000):
            with self.assertRaises(work.WorkError) as ctx:
                self._merged(lot, actor="dev1", migration_landed=number)
            self.assertIn("outside 0001-9999", str(ctx.exception))
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")
        work.close(self.db, lot["id"], abandoned=True, actor="dev1")
        with self.assertRaises(work.WorkError) as ctx:
            self._merged(lot, actor="dev1", migration_landed=452)
        self.assertIn("not reopened", str(ctx.exception))

    def test_le_numero_atterri_survit_a_la_liberation_de_la_reservation(self):
        """#956 (qa) libère `migration`/`migration_repo` à l'état terminal : le
        numéro atterri, lui, ne doit pas disparaître avec la réservation."""
        lot = self._lot()
        self._nx(lot)
        self._merged(lot, actor="dev1", migration_landed=452)
        self.db.execute("UPDATE work_items SET migration = NULL, migration_repo = NULL"
                        " WHERE id = %d" % lot["id"])
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["migration"], item["migration_repo"]), (None, None))
        self.assertEqual(
            work.migration_line(item),
            "452 landed in manaty/nexlink (L122) — file"
            " supabase/migrations/452_<name>.sql")
        proc = self.mesh("work", "show", str(lot["id"]))
        self.assertIn("migration: 452 landed in manaty/nexlink (L122)", proc.stdout)


if __name__ == "__main__":
    unittest.main()
