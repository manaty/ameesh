# SPDX-License-Identifier: AGPL-3.0-only
"""#3181 — un clone abîmé par une coupure de courant n'est pas une forge muette.

La coupure de pc-smichea (23/10, 15:20Z) a laissé des objets libres et une
ref de 0 octet dans le checkout des agents et dans le clone radar (qui lit les
objets du checkout par alternates). Chaque fetch échouait (« object file … is
empty », « bad object refs/… ») et `merge_queue.fetch_fresh` le rendait en
« try again once the remote answers » : la file différait les lots sans fin.

Attendu : `resolutions._run` met en quarantaine datée les objets libres et les
refs libres de 0 octet (jamais un fichier non vide, jamais de gc/prune), refait
le fetch une fois, et le refus restant nomme la corruption locale ; « remote »
reste réservé à une vraie erreur de la forge.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import merge_queue, resolutions


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _write(repo: str, path: str, text: str) -> None:
    with open(os.path.join(repo, path), "w", encoding="utf-8") as handle:
        handle.write(text)


def _commit(repo: str, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _loose(repo: str, sha: str) -> str:
    path = os.path.join(repo, ".git", "objects", sha[:2], sha[2:])
    assert os.path.isfile(path), "l'objet doit être libre pour ce témoin"
    os.chmod(path, 0o644)
    return path


class CorruptionReadingTest(unittest.TestCase):
    """La lecture de l'erreur : corruption locale ou forge injoignable."""

    def test_the_incident_messages_are_local_corruption(self):
        for message in (
                "error: object file .git/objects/61/99d0e78 is empty",
                "fatal: bad object refs/remotes/origin/integration/paquet-h-20261023-1700",
                "error: refs/remotes/origin/x: badRefContent",
                "fatal: loose object 6199d0e78 (stored in .git/objects/61/99) is corrupt",
                "error: origin did not send all necessary objects"):
            self.assertTrue(resolutions.local_corruption(message), message)
            self.assertTrue(resolutions.fetch_failure(message).startswith(
                "local clone corrupted (unclean shutdown?): "), message)

    def test_a_network_error_is_not_local_corruption(self):
        for message in ("ssh: Could not resolve hostname github.com",
                        "fatal: unable to access 'https://github.com/x/y/': "
                        "Connection timed out",
                        "fatal: '/nowhere' does not appear to be a git repository"):
            self.assertFalse(resolutions.local_corruption(message), message)
            self.assertEqual(resolutions.fetch_failure(message), message)

    def test_the_subcommand_skips_config_options(self):
        self.assertEqual(resolutions._subcommand(
            ("-c", "transfer.unpackLimit=1", "fetch", "--quiet")), "fetch")
        self.assertEqual(resolutions._subcommand(("ls-remote", "fetch")), "ls-remote")


class CorruptedCloneTest(unittest.TestCase):
    """Banc : forge nue, checkout des agents, clone radar `--shared`."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3181-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = os.path.join(self.tmp, "origin.git")
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", self.origin)
        self.agents = os.path.join(self.tmp, "agents")
        os.makedirs(self.agents)
        _git(self.agents, "init", "-q", "-b", "main")
        _git(self.agents, "config", "user.name", "banc")
        _git(self.agents, "config", "user.email", "banc@example.invalid")
        _write(self.agents, "shared.py", "one\ntwo\n")
        _commit(self.agents, "base")
        _git(self.agents, "remote", "add", "origin", self.origin)
        _git(self.agents, "push", "-q", "-u", "origin", "main")
        self.clone = resolutions.Replayer(self.agents, os.path.join(self.tmp, "radar"))
        merge_queue.fetch_fresh(self.agents, ["main"], self.clone)

    def publish(self, branch: str) -> str:
        """Une tête nouvelle, écrite en objets libres chez les agents."""
        _git(self.agents, "switch", "-q", "-c", branch, "main")
        _write(self.agents, "shared.py", "FOUR\ntwo\n")
        tip = _commit(self.agents, "lot")
        _git(self.agents, "push", "-q", "origin", branch)
        _git(self.agents, "switch", "-q", "main")
        return tip

    def quarantined(self, git_dir: str) -> list[str]:
        root = os.path.join(git_dir, resolutions.QUARANTINE_DIR)
        found = []
        for base, _dirs, files in os.walk(root):
            found += [os.path.relpath(os.path.join(base, f), root) for f in files]
        return sorted(found)

    def test_empty_object_and_ref_are_quarantined_and_the_fetch_passes(self):
        tip = self.publish("lot/a")
        # la coupure : la tête est un fichier vide chez les agents (alternate
        # du clone), et une ref du clone est vide
        obj = _loose(self.agents, tip)
        open(obj, "w").close()
        ref = os.path.join(self.clone.path, ".git", "refs", "remotes", "origin", "coupure")
        os.makedirs(os.path.dirname(ref), exist_ok=True)
        open(ref, "w").close()
        # sans réparation, le fetch du clone échoue sur la corruption locale
        proc = subprocess.run(["git", "-C", self.clone.path, "fetch", "--quiet", self.origin,
                               "+refs/heads/lot/a:refs/banc/a"], capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(resolutions.local_corruption(proc.stderr), proc.stderr)

        tips, _where = merge_queue.fetch_fresh(self.agents, ["lot/a", "main"], self.clone,
                                               base="main")
        self.assertEqual(tips["lot/a"], tip)
        self.assertFalse(os.path.exists(obj))
        self.assertFalse(os.path.exists(ref))
        agents_q = self.quarantined(os.path.join(self.agents, ".git"))
        clone_q = self.quarantined(os.path.join(self.clone.path, ".git"))
        self.assertTrue(any(p.endswith("objects/%s/%s" % (tip[:2], tip[2:]))
                            for p in agents_q), agents_q)
        self.assertTrue(any(p.endswith("refs/remotes/origin/coupure") for p in clone_q),
                        clone_q)
        # chaque déplacement est journalisé dans la quarantaine
        self.assertTrue(any(p.endswith("moved.log") for p in agents_q + clone_q))

    def test_a_non_empty_corrupt_object_is_refused_not_moved(self):
        tip = self.publish("lot/b")
        obj = _loose(self.agents, tip)
        with open(obj, "wb") as handle:
            handle.write(b"pas du zlib")
        with self.assertRaises(merge_queue.QueueError) as caught:
            merge_queue.fetch_fresh(self.agents, ["lot/b"], self.clone)
        message = str(caught.exception)
        self.assertIn("local clone corrupted (unclean shutdown?)", message)
        self.assertNotIn("remote answers", message)
        with open(obj, "rb") as handle:
            self.assertEqual(handle.read(), b"pas du zlib")
        self.assertEqual(self.quarantined(os.path.join(self.agents, ".git")), [])

    def test_quarantine_never_moves_a_non_empty_file(self):
        tip = self.publish("lot/c")
        _loose(self.agents, tip)
        self.assertEqual(resolutions.quarantine_empty(self.clone.path), [])
        self.assertTrue(os.path.isfile(os.path.join(
            self.agents, ".git", "objects", tip[:2], tip[2:])))

    def empty_ref(self, name: str) -> str:
        ref = os.path.join(self.clone.path, ".git", "refs", "remotes", "origin", name)
        os.makedirs(os.path.dirname(ref), exist_ok=True)
        open(ref, "w").close()
        return ref

    def test_a_ref_made_valid_after_discovery_stays_in_place(self):
        # B1 (relecture) : la ref est vue vide au scan, puis `update-ref`
        # l'écrit avant le déplacement ; relue sous son verrou, elle reste.
        ref = self.empty_ref("course")
        main = _git(self.agents, "rev-parse", "main")
        real = resolutions._empty_files

        def scan_then_write(objects, git_dir):
            found = real(objects, git_dir)
            if ref in found:
                # l'écriture de Git : `<ref>.lock` puis renommage (update-ref
                # refuse une ref vide, le banc écrit comme lui)
                with open(ref + ".lock", "w", encoding="utf-8") as handle:
                    handle.write(main + "\n")
                os.rename(ref + ".lock", ref)
            return found
        with mock.patch.object(resolutions, "_empty_files", side_effect=scan_then_write):
            self.assertEqual(resolutions.quarantine_empty(self.clone.path), [])
        with open(ref, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), main)
        self.assertFalse(os.path.exists(ref + ".lock"))

    def test_a_ref_locked_by_a_git_writer_is_not_moved(self):
        ref = self.empty_ref("verrou")
        open(ref + ".lock", "w").close()
        self.assertEqual(resolutions.quarantine_empty(self.clone.path), [])
        self.assertTrue(os.path.exists(ref))
        self.assertTrue(os.path.exists(ref + ".lock"))  # le verrou d'autrui reste

    def test_an_object_made_valid_after_discovery_stays_in_place(self):
        tip = self.publish("lot/d")
        obj = _loose(self.agents, tip)
        with open(obj, "rb") as handle:
            valid = handle.read()
        open(obj, "w").close()
        real = resolutions._empty_files

        def scan_then_write(objects, git_dir):
            found = real(objects, git_dir)
            if obj in found:
                with open(obj, "wb") as handle:
                    handle.write(valid)
            return found
        with mock.patch.object(resolutions, "_empty_files", side_effect=scan_then_write):
            self.assertEqual(resolutions.quarantine_empty(self.clone.path), [])
        with open(obj, "rb") as handle:
            self.assertEqual(handle.read(), valid)

    def test_an_object_replaced_during_the_move_is_restored(self):
        # l'écrivain (`core.createObject=rename`) remplace l'objet vide entre
        # la relecture et le déplacement : l'objet complet revient à son chemin
        tip = self.publish("lot/e")
        obj = _loose(self.agents, tip)
        with open(obj, "rb") as handle:
            valid = handle.read()
        open(obj, "w").close()
        real_rename = os.rename

        def replace_then_rename(source, target):
            if source == obj:
                fresh = obj + ".tmp_obj_banc"
                with open(fresh, "wb") as handle:
                    handle.write(valid)
                real_rename(fresh, obj)
            return real_rename(source, target)
        with mock.patch("os.rename", side_effect=replace_then_rename):
            self.assertEqual(resolutions.quarantine_empty(self.clone.path), [])
        with open(obj, "rb") as handle:
            self.assertEqual(handle.read(), valid)
        _git(self.agents, "cat-file", "-e", tip)

    def test_two_quarantines_in_the_same_second_keep_both_proofs(self):
        first = self.empty_ref("une")
        with mock.patch.object(resolutions.time, "strftime", return_value="20261023T152000Z"):
            self.assertEqual(resolutions.quarantine_empty(self.clone.path), [first])
            self.empty_ref("une")
            self.assertEqual(resolutions.quarantine_empty(self.clone.path), [first])
        proofs = self.quarantined(os.path.join(self.clone.path, ".git"))
        self.assertEqual(len([p for p in proofs if p.endswith("refs/remotes/origin/une")]), 2,
                         proofs)
        self.assertEqual(len([p for p in proofs if p.endswith("moved.log")]), 2, proofs)

    def test_an_unreachable_forge_still_says_remote(self):
        _git(self.agents, "remote", "set-url", "origin", os.path.join(self.tmp, "absent.git"))
        with self.assertRaises(merge_queue.QueueError) as caught:
            merge_queue.fetch_fresh(self.agents, ["main"], self.clone)
        message = str(caught.exception)
        self.assertIn("try again once the remote answers", message)
        self.assertNotIn("corrupted", message)


if __name__ == "__main__":
    unittest.main()
