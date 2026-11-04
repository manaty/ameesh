# SPDX-License-Identifier: AGPL-3.0-only
"""#5784 : l'installateur du serveur ajoutait `ameesh` au groupe `docker`
(démon rootful = root) ; le rejouer après une bascule rootless (étude L5502)
rendait au compte l'équivalence root. Témoins, sur un simulateur (commandes
système remplacées, chemins sous une racine jetable) : hôte neuf et reprise
d'un hôte rootless convergent vers Docker rootless, sans groupe docker, avec
DOCKER_HOST posé partout ; un compte encore membre, un prérequis manquant ou
une empreinte inattendue arrêtent le script, sans repli sur le rootful.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "serveur" / "docker-rootless.sh"
INSTALLER = ROOT / "deploy" / "serveur" / "installer-serveur.sh"
PINNED = re.search(r"^WRAPPER_SHA256=(\w+)$", SCRIPT.read_text(), re.M).group(1)
SOCK = "unix:///run/user/1001/docker.sock"

# Commandes système simulées : chaque appel est consigné dans $JOURNAL.
STUBS = {
    "id": """
        case "$1" in
          -u) echo 1001 ;;
          -nG) echo "ameesh $GROUPES" ;;
        esac""",
    "getent": 'echo "ameesh:x:1001:1001::$HOME_U:/bin/bash"',
    "dpkg": 'for p in $PAQUETS_ABSENTS; do [[ $2 == "$p" ]] && exit 1; done; exit 0',
    "apt-get": '[[ -z $APT_ECHOUE ]]',
    "dockerd": "exit 0",
    "loginctl": "exit 0",
    "systemctl": "exit 0",
    "apparmor_parser": "exit 0",
    "curl": """
        while [[ $1 != -o ]]; do shift; done
        printf '%s' "$CONTENU_TELECHARGE" > "$2" """,
    # Empreinte simulée : le contenu « moby-epingle » vaut l'empreinte épinglée.
    "sha256sum": """
        if [[ -f $1 ]] && grep -qx moby-epingle "$1"; then echo "$PINNED  $1"
        else exec /usr/bin/sha256sum "$@"; fi""",
    # Pas de root dans l'essai : -o/-g retirés, le reste à install(1).
    "install": """
        for x in "$@"; do [[ $x == "$HOME_U"* ]] && { echo "install root dans le home : $x" >&2; exit 99; }; done
        a=(); while (( $# )); do case $1 in -o|-g) shift 2 ;; *) a+=("$1"); shift ;; esac; done
        exec /usr/bin/install "${a[@]}" """,
    # runuser exécute pour de vrai (même uid dans l'essai), sauf `docker info`.
    "runuser": """
        while [[ $1 != -- ]]; do shift; done; shift
        if [[ $* == *"docker info"* ]]; then echo "$DOCKER_INFO"; else exec "$@"; fi""",
}


class DockerRootlessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.racine = self.tmp / "racine"
        self.home = self.tmp / "home" / "ameesh"
        bin_ = self.tmp / "bin"
        for d in (bin_, self.home, self.racine / "etc", self.racine / "usr/bin",
                  self.racine / "run/user/1001"):
            d.mkdir(parents=True)
        self.journal = self.tmp / "journal"
        for name, body in STUBS.items():
            p = bin_ / name
            p.write_text('#!/bin/bash\necho "%s $*" >> "$JOURNAL"\n%s\n'
                         % (name, textwrap.dedent(body)))
            p.chmod(0o755)
        for b in ("newuidmap", "newgidmap"):
            p = self.racine / "usr/bin" / b
            p.write_text("")
            p.chmod(0o4755)
        self.sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.sock.close)
        self.sock.bind(str(self.racine / "run/user/1001/docker.sock"))
        self.env = {
            "PATH": "%s:/usr/bin:/bin" % bin_, "RACINE": str(self.racine),
            "HOME_U": str(self.home), "JOURNAL": str(self.journal),
            "PINNED": PINNED, "GROUPES": "", "PAQUETS_ABSENTS": "",
            "CONTENU_TELECHARGE": "moby-epingle\n",
            "DOCKER_INFO": "[name=seccomp,profile=builtin name=rootless name=cgroupns] overlay2",
            "TMPDIR": str(self.tmp),
        }

    def run_script(self, **env):
        return subprocess.run(["bash", str(SCRIPT), "ameesh"], capture_output=True,
                              text=True, env={**self.env, **env}, timeout=60)

    def calls(self):
        return self.journal.read_text() if self.journal.exists() else ""

    def assert_converged(self):
        unit = self.home / ".config/systemd/user/docker.service"
        self.assertIn("ExecStart=/usr/local/bin/dockerd-rootless.sh", unit.read_text())
        self.assertEqual((self.home / ".config/environment.d/50-docker.conf").read_text(),
                         "DOCKER_HOST=%s\n" % SOCK)
        dropin = self.racine / "etc/systemd/system/ameesh-runner@.service.d/docker-rootless.conf"
        self.assertIn("Environment=DOCKER_HOST=%s" % SOCK, dropin.read_text())
        self.assertIn("After=user@1001.service", dropin.read_text())
        wrapper = self.racine / "usr/local/bin/dockerd-rootless.sh"
        self.assertEqual(wrapper.read_text(), "moby-epingle\n")
        self.assertTrue(wrapper.stat().st_mode & stat.S_IXUSR)
        calls = self.calls()
        self.assertIn("systemctl --user -M ameesh@ enable --now docker.service", calls)
        self.assertIn("loginctl enable-linger ameesh", calls)
        # jamais le groupe docker, sous aucune forme
        self.assertNotRegex(calls, r"usermod|gpasswd|adduser|groupmod")

    def test_a_fresh_host_converges_to_rootless(self):
        out = self.run_script(PAQUETS_ABSENTS="rootlesskit fuse-overlayfs")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assert_converged()
        self.assertIn("apt-get install -y -qq rootlesskit fuse-overlayfs", self.calls())
        self.assertIn("curl -fsSL", self.calls())
        self.assertEqual((self.racine / "etc/subuid").read_text(), "ameesh:100000:65536\n")
        self.assertEqual((self.racine / "etc/subgid").read_text(), "ameesh:100000:65536\n")
        self.assertIn(SOCK, out.stdout)

    def test_a_rootless_host_resumes_without_change(self):
        (self.racine / "etc/subuid").write_text("ubuntu:100000:65536\nameesh:165536:65536\n")
        (self.racine / "etc/subgid").write_text("ubuntu:100000:65536\nameesh:165536:65536\n")
        env_file = self.home / ".config/ameesh/env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text("AMEESH_X=1\nDOCKER_HOST=unix:///var/run/docker.sock\n")
        self.assertEqual(self.run_script().returncode, 0)
        self.journal.unlink()
        out = self.run_script()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assert_converged()
        # épinglé déjà en place, paquets présents : rien n'est téléchargé ni installé
        self.assertNotIn("curl", self.calls())
        self.assertNotIn("apt-get", self.calls())
        self.assertEqual((self.racine / "etc/subuid").read_text(),
                         "ubuntu:100000:65536\nameesh:165536:65536\n")
        self.assertEqual(env_file.read_text(), "AMEESH_X=1\nDOCKER_HOST=%s\n" % SOCK)

    def test_a_hostile_link_in_the_home_never_reaches_system_units(self):
        # revue cx1 : ~/.config/systemd/user -> /etc/systemd/system, posé par
        # l'utilisateur avant une reprise. Les écritures du home passent par
        # runuser (droits de l'utilisateur) : la cible système, non inscriptible
        # pour lui, reste intacte, et le script s'arrête.
        system = self.racine / "etc/systemd/system"
        system.mkdir(parents=True)
        system.chmod(0o555)
        self.addCleanup(system.chmod, 0o755)
        (self.home / ".config/systemd").mkdir(parents=True)
        (self.home / ".config/systemd/user").symlink_to(system)
        out = self.run_script()
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(list(system.iterdir()), [])
        self.assertEqual(stat.S_IMODE(system.stat().st_mode), 0o555)
        self.assertIn("runuser -u ameesh -- mkdir -p %s/.config/systemd/user" % self.home,
                      self.calls())
        self.assertNotIn("install root dans le home", out.stderr)

    def test_home_writes_run_as_the_user(self):
        self.assertEqual(self.run_script().returncode, 0)
        # chaque commande simulée qui touche au home passe par runuser ; les
        # autres (install en root) s'arrêteraient sur le code 99 du bouchon.
        for line in self.calls().splitlines():
            if line.split(" ")[0] in STUBS and str(self.home) in line:
                self.assertTrue(line.startswith(("getent ", "runuser -u ameesh -- ")), line)

    def test_a_new_range_follows_the_existing_ones(self):
        (self.racine / "etc/subuid").write_text("ubuntu:100000:65536\n")
        self.assertEqual(self.run_script().returncode, 0)
        self.assertIn("ameesh:165536:65536", (self.racine / "etc/subuid").read_text())

    def test_a_member_of_docker_stops_before_any_change(self):
        out = self.run_script(GROUPES="docker")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("membre du groupe docker", out.stderr)
        self.assertIn("gpasswd -d ameesh docker", out.stderr)
        self.assertFalse((self.home / ".config").exists())
        self.assertNotRegex(self.calls(), r"apt-get|curl|systemctl")

    def test_an_unexpected_digest_is_never_installed(self):
        out = self.run_script(CONTENU_TELECHARGE="autre chose\n")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("empreinte inattendue", out.stderr)
        self.assertFalse((self.racine / "usr/local/bin/dockerd-rootless.sh").exists())
        self.assertNotIn("systemctl", self.calls())

    def test_a_missing_prerequisite_is_an_explicit_error(self):
        out = self.run_script(PAQUETS_ABSENTS="slirp4netns", APT_ECHOUE="1")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("installation impossible : slirp4netns", out.stderr)
        (self.racine / "usr/bin/newuidmap").chmod(0o755)
        out = self.run_script()
        self.assertIn("newuidmap absent ou non setuid", out.stderr)

    def test_restricted_userns_needs_the_rootlesskit_profile(self):
        knob = self.racine / "proc/sys/kernel/apparmor_restrict_unprivileged_userns"
        knob.parent.mkdir(parents=True)
        knob.write_text("1\n")
        out = self.run_script()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("aucun profil AppArmor rootlesskit", out.stderr)
        profile = self.racine / "etc/apparmor.d/rootlesskit"
        profile.parent.mkdir(parents=True)
        profile.write_text("profile rootlesskit /usr/bin/rootlesskit flags=(unconfined) {}\n")
        self.assertEqual(self.run_script().returncode, 0)
        self.assertIn("apparmor_parser -r %s" % profile, self.calls())

    def test_a_rootful_answer_is_an_error_not_a_fallback(self):
        out = self.run_script(DOCKER_INFO="[name=seccomp,profile=builtin] overlay2")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("ne sert pas un démon rootless", out.stderr)
        out = self.run_script(DOCKER_INFO="[name=rootless] vfs")
        self.assertIn("pilote de stockage vfs", out.stderr)


class InstallerTest(unittest.TestCase):
    def test_the_installer_no_longer_grants_the_docker_group(self):
        text = INSTALLER.read_text()
        self.assertNotRegex(text, r"usermod[^\n]*docker|gpasswd -a|adduser ameesh docker")
        self.assertIn('"$SRC/deploy/serveur/docker-rootless.sh" ameesh', text)
        # avant WireGuard, Postgres, etc. : un compte encore membre arrête tout
        self.assertLess(text.index("docker-rootless.sh\" ameesh"), text.index("# --- WireGuard"))

    def test_the_script_never_runs_the_setuptool_nor_switches_context(self):
        code = "\n".join(l for l in SCRIPT.read_text().splitlines()
                         if not l.lstrip().startswith("#"))
        self.assertNotIn("setuptool", code)
        self.assertNotIn("context use", code)
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main()
