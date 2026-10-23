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

import errno
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

    def test_git_never_rewrites_a_present_empty_object(self):
        # la preuve de sûreté de `_move_object` contre le Git installé : un
        # objet présent, même vide, n'est jamais réécrit, quel que soit
        # `core.createObject` (relectures cx2/deepseek, gels #16631)
        tmp = tempfile.mkdtemp(prefix="ameesh-3181-git-")
        self.addCleanup(shutil.rmtree, tmp, True)
        _git(tmp, "init", "-q")
        sha = subprocess.run(["git", "-C", tmp, "hash-object", "--stdin"], input=b"banc\n",
                             capture_output=True, check=True).stdout.decode().strip()
        path = os.path.join(tmp, ".git", "objects", sha[:2], sha[2:])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
        for mode in ("rename", "link"):
            subprocess.run(["git", "-C", tmp, "-c", "core.createObject=" + mode,
                            "hash-object", "-w", "--stdin"], input=b"banc\n",
                           capture_output=True)
            self.assertEqual(os.path.getsize(path), 0, mode)

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

    def test_a_second_pass_waits_and_git_rewrite_is_never_carried_away(self):
        # B1 résiduel (cx2) : A voit l'objet vide ; une passe B le met en
        # quarantaine ; Git (réglage par défaut) réécrit l'objet complet ; A
        # emportait l'objet valide. Le verrou du dépôt tient B hors de la
        # fenêtre, et Git par défaut ne remplace pas le fichier vide présent.
        tip = self.publish("lot/f")
        raw = subprocess.run(["git", "-C", self.agents, "cat-file", "commit", tip],
                             capture_output=True, check=True).stdout
        obj = _loose(self.agents, tip)
        open(obj, "w").close()
        real_rename = os.rename
        seen = {}

        def race_then_rename(source, target):
            # entre la relecture `lstat` de A et son `rename` : B, puis Git
            if source == obj and "b" not in seen:
                seen["b"] = None
                with mock.patch.object(resolutions, "QUARANTINE_LOCK_WAIT_S", 0.2):
                    seen["b"] = resolutions.quarantine_empty(self.agents)
                subprocess.run(["git", "-C", self.agents, "hash-object", "-t", "commit",
                                "-w", "--stdin"], input=raw, capture_output=True)
            return real_rename(source, target)
        with mock.patch("os.rename", side_effect=race_then_rename):
            moved = resolutions.quarantine_empty(self.clone.path)
        self.assertEqual(seen["b"], [])  # B n'a pas eu le dépôt
        self.assertEqual(moved, [obj])
        # tout ce qui est parti en quarantaine est vide : aucun objet valide
        root = os.path.join(self.agents, ".git", resolutions.QUARANTINE_DIR)
        for base, _dirs, files in os.walk(root):
            for name in files:
                if name != "moved.log":
                    self.assertEqual(os.path.getsize(os.path.join(base, name)), 0, name)
        # l'objet se réécrit ensuite normalement
        subprocess.run(["git", "-C", self.agents, "hash-object", "-t", "commit", "-w",
                        "--stdin"], input=raw, capture_output=True, check=True)
        _git(self.agents, "cat-file", "-e", tip)

    def test_a_replacing_git_writer_refuses_the_object_repair(self):
        tip = self.publish("lot/g")
        obj = _loose(self.agents, tip)
        open(obj, "w").close()
        _git(self.agents, "config", "core.createObject", "rename")
        self.assertEqual(resolutions.quarantine_empty(self.clone.path), [])
        self.assertTrue(os.path.exists(obj))

    def test_a_linked_worktree_writing_by_rename_refuses_the_object_repair(self):
        # relecture cx2 (gel #16631) : `config.worktree` d'un worktree lié,
        # invisible depuis le dépôt commun, partage pourtant les objets
        tip = self.publish("lot/w")
        obj = _loose(self.agents, tip)
        open(obj, "w").close()
        linked = os.path.join(self.tmp, "lie")
        _git(self.agents, "worktree", "add", "-q", "--detach", linked, "main")
        _git(self.agents, "config", "extensions.worktreeConfig", "true")
        _git(linked, "config", "--worktree", "core.createObject", "rename")
        # le dépôt commun seul ne le voit pas
        self.assertEqual(subprocess.run(["git", "-C", self.agents, "config", "--get",
                                         "core.createObject"]).returncode, 1)
        self.assertTrue(resolutions._replacing_writer(os.path.join(self.agents, ".git")))
        self.assertEqual(resolutions.quarantine_empty(self.clone.path), [])
        self.assertTrue(os.path.exists(obj))

    def test_a_failed_restore_names_where_the_valid_object_is(self):
        tip = self.publish("lot/h")
        obj = _loose(self.agents, tip)
        with open(obj, "rb") as handle:
            valid = handle.read()
        open(obj, "w").close()
        real_rename = os.rename

        def replace_then_rename(source, target):
            if source == obj:
                with open(obj + ".tmp_obj_banc", "wb") as handle:
                    handle.write(valid)
                real_rename(obj + ".tmp_obj_banc", obj)
            return real_rename(source, target)
        with mock.patch("os.rename", side_effect=replace_then_rename), \
                mock.patch("os.link", side_effect=OSError(errno.EIO, "EIO banc")), \
                mock.patch("sys.stderr") as err:
            self.assertEqual(resolutions.quarantine_empty(self.clone.path), [])
        said = "".join(str(c.args[0]) for c in err.write.call_args_list if c.args)
        self.assertIn("NOT restored", said)
        kept = [p for p in self.quarantined(os.path.join(self.agents, ".git"))
                if p.endswith("objects/%s/%s" % (tip[:2], tip[2:]))]
        self.assertEqual(len(kept), 1, kept)
        root = os.path.join(self.agents, ".git", resolutions.QUARANTINE_DIR)
        with open(os.path.join(root, kept[0]), "rb") as handle:
            self.assertEqual(handle.read(), valid)  # rien n'est perdu

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
