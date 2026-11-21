# SPDX-License-Identifier: AGPL-3.0-only
"""#7978 — le clone radar possède ses objets, il n'emprunte plus le checkout.

`git clone --shared` laissait `objects/info/alternates` pointer sur le dépôt
d'un agent : le gc de ce dépôt élaguait un objet que seules les refs du radar
référencent (`refs/ameesh/candidates/remise/…`, `refs/ameesh/resolutions/…`)
et le clone devenait illisible (« bad object »), jusqu'à la panne #7969.
`Replayer.ensure` et le premier lecteur (`fetch_fresh`) copient les objets
dans le clone (`repack -a -d`) puis retirent l'alternate ; la migration d'un
clone existant garde les refs, en quarantaine datée si leur objet manque
déjà.

Témoins : la source élague après migration sans casser le clone ; un clone
neuf est autonome ; une ref morte et un reflog d'objet élagué ne bloquent pas
la migration ; le premier `fetch_fresh` migre un clone hérité ; une ref
réparée entre la lecture de sa valeur et sa suppression (libre ou
empaquetée) survit au compare-and-swap (#44886).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import merge_queue, plan_git, resolutions


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _branch(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def _write(repo: str, path: str, text: str) -> None:
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(text)


def _commit(repo: str, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


class AutonomousRadarCloneTest(unittest.TestCase):
    """Forge nue, checkout des agents, clone radar hérité de `--shared`."""

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ameesh-7978-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.forge = os.path.join(self.tmp, "forge.git")
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", self.forge)
        self.agents = os.path.join(self.tmp, "agents")
        os.makedirs(self.agents)
        _git(self.agents, "init", "-q", "-b", "main")
        _git(self.agents, "config", "user.name", "banc")
        _git(self.agents, "config", "user.email", "banc@example.invalid")
        _write(self.agents, "shared.py", "one\ntwo\n")
        self.base = _commit(self.agents, "base")
        _git(self.agents, "remote", "add", "origin", self.forge)
        _git(self.agents, "push", "-q", "-u", "origin", "main")
        self.clone = os.path.join(self.tmp, "radar", "ameesh.clone")

    def replayer(self) -> resolutions.Replayer:
        return resolutions.Replayer(self.agents, self.clone, target="main")

    def alternates(self) -> str:
        return os.path.join(self.clone, ".git", "objects", "info", "alternates")

    def old_shared_clone(self) -> None:
        """Le clone que le code d'avant #7978 laissait sur chaque hôte."""
        os.makedirs(os.path.dirname(self.clone), exist_ok=True)
        _git(os.path.dirname(self.clone), "clone", "-q", "--shared", "--no-checkout",
             self.agents, self.clone)
        _git(self.clone, "checkout", "-q", "-f", "--detach", self.base)

    def radar_only_commit(self) -> str:
        """Un commit qui n'existe que dans le checkout (jamais poussé)."""
        _git(self.agents, "switch", "-q", "-c", "radar-only", "main")
        _write(self.agents, "radar-only.txt", "candidat\n")
        tip = _commit(self.agents, "radar only")
        _git(self.agents, "switch", "-q", "main")
        return tip

    def prune_source(self) -> None:
        """Le gc du checkout, exactement l'incident du 20/11."""
        _git(self.agents, "branch", "-D", "radar-only")
        _git(self.agents, "reflog", "expire", "--expire=now", "--all")
        _git(self.agents, "gc", "--prune=now", "-q")

    def assert_autonomous(self) -> None:
        self.assertFalse(os.path.exists(self.alternates()),
                         "le clone lit encore les objets du checkout")

    def test_a_migrated_clone_survives_the_source_gc(self) -> None:
        """L'incident #7969 : la ref de remise du radar seule référence
        l'objet, puis la source élague ; le clone reste lisible."""
        tip = self.radar_only_commit()
        self.old_shared_clone()
        _git(self.clone, "update-ref",
             "refs/ameesh/candidates/remise/3885/main-fb029a526", tip)
        self.assertTrue(os.path.exists(self.alternates()))

        self.replayer().ensure()

        self.assert_autonomous()
        self.prune_source()
        # la ref du radar et son objet ont survécu au gc de la source
        self.assertEqual(plan_git._commit(self.clone,
                                          "refs/ameesh/candidates/remise/3885/"
                                          "main-fb029a526"), tip)
        self.assertIsNotNone(plan_git._commit(self.clone, tip))
        fsck = _branch(self.clone, "fsck", "--full")
        self.assertEqual(fsck.returncode, 0, fsck.stderr)
        # le fetch exact du packet sweep, qui échouait « bad object »
        tips, where = merge_queue.fetch_fresh(self.agents, ["main"], self.replayer(),
                                              base="main")
        self.assertEqual(where, self.clone)
        self.assertEqual(tips["main"], self.base)

    def test_a_new_clone_is_autonomous(self) -> None:
        self.replayer().ensure()
        self.assert_autonomous()
        # un commit publié après la création est récupéré localement
        _git(self.agents, "switch", "-q", "main")
        _write(self.agents, "later.txt", "plus tard\n")
        later = _commit(self.agents, "later")
        _git(self.agents, "push", "-q", "origin", "main")
        tips, _where = merge_queue.fetch_fresh(self.agents, ["main"], self.replayer(),
                                               base="main")
        self.assertEqual(tips["main"], later)
        self.assertIsNotNone(plan_git._commit(self.clone, later))

    def test_the_first_reader_migrates_an_existing_clone(self) -> None:
        """`fetch_fresh` lit un clone existant sans `ensure` : il migre avant
        son fetch (le packet sweep ne doit pas attendre un passage radar)."""
        self.old_shared_clone()
        self.assertTrue(os.path.exists(self.alternates()))
        tips, _where = merge_queue.fetch_fresh(self.agents, ["main"], self.replayer(),
                                               base="main")
        self.assertEqual(tips["main"], self.base)
        self.assert_autonomous()

    def test_a_ref_already_robbed_of_its_object_is_quarantined(self) -> None:
        """La garde #7969 traite le symptôme au fetch ; la migration doit
        pouvoir passer sur un clone déjà cassé, sans perdre la ref."""
        self.old_shared_clone()
        missing = "b" * 40
        ref = os.path.join(self.clone, ".git", "refs", "ameesh", "resolutions",
                           "3885", "main-dead")
        os.makedirs(os.path.dirname(ref), exist_ok=True)
        with open(ref, "w", encoding="utf-8") as handle:
            handle.write(missing + "\n")

        self.replayer().ensure()

        self.assert_autonomous()
        self.assertFalse(os.path.exists(ref))
        found = []
        root = os.path.join(self.clone, ".git", resolutions.QUARANTINE_DIR)
        for base, _dirs, files in os.walk(root):
            found += [os.path.relpath(os.path.join(base, name), root) for name in files]
        self.assertTrue(any(path.endswith("refs/ameesh/resolutions/3885/main-dead")
                            for path in found), found)
        logs = [os.path.join(root, path) for path in found if path.endswith("moved.log")]
        self.assertEqual(len(logs), 1, found)
        with open(logs[0], encoding="utf-8") as handle:
            self.assertIn("refs/ameesh/resolutions/3885/main-dead", handle.read())
        fsck = _branch(self.clone, "fsck", "--connectivity-only", "--no-dangling")
        self.assertEqual(fsck.returncode, 0, fsck.stderr)

    def test_a_free_ref_repaired_during_the_probe_survives(self) -> None:
        """B #44886 : entre la lecture de la valeur morte et sa suppression,
        un fetch peut réparer la ref ; le compare-and-swap doit alors la
        laisser en place (une suppression aveugle effacerait la ref neuve)."""
        self.old_shared_clone()
        name = "refs/ameesh/resolutions/3885/main-repaired"
        dead = "b" * 40
        ref = os.path.join(self.clone, ".git", *name.split("/"))
        os.makedirs(os.path.dirname(ref), exist_ok=True)
        with open(ref, "w", encoding="utf-8") as handle:
            handle.write(dead + "\n")
        real_run = resolutions._run

        def repair(cwd, *args, **kwargs):
            if args[:3] == ("cat-file", "-e", dead):
                _git(self.clone, "update-ref", name, self.base)
            return real_run(cwd, *args, **kwargs)

        with mock.patch.object(resolutions, "_run", side_effect=repair):
            moved = resolutions._quarantine_missing_refs_7978(
                self.clone, "bad object " + name)
        self.assertEqual(moved, [])
        self.assertEqual(_git(self.clone, "rev-parse", name), self.base)

    def test_a_packed_ref_repaired_during_the_probe_survives(self) -> None:
        """Même course sur une ref empaquetée : la valeur lue dans
        `packed-refs` est morte, un fetch écrit la ref libre réparée ; la
        suppression avec l'ancienne valeur échoue et ne touche à rien."""
        self.old_shared_clone()
        name = "refs/ameesh/resolutions/3885/main-packed"
        dead = "b" * 40
        pack = os.path.join(self.clone, ".git", "packed-refs")
        with open(pack, "a", encoding="utf-8") as handle:
            if os.path.getsize(pack) == 0:
                handle.write("# pack-refs with: peeled fully-peeled sorted \n")
            handle.write(dead + " " + name + "\n")
        real_run = resolutions._run

        def repair(cwd, *args, **kwargs):
            if args[:3] == ("cat-file", "-e", dead):
                _git(self.clone, "update-ref", name, self.base)
            return real_run(cwd, *args, **kwargs)

        with mock.patch.object(resolutions, "_run", side_effect=repair):
            moved = resolutions._quarantine_missing_refs_7978(
                self.clone, "bad object " + name)
        self.assertEqual(moved, [])
        self.assertEqual(_git(self.clone, "rev-parse", name), self.base)

    def test_a_reflog_of_a_pruned_object_does_not_block_the_migration(self) -> None:
        """Le clone nexlink du 20/11 : un reflog nomme un commit dont un
        sous-arbre a été élagué ; `repack -a` le lit et échouerait sur un
        arbre dont aucune ref n'a besoin."""
        _git(self.agents, "switch", "-q", "-c", "side", "main")
        _write(self.agents, "sub/x.txt", "x\n")
        side = _commit(self.agents, "side")
        sub = _git(self.agents, "rev-parse", side + ":sub")
        _git(self.agents, "switch", "-q", "main")
        _git(self.agents, "branch", "-D", "side")
        # l'objet du sous-arbre n'existe plus que dans l'alternate
        os.unlink(os.path.join(self.agents, ".git", "objects", sub[:2], sub[2:]))
        self.old_shared_clone()
        # le clone suit déjà la forge : `track_forge` ne retire pas ses refs
        _git(self.clone, "remote", "set-url", "origin", self.forge)
        _git(self.clone, "config", "--replace-all", "remote.origin.fetch",
             resolutions.FORGE_REFSPEC)
        ref = os.path.join(self.clone, ".git", "refs", "remotes", "origin",
                           "review", "old")
        log = os.path.join(self.clone, ".git", "logs", "refs", "remotes", "origin",
                           "review", "old")
        os.makedirs(os.path.dirname(ref), exist_ok=True)
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(ref, "w", encoding="utf-8") as handle:
            handle.write(self.base + "\n")
        with open(log, "w", encoding="utf-8") as handle:
            handle.write("0" * 40 + " " + side + " banc <banc@example.invalid> "
                         "1795214698 +0000\tfetch: storing head\n")
        # sans l'expiration des reflogs, le repack échoue sur cet arbre
        probe = _branch(self.clone, "repack", "-a", "-d", "-q")
        self.assertNotEqual(probe.returncode, 0)
        self.assertIn("bad tree object", probe.stderr)

        self.replayer().ensure()

        self.assert_autonomous()
        fsck = _branch(self.clone, "fsck", "--connectivity-only", "--no-dangling")
        self.assertEqual(fsck.returncode, 0, fsck.stderr)


if __name__ == "__main__":
    unittest.main()
