# SPDX-License-Identifier: AGPL-3.0-only
"""#7002 : le pair WireGuard de bureau-goal, posé hors de wg0.conf par
ameesh-bg-peer.service, n'était pas relancé au redémarrage de wg0 (PartOf
seul) et rien ne le voyait. Témoins : l'unité versionnée revient avec wg0
(WantedBy) et déclare son pair ; `reseau-prive.sh verifier` sur le hub est
rouge, avec le nom, pour un pair attendu absent de wg0 et vert sinon ;
ajouter-appareil.sh ne prend pas l'adresse d'un pair hors de wg0.conf et
n'efface plus les autres pairs (wg set, plus syncconf).
"""
from __future__ import annotations

import configparser
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from tests.test_6859_reseau_prive import STUBS as STUBS_6859

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVEUR = ROOT / "deploy" / "serveur"
UNITE = SERVEUR / "ameesh-bg-peer.service"

WG_CONF = """[Interface]
Address = 10.77.0.1/24
ListenPort = 51820
PrivateKey = cle-du-hub

# appareil pc-smichea 10.77.0.2
[Peer]
PublicKey = PC=
AllowedIPs = 10.77.0.2/32

# appareil ameesh-agents-1 10.77.0.3
[Peer]
PublicKey = AG1=
AllowedIPs = 10.77.0.3/32
"""

STUBS = {
    **STUBS_6859,
    "wg": """
        case "$*" in
          "show wg0 allowed-ips") [[ -z $WG_ILLISIBLE ]] || exit 1; printf '%b' "$WG_PAIRS" ;;
          genkey) printf '%s\\n' "${WG_GENKEY:-cle-privee}" ;;
          # #7094 : le vrai `wg pubkey` lit son entrée. Un faux qui sort sans la
          # lire fait prendre SIGPIPE à `echo | wg pubkey` (141, stderr vide)
          # sous pipefail dès que la clé dépasse le tampon du pipe.
          pubkey) cat >/dev/null; echo NOUVELLE= ;;
          *) exit 0 ;;
        esac""",
    "wg-quick": "exit 0",
}

TOUS = r"PC=\t10.77.0.2/32\nAG1=\t10.77.0.3/32\nBG=\t10.77.0.6/32\n"


class PairBgWg0Test(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.racine = self.tmp / "racine"
        (self.racine / "etc/wireguard").mkdir(parents=True)
        (self.racine / "etc/systemd/system").mkdir(parents=True)
        self.wg_conf = self.racine / "etc/wireguard/wg0.conf"
        self.wg_conf.write_text(WG_CONF)
        (self.racine / "etc/wireguard/serveur.pub").write_text("HUB=\n")
        shutil.copy(UNITE, self.racine / "etc/systemd/system/ameesh-bg-peer.service")
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        self.journal = self.tmp / "journal"
        for name, body in STUBS.items():
            p = bin_ / name
            p.write_text('#!/bin/bash\necho "%s $*" >> "$JOURNAL"\n%s\n'
                         % (name, textwrap.dedent(body)))
            p.chmod(0o755)
        self.env = {
            "PATH": "%s:/usr/bin:/bin" % bin_, "RACINE": str(self.racine),
            "JOURNAL": str(self.journal), "WG_PAIRS": TOUS,
            "ECOUTES": "127.0.0.1:5432 10.77.0.1:5432 10.78.0.10:5432",
            "JOIGNABLES": "10.78.0.11", "PG_OUVERTS": "", "PG_PRETS": "",
        }

    def run_script(self, script, *args, **env):
        return subprocess.run(["bash", str(SERVEUR / script), *args], capture_output=True,
                              text=True, env={**self.env, **env}, timeout=60)

    def verify(self, *extra, **env):
        return self.run_script("reseau-prive.sh", "verifier", "10.78.0.0/24", "10.78.0.11",
                               *extra, **env)

    def calls(self):
        return self.journal.read_text() if self.journal.exists() else ""

    # --- unité ------------------------------------------------------------------

    def test_unit_comes_back_with_wg0_and_declares_its_peer(self):
        unite = configparser.ConfigParser(strict=False, interpolation=None)
        unite.optionxform = str
        unite.read_string(UNITE.read_text())
        self.assertIn("wg-quick@wg0.service", unite["Install"]["WantedBy"].split())
        self.assertIn("multi-user.target", unite["Install"]["WantedBy"].split())
        for cle in ("PartOf", "Requires", "After"):
            self.assertIn("wg-quick@wg0.service", unite["Unit"][cle].split(), cle)
        self.assertEqual(unite["Unit"]["X-Ameesh-Pair-WireGuard"], "bureau-goal 10.77.0.6/32")
        self.assertEqual(unite["Service"]["ExecStart"], "/usr/local/sbin/ameesh-bg-peer.sh start")
        # filtre avant pair : jamais dans wg0.conf, aucune clé dans le dépôt
        script = (SERVEUR / "ameesh-bg-peer.sh").read_text()
        self.assertLess(script.index("regle INPUT"), script.index('wg set $IF peer "$PUB" allowed-ips'))
        self.assertIn("PUB_FILE=/etc/ameesh-bg-peer/", script)
        self.assertNotIn("PrivateKey", UNITE.read_text() + script)

    # --- verifier sur le hub -------------------------------------------------------

    def test_hub_green_when_every_expected_peer_is_up(self):
        out = self.verify()
        self.assertEqual(out.returncode, 0, out.stderr)
        for attendu in ("ok pair wg0 pc-smichea (10.77.0.2/32)",
                        "ok pair wg0 ameesh-agents-1 (10.77.0.3/32)",
                        "ok pair wg0 bureau-goal (10.77.0.6/32)"):
            self.assertIn(attendu, out.stderr)

    def test_hub_red_with_name_when_bg_peer_did_not_come_back(self):
        out = self.verify(WG_PAIRS=r"PC=\t10.77.0.2/32\nAG1=\t10.77.0.3/32\n")
        self.assertEqual(out.returncode, 2)
        self.assertIn("ÉCHEC pair wg0 bureau-goal (10.77.0.6/32) absent", out.stderr)
        self.assertIn("1 échec(s)", out.stderr)

    def test_hub_red_when_a_conf_peer_is_missing(self):
        out = self.verify(WG_PAIRS=r"AG1=\t10.77.0.3/32\nBG=\t10.77.0.6/32\n")
        self.assertEqual(out.returncode, 2)
        self.assertIn("ÉCHEC pair wg0 pc-smichea (10.77.0.2/32) absent", out.stderr)

    def test_peer_matched_by_address_field_not_prefix(self):
        # 10.77.0.26 ne vaut pas 10.77.0.2 ; plusieurs AllowedIPs sur un pair
        out = self.verify(WG_PAIRS=r"X=\t10.77.0.26/32\nAG1=\t10.77.0.3/32 10.77.0.6/32\n")
        self.assertEqual(out.returncode, 2)
        self.assertIn("pc-smichea (10.77.0.2/32) absent", out.stderr)
        self.assertIn("ok pair wg0 bureau-goal", out.stderr)

    def test_hub_red_when_wg0_unreadable(self):
        out = self.verify(WG_ILLISIBLE="1")
        self.assertEqual(out.returncode, 2)
        self.assertIn("wg0 illisible", out.stderr)

    def test_hub_down_skips_peers(self):
        out = self.verify("--hub-arrete", WG_PAIRS="")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("pairs non vérifiés", out.stderr)
        self.assertNotIn("wg show", self.calls())

    def test_agents_vm_has_no_wg0_conf(self):
        self.wg_conf.unlink()
        out = self.verify(WG_PAIRS="")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("wg ", self.calls())

    # --- ajouter-appareil.sh -----------------------------------------------------

    def test_new_device_skips_out_of_conf_peer_and_keeps_other_peers(self):
        # .2 et .3 dans wg0.conf, .4/.5 vivants seulement, .6 déclaré par l'unité (pair tombé)
        out = self.run_script("ajouter-appareil.sh", "nouveau",
                              WG_PAIRS=r"PC=\t10.77.0.2/32\nX=\t10.77.0.4/32\nY=\t10.77.0.5/32\n")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Address = 10.77.0.7/32", out.stdout)
        self.assertIn("# appareil nouveau 10.77.0.7", self.wg_conf.read_text())
        self.assertIn("wg set wg0 peer NOUVELLE= allowed-ips 10.77.0.7/32", self.calls())
        self.assertNotIn("syncconf", self.calls())

    def test_new_device_when_pubkey_ignores_stdin_does_not_die_of_sigpipe(self):
        # #7094 : une clé plus grosse que le tampon du pipe (64 Kio) force
        # l'ordonnancement — le faux `wg pubkey` sort avant d'avoir lu, `echo`
        # prend SIGPIPE et le script sort en 141 sans un mot. Le faux doit
        # drainer stdin, comme le vrai `wg pubkey`.
        out = self.run_script("ajouter-appareil.sh", "nouveau",
                              WG_GENKEY="a" * 100_000,
                              WG_PAIRS=r"PC=\t10.77.0.2/32\nX=\t10.77.0.4/32\nY=\t10.77.0.5/32\n")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Address = 10.77.0.7/32", out.stdout)
        self.assertIn("# appareil nouveau 10.77.0.7", self.wg_conf.read_text())

    def test_new_device_stops_when_wg0_unreadable(self):
        out = self.run_script("ajouter-appareil.sh", "nouveau", WG_ILLISIBLE="1")
        self.assertNotEqual(out.returncode, 0)
        self.assertNotIn("nouveau", self.wg_conf.read_text())


if __name__ == "__main__":
    unittest.main()
