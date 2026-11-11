# SPDX-License-Identifier: AGPL-3.0-only
"""#6868 : scripts de réplication PostgreSQL 17 (EP8.3a), témoins sur des faux.

Aucun hôte réel, aucun réseau : `psql`, `pg_ctlcluster`, `pg_isready`,
`pg_basebackup`, `pg_rewind`, `runuser` et `hostname` sont des doublures sous
un répertoire jetable, pilotées par des fichiers d'état. Les témoins portent
sur les points sensibles du lot (SQL, droits) :

- l'inventaire (hub nosync/nofailover, primaire `ameesh-agents-1`, `ANY 1`
  limité aux deux VM d'agents) et ses refus ;
- la configuration idempotente (`conf.d`, bloc marqué de `pg_hba`, rechargement,
  redémarrage seulement sur demande) ;
- l'amorçage sans standby : liste synchrone vide (les COMMIT n'attendent pas),
  puis activation avec un candidat en flux, et `--sans-synchrone` ;
- les rôles `replicator` (LOGIN REPLICATION) et `rewind` (quatre fonctions,
  pas superuser), les slots, et le pgpass 0600 ;
- la promotion manuelle (split brain, nofailover, borne WAL rejouée ; un
  candidat en retard est refusé, `--force` dit la perte possible) ;
- le fichier de secrets doit appartenir à l'utilisateur effectif avant source ;
- `pg_basebackup -X stream -S <slot> -R` et le re-seed qui déplace les données ;
- `pg_rewind`, la re-pose de la configuration copiée par le rewind et l'échec
  qui n'est pas masqué ;
- le retour arrière sans perte de données, grants pg_rewind révoqués avant
  `DROP ROLE` ;
- `etat.sh` : colonnes réelles de `pg_stat_wal_receiver` (pas de `*_lag`) ;
- aucun mot de passe en argument de processus.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPLICATION = ROOT / "deploy" / "serveur" / "replication"
SCRIPTS = tuple(sorted(REPLICATION.glob("*.sh")))

REPLICATOR_PASSWORD = "Replicator-Secret-0123456789abcdef"
REWIND_PASSWORD = "Rewind-Secret-0123456789abcdef"

FAKE_PSQL = r'''
import os
import pathlib
import re
import sys

state = pathlib.Path(os.environ["FAKE_STATE"])
journal = pathlib.Path(os.environ["FAKE_JOURNAL"])
stdin_log = pathlib.Path(os.environ["FAKE_STDIN"])

argv = sys.argv[1:]
with journal.open("a", encoding="utf-8") as out:
    out.write("psql " + " ".join(argv) + "\n")

hote = None
sql = None
i = 0
while i < len(argv):
    arg = argv[i]
    if arg == "-h":
        hote = argv[i + 1]
        i += 2
    elif arg in ("-c", "-tAc", "-Atc"):
        sql = argv[i + 1]
        i += 2
    elif arg in ("-d", "-U", "-p", "-v"):
        i += 2
    elif arg.startswith("-tAc") or arg.startswith("-Atc"):
        sql = argv[i + 1]
        i += 2
    else:
        i += 1

base = state if hote is None else state / "hotes" / hote
if hote is not None and not base.is_dir():
    sys.stderr.write("psql: error: connection to server refused\n")
    sys.exit(2)
if (base / "illisible").exists():
    sys.stderr.write("psql: error: role lookup failed\n")
    sys.exit(2)

DEFAUT = {
    "wal_level": "replica",
    "wal_log_hints": "on",
    "max_wal_senders": "10",
    "max_replication_slots": "10",
    "full_page_writes": "on",
    "ssl": "on",
    "listen_addresses": "localhost,ameesh-agents-1",
    "password_encryption": "scram-sha-256",
    "synchronous_standby_names": 'ANY 1 ("ameesh-agents-1", "ameesh-agents-2")',
}


def lire(nom, defaut=""):
    chemin = base / nom
    if chemin.exists():
        return chemin.read_text(encoding="utf-8").strip()
    return defaut


def ecrire(nom, texte):
    (base / nom).write_text(texte, encoding="utf-8")


def slots():
    chemin = base / "slots"
    if not chemin.exists():
        return []
    return [ligne.split("|") for ligne in chemin.read_text(encoding="utf-8").splitlines() if ligne]


def ecrire_slots(lignes):
    ecrire("slots", "".join("|".join(ligne) + "\n" for ligne in lignes))


def lsn(texte):
    haut, bas = texte.strip().split("/", 1)
    return (int(haut, 16) << 32) + int(bas, 16)


if sql is None:
    texte = sys.stdin.read()
    with stdin_log.open("a", encoding="utf-8") as out:
        out.write(texte + "\n--FIN--\n")
    for nom in ("replicator", "rewind"):
        if "CREATE ROLE " + nom in texte or "ALTER ROLE " + nom in texte:
            (base / ("role_" + nom)).touch()
    grants = base / "grants"
    if "GRANT EXECUTE" in texte:
        ecrire("grants", texte)
    if "REVOKE EXECUTE" in texte and grants.exists():
        # Elles ne dépendent plus du rôle : DROP ROLE peut passer.
        grants.unlink()
    if "DROP ROLE IF EXISTS rewind" in texte:
        if (base / "role_rewind").exists() and grants.exists():
            sys.stderr.write(
                'psql: error: role "rewind" cannot be dropped because some '
                "objects depend on it\n")
            sys.exit(1)
        (base / "role_rewind").unlink(missing_ok=True)
    if "DROP ROLE IF EXISTS replicator" in texte:
        (base / "role_replicator").unlink(missing_ok=True)
    sys.exit(0)

q = " ".join(sql.split())

if q.startswith("SHOW "):
    parametre = q.split()[1]
    print(lire("params/" + parametre, DEFAUT.get(parametre, "")))
elif "pg_is_in_recovery()" in q:
    print(lire("in_recovery", "f"))
elif "pg_create_physical_replication_slot" in q:
    nom = re.search(r"pg_create_physical_replication_slot\('([^']+)'", q).group(1)
    if not any(ligne[0] == nom for ligne in slots()):
        ecrire_slots(slots() + [[nom, "f", "reserved"]])
elif "pg_terminate_backend" in q and "pg_replication_slots" in q:
    nom = re.search(r"slot_name = '([^']+)'", q).group(1)
    ecrire_slots([[ligne[0], "f", ligne[2]] if ligne[0] == nom else ligne
                  for ligne in slots()])
    print("1")
elif "pg_drop_replication_slot" in q:
    nom = re.search(r"slot_name = '([^']+)'", q).group(1)
    ecrire_slots([ligne for ligne in slots()
                  if not (ligne[0] == nom and ligne[1] != "t")])
elif "SELECT active FROM pg_replication_slots" in q:
    nom = re.search(r"slot_name = '([^']+)'", q).group(1)
    for ligne in slots():
        if ligne[0] == nom:
            print(ligne[1])
elif "SELECT 1 FROM pg_replication_slots" in q:
    nom = re.search(r"slot_name = '([^']+)'", q).group(1)
    if any(ligne[0] == nom for ligne in slots()):
        print("1")
elif "SELECT slot_name, active, wal_status FROM pg_replication_slots" in q:
    for ligne in slots():
        print("|".join(ligne))
elif "SELECT application_name FROM pg_stat_replication" in q:
    # B1 : seuls les candidats nommés et réellement en streaming comptent.
    noms = set(re.findall(r"'([^']+)'", q.split(" IN (", 1)[1]))
    chemin = base / "replication"
    if chemin.exists():
        for ligne in chemin.read_text(encoding="utf-8").splitlines():
            champs = ligne.split("|")
            if len(champs) >= 2 and champs[0] in noms and champs[1] == "streaming":
                print(champs[0])
elif "FROM pg_stat_replication" in q:
    chemin = base / "replication"
    if chemin.exists():
        print(chemin.read_text(encoding="utf-8").rstrip("\n"))
elif "pg_last_wal_replay_lsn() >=" in q:
    borne = re.search(r"'([0-9A-Fa-f]+/[0-9A-Fa-f]+)'::pg_lsn", q).group(1)
    print("t" if lsn(lire("replay_lsn", "0/0")) >= lsn(borne) else "f")
elif "pg_last_wal_replay_lsn()" in q:
    print(lire("replay_lsn", "0/0"))
elif "pg_last_wal_receive_lsn()" in q:
    print(lire("receive_lsn", lire("replay_lsn", "0/0")))
elif "pg_current_wal_flush_lsn()" in q:
    print(lire("flush_lsn", "0/0"))
elif "pg_stat_wal_receiver" in q and re.search(
        r"\b(write_lag|flush_lag|replay_lag)\b", q):
    # B2 : ces colonnes n'existent pas dans pg_stat_wal_receiver en PG17 ;
    # la doublure refuse comme PostgreSQL, pour que le témoin ne les couvre
    # plus par accident.
    sys.stderr.write(
        'psql: error: column "write_lag" does not exist\n')
    sys.exit(1)
elif "coalesce(sender_host" in q:
    chemin = base / "receiver"
    if chemin.exists():
        print(chemin.read_text(encoding="utf-8").rstrip("\n"))
elif "SELECT status FROM pg_stat_wal_receiver" in q:
    chemin = base / "receiver"
    if chemin.exists():
        print(chemin.read_text(encoding="utf-8").split("|")[0])
else:
    sys.stderr.write("fake psql: requête non reconnue: " + q + "\n")
    sys.exit(1)
'''

FAKE_PG_ISREADY = r'''
import os
import pathlib
import sys

state = pathlib.Path(os.environ["FAKE_STATE"])
hote = None
args = sys.argv[1:]
for i, arg in enumerate(args):
    if arg == "-h":
        hote = args[i + 1]
base = state / "hotes" / hote if hote else state
sys.exit(0 if (base / "running").exists() else 1)
'''

FAKE_PG_CTLCLUSTER = r'''
import os
import pathlib
import sys

state = pathlib.Path(os.environ["FAKE_STATE"])
journal = pathlib.Path(os.environ["FAKE_JOURNAL"])
args = sys.argv[1:]
action = args[-1] if args else ""
with journal.open("a", encoding="utf-8") as out:
    out.write("pg_ctlcluster " + " ".join(args) + "\n")
if action == "promote":
    (state / "in_recovery").write_text("f", encoding="utf-8")
elif action == "start":
    (state / "running").touch()
    if not (state / "in_recovery").exists():
        (state / "in_recovery").write_text("t", encoding="utf-8")
elif action == "stop":
    (state / "running").unlink(missing_ok=True)
elif action == "restart":
    (state / "running").touch()
    apres = state / "apres_redemarrage"
    if apres.exists():
        params = state / "params"
        params.mkdir(exist_ok=True)
        for ligne in apres.read_text(encoding="utf-8").splitlines():
            if "=" in ligne:
                cle, valeur = ligne.split("=", 1)
                (params / cle).write_text(valeur, encoding="utf-8")
elif action != "reload":
    sys.stderr.write("fake pg_ctlcluster: action inconnue: " + action + "\n")
    sys.exit(2)
'''

FAKE_PG_BASEBACKUP = r'''
import os
import pathlib
import sys

journal = pathlib.Path(os.environ["FAKE_JOURNAL"])
args = sys.argv[1:]
with journal.open("a", encoding="utf-8") as out:
    out.write("pg_basebackup " + " ".join(args) + "\n")
if os.environ.get("FAKE_ECHEC_BASEBACKUP"):
    sys.exit(1)
cible = None
slot = None
i = 0
while i < len(args):
    if args[i] in ("-D", "--pgdata"):
        cible = args[i + 1]
        i += 2
    elif args[i] in ("-S", "--slot"):
        slot = args[i + 1]
        i += 2
    else:
        i += 1
if not cible:
    sys.stderr.write("fake pg_basebackup: -D manquant\n")
    sys.exit(2)
data = pathlib.Path(cible)
data.mkdir(parents=True, exist_ok=True)
(data / "PG_VERSION").write_text("17\n", encoding="utf-8")
(data / "postgresql.auto.conf").write_text(
    "primary_conninfo = 'host=ancienne-source user=replicator'\n", encoding="utf-8")
(data / "standby.signal").touch()
(data / "BASEBACKUP").write_text("slot=" + (slot or ""), encoding="utf-8")
'''

FAKE_PG_REWIND = r'''
import os
import pathlib
import sys

journal = pathlib.Path(os.environ["FAKE_JOURNAL"])
args = sys.argv[1:]
with journal.open("a", encoding="utf-8") as out:
    out.write("pg_rewind " + " ".join(args) + "\n")
if os.environ.get("FAKE_ECHEC_REWIND"):
    sys.exit(1)
cible = None
for arg in args:
    if arg.startswith("--target-pgdata="):
        cible = arg.split("=", 1)[1]
if not cible:
    sys.stderr.write("fake pg_rewind: --target-pgdata manquant\n")
    sys.exit(2)
data = pathlib.Path(cible)
data.mkdir(parents=True, exist_ok=True)
(data / "PG_VERSION").write_text("17\n", encoding="utf-8")
(data / "conf.d").mkdir(exist_ok=True)
(data / "conf.d" / "60-ameesh-replication.conf").write_text(
    "# configuration copiée de la source\n", encoding="utf-8")
with (data / "postgresql.auto.conf").open("a", encoding="utf-8") as out:
    out.write("primary_conninfo = 'host=ancienne-source user=replicator'\n")
'''

FAKE_RUNUSER = r'''
import os
import sys

args = sys.argv[1:]
if "--" in args:
    args = args[args.index("--") + 1:]
os.execvp(args[0], args)
'''

FAKE_HOSTNAME = r'''
import os
print(os.environ.get("AMEESH_REPLICATION_NOM", "ameesh-agents-2"))
'''


@unittest.skipUnless(
    sys.platform.startswith("linux"),
    "scripts Debian 12 (pg_ctlcluster, GNU stat/chmod) : témoins sous Linux")
class ReplicationTest(unittest.TestCase):
    """Banc : arborescence jetable, doublures dans PATH, état en fichiers."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="test-6868-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.pgbin = self.tmp / "pgbin"
        self.etc = self.tmp / "etc"
        self.pgconf = self.tmp / "pgconf"
        self.data = self.tmp / "data"
        self.state = self.tmp / "state"
        self.journal = self.tmp / "journal"
        self.stdin_log = self.tmp / "stdin.log"
        self.pgpass = self.tmp / "pgpass"
        for dossier in (self.bin, self.pgbin, self.etc, self.pgconf / "conf.d",
                        self.state / "params"):
            dossier.mkdir(parents=True)
        self.stub("psql", FAKE_PSQL)
        self.stub("pg_isready", FAKE_PG_ISREADY)
        self.stub("pg_ctlcluster", FAKE_PG_CTLCLUSTER)
        self.stub("runuser", FAKE_RUNUSER)
        self.stub("hostname", FAKE_HOSTNAME)
        # Les doublures adressent les nœuds par leur nom : copie de
        # l'inventaire réel où l'hôte est le nom (les adresses privées #6859
        # sont témoignées par test_inventaire_reseau_prive).
        self.noeuds = self.tmp / "noeuds.conf"
        lignes = []
        for ligne in (REPLICATION / "noeuds.conf").read_text(encoding="utf-8").splitlines():
            champs = ligne.split("#", 1)[0].split()
            if champs:
                champs[1] = champs[0]
                ligne = "  ".join(champs)
            lignes.append(ligne)
        self.noeuds.write_text("\n".join(lignes) + "\n", encoding="utf-8")
        self.stub("pg_basebackup", FAKE_PG_BASEBACKUP)
        self.stub("pg_rewind", FAKE_PG_REWIND)
        (self.pgconf / "pg_hba.conf").write_text(
            "local   all   postgres   peer\n"
            "hostssl ameesh all 10.77.0.0/24 scram-sha-256\n", encoding="utf-8")
        (self.pgconf / "postgresql.conf").write_text("# config de test\n", encoding="utf-8")
        self.env_file = self.etc / "replication.env"
        self.ecrire_env()
        self.ecrire_pgpass()
        self.env = self.base_env()

    # --- outils du banc ------------------------------------------------------

    def stub(self, nom, corps):
        dossier = self.pgbin if nom in ("pg_basebackup", "pg_rewind") else self.bin
        chemin = dossier / nom
        chemin.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(corps).lstrip(),
                          encoding="utf-8")
        chemin.chmod(0o755)

    def ecrire_env(self, contenu=None):
        self.env_file.write_text(contenu if contenu is not None else textwrap.dedent(f'''\
            REPLICATION_SUBNET='10.77.0.0/24'
            REPLICATOR_PASSWORD='{REPLICATOR_PASSWORD}'
            REWIND_PASSWORD='{REWIND_PASSWORD}'
            '''), encoding="utf-8")
        self.env_file.chmod(0o600)

    def ecrire_pgpass(self, contenu=None):
        self.pgpass.write_text(contenu if contenu is not None else textwrap.dedent(f'''\
            *:5432:replication:replicator:{REPLICATOR_PASSWORD}
            *:5432:postgres:rewind:{REWIND_PASSWORD}
            '''), encoding="utf-8")
        self.pgpass.chmod(0o600)

    def base_env(self):
        env = {cle: valeur for cle, valeur in os.environ.items()
               if not cle.startswith(("AMEESH_REPLICATION_", "AGENT_MESH_", "AGENT_MAIL_"))}
        env.update({
            "PATH": f"{self.bin}:{self.pgbin}:{os.environ.get('PATH', '')}",
            "TMPDIR": str(self.tmp),
            "LC_ALL": "C",
            "TZ": "UTC",
            "AMEESH_REPLICATION_ETC": str(self.etc),
            "AMEESH_REPLICATION_ENV": str(self.env_file),
            "AMEESH_REPLICATION_NOEUDS": str(self.noeuds),
            "AMEESH_REPLICATION_PGCONF": str(self.pgconf),
            "AMEESH_REPLICATION_DATA": str(self.data),
            "AMEESH_REPLICATION_PGBIN": str(self.pgbin),
            "AMEESH_REPLICATION_PGPASS": str(self.pgpass),
            "AMEESH_REPLICATION_ATTENTE": "2",
            "AMEESH_REPLICATION_RUNUSER": "oui",
            "AMEESH_REPLICATION_NOM": "ameesh-agents-2",
            "FAKE_STATE": str(self.state),
            "FAKE_JOURNAL": str(self.journal),
            "FAKE_STDIN": str(self.stdin_log),
        })
        return env

    def run_script(self, script, *args, env=None, attendu=0):
        environnement = dict(self.env)
        if env:
            environnement.update(env)
        resultat = subprocess.run(
            [str(REPLICATION / script), *map(str, args)],
            cwd=str(ROOT), env=environnement, text=True,
            capture_output=True, timeout=120)
        if attendu is not None:
            self.assertEqual(resultat.returncode, attendu,
                             f"{script} {args} : rc={resultat.returncode}\n"
                             f"stdout:\n{resultat.stdout}\nstderr:\n{resultat.stderr}")
        return resultat

    def journal_texte(self):
        return self.journal.read_text(encoding="utf-8") if self.journal.exists() else ""

    def stdin_texte(self):
        return self.stdin_log.read_text(encoding="utf-8") if self.stdin_log.exists() else ""

    # --- état simulé ---------------------------------------------------------

    def serveur(self, demarre=True, en_recuperation=None, base=None):
        base = pathlib.Path(base) if base is not None else self.state
        base.mkdir(parents=True, exist_ok=True)
        if demarre:
            (base / "running").touch()
        else:
            (base / "running").unlink(missing_ok=True)
        if en_recuperation is not None:
            (base / "in_recovery").write_text("t" if en_recuperation else "f",
                                              encoding="utf-8")

    def parametre(self, nom, valeur, base=None):
        base = pathlib.Path(base) if base is not None else self.state
        (base / "params").mkdir(parents=True, exist_ok=True)
        (base / "params" / nom).write_text(valeur, encoding="utf-8")

    def slots(self, lignes, base=None):
        base = pathlib.Path(base) if base is not None else self.state
        (base / "slots").write_text(
            "".join("|".join(ligne) + "\n" for ligne in lignes), encoding="utf-8")

    def hote_distant(self, hote, demarre=True, en_recuperation=None):
        base = self.state / "hotes" / hote
        base.mkdir(parents=True, exist_ok=True)
        self.serveur(demarre=demarre, en_recuperation=en_recuperation, base=base)
        (base / "params").mkdir(exist_ok=True)
        return base

    def donnees(self, avec_config=True):
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "PG_VERSION").write_text("17\n", encoding="utf-8")
        (self.data / "postgresql.auto.conf").write_text(
            "# auto.conf d'origine\n", encoding="utf-8")
        if avec_config:
            (self.data / "conf.d").mkdir(exist_ok=True)
            (self.data / "conf.d" / "60-ameesh-replication.conf").write_text(
                "# configuration locale d'origine\n", encoding="utf-8")

    def fragment(self):
        return self.pgconf / "conf.d" / "60-ameesh-replication.conf"

    def hba(self):
        return (self.pgconf / "pg_hba.conf").read_text(encoding="utf-8")

    def auto_conf(self):
        return (self.data / "postgresql.auto.conf").read_text(encoding="utf-8")

    # --- inventaire ----------------------------------------------------------
    def test_inventaire_cible(self):
        lignes = []
        for ligne in (REPLICATION / "noeuds.conf").read_text(encoding="utf-8").splitlines():
            ligne = ligne.split("#", 1)[0].strip()
            if ligne:
                lignes.append(ligne.split())
        self.assertEqual([ligne[0] for ligne in lignes],
                         ["ameesh-agents-1", "ameesh-agents-2", "manaty-mesh-1"])
        self.assertEqual(lignes[0][2:], ["primaire", "oui", "non", "100"])
        self.assertEqual(lignes[1][2:], ["standby", "oui", "non", "90"])
        self.assertEqual(lignes[2][2:], ["standby", "non", "oui", "0"])

    def test_inventaire_reseau_prive(self):
        # EP8.3 (#6860) : les nœuds se joignent sur les adresses privées
        # d'EP8.2 (#6859, RESEAU-PRIVE.md), jamais par un nom public.
        hotes = {}
        for ligne in (REPLICATION / "noeuds.conf").read_text(encoding="utf-8").splitlines():
            ligne = ligne.split("#", 1)[0].strip()
            if ligne:
                champs = ligne.split()
                hotes[champs[0]] = champs[1]
        self.assertEqual(hotes, {"ameesh-agents-1": "10.78.0.11",
                                 "ameesh-agents-2": "10.78.0.12",
                                 "manaty-mesh-1": "10.78.0.10"})

    def test_inventaire_refuse_deux_primaires_et_hub_synchrone(self):
        inventaire = self.tmp / "inventaire.cas"
        inventaire.write_text(textwrap.dedent("""\
            a1 a1 primaire oui non 100
            a2 a2 primaire oui non 90
            """), encoding="utf-8")
        resultat = self.run_script("configurer-noeud.sh", "a1", attendu=1,
                                   env={"AMEESH_REPLICATION_NOEUDS": str(inventaire)})
        self.assertIn("un seul primaire", resultat.stderr)

        inventaire.write_text(textwrap.dedent("""\
            a1 a1 primaire oui non 100
            hub hub standby oui oui 0
            """), encoding="utf-8")
        resultat = self.run_script("configurer-noeud.sh", "a1", attendu=1,
                                   env={"AMEESH_REPLICATION_NOEUDS": str(inventaire)})
        self.assertIn("nofailover et synchrone", resultat.stderr)

    # --- configuration -------------------------------------------------------
    def test_configurer_primaire_hub_roles_slots_et_idempotence(self):
        self.serveur(demarre=True, en_recuperation=False)
        self.parametre("wal_log_hints", "off")  # ancien réglage, avant redémarrage
        premier = self.run_script("configurer-noeud.sh", "manaty-mesh-1")
        self.assertIn("REDÉMARRAGE", premier.stdout)
        self.assertIn("pg_ctlcluster 17 main restart", premier.stdout)
        self.assertNotIn("pg_ctlcluster 17 main restart\n", self.journal_texte())

        fragment = self.fragment().read_text(encoding="utf-8")
        self.assertIn("wal_level = replica", fragment)
        self.assertIn("wal_log_hints = on", fragment)
        self.assertIn("full_page_writes = on", fragment)
        self.assertIn("hot_standby = on", fragment)
        self.assertIn("max_wal_senders = 10", fragment)
        self.assertIn("max_replication_slots = 10", fragment)
        self.assertIn("max_slot_wal_keep_size = '4GB'", fragment)
        self.assertIn("password_encryption = 'scram-sha-256'", fragment)
        # B1 de revue #6868 : aucun standby en flux à l'amorçage, donc liste
        # vide — les COMMIT (CREATE/ALTER ROLE, slots) n'attendent personne.
        self.assertIn("synchronous_standby_names = ''", fragment)
        self.assertIn("aucun candidat synchrone en flux", premier.stdout)

        hba = self.hba()
        self.assertEqual(hba.count("# BEGIN ameesh-replication (lot #6868)"), 1)
        self.assertEqual(hba.count("# END ameesh-replication (lot #6868)"), 1)
        self.assertIn("hostssl replication replicator 10.77.0.0/24 scram-sha-256", hba)
        self.assertIn("hostssl postgres    rewind     10.77.0.0/24 scram-sha-256", hba)
        self.assertIn("local   all   postgres   peer", hba)

        slots = (self.state / "slots").read_text(encoding="utf-8").split()
        self.assertEqual(sorted(ligne.split("|")[0] for ligne in slots),
                         ["ameesh_agents_1", "ameesh_agents_2"])
        self.assertTrue((self.state / "role_replicator").exists())
        self.assertTrue((self.state / "role_rewind").exists())

        stdin = self.stdin_texte()
        self.assertIn("CREATE ROLE replicator LOGIN REPLICATION NOSUPERUSER", stdin)
        self.assertIn("CREATE ROLE rewind LOGIN NOSUPERUSER", stdin)
        self.assertIn("GRANT EXECUTE ON FUNCTION pg_catalog.pg_ls_dir", stdin)
        self.assertIn("pg_read_binary_file(text, bigint, bigint, boolean)", stdin)
        self.assertIn(f"ALTER ROLE replicator PASSWORD '{REPLICATOR_PASSWORD}'", stdin)
        self.assertIn(f"ALTER ROLE rewind PASSWORD '{REWIND_PASSWORD}'", stdin)

        # Aucun mot de passe en argument d'un processus.
        for ligne in self.journal_texte().splitlines():
            self.assertNotIn(REPLICATOR_PASSWORD, ligne)
            self.assertNotIn(REWIND_PASSWORD, ligne)

        # Rejouable : un seul bloc pg_hba, les mêmes deux slots.
        self.run_script("configurer-noeud.sh", "manaty-mesh-1")
        self.assertEqual(self.hba().count("# BEGIN ameesh-replication (lot #6868)"), 1)
        self.assertEqual(len((self.state / "slots").read_text(encoding="utf-8").split()), 2)

        # Après la base backup, un candidat est en flux : un nouveau passage
        # pose ANY 1 (quorum en PG17), toujours sans le hub ni le nœud local.
        (self.state / "replication").write_text(
            "ameesh-agents-2|streaming|quorum|0/5000000|0/5000000\n",
            encoding="utf-8")
        active = self.run_script("configurer-noeud.sh", "manaty-mesh-1")
        fragment = self.fragment().read_text(encoding="utf-8")
        self.assertIn(
            "synchronous_standby_names = 'ANY 1 (\"ameesh-agents-1\", \"ameesh-agents-2\")'",
            fragment)
        ligne_sync = [ligne for ligne in fragment.splitlines()
                      if ligne.startswith("synchronous_standby_names")][0]
        self.assertNotIn("manaty-mesh-1", ligne_sync)
        self.assertIn("candidat(s) synchrone(s) en flux", active.stdout)

        # Geste d'urgence : --sans-synchrone dégrade tout de suite, même avec
        # un candidat en flux ; un passage normal le rétablit ensuite.
        degrade = self.run_script("configurer-noeud.sh", "--sans-synchrone",
                                  "manaty-mesh-1")
        self.assertIn("synchronous_standby_names = ''",
                      self.fragment().read_text(encoding="utf-8"))
        self.assertIn("synchrone désactivé", degrade.stdout)
        self.run_script("configurer-noeud.sh", "manaty-mesh-1")
        self.assertIn("ANY 1 (",
                      self.fragment().read_text(encoding="utf-8"))

    def test_configurer_primaire_sans_standby_amorce_sans_synchrone(self):
        """B1 : sans standby en flux, la préparation termine et n'attend pas.

        Le premier passage du hub (primaire actuel) doit poser rôles et slots
        alors qu'aucun standby n'existe encore : synchronous_standby_names
        reste vide. La liste vide est la condition qui empêche un COMMIT de
        partir en attente synchrone (PostgreSQL seul, sans Patroni).
        """
        self.serveur(demarre=True, en_recuperation=False)
        self.assertFalse((self.state / "replication").exists())
        resultat = self.run_script("configurer-noeud.sh", "manaty-mesh-1")
        self.assertIn("synchronous_standby_names = ''",
                      self.fragment().read_text(encoding="utf-8"))
        self.assertIn("rôles et slots assurés", resultat.stdout)
        self.assertTrue((self.state / "role_replicator").exists())
        self.assertTrue((self.state / "role_rewind").exists())
        slots = sorted(ligne.split("|")[0] for ligne in
                       (self.state / "slots").read_text(encoding="utf-8").split())
        self.assertEqual(slots, ["ameesh_agents_1", "ameesh_agents_2"])
        # L'ordre du script : la configuration vide est rechargée avant le SQL
        # des rôles/slots, qui ne peut donc pas attendre un synchrone absent.
        journal = self.journal_texte()
        self.assertLess(journal.index("pg_ctlcluster 17 main reload"),
                        journal.index("pg_create_physical_replication_slot"))

    def test_configurer_redemarrage_sur_demande(self):
        self.serveur(demarre=True, en_recuperation=False)
        self.parametre("wal_log_hints", "off")
        (self.state / "apres_redemarrage").write_text("wal_log_hints=on\n", encoding="utf-8")
        resultat = self.run_script("configurer-noeud.sh", "--redemarrer", "manaty-mesh-1")
        self.assertIn("redémarrage terminé", resultat.stdout)
        self.assertIn("pg_ctlcluster 17 main restart", self.journal_texte())

    def test_configurer_standby_et_noeud_neuf(self):
        # Standby en service : configuration posée, aucun rôle ni slot créé ici.
        self.serveur(demarre=True, en_recuperation=True)
        resultat = self.run_script("configurer-noeud.sh", "ameesh-agents-2")
        self.assertIn("nœud standby effectif", resultat.stdout)
        self.assertFalse((self.state / "role_replicator").exists())
        self.assertFalse((self.state / "slots").exists())
        self.assertTrue(self.fragment().exists())

        # Nœud neuf, serveur arrêté : configuration seule, aucun psql.
        self.journal.unlink(missing_ok=True)
        self.serveur(demarre=False)
        resultat = self.run_script("configurer-noeud.sh", "ameesh-agents-2")
        self.assertIn("serveur arrêté", resultat.stdout)
        self.assertNotIn("psql ", self.journal_texte())

    def test_secrets_0600_et_validation(self):
        self.serveur(demarre=False)
        self.env_file.chmod(0o644)
        resultat = self.run_script("configurer-noeud.sh", "ameesh-agents-2", attendu=1)
        self.assertIn("0600", resultat.stderr)

        self.env_file.chmod(0o600)
        self.ecrire_env("REPLICATION_SUBNET='10.77.0.0/24'\n"
                        "REPLICATOR_PASSWORD='court'\n"
                        f"REWIND_PASSWORD='{REWIND_PASSWORD}'\n")
        resultat = self.run_script("configurer-noeud.sh", "ameesh-agents-2", attendu=1)
        self.assertIn("24 caractères", resultat.stderr)

        self.ecrire_env("REPLICATION_SUBNET='10.77.0.0/24'\n"
                        "REPLICATOR_PASSWORD='mot de passe avec espaces 0123456789'\n"
                        f"REWIND_PASSWORD='{REWIND_PASSWORD}'\n")
        resultat = self.run_script("configurer-noeud.sh", "ameesh-agents-2", attendu=1)
        self.assertIn("caractères autorisés", resultat.stderr)

        # Un CIDR invalide écrirait un pg_hba qui empêcherait le redémarrage.
        self.ecrire_env("REPLICATION_SUBNET='999.1.1.1/33'\n"
                        f"REPLICATOR_PASSWORD='{REPLICATOR_PASSWORD}'\n"
                        f"REWIND_PASSWORD='{REWIND_PASSWORD}'\n")
        resultat = self.run_script("configurer-noeud.sh", "ameesh-agents-2", attendu=1)
        self.assertIn("CIDR valide", resultat.stderr)
        self.assertFalse(self.fragment().exists())

    def test_secret_mauvais_proprietaire_refuse_avant_source(self):
        # B2 de revue #6868 (2e passe) : un 0600 appartenant à un autre compte
        # serait modifiable par lui, puis sourcé en root. La doublure de stat
        # annonce un autre uid pour replication.env ; le script doit refuser
        # AVANT de le sourcer.
        vrai_stat = shutil.which("stat")
        self.assertIsNotNone(vrai_stat)
        self.stub("stat", f'''
import os
import sys
args = sys.argv[1:]
if "-c" in args and "%u" in args and args[-1].endswith("replication.env"):
    print("4242")
    sys.exit(0)
os.execv({vrai_stat!r}, ["stat"] + args)
''')
        self.serveur(demarre=False)
        resultat = self.run_script("configurer-noeud.sh", "ameesh-agents-2", attendu=1)
        self.assertIn("doit appartenir à l'utilisateur effectif", resultat.stderr)
        self.assertIn("uid 4242", resultat.stderr)

    # --- promotion -----------------------------------------------------------
    def test_promotion_refuse_hub_et_split_brain(self):
        self.serveur(demarre=True, en_recuperation=True)
        resultat = self.run_script("promouvoir.sh", "manaty-mesh-1", attendu=1)
        self.assertIn("nofailover", resultat.stderr)
        self.assertNotIn("pg_ctlcluster 17 main promote", self.journal_texte())

        # Un autre nœud répond comme primaire : refus.
        self.journal.unlink(missing_ok=True)
        self.serveur(demarre=True, en_recuperation=True)
        self.hote_distant("ameesh-agents-1", demarre=True, en_recuperation=False)
        resultat = self.run_script("promouvoir.sh", "ameesh-agents-2", attendu=1)
        self.assertIn("split brain", resultat.stderr)
        self.assertIn("un primaire répond", resultat.stderr)
        self.assertNotIn("pg_ctlcluster 17 main promote", self.journal_texte())

        # Nœud joignable au rôle illisible : refus aussi.
        self.hote_distant("ameesh-agents-1", demarre=True, en_recuperation=True)
        (self.state / "hotes" / "ameesh-agents-1" / "illisible").touch()
        resultat = self.run_script("promouvoir.sh", "ameesh-agents-2", attendu=1)
        self.assertIn("rôle est illisible", resultat.stderr)

        # pgpass absent : refus avant toute promotion (aucun pari sur le secret).
        (self.state / "hotes" / "ameesh-agents-1" / "illisible").unlink()
        self.pgpass.unlink()
        resultat = self.run_script("promouvoir.sh", "ameesh-agents-2", attendu=1)
        self.assertIn("absent", resultat.stderr)

    def test_promotion_ok_cree_les_slots_et_recharge(self):
        self.serveur(demarre=True, en_recuperation=True)
        (self.state / "replay_lsn").write_text("0/5000000", encoding="utf-8")
        resultat = self.run_script("promouvoir.sh", "--borne", "0/5000000",
                                   "ameesh-agents-2")
        self.assertIn("promotion confirmée", resultat.stdout)
        self.assertIn("candidat à jour", resultat.stdout)
        self.assertIn("pg_ctlcluster 17 main promote", self.journal_texte())
        self.assertIn("pg_ctlcluster 17 main reload", self.journal_texte())
        self.assertEqual((self.state / "in_recovery").read_text(encoding="utf-8"), "f")
        slots = sorted(ligne.split("|")[0] for ligne in
                       (self.state / "slots").read_text(encoding="utf-8").split())
        self.assertEqual(slots, ["ameesh_agents_1", "manaty_mesh_1"])
        # Aucun standby en flux juste après la promotion : liste vide, sinon
        # les premiers COMMIT du nouveau primaire attendraient indéfiniment.
        self.assertIn("synchronous_standby_names = ''",
                      self.fragment().read_text(encoding="utf-8"))
        self.assertIn("synchrones actifs après rechargement : aucun", resultat.stdout)

        # ameesh-agents-2 suit le nouveau primaire : un passage active ANY 1.
        (self.state / "replication").write_text(
            "ameesh-agents-2|streaming|quorum|0/5000000|0/5000000\n",
            encoding="utf-8")
        self.run_script("configurer-noeud.sh", "ameesh-agents-2")
        self.assertIn("ANY 1 (",
                      self.fragment().read_text(encoding="utf-8"))

    def test_promotion_refuse_sans_borne_ou_candidat_en_retard(self):
        self.serveur(demarre=True, en_recuperation=True)
        (self.state / "replay_lsn").write_text("0/4000000", encoding="utf-8")

        # LSN invalide : refus avant tout geste.
        resultat = self.run_script("promouvoir.sh", "--borne", "pas-un-lsn",
                                   "ameesh-agents-2", attendu=1)
        self.assertIn("LSN PostgreSQL attendu", resultat.stderr)
        self.assertNotIn("pg_ctlcluster 17 main promote", self.journal_texte())

        # Sans borne (bascule planifiée) : refus ; un candidat en retard ne
        # prouve pas qu'il a tous les COMMIT confirmés (B1, 2e passe).
        resultat = self.run_script("promouvoir.sh", "ameesh-agents-2", attendu=1)
        self.assertIn("borne WAL de l'ancien primaire absente", resultat.stderr)
        self.assertNotIn("pg_ctlcluster 17 main promote", self.journal_texte())

        resultat = self.run_script("promouvoir.sh", "--borne", "0/5000000",
                                   "ameesh-agents-2", attendu=1)
        self.assertIn("candidat en retard", resultat.stderr)
        self.assertIn("0/4000000", resultat.stderr)
        self.assertNotIn("pg_ctlcluster 17 main promote", self.journal_texte())

        # --force est la dérogation de désastre : elle dit la perte possible.
        resultat = self.run_script("promouvoir.sh", "--borne", "0/5000000",
                                   "--force", "ameesh-agents-2")
        self.assertIn("--force accordé", resultat.stdout)
        self.assertIn("COMMIT confirmés peuvent être perdus", resultat.stdout)
        self.assertIn("pg_ctlcluster 17 main promote", self.journal_texte())

    def test_promotion_sans_borne_force_dit_la_perte(self):
        self.serveur(demarre=True, en_recuperation=True)
        resultat = self.run_script("promouvoir.sh", "--force", "ameesh-agents-2")
        self.assertIn("aucune borne WAL", resultat.stdout)
        self.assertIn("COMMIT confirmés peuvent être perdus", resultat.stdout)
        self.assertIn("pg_ctlcluster 17 main promote", self.journal_texte())

    # --- base backup ---------------------------------------------------------
    def test_initialiser_standby_basebackup_et_reprise(self):
        self.serveur(demarre=False)
        self.hote_distant("manaty-mesh-1", demarre=True, en_recuperation=False)
        self.slots([["ameesh_agents_2", "f", "reserved"]],
                   base=self.state / "hotes" / "manaty-mesh-1")
        (self.state / "receiver").write_text(
            "streaming|manaty-mesh-1|ameesh_agents_2|00:00:00.001|00:00:00.001|00:00:00.001\n",
            encoding="utf-8")

        resultat = self.run_script("initialiser-standby.sh",
                                   "--source", "manaty-mesh-1", "ameesh-agents-2")
        self.assertIn("standby ameesh-agents-2 en flux", resultat.stdout)

        journal = self.journal_texte()
        self.assertIn("pg_basebackup -w -h manaty-mesh-1 -p 5432 -U replicator", journal)
        self.assertIn("-X stream", journal)
        self.assertIn("-S ameesh_agents_2", journal)
        self.assertIn("-R -c fast", journal)
        self.assertNotIn(REPLICATOR_PASSWORD, journal)
        self.assertIn("pg_ctlcluster 17 main start", journal)

        self.assertTrue((self.data / "PG_VERSION").exists())
        self.assertTrue((self.data / "standby.signal").exists())
        auto = self.auto_conf()
        self.assertEqual(auto.count("primary_conninfo"), 1,
                         "la ligne héritée de pg_basebackup -R doit être remplacée")
        self.assertIn("host=manaty-mesh-1", auto)
        self.assertIn("user=replicator", auto)
        self.assertIn("application_name=ameesh-agents-2", auto)
        self.assertIn("passfile=" + str(self.pgpass), auto)
        self.assertIn("primary_slot_name = 'ameesh_agents_2'", auto)
        self.assertNotIn(REPLICATOR_PASSWORD, auto)

        mode = self.pgpass.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        pgpass = self.pgpass.read_text(encoding="utf-8")
        self.assertIn(":replication:replicator:", pgpass)
        self.assertIn(":postgres:rewind:", pgpass)

        # Rejouer la reprise ne doit pas empiler les marqueurs ni les lignes.
        self.run_script("suivre-primaire.sh", "--source", "manaty-mesh-1",
                        "ameesh-agents-2")
        auto = self.auto_conf()
        self.assertEqual(auto.count("# BEGIN ameesh-replication (lot #6868)"), 1)
        self.assertEqual(auto.count("# END ameesh-replication (lot #6868)"), 1)
        self.assertEqual(auto.count("primary_conninfo ="), 1)

    def test_initialiser_refuse_source_locale_standby_et_repertoire_plein(self):
        self.serveur(demarre=False)
        resultat = self.run_script("initialiser-standby.sh",
                                   "--source", "ameesh-agents-2", "ameesh-agents-2",
                                   attendu=1)
        self.assertIn("même nœud", resultat.stderr)

        self.hote_distant("manaty-mesh-1", demarre=True, en_recuperation=True)
        self.slots([["ameesh_agents_2", "f", "reserved"]],
                   base=self.state / "hotes" / "manaty-mesh-1")
        resultat = self.run_script("initialiser-standby.sh",
                                   "--source", "manaty-mesh-1", "ameesh-agents-2",
                                   attendu=1)
        self.assertIn("en récupération", resultat.stderr)

        self.hote_distant("manaty-mesh-1", demarre=True, en_recuperation=False)
        self.data.mkdir()
        (self.data / "PG_VERSION").write_text("17\n", encoding="utf-8")
        resultat = self.run_script("initialiser-standby.sh",
                                   "--source", "manaty-mesh-1", "ameesh-agents-2",
                                   attendu=1)
        self.assertIn("n'est pas vide", resultat.stderr)
        self.assertNotIn("pg_basebackup ", self.journal_texte())

    def test_initialiser_reinitialiser_deplace_les_donnees(self):
        self.serveur(demarre=False)
        self.hote_distant("manaty-mesh-1", demarre=True, en_recuperation=False)
        self.slots([["ameesh_agents_2", "f", "reserved"]],
                   base=self.state / "hotes" / "manaty-mesh-1")
        (self.state / "receiver").write_text("streaming|manaty-mesh-1|x|||\n",
                                             encoding="utf-8")
        self.data.mkdir()
        (self.data / "PG_VERSION").write_text("17\n", encoding="utf-8")
        (self.data / "ancien").write_text("à conserver\n", encoding="utf-8")

        self.run_script("initialiser-standby.sh", "--reinitialiser",
                        "--source", "manaty-mesh-1", "ameesh-agents-2")
        anciens = list(self.tmp.glob("data.remplace-*"))
        self.assertEqual(len(anciens), 1, anciens)
        self.assertTrue((anciens[0] / "ancien").exists())
        self.assertTrue((self.data / "PG_VERSION").exists())
        self.assertFalse((self.data / "ancien").exists())

    def test_initialiser_slot_absent_refuse(self):
        self.serveur(demarre=False)
        self.hote_distant("manaty-mesh-1", demarre=True, en_recuperation=False)
        resultat = self.run_script("initialiser-standby.sh",
                                   "--source", "manaty-mesh-1", "ameesh-agents-2",
                                   attendu=1)
        self.assertIn("slot ameesh_agents_2 absent", resultat.stderr)
        self.assertNotIn("pg_basebackup ", self.journal_texte())

    # --- pg_rewind -----------------------------------------------------------
    def test_reintegrer_pg_rewind_et_repose_la_configuration(self):
        self.donnees()
        self.serveur(demarre=False)
        self.hote_distant("ameesh-agents-1", demarre=True, en_recuperation=False)
        (self.state / "receiver").write_text("streaming|ameesh-agents-1|x|||\n",
                                             encoding="utf-8")

        resultat = self.run_script("reintegrer.sh",
                                   "--source", "ameesh-agents-1", "manaty-mesh-1")
        self.assertIn("pg_rewind terminé", resultat.stdout)

        journal = self.journal_texte()
        self.assertIn(f"--target-pgdata={self.data}", journal)
        self.assertIn("--source-server=host=ameesh-agents-1 port=5432 user=rewind", journal)
        self.assertNotIn(REWIND_PASSWORD, journal)

        # Le rewind copie la config de la source ; le script repose la nôtre.
        fragment = self.fragment().read_text(encoding="utf-8")
        self.assertIn("wal_level = replica", fragment)
        self.assertNotIn("configuration copiée de la source", fragment)
        auto = self.auto_conf()
        self.assertEqual(auto.count("primary_conninfo"), 1)
        self.assertIn("host=ameesh-agents-1", auto)
        self.assertIn("application_name=manaty-mesh-1", auto)
        self.assertIn("primary_slot_name = 'manaty_mesh_1'", auto)
        self.assertNotIn("ancienne-source", auto)
        self.assertTrue((self.data / "standby.signal").exists())
        self.assertIn("pg_ctlcluster 17 main start", journal)
        self.assertTrue((self.state / "running").exists())

    def test_reintegrer_echec_non_masque(self):
        self.donnees()
        self.serveur(demarre=False)
        self.hote_distant("ameesh-agents-1", demarre=True, en_recuperation=False)
        resultat = self.run_script("reintegrer.sh",
                                   "--source", "ameesh-agents-1", "manaty-mesh-1",
                                   attendu=1, env={"FAKE_ECHEC_REWIND": "1"})
        self.assertIn("pg_rewind a échoué", resultat.stderr)
        self.assertIn("initialiser-standby.sh --reinitialiser", resultat.stderr)
        self.assertIn("NE PAS le redémarrer en primaire", resultat.stderr)
        self.assertNotIn("pg_ctlcluster 17 main start", self.journal_texte())
        self.assertTrue((self.data / "PG_VERSION").exists())

    def test_reintegrer_exige_source_et_serveur_arrete(self):
        self.donnees()
        self.serveur(demarre=True, en_recuperation=False)
        resultat = self.run_script("reintegrer.sh", "--source", "ameesh-agents-1",
                                   "manaty-mesh-1", attendu=1)
        self.assertIn("serveur local tourne", resultat.stderr)

        self.serveur(demarre=False)
        resultat = self.run_script("reintegrer.sh", "--source", "ameesh-agents-1",
                                   "manaty-mesh-1", attendu=1)
        self.assertIn("injoignable", resultat.stderr)

        self.hote_distant("ameesh-agents-1", demarre=True, en_recuperation=True)
        resultat = self.run_script("reintegrer.sh", "--source", "ameesh-agents-1",
                                   "manaty-mesh-1", attendu=1)
        self.assertIn("en récupération", resultat.stderr)

    # --- suivre un nouveau primaire -----------------------------------------
    def test_suivre_primaire_et_refus_second_primaire(self):
        self.donnees()
        self.serveur(demarre=True, en_recuperation=True)
        self.hote_distant("ameesh-agents-1", demarre=True, en_recuperation=False)
        (self.state / "receiver").write_text("streaming|ameesh-agents-1|x|||\n",
                                             encoding="utf-8")
        resultat = self.run_script("suivre-primaire.sh",
                                   "--source", "ameesh-agents-1", "ameesh-agents-2")
        self.assertIn("en flux depuis ameesh-agents-1", resultat.stdout)
        auto = self.auto_conf()
        self.assertIn("host=ameesh-agents-1", auto)
        self.assertIn("application_name=ameesh-agents-2", auto)
        self.assertIn("pg_ctlcluster 17 main reload", self.journal_texte())
        self.assertNotIn(REPLICATOR_PASSWORD, auto)

        # Un nœud qui se croit primaire ne se met pas à suivre un autre.
        self.serveur(demarre=True, en_recuperation=False)
        resultat = self.run_script("suivre-primaire.sh",
                                   "--source", "ameesh-agents-1", "ameesh-agents-2",
                                   attendu=1)
        self.assertIn("se croit primaire", resultat.stderr)

    # --- retour arrière ------------------------------------------------------
    def test_retour_arriere_configuration_slots_et_roles(self):
        self.serveur(demarre=True, en_recuperation=False)
        self.run_script("configurer-noeud.sh", "manaty-mesh-1")
        self.assertTrue((self.state / "role_replicator").exists())
        self.slots([["ameesh_agents_1", "f", "reserved"],
                    ["ameesh_agents_2", "t", "reserved"]])
        (self.state / "role_replicator").touch()
        (self.state / "role_rewind").touch()

        resultat = self.run_script("retour-arriere.sh", "--tout", "--force",
                                   "manaty-mesh-1")
        self.assertIn("configuration de réplication retirée", resultat.stdout)
        self.assertFalse(self.fragment().exists())
        hba = self.hba()
        self.assertNotIn("ameesh-replication", hba)
        self.assertIn("hostssl ameesh all 10.77.0.0/24 scram-sha-256", hba)
        slots_restants = (self.state / "slots").read_text(encoding="utf-8").strip() \
            if (self.state / "slots").exists() else ""
        self.assertEqual(slots_restants, "")
        self.assertFalse((self.state / "role_replicator").exists())
        self.assertFalse((self.state / "role_rewind").exists())

        # B3 de revue #6868 : les quatre grants pg_rewind sont révoqués avant
        # DROP ROLE rewind ; la doublure refuse le DROP tant qu'ils existent,
        # comme PostgreSQL (« objects depend on it »).
        stdin = self.stdin_texte()
        self.assertLess(
            stdin.index("REVOKE EXECUTE ON FUNCTION pg_catalog.pg_ls_dir(text, boolean, boolean) FROM rewind"),
            stdin.index("DROP ROLE IF EXISTS rewind"))
        self.assertLess(
            stdin.index("REVOKE EXECUTE ON FUNCTION pg_catalog.pg_stat_file(text, boolean) FROM rewind"),
            stdin.index("DROP ROLE IF EXISTS rewind"))
        self.assertLess(
            stdin.index("REVOKE EXECUTE ON FUNCTION pg_catalog.pg_read_binary_file(text) FROM rewind"),
            stdin.index("DROP ROLE IF EXISTS rewind"))
        self.assertLess(
            stdin.index("REVOKE EXECUTE ON FUNCTION pg_catalog.pg_read_binary_file(text, bigint, bigint, boolean) FROM rewind"),
            stdin.index("DROP ROLE IF EXISTS rewind"))
        self.assertFalse((self.state / "grants").exists())

        # Rejouable : rien à retirer, aucun échec.
        self.run_script("retour-arriere.sh", "--tout", "--force", "manaty-mesh-1")

    def test_retour_arriere_slot_actif_refuse_sans_force(self):
        self.serveur(demarre=True, en_recuperation=False)
        self.run_script("configurer-noeud.sh", "manaty-mesh-1")
        self.slots([["ameesh_agents_2", "t", "reserved"]])
        resultat = self.run_script("retour-arriere.sh", "--slots",
                                   "manaty-mesh-1", attendu=1)
        self.assertIn("slot ameesh_agents_2 (ameesh-agents-2) actif", resultat.stderr)
        self.assertFalse(self.fragment().exists())

    # --- état ----------------------------------------------------------------
    def test_etat_primaire_et_standby(self):
        # etat.sh ne lit aucun secret : le fichier 0600 peut être absent.
        self.env_file.unlink()
        self.serveur(demarre=True, en_recuperation=False)
        (self.state / "replication").write_text(
            "ameesh-agents-2|streaming|quorum|0/5000000|0/5000000\n"
            "manaty-mesh-1|streaming|async|0/5000000|0/4FFFF00\n",
            encoding="utf-8")
        self.slots([["ameesh_agents_2", "t", "reserved"],
                    ["manaty_mesh_1", "f", "reserved"]])
        resultat = self.run_script("etat.sh", "ameesh-agents-1")
        self.assertIn("primaire", resultat.stdout)
        self.assertIn("ameesh-agents-2", resultat.stdout)
        self.assertIn("manaty-mesh-1", resultat.stdout)
        # ANY 1 rend sync_state=quorum en PG17 : ce n'est pas un défaut.
        self.assertIn("quorum", resultat.stdout)
        self.assertNotIn("aucun standby sync/quorum", resultat.stdout)
        self.assertIn("slot manaty_mesh_1 inactif", resultat.stdout)

        # B1 : une liste active sans candidat en flux = COMMIT bloqués.
        (self.state / "replication").unlink()
        resultat = self.run_script("etat.sh", "ameesh-agents-1")
        self.assertIn("les COMMIT peuvent attendre", resultat.stdout)

        # Liste vide : synchrone inactif, averti (amorçage ou dégradation).
        self.parametre("synchronous_standby_names", "")
        resultat = self.run_script("etat.sh", "ameesh-agents-1")
        self.assertIn("synchronous_standby_names vide", resultat.stdout)

        # Le hub (nofailover) ne doit jamais apparaître dans les synchrones.
        self.parametre("synchronous_standby_names",
                       'ANY 1 ("manaty-mesh-1")')
        resultat = self.run_script("etat.sh", "ameesh-agents-1")
        self.assertIn("nofailover mais apparaît", resultat.stdout)

        # wal_log_hints off : pg_rewind impossible, averti.
        self.parametre("wal_log_hints", "off")
        resultat = self.run_script("etat.sh", "ameesh-agents-1")
        self.assertIn("pg_rewind de ce nœud impossible", resultat.stdout)

        # Standby : flux entrant, colonnes réelles de PG17 (written/flushed/
        # latest_end_lsn, last_msg_receipt_time), pas de write_lag fantôme.
        self.serveur(demarre=True, en_recuperation=True)
        (self.state / "receiver").write_text(
            "streaming|ameesh-agents-1|ameesh_agents_2|0/5000000|0/4FF0000|0/4FF0000|2026-11-11 10:00:00\n",
            encoding="utf-8")
        resultat = self.run_script("etat.sh", "ameesh-agents-2")
        self.assertIn("flux entrant", resultat.stdout)
        self.assertIn("statut=streaming", resultat.stdout)
        self.assertIn("written_lsn=0/5000000", resultat.stdout)
        self.assertIn("recu=2026-11-11 10:00:00", resultat.stdout)
        self.assertNotIn("write_lag", resultat.stdout)

    def test_etat_injoignable(self):
        self.serveur(demarre=False)
        resultat = self.run_script("etat.sh", "ameesh-agents-1", attendu=1)
        self.assertIn("INJOIGNABLE", resultat.stdout)

    # --- contrats de fichiers -------------------------------------------------
    def test_scripts_syntaxe_licence_et_executables(self):
        for script in SCRIPTS:
            self.assertTrue(os.access(script, os.X_OK), script)
            contenu = script.read_text(encoding="utf-8")
            self.assertIn("SPDX-License-Identifier: AGPL-3.0-only", contenu, script)
            if script.name != "lib.sh":  # lib.sh est sourcé, pas exécuté
                self.assertIn("set -euo pipefail", contenu, script)
            resultat = subprocess.run(["bash", "-n", str(script)],
                                      capture_output=True, text=True)
            self.assertEqual(resultat.returncode, 0, resultat.stderr)
        roles = (REPLICATION / "sql" / "roles.sql").read_text(encoding="utf-8")
        self.assertIn("REPLICATION", roles)
        self.assertEqual(roles.count("GRANT EXECUTE ON FUNCTION"), 4)
        self.assertNotIn("PASSWORD", roles)
        self.assertNotIn("SUPERUSER;", roles)
        readme = (REPLICATION / "README.md").read_text(encoding="utf-8")
        for attendu in ("EP8.3", "pg_rewind", "retour arrière", "noeuds.conf",
                        "replication.env", "synchronous_standby_names",
                        "sans-synchrone", "quorum"):
            self.assertIn(attendu, readme)
        self.assertTrue((REPLICATION / "noeuds.conf").exists())

    def test_aucun_secret_dans_le_depot(self):
        for chemin in list(REPLICATION.rglob("*")):
            if not chemin.is_file() or chemin.suffix not in (".sh", ".sql", ".md", ".conf"):
                continue
            texte = chemin.read_text(encoding="utf-8")
            self.assertNotIn(REPLICATOR_PASSWORD, texte, chemin)
            self.assertNotIn(REWIND_PASSWORD, texte, chemin)


if __name__ == "__main__":
    unittest.main()
