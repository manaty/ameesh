# SPDX-License-Identifier: AGPL-3.0-only
"""#6858 (EP8.1) — archivage WAL hors hôte et restauration à un instant.

Le vrai `deploy/serveur/pgbackrest/pgbackrest-ameesh.sh` est exécuté. Les
chemins système sont redirigés par AMEESH_PGBR_* vers un dossier temporaire ;
`pgbackrest`, `systemctl`, les binaires Postgres et `ameesh` sont faux et
notent leurs arguments. Aucune base, aucun réseau, aucune unité réelle.
"""
from __future__ import annotations

import getpass
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIR = ROOT / "deploy/serveur/pgbackrest"
SCRIPT = DIR / "pgbackrest-ameesh.sh"

# Les outils de Postgres refusent un autre compte que celui de Postgres, comme
# initdb et pg_ctl refusent root ; `runuser` faux passe le compte par RUNAS.
FAKE = """#!/bin/sh
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$JOURNAL"
case "$(basename "$0")" in
  ameesh) printf 'ameesh-config %s\\n' "$AMEESH_CONFIG" >> "$JOURNAL"
          cat "$AMEESH_CONFIG" >> "$JOURNAL" ;;
  runuser) u=$2; shift 3; RUNAS=$u exec "$@" ;;
  pgbackrest|pg_ctl|psql|initdb|pg_restore|pg_isready)
    if [ "${RUNAS:-$(id -un)}" != "$PG_ATTENDU" ]; then
      echo "$(basename "$0") refusé sous ${RUNAS:-$(id -un)}" >&2; exit 1
    fi ;;
esac
case "$(basename "$0")" in
  pg_restore) printf 'entrée %s\\n' "$(cat)" >> "$JOURNAL" ;;
esac
exit 0
"""

ENV_OK = """PGBR_S3_BUCKET=ameesh-wal
PGBR_S3_REGION=nl-ams
PGBR_S3_KEY=SCWKEY
PGBR_S3_KEY_SECRET=secret-s3
PGBR_CIPHER_PASS=phrase-chiffrement
"""


class PgBackRestAmeesh(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="t6858-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name in ("pgbackrest", "systemctl", "pg_isready", "pg_ctl", "psql",
                     "initdb", "pg_restore", "ameesh", "runuser"):
            p = self.bin / name
            p.write_text(FAKE)
            p.chmod(0o755)
        self.journal = self.tmp / "journal"
        self.journal.touch()
        self.pgconf = self.tmp / "pgconf"
        self.pgdata = self.tmp / "pgdata"
        self.pgdata.mkdir()
        self.envfile = self.tmp / "pgbackrest.env"
        self.etc = self.tmp / "etc-pgbackrest"
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.tmp),
            "JOURNAL": str(self.journal),
            "AMEESH_PG_BIN": str(self.bin),
            "AMEESH_PGBR_PG_CONF": str(self.pgconf),
            "AMEESH_PGBR_PG_DATA": str(self.pgdata),
            "AMEESH_PGBR_PG_USER": getpass.getuser(),
            "PG_ATTENDU": getpass.getuser(),
            "AMEESH_PGBR_ENV": str(self.envfile),
            "AMEESH_PGBR_ETC": str(self.etc),
            "AMEESH_PGBR_SPOOL": str(self.tmp / "spool"),
            "AMEESH_PGBR_LOGS": str(self.tmp / "logs"),
        }

    def run_script(self, *args):
        return subprocess.run(["bash", str(SCRIPT), *args], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def calls(self):
        return self.journal.read_text().splitlines()

    # --- configurer ------------------------------------------------------------
    def test_configurer_sans_env_refuse(self):
        r = self.run_script("configurer")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("bureau confirme le bucket", r.stderr)
        self.assertFalse((self.etc / "pgbackrest.conf").exists())

    def test_configurer_ecrit_conf_privee(self):
        self.envfile.write_text(ENV_OK)
        r = self.run_script("configurer")
        self.assertEqual(r.returncode, 0, r.stderr)
        conf = self.etc / "pgbackrest.conf"
        self.assertEqual(stat.S_IMODE(conf.stat().st_mode), 0o640)
        text = conf.read_text()
        for line in ("repo1-type=s3", "repo1-s3-bucket=ameesh-wal",
                     "repo1-s3-endpoint=s3.nl-ams.scw.cloud", "repo1-s3-region=nl-ams",
                     "repo1-cipher-type=aes-256-cbc", "repo1-cipher-pass=phrase-chiffrement",
                     "repo1-retention-full-type=time", "repo1-retention-full=14",
                     "archive-push-queue-max=4GiB", "[ameesh]", f"pg1-path={self.pgdata}"):
            self.assertIn(line + "\n", text)
        # configurer ne touche ni Postgres ni systemd
        self.assertFalse(self.pgconf.exists())
        self.assertEqual(self.calls(), [])
        self.assertNotIn("secret-s3", r.stderr)

    def test_configurer_refuse_variable_manquante(self):
        self.envfile.write_text(ENV_OK.replace("PGBR_CIPHER_PASS=phrase-chiffrement\n", ""))
        r = self.run_script("configurer")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("PGBR_CIPHER_PASS manquant", r.stderr)

    def test_configurer_refuse_retour_a_la_ligne(self):
        self.envfile.write_text(ENV_OK + "PGBR_REPO_PATH=$'/a\\nrepo1-type=posix'\n")
        r = self.run_script("configurer")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("retour à la ligne refusé", r.stderr)
        self.assertFalse((self.etc / "pgbackrest.conf").exists())

    def test_stanza_personnalisee_refusee(self):
        # seule `configurer` lit le fichier : une autre stanza y divergerait
        # des minuteries et des autres commandes (relecture ameesh-cx2, B2)
        self.envfile.write_text(ENV_OK + "PGBR_STANZA=autre\n")
        r = self.run_script("configurer")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("PGBR_STANZA refusé", r.stderr)
        self.assertFalse((self.etc / "pgbackrest.conf").exists())

    def test_configurer_refuse_bucket_invalide(self):
        self.envfile.write_text(ENV_OK.replace("ameesh-wal", "Bucket_Invalide"))
        self.assertNotEqual(self.run_script("configurer").returncode, 0)

    # --- activer / desactiver --------------------------------------------------
    def configure(self):
        self.envfile.write_text(ENV_OK)
        self.assertEqual(self.run_script("configurer").returncode, 0)

    def test_activer_sans_conf_refuse(self):
        r = self.run_script("activer")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_activer_sans_redemarrage_ne_redemarre_rien(self):
        self.configure()
        r = self.run_script("activer")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls(), ["pgbackrest --stanza=ameesh stanza-create"])
        conf = (self.pgconf / "conf.d/ameesh-archivage.conf").read_text()
        self.assertIn("archive_mode = on\n", conf)
        self.assertIn("wal_log_hints = on\n", conf)
        self.assertIn("archive_command = 'pgbackrest --stanza=ameesh archive-push %p'\n", conf)

    def test_activer_avec_redemarrage(self):
        self.configure()
        r = self.run_script("activer", "--redemarrer")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = [c for c in self.calls() if not c.startswith("pg_isready")]
        self.assertEqual(calls, [
            "pgbackrest --stanza=ameesh stanza-create",
            "systemctl restart postgresql@17-main",
            "pgbackrest --stanza=ameesh check",
            "pgbackrest --stanza=ameesh --type=full backup",
            "systemctl daemon-reload",
            "systemctl enable --now ameesh-pgbackrest-full.timer ameesh-pgbackrest-diff.timer",
        ])

    def test_desactiver_retour_arriere(self):
        self.configure()
        self.run_script("activer")
        r = self.run_script("desactiver")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.pgconf / "conf.d/ameesh-archivage.conf").exists())
        self.assertIn("systemctl disable --now ameesh-pgbackrest-full.timer "
                      "ameesh-pgbackrest-diff.timer", self.calls())
        self.assertFalse(any("restart" in c for c in self.calls()))
        self.run_script("desactiver", "--redemarrer")
        self.assertIn("systemctl restart postgresql@17-main", self.calls())

    def test_sauvegarder_type_borne(self):
        self.assertNotEqual(self.run_script("sauvegarder", "incr").returncode, 0)
        self.assertEqual(self.run_script("sauvegarder", "diff").returncode, 0)
        self.assertEqual(self.calls(), ["pgbackrest --stanza=ameesh --type=diff backup"])

    # --- exercices ---------------------------------------------------------------
    def test_restaurer_refuse_la_base_vive(self):
        for dossier in (self.pgdata, self.pgdata / "sous", self.tmp):
            r = self.run_script("restaurer", "--cible", "2026-11-11 08:00:00+00",
                                "--dossier", str(dossier))
            self.assertNotEqual(r.returncode, 0, dossier)
            self.assertIn("base vive", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_restaurer_refuse_dossier_non_vide_et_port_vif(self):
        d = self.tmp / "plein"
        d.mkdir()
        (d / "x").touch()
        r = self.run_script("restaurer", "--cible", "x", "--dossier", str(d))
        self.assertIn("n'est pas vide", r.stderr)
        r = self.run_script("restaurer", "--cible", "x", "--dossier", str(self.tmp / "v"),
                            "--port", "5432")
        self.assertIn("jamais 5432", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_restaurer_a_un_instant(self):
        d = self.tmp / "exercice"
        r = self.run_script("restaurer", "--cible", "2026-11-11 08:00:00+00",
                            "--dossier", str(d), "--port", "55433")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        self.assertEqual(calls[0], f"pgbackrest --stanza=ameesh --pg1-path={d} --type=time "
                         "--target=2026-11-11 08:00:00+00 --target-action=promote "
                         "--archive-mode=off --delta=n restore")
        self.assertIn("ameesh doctor --probe", calls)
        self.assertTrue(any(f"host={d}&port=55433" in c for c in calls), calls)
        self.assertTrue(any(c.startswith("pg_ctl -D") and c.endswith("stop") for c in calls))
        self.assertIn("RTO mesuré", r.stderr)
        self.assertFalse(d.exists())  # instance jetable effacée sans --garder

    def test_exercice_dump_garder(self):
        dump = self.tmp / "ameesh.dump"
        dump.write_bytes(b"PGDMP")
        d = self.tmp / "exercice"
        r = self.run_script("exercice-dump", "--fichier", str(dump), "--dossier", str(d),
                            "--garder")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"pg_restore -h {d} -p 55432 -U postgres -d ameesh --no-acl "
                      "--exit-on-error", self.calls())
        self.assertIn("entrée PGDMP", self.calls())
        self.assertIn("ameesh doctor --probe", self.calls())
        conf = (d / "postgresql.conf").read_text()
        self.assertIn("listen_addresses = ''", conf)
        self.assertIn("archive_mode = off", conf)

    def test_exercices_sous_le_compte_de_postgres(self):
        # lancés par un autre compte (root en vrai), les outils de Postgres
        # passent par runuser ; appelés directement, les faux refusent
        # (relecture ameesh-cx2, B1)
        self.env["AMEESH_PGBR_PG_USER"] = self.env["PG_ATTENDU"] = "pgfaux"
        dump = self.tmp / "ameesh.dump"
        dump.write_bytes(b"PGDMP")
        r = self.run_script("exercice-dump", "--fichier", str(dump),
                            "--dossier", str(self.tmp / "e1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_script("restaurer", "--cible", "2026-11-11 08:00:00+00",
                            "--dossier", str(self.tmp / "e2"))
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        for i, c in enumerate(calls):
            if c.split(" ")[0] in ("pgbackrest", "pg_ctl", "psql", "initdb", "pg_restore"):
                self.assertTrue(calls[i - 1].startswith("runuser -u pgfaux -- "), c)
        self.assertGreaterEqual(sum(c.startswith("runuser -u pgfaux -- ") for c in calls), 10)

    def test_faux_outils_refusent_un_autre_compte(self):
        self.env["PG_ATTENDU"] = "pgfaux"
        r = subprocess.run([str(self.bin / "initdb"), "-D", "x"], env=self.env,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)

    # --- unités et installateur ------------------------------------------------
    def test_unites(self):
        svc = (DIR / "ameesh-pgbackrest@.service").read_text()
        self.assertIn("User=postgres\n", svc)
        self.assertIn("ConditionPathExists=/etc/postgresql/17/main/conf.d/ameesh-archivage.conf\n", svc)
        self.assertIn("ExecStart=/usr/local/sbin/ameesh-pgbackrest sauvegarder %i\n", svc)
        self.assertIn("Unit=ameesh-pgbackrest@full.service\n",
                      (DIR / "ameesh-pgbackrest-full.timer").read_text())
        self.assertIn("Unit=ameesh-pgbackrest@diff.service\n",
                      (DIR / "ameesh-pgbackrest-diff.timer").read_text())

    def test_installateur_n_active_pas(self):
        inst = (ROOT / "deploy/serveur/installer-serveur.sh").read_text()
        self.assertIn("/usr/local/sbin/ameesh-pgbackrest", inst)
        self.assertNotIn("ameesh-pgbackrest activer", inst.replace(
            "`ameesh-pgbackrest activer --redemarrer`", ""))


if __name__ == "__main__":
    unittest.main()
