# SPDX-License-Identifier: AGPL-3.0-only
"""#5494: the versioned staging quiescence probe, with a simulated ssh.

No real host is contacted: `ssh` is a stub that answers a canned report per
target and records every call. One test runs the read-only collector for real
on the local host, with a stub `ss`.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/mise-a-jour/quiescence-staging.sh"
TOPOLOGIE = ("ameesh-agents-1=root@10.77.0.3 ameesh-agents-2=root@10.77.0.4 "
             "ameesh-agents-3=root@10.77.0.5 manaty-mesh-1=root@10.77.0.1 pc-smichea=poste")

SSH = r'''#!/usr/bin/env bash
# simulated ssh: last option-free words are the target then the command
args=("$@"); cible=""
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in -o) i=$((i+1)) ;; -*) ;; *) cible=${args[$i]}; commande=${args[$((i+1))]}; break ;; esac
done
cat > /dev/null
printf '%s\t%s\n' "$cible" "$commande" >> "$FAUX/appels"
[ -e "$FAUX/$cible.rc" ] && exit "$(cat "$FAUX/$cible.rc")"
[ -e "$FAUX/$cible.out" ] && cat "$FAUX/$cible.out"
[ -e "$FAUX/$cible.incomplet" ] || echo ok
'''

NOTIFIER = r'''#!/usr/bin/env bash
printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$FAUX/alertes"
'''


class QuiescenceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="5494-quiescence-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.faux = self.root / "faux"
        self.faux.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for nom, texte in (("ssh", SSH), ("notifier", NOTIFIER)):
            (self.bin / nom).write_text(texte)
            (self.bin / nom).chmod(0o755)
        self.prive = self.root / "prive"
        self.prive.mkdir(mode=0o700)
        self.bascule = self.prive / "bascule.env"
        self.bascule.write_text('HOTES_STAGING="%s"\nHOTE_LOCAL=ameesh-agents-2\n' % TOPOLOGIE)
        self.env = dict(
            os.environ, PATH="%s:%s" % (self.bin, os.environ["PATH"]),
            HOME=str(self.root), FAUX=str(self.faux), BASCULE_ENV=str(self.bascule),
            STAGING_PREVIOUS_HOST="ameesh-agents-1", STAGING_PREVIOUS_RUN="ab12cd",
            QUIESCENCE_NOTIFIER=str(self.bin / "notifier"),
            QUIESCENCE_TRAVAIL=str(self.root / "travail"))

    def sonde(self, **env):
        return subprocess.run(["bash", str(SCRIPT)], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=60)

    def rapporter(self, cible, texte):
        (self.faux / (cible + ".out")).write_text(texte + "\n")

    def appels(self):
        fichier = self.faux / "appels"
        if not fichier.exists():
            return []
        return [ligne.split("\t", 1) for ligne in fichier.read_text().splitlines()]

    def alertes(self):
        fichier = self.faux / "alertes"
        return fichier.read_text().splitlines() if fichier.exists() else []

    def test_quiescence_prouvee_sur_les_quatre_vm(self):
        res = self.sonde()
        self.assertEqual(res.returncode, 0, res.stderr)
        appels = self.appels()
        self.assertEqual([cible for cible, _ in appels],
                         ["root@10.77.0.3", "root@10.77.0.4", "root@10.77.0.5",
                          "root@10.77.0.1"])
        # previous host first, with its own address for the ssh sessions
        self.assertTrue(appels[0][1].startswith("bash -s -- precedent ab12cd - 0 10.77.0.3"),
                        appels[0][1])
        for _, commande in appels[2:]:
            self.assertTrue(commande.startswith("bash -s -- cible ab12cd - 0 10.77.0.3"),
                            commande)
        # the current host allows only the calling executor and the probe's session
        self.assertNotIn(" - 0 ", appels[1][1])
        self.assertIn("poste sans cible ssh", res.stderr)
        self.assertEqual(self.alertes(), [])

    def test_chaque_fait_bloque_et_alerte_le_pilote_et_le_bureau(self):
        cas = [
            ("root@10.77.0.3", "etranger\t1001\t4242\tbash staging.sh"),
            ("root@10.77.0.3", "run\t4243\tbash /h/ameesh-release-staging-ab12cd-x/worktree/deploy/mise-a-jour/vm.sh"),
            ("root@10.77.0.3", "verrou\t/home/s/.local/state/ameesh/mise-a-jour/staging.lock"),
            ("root@10.77.0.3", "retour\t/home/s/ameesh-release-staging-ab12cd-x/worktree/.retour-apres-arret"),
            ("root@10.77.0.3", "poste\tposte-maj-staging-12-retour.service"),
            ("root@10.77.0.5", "session\t10.77.0.3\troot"),
            ("root@10.77.0.1", "operation\t77\tpython3 /root/.cache/ameesh/bascule-9.py activer"),
            ("root@10.77.0.4", "illisible\tss absent"),
        ]
        for cible, fait in cas:
            with self.subTest(fait=fait.split("\t")[0]):
                for f in self.faux.iterdir():
                    f.unlink()
                self.rapporter(cible, fait)
                res = self.sonde(STAGING_PREVIOUS_RUN="run-" + fait.split("\t")[0])
                self.assertEqual(res.returncode, 1, res.stderr)
                self.assertIn(fait.split("\t")[1], res.stderr)
                alertes = self.alertes()
                self.assertEqual([a.split("\t")[0] for a in alertes], ["mesh-design", "bureau"])
                self.assertIn("Aucune mutation", alertes[0])

    def test_alerte_par_defaut_avec_le_python_du_venv_de_lexecutant(self):
        # B1 (docs-01): without QUIESCENCE_NOTIFIER, the mail goes through the
        # executing account's stable venv, never the python3 of the unit PATH.
        venv = self.root / ".local/share/ameesh/venv/bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text(
            '#!/usr/bin/env bash\n'
            'if [ "$1" = - ]; then cat > /dev/null; printf "%s\\n" "$2" >> "$FAUX/python"; fi\n')
        (venv / "python").chmod(0o755)
        (self.faux / "root@10.77.0.3.rc").write_text("255")
        env = dict(self.env, PATH="/usr/bin:/bin")
        env.pop("QUIESCENCE_NOTIFIER")
        res = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(res.returncode, 1)
        self.assertNotIn("non remise", res.stderr)
        self.assertEqual((self.faux / "python").read_text().split(), ["mesh-design", "bureau"])

    def test_hote_precedent_injoignable_bloque_sans_contacter_les_cibles(self):
        (self.faux / "root@10.77.0.3.rc").write_text("255")
        res = self.sonde()
        self.assertEqual(res.returncode, 1)
        self.assertIn("ameesh-agents-1 injoignable", res.stderr)
        self.assertEqual([c for c, _ in self.appels()], ["root@10.77.0.3"])
        self.assertEqual(len(self.alertes()), 2)

    def test_cible_injoignable_ou_rapport_incomplet_bloque(self):
        (self.faux / "root@10.77.0.1.rc").write_text("255")
        res = self.sonde()
        self.assertEqual(res.returncode, 1)
        self.assertIn("cible manaty-mesh-1 injoignable", res.stderr)
        (self.faux / "root@10.77.0.1.rc").unlink()
        (self.faux / "root@10.77.0.5.incomplet").touch()
        res = self.sonde(STAGING_PREVIOUS_RUN="ab12ce")
        self.assertEqual(res.returncode, 1)
        self.assertIn("cible ameesh-agents-3 injoignable ou rapport incomplet", res.stderr)

    def test_hote_absent_declare_nest_pas_verifie(self):
        (self.faux / "root@10.77.0.1.rc").write_text("255")
        res = self.sonde(QUIESCENCE_ABSENTS="manaty-mesh-1")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("root@10.77.0.1", [c for c, _ in self.appels()])

    def test_poste_declare_joignable_ou_non(self):
        env = dict(QUIESCENCE_SSH_PC_SMICHEA="smichea@10.77.0.9")
        (self.faux / "smichea@10.77.0.9.rc").write_text("255")
        res = self.sonde(**env)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("poste injoignable, non verifie", res.stderr)
        (self.faux / "smichea@10.77.0.9.rc").unlink()
        self.rapporter("smichea@10.77.0.9", "poste\tposte-maj-staging-12-redemarrer.service")
        res = self.sonde(**env)
        self.assertEqual(res.returncode, 1)
        self.assertIn("poste-maj-staging-12-redemarrer", res.stderr)
        self.assertTrue(self.appels()[-1][1].startswith("bash -s -- poste "))

    def test_hote_precedent_hors_topologie_ou_entree_absente(self):
        res = self.sonde(STAGING_PREVIOUS_HOST="bureau-goal")
        self.assertEqual(res.returncode, 1)
        self.assertIn("hors de la topologie", res.stderr)
        self.assertEqual(self.appels(), [])
        res = self.sonde(STAGING_PREVIOUS_RUN="")
        self.assertEqual(res.returncode, 1)
        self.assertIn("STAGING_PREVIOUS_RUN absent", res.stderr)

    def test_une_alerte_par_run_et_par_raison(self):
        (self.faux / "root@10.77.0.3.rc").write_text("255")
        self.sonde()
        self.sonde()
        self.assertEqual(len(self.alertes()), 2)
        (self.faux / "root@10.77.0.3.rc").unlink()
        self.rapporter("root@10.77.0.3", "verrou\t/x/staging.lock")
        self.sonde()
        self.assertEqual(len(self.alertes()), 4)
        (self.faux / "root@10.77.0.3.out").unlink()
        self.assertEqual(self.sonde().returncode, 0)
        self.assertFalse((self.root / "travail" / "quiescence-ab12cd.alerte").exists())

    def test_topologie_composee_des_fichiers_par_vm(self):
        self.bascule.write_text("HOTE_LOCAL=ameesh-agents-2\nSERVEUR_SSH=root@10.77.0.1\n")
        for fichier, nom, adresse in (("bascule-a1.env", "ameesh-agents-1", "10.77.0.3"),
                                      ("bascule-a2.env", "ameesh-agents-2", "10.77.0.4"),
                                      ("bascule-agents-3.env", "ameesh-agents-3", "10.77.0.5")):
            (self.prive / fichier).write_text(
                "AGENTS_SSH=root@%s\nHOTE_AGENTS=%s\n" % (adresse, nom))
        res = self.sonde()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(self.appels()), 4)
        (self.prive / "bascule-a2.env").unlink()
        res = self.sonde(STAGING_PREVIOUS_RUN="ab12cf")
        self.assertEqual(res.returncode, 1)
        self.assertIn("topologie illisible", res.stderr)

    def test_collecteur_reel_en_local_lit_les_sessions_ssh(self):
        # The previous host is the current one, without ssh target: the real
        # read-only collector runs locally; `ss` is stubbed.
        (self.bin / "ss").write_text(
            "#!/usr/bin/env bash\ncat \"$FAUX/ss\" 2>/dev/null\n")
        (self.bin / "ss").chmod(0o755)
        topologie = TOPOLOGIE.replace("pc-smichea=poste", "pc-smichea=local")
        self.bascule.write_text('HOTES_STAGING="%s"\nHOTE_LOCAL=pc-smichea\n' % topologie)
        env = dict(STAGING_PREVIOUS_HOST="pc-smichea",
                   QUIESCENCE_ADRESSES_PC_SMICHEA="192.0.2.7")
        (self.faux / "ss").write_text(
            '0 0 10.0.0.9:22 198.51.100.4:5555 users:(("sshd",pid=1,fd=4))\n')
        res = self.sonde(**env)
        self.assertEqual(res.returncode, 0, res.stderr)
        (self.faux / "ss").write_text(
            '0 0 10.0.0.9:22 [::ffff:192.0.2.7]:5555 users:(("sshd",pid=1,fd=4))\n')
        res = self.sonde(**env)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("session 192.0.2.7 ?", res.stderr)
        # on the remote VMs the stubbed ssh answered for (b)
        self.assertEqual(len(self.appels()), 8)
        # impl-01 N: a ss that prints the State column is still read
        (self.faux / "ss").write_text(
            'ESTAB 0 0 10.0.0.9:22 192.0.2.7:5555 users:(("sshd",pid=1,fd=4))\n')
        res = self.sonde(STAGING_PREVIOUS_RUN="ab12d1", **env)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("session 192.0.2.7 ?", res.stderr)
        (self.faux / "ss").write_text("garbage\n")
        res = self.sonde(STAGING_PREVIOUS_RUN="ab12d2", **env)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("ligne ss: garbage", res.stderr)
        # B2 (docs-01): a failing ss proves nothing
        (self.bin / "ss").write_text("#!/usr/bin/env bash\nexit 1\n")
        res = self.sonde(STAGING_PREVIOUS_RUN="ab12d0", **env)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("ss en echec", res.stderr)


if __name__ == "__main__":
    unittest.main()
