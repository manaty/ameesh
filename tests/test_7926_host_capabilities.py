# SPDX-License-Identifier: AGPL-3.0-only
"""#7926 : capacité `android-emulator` (groupe kvm) déclarée, appliquée, vérifiée.

La fiche `Host` déclare ses capacités (`capabilities: [android-emulator]`).
Le provisioning n'accorde que les groupes de ces capacités, `ameesh doctor`
compare la déclaration à l'état réel dans les deux sens, le placement et
`ameesh agent observe` lisent la déclaration. Rien n'est deviné : ni chmod,
ni ACL, ni règle udev ; une capacité inconnue n'accorde rien.
"""
from __future__ import annotations

import os
import stat as stat_mod
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from ameesh import agent_observation, canon, host_capabilities, placement

from .support import REPO
from .test_canon import (_TmpMixin, codes, commit_all, fiche, git, publish,
                         write)

HOSTE = """---
type: Host
title: %s
responsible: human:alice
%s---
"""

INSTALL = os.path.join(REPO, "deploy", "guest", "install.sh")


def group(name: str, gid: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(gr_name=name, gr_gid=gid)


def passwd(name: str, gid: int = 1000, uid: int = 1000) -> types.SimpleNamespace:
    return types.SimpleNamespace(pw_name=name, pw_gid=gid, pw_uid=uid)


# ==========================================================================
# lecture de la déclaration
# ==========================================================================

class ParseTest(unittest.TestCase):
    def test_absent_declares_nothing(self):
        self.assertEqual(host_capabilities.parse(None), ([], [], []))

    def test_a_scalar_is_one_capability(self):
        self.assertEqual(host_capabilities.parse("android-emulator"),
                         (["android-emulator"], [], []))

    def test_duplicates_are_read_once_and_unknown_are_separate(self):
        known, problems, unknown = host_capabilities.parse(
            ["android-emulator", "android-emulator", "gpu", ""])
        self.assertEqual(known, ["android-emulator"])
        self.assertEqual(problems, ["`capabilities`: empty capability name"])
        self.assertEqual(unknown, ["gpu"])

    def test_a_malformed_value_declares_nothing(self):
        for bad in ({"kvm": "group"}, True, [["kvm"]], [False], object()):
            with self.subTest(bad=bad):
                known, problems, unknown = host_capabilities.parse(bad)
                self.assertEqual((known, unknown), ([], []))
                self.assertTrue(problems, bad)

    def test_groups_of_known_capabilities_only(self):
        self.assertEqual(host_capabilities.groups_of(["android-emulator"]), ["kvm"])
        self.assertEqual(host_capabilities.groups_of(["gpu", "android-emulator"]),
                         ["kvm"])
        self.assertEqual(host_capabilities.groups_of(None), [])
        self.assertEqual(host_capabilities.groups_of(["android-emulator",
                                                      "android-emulator"]), ["kvm"])


class CanonCardTest(_TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.make_tmp()

    def loaded(self, body: str):
        write(self.root, "hotes/ag3.md", HOSTE % ("ameesh-agents-3", body))
        return canon.load(self.root, untrusted=True)

    def test_a_declared_capability_is_parsed_and_serialised(self):
        loaded = self.loaded("capabilities: [android-emulator]\n")
        self.assertEqual(loaded.hosts[0].capabilities, ["android-emulator"])
        self.assertEqual(loaded.to_dict()["hosts"][0]["capabilities"],
                         ["android-emulator"])
        self.assertNotIn("host-capabilities-invalid", codes(canon.validate(loaded)))

    def test_an_undeclared_host_has_no_capability(self):
        self.assertIsNone(self.loaded("").hosts[0].capabilities)

    def test_an_unknown_capability_is_a_warning_and_grants_nothing(self):
        loaded = self.loaded("capabilities: [gpu]\n")
        self.assertEqual(loaded.hosts[0].capabilities, [])
        findings = canon.validate(loaded)
        self.assertIn("host-capability-unknown", codes(findings, canon.WARNING))
        self.assertNotIn("host-capability-unknown", codes(findings, canon.ERROR))

    def test_a_malformed_field_is_an_error(self):
        loaded = self.loaded("capabilities:\n  kvm: group\n")
        self.assertIsNone(loaded.hosts[0].capabilities)
        self.assertIn("host-capabilities-invalid", codes(canon.validate(loaded)))


# ==========================================================================
# état réel : doctor, dans les deux sens
# ==========================================================================

class FakeHost:
    """Un hôte simulé : comptes, groupes et /dev/kvm injectés."""

    def __init__(self, *, user: str = "ameesh", groups=("kvm",),
                 memberships=("kvm",), device=True, mode=0o660, gid=993,
                 uid=4242, process_groups=("kvm",)):
        self.user = user
        self.uid = uid
        self.groups = {name: group(name, 900 + index)
                       for index, name in enumerate(groups)}
        self.memberships = list(memberships)
        self.process_groups = set(process_groups)
        self.device = device
        self.mode = mode
        self.gid = gid

    def pwnam(self, name: str):
        if name != self.user:
            raise KeyError(name)
        return passwd(name, uid=self.uid)

    def grnam(self, name: str):
        if name not in self.groups:
            raise KeyError(name)
        return self.groups[name]

    def grgid(self, gid: int):
        for value in self.groups.values():
            if value.gr_gid == gid:
                return value
        raise KeyError(gid)

    def grouplist(self, name: str, gid: int):
        return [gid] + [self.groups[group].gr_gid for group in self.memberships
                        if group in self.groups]

    def stat(self, path: str):
        if not self.device:
            raise FileNotFoundError(path)
        return types.SimpleNamespace(
            st_mode=(stat_mod.S_IFCHR
                     if self.device is True or self.device == "char"
                     else stat_mod.S_IFREG) | self.mode,
            st_gid=self.gid)

    def probe(self, capabilities, **kwargs):
        kwargs.setdefault("euid", self.uid)
        kwargs.setdefault("process_groups", self.process_groups)
        return host_capabilities.probe(
            capabilities, pwnam=self.pwnam, grnam=self.grnam,
            grgid=self.grgid, grouplist=self.grouplist, stat=self.stat, **kwargs)


class ProbeTest(unittest.TestCase):
    def test_a_declared_capability_satisfied_is_ok(self):
        result = FakeHost().probe(["android-emulator"], user="ameesh")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["groups"], ["kvm"])
        self.assertEqual(result["missing_groups"], [])
        self.assertEqual(result["problems"], [])
        self.assertTrue(result["device"]["character"])
        self.assertEqual(result["device"]["mode"], 0o660)

    def test_the_user_outside_the_group_is_a_problem(self):
        result = FakeHost(memberships=[]).probe(["android-emulator"], user="ameesh")
        self.assertFalse(result["ok"])
        self.assertEqual(result["missing_groups"], ["kvm"])
        self.assertIn("not in group kvm", " ".join(result["problems"]))

    def test_an_absent_group_is_a_problem(self):
        result = FakeHost(groups=()).probe(["android-emulator"], user="ameesh")
        self.assertFalse(result["ok"])
        self.assertEqual(result["group_missing"], ["kvm"])
        self.assertIn("group kvm does not exist", " ".join(result["problems"]))

    def test_an_absent_user_is_a_problem(self):
        result = FakeHost(user="other").probe(["android-emulator"], user="ameesh")
        self.assertFalse(result["ok"])
        self.assertTrue(result["user_missing"])
        self.assertIn("user ameesh does not exist", " ".join(result["problems"]))

    def test_kvm_missing_or_not_a_character_device_is_a_problem(self):
        for host in (FakeHost(device=False), FakeHost(device="file")):
            with self.subTest(host=host.device):
                result = host.probe(["android-emulator"], user="ameesh")
                self.assertFalse(result["ok"])
                text = " ".join(result["problems"])
                self.assertTrue("/dev/kvm is missing" in text
                                or "not a character device" in text, text)

    def test_a_group_granted_without_a_declaration_is_a_warning_only(self):
        # #7927 n'est pas livré : AG3 porte kvm sans le déclarer. Le doctor
        # doit le dire sans rendre l'hôte rouge.
        result = FakeHost().probe([], user="ameesh")
        self.assertTrue(result["ok"], result)
        self.assertIn("granted to ameesh but no capability is declared",
                      " ".join(result["warnings"]))

    def test_the_account_group_not_yet_in_the_process_needs_a_restart(self):
        # usermod a accordé kvm, mais l'exécuteur a été lancé avant : le
        # docteur ne doit pas se dire vert sans le dire (N2 de #7926).
        result = FakeHost(process_groups=()).probe(["android-emulator"],
                                                   user="ameesh")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["process_missing"], ["kvm"])
        self.assertIn("restart it (ameesh-runner-agent@", " ".join(result["warnings"]))

    def test_a_restarted_process_carries_no_restart_warning(self):
        result = FakeHost(process_groups=("kvm",)).probe(["android-emulator"],
                                                         user="ameesh")
        self.assertEqual(result["process_missing"], [])
        self.assertNotIn("restart", " ".join(result["warnings"]))

    def test_another_process_uid_does_not_speak_for_the_executors(self):
        # root (ou un autre compte) ne peut pas conclure sur les groupes des
        # exécuteurs : pas d'avertissement de redémarrage depuis son processus.
        result = FakeHost(process_groups=()).probe(["android-emulator"],
                                                   user="ameesh", euid=0)
        self.assertEqual(result["process_missing"], [])
        self.assertNotIn("restart", " ".join(result["warnings"]))

    def test_a_world_writable_device_is_a_warning_not_a_ko(self):
        result = FakeHost(mode=0o666).probe(["android-emulator"], user="ameesh")
        self.assertTrue(result["ok"], result)
        self.assertIn("world-writable", " ".join(result["warnings"]))

    def test_a_device_owned_by_another_group_is_a_warning(self):
        result = FakeHost(gid=901).probe(["android-emulator"], user="ameesh")
        self.assertTrue(result["ok"], result)
        self.assertIn("belongs to group", " ".join(result["warnings"]))

    def test_an_unknown_capability_is_ignored(self):
        result = FakeHost().probe(["gpu"], user="ameesh")
        self.assertTrue(result["ok"])
        self.assertEqual(result["groups"], [])


class DoctorTest(unittest.TestCase):
    def card(self, *capabilities):
        return types.SimpleNamespace(
            hosts=[types.SimpleNamespace(title="ameesh-agents-3",
                                         capabilities=list(capabilities))])

    def doctor(self, canons, result, **kwargs):
        cfg = types.SimpleNamespace(host="ameesh-agents-3")
        return host_capabilities.doctor_lines(
            cfg, canons=canons, user="ameesh",
            probe_fn=lambda capabilities, **more: result, **kwargs)

    def test_a_declared_capability_applied_is_green(self):
        ko, lines = self.doctor([self.card("android-emulator")], {
            "problems": [], "warnings": [], "groups": ["kvm"]})
        self.assertFalse(ko)
        self.assertEqual(lines, ["capabilities: android-emulator (group kvm)"])

    def test_a_declared_capability_not_applied_is_a_ko(self):
        ko, lines = self.doctor([self.card("android-emulator")], {
            "problems": ["user ameesh is not in group kvm"],
            "warnings": [], "groups": ["kvm"]})
        self.assertTrue(ko)
        self.assertIn("KO", lines[0])
        self.assertIn("not in group kvm", lines[0])

    def test_a_group_without_declaration_is_a_warning_not_a_ko(self):
        ko, lines = self.doctor([self.card()], {
            "problems": [],
            "warnings": ["group kvm is granted to ameesh but no capability is declared"],
            "groups": []})
        self.assertFalse(ko)
        self.assertIn("warning", lines[0])

    def test_the_line_says_when_the_executors_need_a_restart(self):
        ko, lines = self.doctor([self.card("android-emulator")], {
            "problems": [], "groups": ["kvm"],
            "warnings": ["group kvm is granted to ameesh but not yet in the "
                         "executor process: restart it "
                         "(ameesh-runner-agent@<agent>.service) to use /dev/kvm"]})
        self.assertFalse(ko)
        self.assertIn("warning", lines[0])
        self.assertIn("restart it", lines[0])

    def test_no_card_or_no_host_says_nothing(self):
        self.assertEqual(self.doctor([], {"problems": [], "warnings": [],
                                          "groups": []}), (False, []))
        cfg = types.SimpleNamespace(host="")
        self.assertEqual(host_capabilities.doctor_lines(cfg), (False, []))

    def test_an_unreadable_canon_does_not_break_the_doctor(self):
        cfg = types.SimpleNamespace(host="ameesh-agents-3")
        with mock.patch.object(canon, "load_configured",
                               side_effect=RuntimeError("boom")):
            ko, lines = host_capabilities.doctor_lines(cfg)
        self.assertFalse(ko)
        self.assertIn("not checked", lines[0])


# ==========================================================================
# provisioning : n'accorde que ce qui est déclaré, idempotent
# ==========================================================================

class ApplyTest(unittest.TestCase):
    def apply(self, capabilities, *, probe=None, dry_run=False):
        calls = []
        code, lines = host_capabilities.apply(
            capabilities, user="ameesh", dry_run=dry_run,
            run=lambda command, check: calls.append(command),
            probe_fn=probe or (lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("dry run must not probe"))))
        return code, lines, calls

    def test_a_dry_run_prints_the_usermod_without_probing(self):
        code, lines, calls = self.apply(["android-emulator"], dry_run=True)
        self.assertEqual((code, lines), (0, ["+ usermod -aG kvm ameesh"]))
        self.assertEqual(calls, [])

    def test_a_missing_group_member_is_added_once(self):
        code, lines, calls = self.apply(["android-emulator"], probe=lambda *a, **k: {
            "user_missing": False, "group_missing": [], "missing_groups": ["kvm"]})
        self.assertEqual(code, 0)
        self.assertEqual(calls, [["usermod", "-aG", "kvm", "ameesh"]])
        self.assertEqual(lines, ["applied: usermod -aG kvm ameesh"])

    def test_an_existing_member_is_left_alone(self):
        code, lines, calls = self.apply(["android-emulator"], probe=lambda *a, **k: {
            "user_missing": False, "group_missing": [], "missing_groups": []})
        self.assertEqual((code, lines, calls),
                         (0, ["capabilities: ameesh already in group kvm"], []))

    def test_an_absent_user_or_group_applies_nothing(self):
        for probe in ({"user_missing": True, "group_missing": [],
                       "missing_groups": []},
                      {"user_missing": False, "group_missing": ["kvm"],
                       "missing_groups": []}):
            with self.subTest(probe=probe):
                code, _lines, calls = self.apply(["android-emulator"],
                                               probe=lambda *a, **k: probe)
                self.assertEqual(code, 2)
                self.assertEqual(calls, [])

    def test_nothing_declared_applies_nothing(self):
        self.assertEqual(self.apply([], dry_run=True), (0, [], []))
        self.assertEqual(self.apply(["gpu"], dry_run=True), (0, [], []))


# ==========================================================================
# fiches lues par l'installation et par l'observation
# ==========================================================================

class CardTest(_TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.make_tmp()

    def cfg(self, root: str | None = None):
        return types.SimpleNamespace(canon=root or self.root, canon_ref="",
                                     canon_untrusted=True)

    def repo(self):
        """Un canon git avec sa branche relue `origin/main`."""
        _bare, clone = publish(self.root, "canon")
        return clone

    def test_card_reads_the_declaration(self):
        write(self.root, "hotes/ag3.md",
              HOSTE % ("ameesh-agents-3", "capabilities: [android-emulator]\n"))
        data, path = host_capabilities.card(self.root, "ameesh-agents-3",
                                            untrusted=True)
        self.assertIsNotNone(data)
        self.assertTrue(path.endswith("hotes/ag3.md"))
        self.assertEqual(host_capabilities.from_card(data),
                         (["android-emulator"], [], []))

    def test_card_reads_the_reviewed_head_not_the_working_tree(self):
        # N3 de #7926 : ameesh peut réécrire son arbre de travail ; root ne
        # doit lire la déclaration que dans le commit poussé (`origin/main`).
        clone = self.repo()
        write(clone, "hotes/ag3.md",
              HOSTE % ("ameesh-agents-3", "capabilities: [android-emulator]\n"))
        commit_all(clone)
        # l'arbre de travail dit le contraire, sans commit : il est ignoré
        write(clone, "hotes/ag3.md", HOSTE % ("ameesh-agents-3", ""))
        data, path = host_capabilities.card(clone, "ameesh-agents-3")
        self.assertEqual(path, "hotes/ag3.md")
        self.assertEqual(host_capabilities.from_card(data),
                         (["android-emulator"], [], []))

    def test_card_ignores_a_local_commit_that_was_never_pushed(self):
        clone = self.repo()
        write(clone, "hotes/ag3.md", HOSTE % ("ameesh-agents-3", ""))
        commit_all(clone)
        write(clone, "hotes/ag3.md",
              HOSTE % ("ameesh-agents-3", "capabilities: [android-emulator]\n"))
        git(clone, "add", "-A")
        git(clone, "commit", "-q", "-m", "local, jamais poussé")
        data, _path = host_capabilities.card(clone, "ameesh-agents-3")
        self.assertEqual(host_capabilities.from_card(data), ([], [], []))

    def test_a_canon_without_hotes_reads_no_card(self):
        # N4 de #7926 : sans `hotes/`, le parcours ne va pas plus loin (un
        # worktree imbriqué portait sinon une fiche homonyme).
        write(self.root, "w7248/hotes/ag3.md",
              HOSTE % ("ameesh-agents-3", "capabilities: [android-emulator]\n"))
        self.assertEqual(host_capabilities.card(self.root, "ameesh-agents-3",
                                                untrusted=True), (None, None))
        clone = self.repo()
        write(clone, "w7248/hotes/ag3.md",
              HOSTE % ("ameesh-agents-3", "capabilities: [android-emulator]\n"))
        commit_all(clone)
        self.assertEqual(host_capabilities.card(clone, "ameesh-agents-3"),
                         (None, None))

    def test_card_ignores_other_types_and_other_titles(self):
        write(self.root, "membres/ag3.md", fiche(type="Member", title="ameesh-agents-3"))
        write(self.root, "hotes/autre.md", fiche(type="Host", title="autre"))
        self.assertIsNone(host_capabilities.card(self.root, "ameesh-agents-3",
                                                 untrusted=True)[0])

    def test_card_for_reads_the_configured_canon(self):
        write(self.root, "hotes/ag3.md", HOSTE % ("ameesh-agents-3", ""))
        data, _path = host_capabilities.card_for(self.cfg(), "ameesh-agents-3")
        self.assertEqual(data["title"], "ameesh-agents-3")

    def test_card_for_reads_a_git_canon_at_its_reviewed_head(self):
        bare, clone = publish(self.root, "canon")
        write(clone, "hotes/ag3.md",
              HOSTE % ("ameesh-agents-3", "capabilities: [android-emulator]\n"))
        commit_all(clone)
        write(clone, "hotes/ag3.md", HOSTE % ("ameesh-agents-3", ""))
        cfg = types.SimpleNamespace(canon=clone, canon_ref="",
                                    canon_untrusted=False)
        data, _path = host_capabilities.card_for(cfg, "ameesh-agents-3")
        self.assertEqual(host_capabilities.from_card(data),
                         (["android-emulator"], [], []))
        self.assertTrue(os.path.isdir(bare))

    def test_card_for_marks_an_unreadable_reviewed_head(self):
        # Une tête relue illisible n'est pas « absent du canon » : l'appelant
        # doit pouvoir le dire (agent observe -> canon_unreadable).
        cfg = types.SimpleNamespace(canon=self.root, canon_ref="",
                                    canon_untrusted=False)
        with self.assertRaises(canon.CanonError):
            host_capabilities.card_for(cfg, "ameesh-agents-3")

    def test_hosts_with_filters_the_canon(self):
        item = types.SimpleNamespace(hosts=[
            types.SimpleNamespace(title="a", capabilities=["android-emulator"]),
            types.SimpleNamespace(title="b", capabilities=None),
            types.SimpleNamespace(title="c", capabilities=["android-emulator"])])
        self.assertEqual(host_capabilities.hosts_with(item, "android-emulator"),
                         ["a", "c"])


@unittest.skipUnless(sys.platform.startswith("linux"),
                     "deploy/guest/install.sh provisionne un invité Linux "
                     "(systemd, apt, /proc/meminfo)")
class InstallScriptTest(unittest.TestCase):
    """L'installation n'applique le groupe que si la fiche le déclare."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="7926-install-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        _bare, self.canon = publish(self.tmp, "canon")
        write(self.canon, "hotes/ag3.md",
              HOSTE % ("ag3-test", "capabilities: [android-emulator]\n"))
        write(self.canon, "hotes/ag2.md", HOSTE % ("ag2-test", ""))
        commit_all(self.canon)

    def install(self, host: str, *extra: str):
        return subprocess.run(
            ["bash", INSTALL, "--dry-run", "--user", "ameesh-guest-dry-run",
             "--key", "ssh-ed25519 AAAAC3Nza test@poste",
             "--from", "203.0.113.7", "--host", host, "--canon", self.canon,
             *extra], capture_output=True, text=True)

    def test_a_declared_capability_prints_the_usermod(self):
        res = self.install("ag3-test")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("+ usermod -aG kvm ameesh", res.stdout)

    def test_an_undeclared_host_gets_no_group(self):
        res = self.install("ag2-test")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("usermod -aG kvm", res.stdout)

    def test_the_installer_reads_the_reviewed_head_not_the_working_tree(self):
        # N3 de #7926 : la fiche non commitée ne doit pas accorder kvm à root.
        write(self.canon, "hotes/ag2.md",
              HOSTE % ("ag2-test", "capabilities: [android-emulator]\n"))
        res = self.install("ag2-test")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("usermod -aG kvm", res.stdout)

    def test_an_unreadable_reviewed_head_applies_nothing_and_says_so(self):
        plain = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(os.path.join(plain, "hotes"))
        write(plain, "hotes/ag3.md",
              HOSTE % ("ag3-test", "capabilities: [android-emulator]\n"))
        res = subprocess.run(
            ["bash", INSTALL, "--dry-run", "--user", "ameesh-guest-dry-run",
             "--key", "ssh-ed25519 AAAAC3Nza test@poste",
             "--from", "203.0.113.7", "--host", "ag3-test",
             "--canon", plain], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("usermod -aG kvm", res.stdout)
        self.assertIn("no readable reviewed ref", res.stdout)

    def test_no_capabilities_option_applies_nothing(self):
        res = self.install("ag3-test", "--no-capabilities")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("usermod -aG kvm", res.stdout)

    def test_an_absent_canon_or_card_applies_nothing(self):
        for args in (("--canon", os.path.join(self.tmp, "nope")),
                     ("--canon", self.canon, "--host", "nobody")):
            with self.subTest(args=args):
                res = subprocess.run(
                    ["bash", INSTALL, "--dry-run", "--user", "ameesh-guest-dry-run",
                     "--key", "ssh-ed25519 AAAAC3Nza test@poste",
                     "--from", "203.0.113.7", *args],
                    capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertNotIn("usermod -aG kvm", res.stdout)

    def test_nothing_widens_the_device(self):
        res = self.install("ag3-test")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("chmod", res.stdout)
        self.assertNotIn("udev", res.stdout)
        self.assertNotIn("setfacl", res.stdout)


# ==========================================================================
# placement et observation lisent la déclaration
# ==========================================================================

class PlacementTest(_TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.make_tmp()
        # le canon d'exemple admet `orchestre` sur `atelier` seulement
        import shutil
        from .test_canon import EXAMPLE
        shutil.copytree(EXAMPLE, self.root, dirs_exist_ok=True)

    def write_atelier(self, capabilities=None):
        fields = dict(type="Host", title="atelier", responsible="human:bruno")
        if capabilities is not None:
            fields["capabilities"] = capabilities
        write(self.root, "hotes/atelier.md", fiche(**fields))

    def load(self):
        return canon.load(self.root, untrusted=True)

    def test_placement_ignores_the_capability_unless_asked(self):
        self.write_atelier(["android-emulator"])
        loaded = self.load()
        choix = {c["host"]: c for c in placement.proposals(loaded, "orchestre")}
        self.assertTrue(choix["atelier"]["admissible"])
        self.assertEqual(choix["atelier"]["capabilities"], ["android-emulator"])

    def test_a_requested_capability_removes_the_other_hosts(self):
        self.write_atelier(["android-emulator"])
        loaded = self.load()
        choix = {c["host"]: c for c in placement.proposals(
            loaded, "orchestre", capability="android-emulator")}
        self.assertTrue(choix["atelier"]["admissible"])
        self.assertFalse(choix["banc"]["admissible"])
        self.assertIn("does not declare capability android-emulator",
                      " ".join(choix["banc"]["reasons"]))

    def test_a_host_without_the_requested_capability_is_not_admissible(self):
        self.write_atelier([])
        loaded = self.load()
        choix = {c["host"]: c for c in placement.proposals(
            loaded, "orchestre", capability="android-emulator")}
        self.assertFalse(choix["atelier"]["admissible"])
        self.assertIn("does not declare capability android-emulator",
                      " ".join(choix["atelier"]["reasons"]))
        self.assertEqual(placement.capable_hosts(loaded, "android-emulator"), [])

    def test_the_report_carries_the_capability_and_capable_hosts(self):
        self.write_atelier(["android-emulator"])
        loaded = self.load()
        entries = placement.report(loaded, "orchestre",
                                   capability="android-emulator")
        self.assertEqual(entries[0]["capability"], "android-emulator")
        self.assertEqual([c["host"] for c in entries[0]["admissible"]], ["atelier"])
        self.assertEqual(placement.capable_hosts(loaded, "android-emulator"),
                         ["atelier"])


class ObservationHostTest(_TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.make_tmp()
        write(self.root, "hotes/ag3.md",
              HOSTE % ("ameesh-agents-3", "capabilities: [android-emulator]\n"))
        self.cfg = types.SimpleNamespace(canon=self.root, canon_ref="",
                                         canon_untrusted=True)

    def test_the_host_block_carries_the_declared_capabilities(self):
        block = agent_observation._host_block(self.cfg, {"host": "ameesh-agents-3"})
        self.assertEqual(block, {"name": "ameesh-agents-3",
                                 "capabilities": ["android-emulator"],
                                 "available": True, "reason": None})

    def test_no_host_and_unknown_host_are_named_facts(self):
        self.assertIsNone(agent_observation._host_block(self.cfg, {}))
        block = agent_observation._host_block(self.cfg, {"host": "autre"})
        self.assertEqual(block, {"name": "autre", "capabilities": [],
                                 "available": False, "reason": "host_not_in_canon"})

    def test_an_unreadable_canon_is_a_named_fact(self):
        with mock.patch.object(host_capabilities, "card_for",
                               side_effect=RuntimeError("boom")):
            block = agent_observation._host_block(self.cfg, {"host": "ag3"})
        self.assertEqual(block["reason"], "canon_unreadable")
        self.assertFalse(block["available"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
