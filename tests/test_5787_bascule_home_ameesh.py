# SPDX-License-Identifier: AGPL-3.0-only
"""#5787 : la bascule d'un mesh (basculer-mesh.sh, exécuté en root sur la VM)
écrivait dans /home/ameesh en root (install -o, cat > puis chown, config.json
réécrit par python3), puis posait un paquet git (chown) et le vidage du mesh
sous des noms devinables de /tmp : un lien posé par ameesh (~/.ssh vers /etc/systemd/system) menait root
à écrire hors du home. Témoin statique : toute écriture du home ou d'un
fichier temporaire passe par runuser -u ameesh ; vm.sh et poste.sh, dans
l'empreinte, n'écrivent pas le home d'ameesh en root (poste.sh : listes de
sauvegarde_serveur dans un dossier privé de root ; idem staging.sh,
sauvegarde_serveur_vm, #5799).
"""
from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASCULE = ROOT / "deploy" / "serveur" / "basculer-mesh.sh"
VM = ROOT / "deploy" / "mise-a-jour" / "vm.sh"
POSTE = ROOT / "deploy" / "mise-a-jour" / "poste.sh"
STAGING = ROOT / "deploy" / "mise-a-jour" / "staging.sh"


def code(path: pathlib.Path) -> list[str]:
    return [l for l in path.read_text().splitlines() if not l.lstrip().startswith("#")]


class BasculeHomeAmeeshTest(unittest.TestCase):
    def test_home_writes_run_as_the_user(self):
        for line in code(BASCULE):
            if "/home/ameesh" in line and re.search(r"install |mkdir |cat >|tee |cp |mv ", line):
                self.assertIn("runuser -u ameesh -- ", line, line)

    def test_no_root_ownership_change(self):
        text = "\n".join(code(BASCULE))
        self.assertNotRegex(text, r"\bchown\b")
        self.assertNotRegex(text, r"install [^\n]*-o ")

    def test_the_server_config_is_rewritten_by_the_user(self):
        text = BASCULE.read_text()
        self.assertIn("runuser -u ameesh -- python3 - '$HOTE_VM'", text)
        self.assertNotIn('ssh_vm "python3 -', text)

    def test_the_bundle_never_lands_under_a_guessable_tmp_name(self):
        text = "\n".join(code(BASCULE))
        self.assertNotRegex(text, r"scp [^\n]*:/tmp/")
        self.assertIn("t=\\$(runuser -u ameesh -- mktemp)", text)
        self.assertNotRegex(text, r"[ >]/tmp/[\w.-]+\b")

    def test_vm_root_commands_leave_the_home_alone(self):
        for line in code(VM):
            if re.search(r"\bssh_vm\b", line):
                self.assertNotIn("/home/ameesh", line, line)

    def test_backup_lists_never_live_in_the_user_dir(self):
        # revue DeepSeek (mty-amh-impl-03, beb212b37603) : les listes avant/après
        # de sauvegarde_serveur étaient des mktemp dans $d (dossier d'ameesh),
        # réécrites par redirection root pendant pg_dump. #5799 : même motif
        # dans sauvegarde_serveur_vm de staging.sh (VM manaty-mesh-1).
        for script in (POSTE, STAGING):
            text = "\n".join(code(script))
            self.assertNotRegex(text, r'mktemp "\$d/', script)
            self.assertIn("t=$(mktemp -d)", text, script)
            self.assertRegex(text, r'before=\$t/avant', script)
            self.assertRegex(text, r'after=\$t/apres', script)

    def test_poste_root_commands_leave_the_home_alone(self):
        for line in code(POSTE):
            if re.search(r"\bssh_vm(_borne)?\b", line):
                self.assertNotIn("/home/ameesh", line, line)


if __name__ == "__main__":
    unittest.main()
