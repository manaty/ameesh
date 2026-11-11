# SPDX-License-Identifier: AGPL-3.0-only
"""#6861 : passage d'EP8.3 à etcd + Patroni (EP8.4), témoins sur des faux.

Aucun hôte réel. `psql`, `pg_isready`, `pg_controldata`, `systemctl`,
`etcdctl`, `etcd`, `patroni` et `suivre-primaire.sh` sont des doublures dans
un répertoire jetable, pilotées par un fichier d'état JSON ; l'API /cluster
de Patroni est lue d'un document enregistré (AMEESH_ADOPTION_LECTURE). Les
témoins couvrent :

- `adoption.py` : concordance topologie.json / noeuds.conf, règles pg_hba et
  clients que le pg_hba de Patroni perdrait, slots inconnus de Patroni, état
  cible de la grappe (un leader, hub jamais synchrone) ;
- `adopter.sh` : simulation sans geste, ordre des gestes réels (sauvegarde,
  start.conf manual, unité Debian arrêtée avant Patroni), refus (second
  amorçage, autre primaire, identifiant système différent, auto.conf géré),
  retrait du bloc de reprise d'EP8.3, mots de passe sur l'entrée standard ;
- `retour-arriere.sh` : le leader en dernier, configuration restaurée,
  fragment sans liste synchrone, standby confié à suivre-primaire.sh ;
- `verifier-clients.py` (lourd) : une vraie paire PostgreSQL 17 locale en
  réplication synchrone, primaire tué et standby promu : reconnexion,
  balayage de l'écouteur et RPO 0.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PATRONI = ROOT / "deploy" / "serveur" / "patroni"
REPLICATION = ROOT / "deploy" / "serveur" / "replication"
TOPOLOGIE = PATRONI / "topologie.json"

REPLICATION_PASSWORD = "Patroni-Replication-0123456789abcdef"
SUPERUSER_PASSWORD = "Patroni-Superuser-0123456789abcdef"
EP83_REPLICATOR = "Replicator-Secret-0123456789abcdef"
EP83_REWIND = "Rewind-Secret-0123456789abcdef"
SYSID = "7400000000000000001"


def charger_adoption():
    spec = importlib.util.spec_from_file_location("adoption", PATRONI / "adoption.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Doublures : un seul programme Python, aiguillé par le nom d'appel.
FAUX = r'''
import json, os, pathlib, re, sys
etat_f = pathlib.Path(os.environ["FAKE_STATE"])
journal = pathlib.Path(os.environ["FAKE_JOURNAL"])
nom = os.environ["FAUX_NOM"]
argv = sys.argv[1:]
etat = json.loads(etat_f.read_text())
entree = ""
if nom == "psql" and not any(a in ("-c", "-tAc") for a in argv):
    entree = sys.stdin.read()
with journal.open("a") as out:
    out.write(json.dumps({"prog": nom, "argv": argv, "stdin": entree}) + "\n")

def sauver():
    etat_f.write_text(json.dumps(etat))

def hote():
    return argv[argv.index("-h") + 1] if "-h" in argv else "local"

if nom == "pg_isready":
    sys.exit(0 if etat["en_service"].get(hote(), False) else 2)
if nom == "psql":
    h = hote()
    if not etat["en_service"].get(h, False):
        sys.exit(2)
    requete = argv[argv.index("-tAc") + 1] if "-tAc" in argv else entree
    if "pg_is_in_recovery" in requete:
        print("t" if etat["recuperation"].get(h, False) else "f")
    elif "SHOW wal_log_hints" in requete:
        print(etat.get("wal_log_hints", "on"))
    elif "client_addr" in requete:
        print("\n".join(etat.get("clients", [])))
    elif "pg_replication_slots" in requete:
        print("\n".join(etat.get("slots", [])))
    elif "pg_stat_replication" in requete:
        pass
    sys.exit(0)
if nom == "pg_controldata":
    print("pg_control version number:            1700")
    print("Database system identifier:           %s" % etat["sysid"])
    sys.exit(0)
if nom == "systemctl":
    if argv[:2] == ["is-active", "--quiet"]:
        sys.exit(0 if argv[2] in etat["actives"] else 3)
    actif = "--now" in argv
    unite = argv[-1]
    if argv[0] == "enable" and actif:
        etat["actives"].append(unite)
        if unite == "patroni.service" or unite.startswith("postgresql@"):
            etat["en_service"]["local"] = True
    if argv[0] == "disable" and actif:
        etat["actives"] = [u for u in etat["actives"] if u != unite]
        if unite in ("patroni.service",) or unite.startswith("postgresql@"):
            etat["en_service"]["local"] = False
    sauver()
    sys.exit(0)
if nom == "etcdctl":
    if etat.get("etcd_injoignable"):
        sys.exit(1)
    if "health" in argv:
        for i in range(etat.get("etcd_sains", 3)):
            print("https://10.78.0.1%d:2379 is healthy: successfully committed proposal: took = 2ms" % i)
        sys.exit(0)
    if argv and "get" in argv:
        cle = argv[-1]
        valeur = etat.get("dcs", {}).get(cle.rsplit("/", 1)[-1])
        if valeur:
            print(valeur)
        sys.exit(0)
if nom == "etcd":
    print("etcd Version: %s" % etat.get("version_etcd", "3.5.17"))
    sys.exit(0)
if nom == "patroni":
    print("patroni %s" % etat.get("version_patroni", "4.1.5"))
    sys.exit(0)
if nom == "suivre-primaire.sh":
    sys.exit(0)
sys.exit(0)
'''


def cluster(*membres):
    return {"members": [dict(name=n, role=r, state=s, lag=l) for n, r, s, l in membres]}


CIBLE = cluster(("ameesh-agents-1", "leader", "running", None),
                ("ameesh-agents-2", "sync_standby", "streaming", 0),
                ("manaty-mesh-1", "replica", "streaming", 0))


class AdoptionPyTest(unittest.TestCase):
    """Les lectures d'adoption.py, sans réseau."""

    def setUp(self):
        self.mod = charger_adoption()
        self.topologie = json.loads(TOPOLOGIE.read_text(encoding="utf-8"))
        self.tmp = tempfile.mkdtemp(prefix="t6861-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_topologie_et_inventaire_ep83_concordent(self):
        noeuds = self.mod.lire_noeuds_conf(REPLICATION / "noeuds.conf")
        self.assertEqual(self.mod.comparer_topologie(self.topologie, noeuds), [])

    def test_topologie_ecarts_detectes(self):
        noeuds = self.mod.lire_noeuds_conf(REPLICATION / "noeuds.conf")
        noeuds[1]["hote"] = "10.78.0.99"
        noeuds[2]["synchrone"] = True
        ecarts = self.mod.comparer_topologie(self.topologie, noeuds)
        self.assertTrue(any("10.78.0.99" in e for e in ecarts), ecarts)
        self.assertTrue(any("manaty-mesh-1" in e and "nosync" in e for e in ecarts), ecarts)

    def _patroni_yml(self):
        chemin = pathlib.Path(self.tmp, "patroni.yml")
        generer = self.mod._module("generer_configuration", "generer-configuration.py")
        bloc = generer.bloc_pg_hba(self.topologie)
        # même forme que le rendu : pg_hba de bootstrap puis celui de postgresql
        chemin.write_text("bootstrap:\n  pg_hba:\n    - host all all 0.0.0.0/0 trust\n"
                          "postgresql:\n  listen: x\n  pg_hba:\n%s\n  authentication: {}\n"
                          % bloc, encoding="utf-8")
        return chemin

    def test_hba_regle_wireguard_perdue_et_bloc_ep83_attendu(self):
        hba = pathlib.Path(self.tmp, "pg_hba.conf")
        hba.write_text(textwrap.dedent("""\
            local all postgres peer
            # ameesh-mesh : appareils du réseau WireGuard, TLS obligatoire
            hostssl ameesh  all  10.77.0.0/24  scram-sha-256
            # BEGIN ameesh-replication (lot #6868)
            hostssl postgres    rewind     10.78.0.0/24 scram-sha-256
            # END ameesh-replication (lot #6868)
            """), encoding="utf-8")
        clients = pathlib.Path(self.tmp, "clients")
        clients.write_text("10.78.0.12\n10.77.0.2\n10.77.0.5\n", encoding="utf-8")
        ecarts = self.mod.controler_hba(str(self._patroni_yml()), str(hba), str(clients))
        self.assertIn("règle perdue : hostssl ameesh all 10.77.0.0/24 scram-sha-256", ecarts)
        self.assertFalse(any("rewind" in e for e in ecarts), ecarts)
        self.assertIn("client connecté non couvert : 10.77.0.5", ecarts)
        self.assertFalse(any("10.77.0.2" in e or "10.78.0.12" in e for e in ecarts), ecarts)

    def test_hba_rendu_sans_pyyaml_identique(self):
        chemin = self._patroni_yml()
        avec = self.mod.hba_rendu(str(chemin))
        import builtins
        vrai_import = builtins.__import__

        def sans_yaml(nom, *a, **k):
            if nom == "yaml":
                raise ImportError(nom)
            return vrai_import(nom, *a, **k)

        builtins.__import__ = sans_yaml
        try:
            sans = self.mod.hba_rendu(str(chemin))
        finally:
            builtins.__import__ = vrai_import
        self.assertEqual(avec, sans)
        self.assertIn("local all postgres peer", sans)

    def test_slots_membres_connus_et_slot_etranger(self):
        self.assertEqual(self.mod.controler_slots(
            self.topologie, "ameesh-agents-1", ["ameesh_agents_2", "manaty_mesh_1"]), [])
        ecarts = self.mod.controler_slots(
            self.topologie, "ameesh-agents-1", ["ameesh_agents_2", "pgbackrest"])
        self.assertEqual(len(ecarts), 1)
        self.assertIn("pgbackrest", ecarts[0])

    def _grappe(self, doc):
        chemin = pathlib.Path(self.tmp, "cluster.json")
        chemin.write_text(json.dumps(doc), encoding="utf-8")
        return self.mod.lire_grappe({"AMEESH_ADOPTION_LECTURE": str(chemin)}, None)[0]

    def test_cible_conforme(self):
        ecarts, notes = self.mod.verifier_cible(self._grappe(CIBLE), self.topologie, 1048576)
        self.assertEqual((ecarts, notes), ([], []))

    def test_cible_hub_synchrone_deux_leaders_et_retard(self):
        doc = cluster(("ameesh-agents-1", "leader", "running", None),
                      ("ameesh-agents-2", "leader", "running", None),
                      ("manaty-mesh-1", "sync_standby", "streaming", 2 ** 21))
        ecarts, _ = self.mod.verifier_cible(self._grappe(doc), self.topologie, 1048576)
        self.assertTrue(any(e.startswith("leaders :") for e in ecarts), ecarts)
        self.assertTrue(any("nosync mais synchrone" in e for e in ecarts), ecarts)
        self.assertTrue(any("retard" in e for e in ecarts), ecarts)

    def test_cible_leader_non_prefere_est_une_note(self):
        doc = cluster(("ameesh-agents-2", "leader", "running", None),
                      ("ameesh-agents-1", "sync_standby", "streaming", 0),
                      ("manaty-mesh-1", "replica", "streaming", 0))
        ecarts, notes = self.mod.verifier_cible(self._grappe(doc), self.topologie, 1048576)
        self.assertEqual(ecarts, [])
        self.assertTrue(notes and "switchover" in notes[0])

    def test_etat_membre(self):
        grappe = self._grappe(CIBLE)
        self.assertEqual(self.mod.etat_membre(grappe, "ameesh-agents-1"), "leader")
        self.assertEqual(self.mod.etat_membre(grappe, "ameesh-agents-2"), "sync")
        self.assertEqual(self.mod.etat_membre(grappe, "manaty-mesh-1"), "replica")
        self.assertIsNone(self.mod.etat_membre(grappe, "absent"))


class ScriptsTest(unittest.TestCase):
    """adopter.sh et retour-arriere.sh sur des doublures."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="t6861-sh-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        faux = self.bin / "faux.py"
        faux.write_text(FAUX, encoding="utf-8")
        for nom in ("psql", "pg_isready", "pg_controldata", "systemctl", "etcdctl",
                    "etcd", "patroni", "suivre-primaire.sh"):
            lanceur = self.bin / nom
            # le nom d'appel reste celui de la doublure (aiguillage de FAUX)
            lanceur.write_text('#!/bin/sh\nFAUX_NOM=%s exec "%s" -I "%s" "$@"\n'
                               % (nom, sys.executable, faux), encoding="utf-8")
            lanceur.chmod(0o755)
        self.pgconf = self.tmp / "etc-pg"
        (self.pgconf / "conf.d").mkdir(parents=True)
        (self.pgconf / "postgresql.conf").write_text("data_directory = 'x'\ninclude_dir = 'conf.d'\n")
        (self.pgconf / "pg_hba.conf").write_text(textwrap.dedent("""\
            local all postgres peer
            # BEGIN ameesh-replication (lot #6868)
            hostssl replication replicator 10.78.0.0/24 scram-sha-256
            hostssl postgres    rewind     10.78.0.0/24 scram-sha-256
            # END ameesh-replication (lot #6868)
            """))
        (self.pgconf / "start.conf").write_text("auto\n")
        (self.pgconf / "conf.d" / "60-ameesh-replication.conf").write_text(
            "synchronous_standby_names = 'ANY 1 (\"ameesh-agents-1\", \"ameesh-agents-2\")'\n")
        self.data = self.tmp / "data"
        self.data.mkdir()
        (self.data / "postgresql.auto.conf").write_text("work_mem = '8MB'\n")
        self.pgpass = self.tmp / "pgpass"
        self.pgpass.write_text("*:5432:replication:replicator:%s\n*:5432:postgres:rewind:%s\n"
                               % (EP83_REPLICATOR, EP83_REWIND))
        self.pgpass.chmod(0o600)
        self.env_patroni = self.tmp / "patroni.env"
        self.env_patroni.write_text(textwrap.dedent("""\
            PATRONI_RESTAPI_USERNAME=patroni
            PATRONI_RESTAPI_PASSWORD=Rest-Secret-0123456789abcdef
            PATRONI_REPLICATION_USERNAME=replicator
            PATRONI_REPLICATION_PASSWORD=%s
            PATRONI_SUPERUSER_PASSWORD=%s
            AMEESH_DB_REST_URL=https://10.78.0.11:8008
            AMEESH_DB_ETCD_ENDPOINTS=https://10.78.0.11:2379,https://10.78.0.12:2379,https://10.78.0.10:2379
            AMEESH_DB_CA=/etc/ameesh/tls/ca.crt
            AMEESH_DB_ETCD_CERT=/etc/patroni/tls/etcd-client.crt
            AMEESH_DB_ETCD_KEY=/etc/patroni/tls/etcd-client.key
            """ % (REPLICATION_PASSWORD, SUPERUSER_PASSWORD)))
        self.env_patroni.chmod(0o640)
        self.env_ep83 = self.tmp / "replication.env"
        self.env_ep83.write_text("REPLICATION_SUBNET=10.78.0.0/24\nREPLICATOR_PASSWORD=%s\nREWIND_PASSWORD=%s\n"
                                 % (EP83_REPLICATOR, EP83_REWIND))
        self.env_ep83.chmod(0o600)
        self.sauvegardes = self.tmp / "avant-patroni"
        self.cluster = self.tmp / "cluster.json"
        self.ecrire_cluster(cluster())
        self.etat = self.tmp / "etat.json"
        self.journal = self.tmp / "journal"
        self.ecrire_etat({})

    def ecrire_cluster(self, doc):
        self.cluster.write_text(json.dumps(doc), encoding="utf-8")

    def ecrire_etat(self, etat):
        base = {"en_service": {"local": True, "10.78.0.11": True, "10.78.0.12": True,
                               "10.78.0.10": True},
                "recuperation": {"local": False, "10.78.0.11": False, "10.78.0.12": True,
                                 "10.78.0.10": True},
                "actives": [], "sysid": SYSID, "dcs": {},
                "slots": ["ameesh_agents_2", "manaty_mesh_1"],
                "clients": ["10.78.0.12", "10.77.0.2"]}
        base.update(etat)
        self.etat.write_text(json.dumps(base), encoding="utf-8")

    def lire_etat(self):
        return json.loads(self.etat.read_text(encoding="utf-8"))

    def patroni_yml(self, nom):
        mod = charger_adoption()
        generer = mod._module("generer_configuration", "generer-configuration.py")
        topologie = json.loads(TOPOLOGIE.read_text(encoding="utf-8"))
        chemin = self.tmp / "patroni.yml"
        chemin.write_text("scope: ameesh\nname: %s\npostgresql:\n  pg_hba:\n%s\n"
                          % (nom, generer.bloc_pg_hba(topologie)), encoding="utf-8")
        return chemin

    def lancer(self, script, *args, nom="ameesh-agents-1"):
        self.patroni_yml(nom)
        env = dict(os.environ)
        env.update({
            "PATH": "%s:%s" % (self.bin, os.environ.get("PATH", "")),
            "FAKE_STATE": str(self.etat), "FAKE_JOURNAL": str(self.journal),
            "TMPDIR": str(self.tmp),
            "AMEESH_REPLICATION_PGCONF": str(self.pgconf),
            "AMEESH_REPLICATION_DATA": str(self.data),
            "AMEESH_REPLICATION_PGBIN": str(self.bin),
            "AMEESH_REPLICATION_PGPASS": str(self.pgpass),
            "AMEESH_REPLICATION_ENV": str(self.env_ep83),
            "AMEESH_REPLICATION_NOM": nom,
            "AMEESH_REPLICATION_ATTENTE": "2",
            "AMEESH_REPLICATION_RUNUSER": "non",
            "AMEESH_PATRONI_CONF": str(self.tmp / "patroni.yml"),
            "AMEESH_PATRONI_ENV": str(self.env_patroni),
            "AMEESH_PATRONI_ETCD": str(self.bin / "etcd"),
            "AMEESH_PATRONI_ETCDCTL": str(self.bin / "etcdctl"),
            "AMEESH_PATRONI_BIN": str(self.bin / "patroni"),
            "AMEESH_PATRONI_SAUVEGARDES": str(self.sauvegardes),
            "AMEESH_PATRONI_WATCHDOG": str(self.tmp / "watchdog"),
            "AMEESH_PATRONI_SUIVRE": str(self.bin / "suivre-primaire.sh"),
            "AMEESH_ADOPTION_LECTURE": str(self.cluster),
        })
        return subprocess.run([str(PATRONI / script), *args], capture_output=True,
                              text=True, env=env, timeout=60)

    def appels(self, prog=None):
        if not self.journal.exists():
            return []
        lignes = [json.loads(l) for l in self.journal.read_text().splitlines()]
        return [l for l in lignes if prog is None or l["prog"] == prog]

    def gestes_systemctl(self):
        return [a["argv"] for a in self.appels("systemctl") if a["argv"][0] != "is-active"]

    def assertOk(self, res):
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def assertRefus(self, res, motif):
        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn(motif, res.stdout + res.stderr)

    # --- adopter.sh -----------------------------------------------------------

    def test_verifier_primaire_passe_sans_geste(self):
        res = self.lancer("adopter.sh", "verifier")
        self.assertOk(res)
        self.assertIn("contrôles passés", res.stdout)
        self.assertIn("aucun chien de garde", res.stdout)
        self.assertEqual(self.gestes_systemctl(), [])

    def test_verifier_refuse_parametre_gere_dans_auto_conf(self):
        (self.data / "postgresql.auto.conf").write_text(
            "work_mem = '8MB'\nsynchronous_standby_names = 'x'\n")
        self.assertRefus(self.lancer("adopter.sh", "verifier"), "synchronous_standby_names")

    def test_verifier_refuse_client_non_couvert_sauf_accord(self):
        self.ecrire_etat({"clients": ["10.78.0.12", "10.77.0.5"]})
        self.assertRefus(self.lancer("adopter.sh", "verifier"), "10.77.0.5")
        res = self.lancer("adopter.sh", "--accepter-hba", "verifier")
        self.assertOk(res)
        self.assertIn("--accepter-hba accordé", res.stdout)

    def test_verifier_refuse_slot_etranger_versions_et_wal_log_hints(self):
        self.ecrire_etat({"slots": ["ameesh_agents_2", "pgbackrest"]})
        self.assertRefus(self.lancer("adopter.sh", "verifier"), "pgbackrest")
        self.ecrire_etat({"version_etcd": "3.4.30"})
        self.assertRefus(self.lancer("adopter.sh", "verifier"), "etcd 3.5.x attendu")
        self.ecrire_etat({"version_patroni": "3.3.2"})
        self.assertRefus(self.lancer("adopter.sh", "verifier"), "Patroni 4.x attendu")
        self.ecrire_etat({"wal_log_hints": "off"})
        self.assertRefus(self.lancer("adopter.sh", "verifier"), "wal_log_hints=off")

    def test_verifier_refuse_configuration_d_un_autre_noeud_et_env_lisible(self):
        res = self.lancer("adopter.sh", "verifier", nom="ameesh-agents-1")
        self.assertOk(res)
        self.env_patroni.chmod(0o644)
        self.assertRefus(self.lancer("adopter.sh", "verifier"), "0640")

    def test_primaire_simulation_ne_touche_rien(self):
        res = self.lancer("adopter.sh", "primaire")
        self.assertOk(res)
        self.assertIn("[simulation] systemctl disable --now postgresql@17-main", res.stdout)
        self.assertEqual(self.gestes_systemctl(), [])
        self.assertFalse(self.sauvegardes.exists())
        self.assertEqual((self.pgconf / "start.conf").read_text(), "auto\n")

    def test_primaire_geste_reel_ordre_sauvegarde_et_mots_de_passe(self):
        self.ecrire_cluster(cluster(("ameesh-agents-1", "leader", "running", None)))
        res = self.lancer("adopter.sh", "--geste-reel", "primaire")
        self.assertOk(res)
        self.assertEqual(self.gestes_systemctl(), [
            ["disable", "--now", "postgresql@17-main"],
            ["enable", "--now", "patroni.service"]])
        dernier = self.sauvegardes / "dernier"
        for f in ("postgresql.conf", "pg_hba.conf", "start.conf", "postgresql.auto.conf",
                  "pgpass", "role"):
            self.assertTrue((dernier / f).exists(), f)
        self.assertTrue((dernier / "conf.d" / "60-ameesh-replication.conf").exists())
        self.assertEqual((dernier / "role").read_text().strip(), "primaire")
        self.assertEqual(stat.S_IMODE(os.stat(dernier.resolve()).st_mode), 0o700)
        self.assertTrue((self.pgconf / "start.conf").read_text().endswith("manual\n"))
        stdin = "".join(a["stdin"] for a in self.appels("psql"))
        self.assertIn("ALTER ROLE replicator PASSWORD '%s'" % REPLICATION_PASSWORD, stdin)
        self.assertIn("ALTER ROLE postgres PASSWORD '%s'" % SUPERUSER_PASSWORD, stdin)
        self.assertIn("SET log_statement = 'none'", stdin)
        for appel in self.appels():
            argv = " ".join(appel["argv"])
            self.assertNotIn(REPLICATION_PASSWORD, argv)
            self.assertNotIn(SUPERUSER_PASSWORD, argv)

    def test_primaire_refuse_second_amorcage_et_autre_primaire(self):
        self.ecrire_etat({"dcs": {"leader": "ameesh-agents-2"}})
        self.assertRefus(self.lancer("adopter.sh", "primaire"), "déjà un leader")
        self.ecrire_etat({"dcs": {"initialize": SYSID}})
        self.assertRefus(self.lancer("adopter.sh", "primaire"), "clé initialize")
        self.ecrire_etat({"recuperation": {"local": False, "10.78.0.12": False,
                                           "10.78.0.10": True}})
        self.assertRefus(self.lancer("adopter.sh", "primaire"), "split brain")
        self.ecrire_etat({"etcd_sains": 1})
        self.assertRefus(self.lancer("adopter.sh", "primaire"), "sans quorum")
        self.ecrire_etat({"etcd_injoignable": True, "etcd_sains": 3})
        self.assertNotEqual(self.lancer("adopter.sh", "primaire").returncode, 0)
        self.assertEqual(self.gestes_systemctl(), [])

    def test_primaire_refuse_sur_un_standby(self):
        self.ecrire_etat({"recuperation": {"local": True}})
        self.assertRefus(self.lancer("adopter.sh", "primaire", nom="ameesh-agents-2"),
                         "se joue sur le primaire")

    def test_primaire_refuse_sur_le_hub_nofailover(self):
        self.ecrire_etat({"recuperation": {"local": False, "10.78.0.11": True,
                                           "10.78.0.12": True},
                          "slots": ["ameesh_agents_1", "ameesh_agents_2"]})
        self.assertRefus(self.lancer("adopter.sh", "primaire", nom="manaty-mesh-1"),
                         "nofailover")
        self.assertEqual(self.gestes_systemctl(), [])

    def test_primaire_deja_leader_ne_refait_rien(self):
        self.ecrire_etat({"actives": ["patroni.service"]})
        self.ecrire_cluster(CIBLE)
        res = self.lancer("adopter.sh", "--geste-reel", "primaire")
        self.assertOk(res)
        self.assertIn("déjà leader", res.stdout)
        self.assertEqual(self.gestes_systemctl(), [])
        # B2 de revue : le rejeu repose les mots de passe
        stdin = "".join(a["stdin"] for a in self.appels("psql"))
        self.assertIn("ALTER ROLE replicator PASSWORD '%s'" % REPLICATION_PASSWORD, stdin)

    def _standby(self, **etat):
        base = {"recuperation": {"local": True, "10.78.0.11": False},
                "dcs": {"leader": "ameesh-agents-1", "initialize": SYSID},
                "slots": []}
        base.update(etat)
        self.ecrire_etat(base)
        (self.data / "postgresql.auto.conf").write_text(textwrap.dedent("""\
            work_mem = '8MB'
            primary_conninfo = 'host=old'
            # BEGIN ameesh-replication (lot #6868)
            primary_conninfo = 'host=10.78.0.11 user=replicator'
            primary_slot_name = 'ameesh_agents_2'
            # END ameesh-replication (lot #6868)
            """))

    def test_standby_geste_reel_retire_la_reprise_et_passe_en_flux(self):
        self._standby()
        self.ecrire_cluster(CIBLE)
        res = self.lancer("adopter.sh", "--geste-reel", "standby", nom="ameesh-agents-2")
        self.assertOk(res)
        auto = (self.data / "postgresql.auto.conf").read_text()
        self.assertEqual(auto, "work_mem = '8MB'\n")
        self.assertIn("primary_slot_name", (self.sauvegardes / "dernier" / "postgresql.auto.conf").read_text())
        self.assertEqual(self.gestes_systemctl(), [
            ["disable", "--now", "postgresql@17-main"],
            ["enable", "--now", "patroni.service"]])
        self.assertFalse([a for a in self.appels("psql") if "ALTER ROLE" in a["stdin"]])

    def test_standby_refuse_identifiant_systeme_different_et_sans_leader(self):
        self._standby(sysid="7400000000000000999")
        self.ecrire_cluster(CIBLE)
        self.assertRefus(self.lancer("adopter.sh", "standby", nom="ameesh-agents-2"),
                         "identifiant système")
        self._standby(dcs={})
        self.assertRefus(self.lancer("adopter.sh", "standby", nom="ameesh-agents-2"),
                         "aucun leader")
        self._standby()
        self.ecrire_cluster(cluster(("ameesh-agents-1", "leader", "stopped", None)))
        self.assertRefus(self.lancer("adopter.sh", "standby", nom="ameesh-agents-2"),
                         "n'est pas leader")
        self.assertEqual(self.gestes_systemctl(), [])

    def test_sauvegarde_refusee_si_patroni_est_deja_passe(self):
        (self.pgconf / "postgresql.base.conf").write_text("x\n")
        self.ecrire_cluster(cluster(("ameesh-agents-1", "leader", "running", None)))
        self.assertRefus(self.lancer("adopter.sh", "--geste-reel", "primaire"),
                         "postgresql.base.conf existe")
        self.assertEqual(self.gestes_systemctl(), [])

    def test_grappe_conforme_et_ecart(self):
        self.ecrire_cluster(CIBLE)
        self.assertOk(self.lancer("adopter.sh", "grappe"))
        self.ecrire_cluster(cluster(("ameesh-agents-1", "leader", "running", None),
                                    ("ameesh-agents-2", "replica", "streaming", 0),
                                    ("manaty-mesh-1", "sync_standby", "streaming", 0)))
        self.assertRefus(self.lancer("adopter.sh", "grappe"), "nosync mais synchrone")

    # --- retour-arriere.sh ----------------------------------------------------

    def _adopter_primaire(self):
        self.ecrire_cluster(cluster(("ameesh-agents-1", "leader", "running", None)))
        self.assertOk(self.lancer("adopter.sh", "--geste-reel", "primaire"))
        # Patroni réécrit la configuration du nœud :
        os.replace(self.pgconf / "postgresql.conf", self.pgconf / "postgresql.base.conf")
        (self.pgconf / "postgresql.conf").write_text("include 'postgresql.base.conf'\n")
        (self.pgconf / "pg_hba.conf").write_text("hostssl all all 10.78.0.0/24 scram-sha-256\n")
        self.pgpass.write_text("10.78.0.11:5432:*:replicator:%s\n" % REPLICATION_PASSWORD)
        self.journal.unlink()

    def test_retour_le_leader_s_arrete_en_dernier(self):
        self._adopter_primaire()
        self.ecrire_cluster(CIBLE)
        self.assertRefus(self.lancer("retour-arriere.sh", "--geste-reel", "arreter"),
                         "s'arrête en dernier")
        self.assertRefus(self.lancer("retour-arriere.sh", "--geste-reel", "primaire"),
                         "sert encore une base")
        self.assertEqual(self.gestes_systemctl(), [])

    def _autres_arretes(self, **etat):
        courant = self.lire_etat()
        courant["en_service"].update({"10.78.0.12": False, "10.78.0.10": False})
        courant.update(etat)
        self.etat.write_text(json.dumps(courant), encoding="utf-8")

    def test_retour_primaire_restaure_sans_synchrone(self):
        self._adopter_primaire()
        self._autres_arretes()
        self.ecrire_cluster(cluster(("ameesh-agents-1", "leader", "running", None)))
        res = self.lancer("retour-arriere.sh", "--geste-reel", "primaire")
        self.assertOk(res)
        self.assertEqual(self.gestes_systemctl(), [
            ["disable", "--now", "patroni.service"],
            ["enable", "--now", "postgresql@17-main"]])
        self.assertFalse((self.pgconf / "postgresql.base.conf").exists())
        self.assertIn("include_dir", (self.pgconf / "postgresql.conf").read_text())
        self.assertIn("rewind", (self.pgconf / "pg_hba.conf").read_text())
        fragment = (self.pgconf / "conf.d" / "60-ameesh-replication.conf").read_text()
        self.assertIn("synchronous_standby_names = ''", fragment)
        self.assertTrue((self.pgconf / "start.conf").read_text().endswith("auto\n"))
        pgpass = self.pgpass.read_text()
        self.assertIn(EP83_REPLICATOR, pgpass)
        self.assertIn(EP83_REWIND, pgpass)
        stdin = "".join(a["stdin"] for a in self.appels("psql"))
        self.assertIn("ALTER ROLE replicator PASSWORD '%s'" % EP83_REPLICATOR, stdin)

    def test_retour_standby_confie_a_suivre_primaire(self):
        self._adopter_primaire()
        self.ecrire_etat({"actives": []})
        self.assertRefus(self.lancer("retour-arriere.sh", "standby", nom="ameesh-agents-2"),
                         "--source")
        res = self.lancer("retour-arriere.sh", "--geste-reel", "standby", "--source",
                          "ameesh-agents-1", nom="ameesh-agents-2")
        self.assertOk(res)
        self.assertTrue((self.data / "standby.signal").exists())
        # B1 de revue : la ligne Patroni ne passe plus avant celles d'EP8.3
        pgpass = self.pgpass.read_text()
        self.assertNotIn(REPLICATION_PASSWORD, pgpass)
        self.assertIn(EP83_REPLICATOR, pgpass)
        self.assertEqual([a["argv"] for a in self.appels("suivre-primaire.sh")],
                         [["--source", "ameesh-agents-1", "ameesh-agents-2"]])
        self.assertEqual(self.gestes_systemctl(), [["enable", "postgresql@17-main"]])

    def test_retour_primaire_patroni_arrete_refuse_un_second_primaire(self):
        # B3 de revue : Patroni local arrêté, une bascule a fait d'un autre
        # nœud le leader, ou un autre nœud sert encore : aucun geste.
        self._adopter_primaire()
        self._autres_arretes(actives=[], dcs={"leader": "ameesh-agents-2"})
        self.assertRefus(self.lancer("retour-arriere.sh", "--geste-reel", "primaire"),
                         "le DCS désigne ameesh-agents-2")
        self._autres_arretes(actives=[], dcs={})
        etat = self.lire_etat()
        etat["en_service"]["10.78.0.12"] = True
        self.etat.write_text(json.dumps(etat), encoding="utf-8")
        self.assertRefus(self.lancer("retour-arriere.sh", "--geste-reel", "primaire"),
                         "ameesh-agents-2 (10.78.0.12) sert encore une base")
        self.assertEqual(self.gestes_systemctl(), [])

    def test_retour_api_illisible_n_est_pas_un_non_leader(self):
        self.ecrire_etat({"actives": ["patroni.service"]})
        self.cluster.write_text("{illisible", encoding="utf-8")
        self.assertRefus(self.lancer("retour-arriere.sh", "--geste-reel", "arreter",
                                     nom="ameesh-agents-2"), "API Patroni illisible")
        self.assertEqual(self.gestes_systemctl(), [])

    def test_retour_sans_sauvegarde_refuse_et_etcd_en_dernier(self):
        self.assertRefus(self.lancer("retour-arriere.sh", "--geste-reel", "standby",
                                     "--source", "ameesh-agents-1", nom="ameesh-agents-2"),
                         "sauvegarde d'adopter.sh absente")
        self.ecrire_etat({"actives": ["patroni.service"]})
        self.assertRefus(self.lancer("retour-arriere.sh", "--geste-reel", "etcd"),
                         "etcd s'arrête en dernier")

    def test_scripts_syntaxe_licence_et_executables(self):
        for nom in ("adopter.sh", "retour-arriere.sh", "lib-patroni.sh"):
            chemin = PATRONI / nom
            self.assertEqual(subprocess.run(["bash", "-n", str(chemin)]).returncode, 0, nom)
            self.assertIn("SPDX-License-Identifier: AGPL-3.0-only", chemin.read_text())
        for nom in ("adopter.sh", "retour-arriere.sh", "adoption.py", "verifier-clients.py"):
            self.assertTrue(os.access(PATRONI / nom, os.X_OK), nom)


# --- verifier-clients.py sur une vraie paire locale -----------------------------

PG_BIN = pathlib.Path(os.environ.get("AMEESH_TEST_PG_BIN", "/usr/lib/postgresql/17/bin"))
VENV_PY = pathlib.Path(os.environ.get(
    "AMEESH_TEST_PSYCOPG_PYTHON",
    os.path.expanduser("~/.local/share/ameesh/venv/bin/python")))


def _port_libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env_client():
    """Environnement des clients du témoin : sans PG* hérité (PGPASSWORD de la
    CI, PGSSLMODE ou PGTARGETSESSIONATTRS d'un guest changeraient libpq)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env["PYTHONPATH"] = str(ROOT / "src")
    return env


def _python_psycopg():
    for candidat in (sys.executable, str(VENV_PY)):
        if candidat and os.path.exists(candidat) and subprocess.run(
                [candidat, "-c", "import psycopg"], capture_output=True).returncode == 0:
            return candidat
    return None


class VerifierClientsGardeTest(unittest.TestCase):
    """Sans paire locale : hors des hôtes locaux, --geste-reel est exigé."""

    def test_hors_hotes_locaux_exige_le_geste_reel(self):
        python = _python_psycopg()
        if not python:
            self.skipTest("psycopg absent")
        res = subprocess.run(
            [python, str(PATRONI / "verifier-clients.py"), "--dsn",
             "host=10.78.0.11,10.78.0.12 dbname=ameesh", "--tuer", "true"],
            capture_output=True, text=True, timeout=30,
            env=_env_client())
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("--geste-reel", res.stderr)


@unittest.skipUnless(all((PG_BIN / b).exists() for b in ("initdb", "pg_ctl", "pg_basebackup")),
                     "binaires serveur PostgreSQL 17 absents")
@unittest.skipIf(os.geteuid() == 0, "initdb refuse root")
class VerifierClientsTest(unittest.TestCase):
    """Primaire tué, standby synchrone promu : le client tient l'acceptation."""

    def setUp(self):
        self.python = _python_psycopg()
        if not self.python:
            self.skipTest("psycopg absent")
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="t6861-pg-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.a, self.b = self.tmp / "a", self.tmp / "b"
        self.pa, self.pb = _port_libre(), _port_libre()
        # Le chemin d'une socket Unix est borné à 107 octets : un TMPDIR
        # profond (guest, CI) le dépasserait. Répertoire vide, quelques octets.
        self.socket = pathlib.Path(tempfile.mkdtemp(prefix="t6861s-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.socket, True)
        try:
            self._monter_paire()
        except subprocess.CalledProcessError as exc:
            # Environnement (ports, droits, binaires partiels) : non concluant,
            # pas un échec du client (canon §3).
            self.skipTest("paire PostgreSQL locale impossible : %s %s" % (
                exc.cmd[0], (exc.stderr or b"").decode(errors="replace")[-300:]))

    def _monter_paire(self):
        subprocess.run([str(PG_BIN / "initdb"), "-D", str(self.a), "-U", "postgres",
                        "--auth=trust", "-E", "UTF8", "--locale=C"],
                       check=True, capture_output=True, env=_env_client())
        with open(self.a / "postgresql.conf", "a", encoding="utf-8") as fh:
            fh.write("listen_addresses = '127.0.0.1'\nunix_socket_directories = '%s'\n"
                     "wal_level = replica\nmax_wal_senders = 5\n"
                     "synchronous_standby_names = 'b'\nsynchronous_commit = on\n"
                     % self.socket)
        with open(self.a / "pg_hba.conf", "a", encoding="utf-8") as fh:
            fh.write("host replication postgres 127.0.0.1/32 trust\n")
        self._pg_ctl(self.a, self.pa, "start")
        self.addCleanup(self._pg_ctl, self.a, self.pa, "stop", "-m", "immediate", check=False)
        subprocess.run([str(PG_BIN / "pg_basebackup"), "-D", str(self.b), "-h", "127.0.0.1",
                        "-p", str(self.pa), "-U", "postgres", "-X", "stream", "-R",
                        "-d", "host=127.0.0.1 port=%d user=postgres application_name=b" % self.pa],
                       check=True, capture_output=True, env=_env_client())
        # Promu, B n'attend aucun synchrone : c'est ce que Patroni pose
        # (synchronous_mode non strict sans candidat).
        with open(self.b / "postgresql.auto.conf", "a", encoding="utf-8") as fh:
            fh.write("synchronous_standby_names = ''\n")
        self._pg_ctl(self.b, self.pb, "start")
        self.addCleanup(self._pg_ctl, self.b, self.pb, "stop", "-m", "immediate", check=False)
        self._attendre_sync()

    def _pg_ctl(self, data, port, action, *extra, check=True):
        options = "-p %d -k %s" % (port, self.socket)
        return subprocess.run([str(PG_BIN / "pg_ctl"), "-D", str(data), "-o", options,
                               "-l", str(data) + ".log", "-w", action, *extra],
                              check=check, capture_output=True, env=_env_client())

    def _attendre_sync(self):
        for _ in range(100):
            res = subprocess.run(
                [str(PG_BIN / "psql"), "-h", "127.0.0.1", "-p", str(self.pa), "-U", "postgres",
                 "-d", "postgres", "-tAc",
                 "SELECT sync_state FROM pg_stat_replication WHERE application_name = 'b'"],
                capture_output=True, text=True, env=_env_client())
            if res.stdout.strip() == "sync":
                return
            time.sleep(0.1)
        self.fail("standby jamais synchrone")

    def test_bascule_reconnexion_balayage_et_rpo_nul(self):
        tuer = self.tmp / "tuer.sh"
        tuer.write_text(textwrap.dedent("""\
            #!/bin/sh
            set -e
            "%(bin)s/pg_ctl" -D "%(a)s" -m immediate -w stop
            sleep 1
            "%(bin)s/pg_ctl" -D "%(b)s" -w promote
            """ % {"bin": PG_BIN, "a": self.a, "b": self.b}))
        tuer.chmod(0o755)
        rapport = self.tmp / "rapport.json"
        dsn = ("host=127.0.0.1,127.0.0.1 port=%d,%d user=postgres dbname=postgres"
               % (self.pa, self.pb))
        res = subprocess.run(
            [self.python, str(PATRONI / "verifier-clients.py"), "--dsn", dsn,
             "--tuer", str(tuer), "--avant", "1.5", "--apres", "2", "--connect-timeout", "2", "--exigence", "60",
             "--rapport", str(rapport)],
            capture_output=True, text=True, timeout=120,
            env=_env_client())
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        doc = json.loads(rapport.read_text(encoding="utf-8"))
        self.assertTrue(doc["ok"], doc["ecarts"])
        mesures = doc["mesures"]
        self.assertEqual(mesures["rpo_lignes_perdues"], 0)
        self.assertLess(mesures["delai_nouveau_primaire_s"], 60)
        self.assertEqual(len(set(mesures["serveurs_ecrivain"])), 2)
        self.assertIn("reprise_ecouteur_s", mesures)


if __name__ == "__main__":
    unittest.main()
