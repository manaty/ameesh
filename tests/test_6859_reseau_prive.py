# SPDX-License-Identifier: AGPL-3.0-only
"""#6859 (EP8.2) : réseau privé des nœuds de base. Témoins, sur un simulateur
(commandes système remplacées, /etc sous une racine jetable) : la carte privée
prend une adresse fixe sans passerelle ; Postgres écoute aussi l'adresse
privée et pg_hba n'admet que hostssl + scram-sha-256 sur le sous-réseau ; une
adresse publique ou WireGuard est refusée ; listen_addresses n'est appliqué
qu'avec --redemarrer ; la vérification échoue sur une base exposée, un pair
injoignable ou un hub qui répond pendant l'essai hub arrêté.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "serveur" / "reseau-prive.sh"
MAC = "de:00:00:aa:bb:01"

STUBS = {
    "curl": 'printf \'%s\' "$METADONNEES_JSON"',
    "ip": """
        case "$*" in
          "-o link") echo "2: ens2: <UP> mtu 1500 link/ether de:00:00:00:00:02 brd ff"
                     echo "3: ens5: <UP> mtu 1500 link/ether DE:00:00:AA:BB:01 brd ff" ;;
          "-4 -o addr show dev ens5") [[ -z $ADR_ENS5 ]] || echo "3: ens5 inet $ADR_ENS5 scope global ens5" ;;
          # pas de wg0 (VM d'agents) : `ip` sort en erreur, comme le vrai
          "-4 -o addr show dev wg0") [[ -n $ADR_WG0 ]] && echo "4: wg0 inet $ADR_WG0 scope global wg0" ;;
          *) exit 1 ;;
        esac""",
    "systemctl": '[[ $* == "-q is-active systemd-networkd" ]] && exit ${NETWORKD_INACTIF:+3}; exit 0',
    "networkctl": "exit 0",
    "ifup": "exit 0",
    "sleep": "exit 0",
    "install": """
        a=(); while (( $# )); do case $1 in -o|-g) shift 2 ;; *) a+=("$1"); shift ;; esac; done
        exec /usr/bin/install "${a[@]}" """,
    "runuser": 'while [[ $1 != -- ]]; do shift; done; shift; exec "$@"',
    "psql": '[[ $* == *"SHOW listen_addresses"* ]] && echo "$ECOUTE_ACTUELLE"; exit 0',
    "pg_isready": 'for p in $PG_PRETS; do [[ $* == *"-h $p "* ]] && exit 0; done; [[ $* == *localhost* ]]',
    "ss": 'for a in $ECOUTES; do echo "LISTEN 0 200 $a 0.0.0.0:*"; done',
    "ping": 'for a in $JOIGNABLES; do [[ $* == *" $a" ]] && exit 0; done; exit 1',
    "timeout": 'for p in $PG_OUVERTS; do [[ $* == *"/dev/tcp/$p/5432"* ]] && exit 0; done; exit 1',
}


class ReseauPriveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.racine = self.tmp / "racine"
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        self.pg = self.racine / "etc/postgresql/17/main"
        (self.pg / "conf.d").mkdir(parents=True)
        self.hba = self.pg / "pg_hba.conf"
        self.hba.write_text("# ameesh-mesh : appareils du réseau WireGuard, TLS obligatoire\n"
                            "hostssl ameesh  all  10.77.0.0/24  scram-sha-256\n")
        self.journal = self.tmp / "journal"
        for name, body in STUBS.items():
            p = bin_ / name
            p.write_text('#!/bin/bash\necho "%s $*" >> "$JOURNAL"\n%s\n'
                         % (name, textwrap.dedent(body)))
            p.chmod(0o755)
        self.env = {
            "PATH": "%s:/usr/bin:/bin" % bin_, "RACINE": str(self.racine),
            "JOURNAL": str(self.journal),
            "METADONNEES_JSON": '{"private_nics": [{"mac_address": "%s"}]}' % MAC.upper(),
            "ADR_ENS5": "10.78.0.10/24", "ADR_WG0": "10.77.0.1/24",
            "ECOUTE_ACTUELLE": "localhost,10.77.0.1",
        }

    def run_script(self, *args, **env):
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True,
                              text=True, env={**self.env, **env}, timeout=60)

    def calls(self):
        return self.journal.read_text() if self.journal.exists() else ""

    # --- carte privée ---------------------------------------------------------

    def test_interface_static_address_without_gateway(self):
        out = self.run_script("interface", "10.78.0.10/24")
        self.assertEqual(out.returncode, 0, out.stderr)
        net = (self.racine / "etc/systemd/network/20-ameesh-prive.network").read_text()
        self.assertIn("MACAddress=%s" % MAC, net)
        self.assertIn("Address=10.78.0.10/24", net)
        self.assertIn("DHCP=no", net)
        self.assertNotIn("Gateway", net)
        self.assertNotIn("DNS=", net)
        self.assertIn("networkctl reconfigure ens5", self.calls())

    def test_interface_ifupdown_when_networkd_is_off(self):
        out = self.run_script("interface", "10.78.0.10/24", "--mac", MAC, NETWORKD_INACTIF="1")
        self.assertEqual(out.returncode, 0, out.stderr)
        conf = (self.racine / "etc/network/interfaces.d/ameesh-prive").read_text()
        self.assertIn("iface ens5 inet static", conf)
        self.assertIn("address 10.78.0.10/24", conf)
        self.assertNotIn("gateway", conf)
        self.assertNotIn("curl", self.calls())

    def test_interface_refuses_public_or_wireguard_address(self):
        for cidr in ("51.15.137.245/32", "10.77.0.9/24", "10.78.0.300/24", "10.78.0.10"):
            out = self.run_script("interface", cidr)
            self.assertEqual(out.returncode, 2, cidr)
        self.assertFalse((self.racine / "etc/systemd").exists())

    def test_interface_needs_mac_when_several_private_nics(self):
        two = '{"private_nics": [{"mac_address": "a"}, {"mac_address": "b"}]}'
        out = self.run_script("interface", "10.78.0.10/24", METADONNEES_JSON=two)
        self.assertEqual(out.returncode, 2)
        self.assertIn("--mac", out.stderr)

    def test_interface_fails_when_address_not_taken(self):
        out = self.run_script("interface", "10.78.0.10/24", ADR_ENS5="")
        self.assertEqual(out.returncode, 2)
        self.assertIn("n'a pas pris", out.stderr)

    # --- Postgres -------------------------------------------------------------

    def test_postgres_listens_on_private_and_keeps_wireguard(self):
        out = self.run_script("postgres", "10.78.0.10", "10.78.0.0/24")
        self.assertEqual(out.returncode, 0, out.stderr)
        conf = (self.pg / "conf.d/reseau-prive.conf").read_text()
        self.assertIn("listen_addresses = 'localhost,10.77.0.1,10.78.0.10'", conf)
        hba = self.hba.read_text()
        self.assertIn("hostssl ameesh  all  10.78.0.0/24  scram-sha-256", hba)
        self.assertNotRegex(hba, r"(?m)^host\s|trust|0\.0\.0\.0/0")
        dropin = (self.racine / "etc/systemd/system/postgresql@.service.d/reseau-prive.conf").read_text()
        self.assertIn("show to 10.78.0.10/32", dropin)
        self.assertIn("ExecStartPre=-", dropin)  # carte absente : Postgres démarre quand même
        # rechargé, jamais redémarré sans --redemarrer
        self.assertIn("pg_reload_conf", self.calls())
        self.assertNotIn("systemctl restart", self.calls())
        self.assertIn("--redemarrer", out.stderr)

    def test_postgres_on_a_node_without_wireguard_hub(self):
        out = self.run_script("postgres", "10.78.0.11", "10.78.0.0/24", ADR_WG0="")
        self.assertEqual(out.returncode, 0, out.stderr)
        conf = (self.pg / "conf.d/reseau-prive.conf").read_text()
        self.assertIn("listen_addresses = 'localhost,10.78.0.11'", conf)

    def test_postgres_is_idempotent(self):
        for _ in range(2):
            self.assertEqual(self.run_script("postgres", "10.78.0.10", "10.78.0.0/24").returncode, 0)
        self.assertEqual(self.hba.read_text().count("10.78.0.0/24"), 1)

    def test_postgres_subnet_change_replaces_the_line(self):
        self.run_script("postgres", "10.78.0.10", "10.78.0.0/24")
        self.run_script("postgres", "10.78.0.10", "10.78.0.0/25")
        hba = self.hba.read_text()
        self.assertNotIn("10.78.0.0/24", hba)
        self.assertEqual(hba.count("10.78.0.0/25"), 1)
        self.assertIn("10.77.0.0/24", hba)  # la ligne WireGuard d'origine reste

    def test_postgres_restarts_only_when_asked(self):
        out = self.run_script("postgres", "10.78.0.10", "10.78.0.0/24", "--redemarrer")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("systemctl restart postgresql@17-main", self.calls())

    def test_postgres_already_listening_needs_no_restart(self):
        out = self.run_script("postgres", "10.78.0.10", "10.78.0.0/24", "--redemarrer",
                              ECOUTE_ACTUELLE="localhost,10.77.0.1,10.78.0.10")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("systemctl restart", self.calls())

    def test_postgres_refuses_wide_or_public_subnets(self):
        for sr in ("0.0.0.0/0", "10.0.0.0/8", "10.77.0.0/24", "51.15.0.0/16", "10.78.0.0"):
            out = self.run_script("postgres", "10.78.0.10", sr)
            self.assertEqual(out.returncode, 2, sr)
        self.assertNotIn("10.78", self.hba.read_text())

    # --- vérification -----------------------------------------------------------

    def verify(self, *extra, **env):
        base = {"ECOUTES": "127.0.0.1:5432 [::1]:5432 10.77.0.1:5432 10.78.0.10:5432",
                "JOIGNABLES": "10.78.0.11 10.78.0.12", "PG_OUVERTS": "", "PG_PRETS": ""}
        return self.run_script("verifier", "10.78.0.0/24", "10.78.0.11", "10.78.0.12",
                               *extra, **{**base, **env})

    def test_verify_with_hub_down(self):
        out = self.verify("--hub-arrete", PG_OUVERTS="10.78.0.11", PG_PRETS="10.78.0.11")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("10.78.0.11 : Postgres prêt", out.stderr)
        self.assertIn("injoignable", out.stderr)

    def test_verify_fails_when_hub_still_answers(self):
        out = self.verify("--hub-arrete", JOIGNABLES="10.78.0.11 10.78.0.12 10.77.0.1")
        self.assertEqual(out.returncode, 2)
        self.assertIn("hub arrêté", out.stderr)
        # sans --hub-arrete, un hub joignable n'est pas une faute
        self.assertEqual(self.verify(JOIGNABLES="10.78.0.11 10.78.0.12 10.77.0.1").returncode, 0)

    def test_verify_fails_on_public_database(self):
        for ecoute in ("0.0.0.0:5432", "*:5432", "[::]:5432", "51.15.137.245:5432"):
            out = self.verify(ECOUTES="127.0.0.1:5432 %s" % ecoute)
            self.assertEqual(out.returncode, 2, ecoute)
            self.assertIn("base exposée", out.stderr)

    def test_verify_fails_on_unreachable_peer_or_unready_postgres(self):
        self.assertEqual(self.verify(JOIGNABLES="10.78.0.11").returncode, 2)
        out = self.verify(PG_OUVERTS="10.78.0.12")
        self.assertEqual(out.returncode, 2)
        self.assertIn("Postgres pas prêt", out.stderr)

    def test_verify_refuses_peer_outside_subnet(self):
        out = self.run_script("verifier", "10.78.0.0/24", "10.77.0.3")
        self.assertEqual(out.returncode, 2)


if __name__ == "__main__":
    unittest.main()
