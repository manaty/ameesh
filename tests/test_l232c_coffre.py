# SPDX-License-Identifier: AGPL-3.0-only
"""L232c : magasin de secrets (références, pont 0600, rotation, canari).

Aucune base, aucun root, aucun SSH : racines jetables, transport et contrôle
de santé simulés. La valeur de canari ne doit apparaître nulle part hors du
fichier de destination.
"""
from __future__ import annotations

import grp
import importlib.util
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/mise-a-jour/coffre-release.py"
CANARY = b"sk-canary-0123456789abcdef"
HOST = "hote-test"
REFERENCE = "harnais.env#DEEPSEEK_API_KEY"


def charger():
    spec = importlib.util.spec_from_file_location("coffre_l232c", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


coffre = charger()
UTILISATEUR = pwd.getpwuid(os.geteuid()).pw_name
GROUPE = grp.getgrgid(os.getegid()).gr_name


class ReferenceTest(unittest.TestCase):
    def test_reference_valide(self):
        self.assertEqual(coffre.parse_reference("harnais.env#CLE"), ("harnais.env", "CLE"))

    def test_references_refusees(self):
        for mauvais in ("a/b#K", "../x#K", "a#", "#K", "a#K#L", "a b#K",
                        "a.b#1K", "", "a.env#CLE\n"):
            with self.subTest(reference=mauvais):
                with self.assertRaises(coffre.Refused):
                    coffre.parse_reference(mauvais)

    def test_valeur_d_environnement(self):
        self.assertEqual(coffre.env_value(b"A=1\nCLE=valeur\nB=2\n", "CLE"), b"valeur")
        self.assertEqual(coffre.env_value(b"# commentaire\nCLE=a b c\r\n", "CLE"), b"a b c")

    def test_environnement_refuse(self):
        cas = ((b"CLE=a\nCLE=b\n", "env_duplicate"), (b"A=1\n", "env_missing"),
               (b"CLE=\n", "env_value"), (b"CLE=a\x00b\n", "env_value"),
               (b"PAS DE EGAL\n", "env_format"), (b"CLE=a\n\xff\n", "env_format"))
        for data, categorie in cas:
            with self.subTest(categorie=categorie):
                with self.assertRaises(coffre.Refused) as caught:
                    coffre.env_value(data, "CLE")
                self.assertEqual(caught.exception.category, categorie)

    def test_erreur_sans_valeur(self):
        with self.assertRaises(coffre.Refused) as caught:
            coffre.env_value(b"PAS DE EGAL\n" + CANARY + b"\n", "CLE")
        self.assertNotIn(CANARY, str(caught.exception).encode())


class Materiel(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ameesh-l232c-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "store"
        self.store.mkdir(mode=0o700)
        self.env = self.store / "harnais.env"
        self.env.write_bytes(b"# cles\nDEEPSEEK_API_KEY=" + CANARY + b"\nAUTRE=x\n")
        os.chmod(self.env, 0o600)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.account_dir = self.home / ".config" / "ameesh"
        self.account_dir.mkdir(parents=True, mode=0o700)
        for directory in (self.home, self.home / ".config", self.account_dir):
            os.chmod(directory, 0o700)
        self.key = self.account_dir / "deepseek.key"
        self.destination = self.destination_document()
        self.policy_document = {
            "version": 1, "host": HOST,
            "store": {"root": str(self.store), "max_bytes": 65536},
            "references": {REFERENCE: {"destinations": [dict(self.destination)]}}}
        self.helper_document = {"version": 1, "host": HOST, "max_bytes": 65536,
                                "destinations": [dict(self.destination)]}
        self.policy = coffre.Politique(self.policy_document)
        self.lecteur = coffre.Lecteur(self.policy)
        self.sante = lambda account, path: True
        self.installeur = coffre.Installeur(self.helper_document, read_as=self.sante)

    def destination_document(self, **remplacements):
        document = {"host": HOST, "account": UTILISATEUR, "kind": "account_file",
                    "path": str(self.key), "owner": UTILISATEUR, "group": GROUPE,
                    "mode": "0600", "dir_mode": "0700", "keep_previous": 1,
                    "health": "account_read"}
        document.update(remplacements)
        return document

    def politique(self, destination=None, **remplacements):
        document = json.loads(json.dumps(self.policy_document))
        if destination is not None:
            document["references"][REFERENCE]["destinations"] = [destination]
        document.update(remplacements)
        return coffre.Politique(document)

    def trame(self, value=CANARY, *, operation="install", version=None,
              destination=None, reference=REFERENCE):
        version = version or os.urandom(16).hex()
        cible = dict(self.destination if destination is None else destination)
        request = {"protocol": coffre.PROTOCOL, "operation": operation,
                   "reference": reference, "version": version, "bytes": len(value),
                   "destination": cible}
        return coffre.frame(request, value), request

    def handle(self, value=CANARY, **kw):
        payload, request = self.trame(value, **kw)
        return json.loads(self.installeur.handle(payload)), request

    def installer(self, version, value=CANARY, **kw):
        payload, request = self.trame(value, version=version, **kw)
        return json.loads(self.installeur.handle(payload)), request


class MagasinTest(Materiel):
    def test_lecture_allowlistee(self):
        self.assertEqual(self.lecteur.read(REFERENCE), CANARY)

    def test_reference_hors_politique(self):
        with self.assertRaises(coffre.Refused) as caught:
            self.lecteur.read("autre.env#CLE")
        self.assertEqual(caught.exception.category, "reference_unknown")

    def test_fichier_lisible_par_tous(self):
        os.chmod(self.env, 0o644)
        with self.assertRaises(coffre.Refused) as caught:
            self.lecteur.read(REFERENCE)
        self.assertEqual(caught.exception.category, "file_mode")

    def test_lien_refuse(self):
        (self.store / "lien.env").symlink_to(self.env)
        document = json.loads(json.dumps(self.policy_document))
        document["references"] = {"lien.env#DEEPSEEK_API_KEY": {"destinations": [dict(self.destination)]}}
        lecteur = coffre.Lecteur(coffre.Politique(document))
        with self.assertRaises(coffre.Refused) as caught:
            lecteur.read("lien.env#DEEPSEEK_API_KEY")
        self.assertEqual(caught.exception.category, "file_type")

    def test_racine_non_privee(self):
        os.chmod(self.store, 0o777)
        with self.assertRaises(coffre.Refused) as caught:
            coffre.Lecteur(self.policy)
        self.assertEqual(caught.exception.category, "store_mode")

    def test_taille_bornee(self):
        document = json.loads(json.dumps(self.policy_document))
        document["store"]["max_bytes"] = 8
        lecteur = coffre.Lecteur(coffre.Politique(document))
        with self.assertRaises(coffre.Refused) as caught:
            lecteur.read(REFERENCE)
        self.assertEqual(caught.exception.category, "file_size")


class PolitiqueTest(Materiel):
    def test_cles_exactes(self):
        document = json.loads(json.dumps(self.policy_document))
        document["en_plus"] = 1
        with self.assertRaises(coffre.Refused) as caught:
            coffre.Politique(document)
        self.assertEqual(caught.exception.category, "policy_keys")
        with self.assertRaises(coffre.Refused) as caught:
            self.politique(self.destination_document(en_plus=1))
        self.assertEqual(caught.exception.category, "destination_keys")

    def test_pont_exige_0600_et_le_compte(self):
        for remplacements in ({"mode": "0640"}, {"dir_mode": "0750"},
                              {"owner": "root"}):
            with self.subTest(**remplacements):
                with self.assertRaises(coffre.Refused) as caught:
                    self.politique(self.destination_document(**remplacements))
                self.assertEqual(caught.exception.category, "destination_bridge")

    def test_destination_systeme(self):
        systeme = {"host": HOST, "account": UTILISATEUR, "kind": "env_file",
                   "path": "/etc/ameesh/guest.env", "owner": "root",
                   "group": GROUPE, "mode": "0640", "dir_mode": "0750",
                   "keep_previous": 1, "health": "root_read"}
        coffre.Destination(systeme)
        for remplacements in ({"owner": UTILISATEUR}, {"health": "account_read"},
                              {"mode": "0600"}):
            with self.subTest(**remplacements):
                with self.assertRaises(coffre.Refused):
                    coffre.Destination(dict(systeme, **remplacements))

    def test_destination_inconnue(self):
        with self.assertRaises(coffre.Refused) as caught:
            self.policy.destination(REFERENCE, HOST, "autre-compte")
        self.assertEqual(caught.exception.category, "destination_unknown")


class InstalleurTest(Materiel):
    def test_install_pont(self):
        receipt, request = self.handle()
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["category"], "installed")
        self.assertFalse(receipt["replayed"])
        self.assertEqual(self.key.read_bytes(), CANARY)
        self.assertEqual(stat.S_IMODE(os.stat(self.key).st_mode), 0o600)
        sidecar = self.key.with_name(self.key.name + ".version")
        self.assertEqual(stat.S_IMODE(os.stat(sidecar).st_mode), 0o600)
        record = json.loads(sidecar.read_bytes())
        self.assertEqual(record, {"version": request["version"]})
        self.assertNotIn(CANARY, sidecar.read_bytes())
        self.assertEqual(stat.S_IMODE(os.stat(self.account_dir).st_mode), 0o700)

    def test_rejeu_sans_reecriture(self):
        _, request = self.handle()
        avant = os.stat(self.key).st_ino
        receipt, _ = self.handle(version=request["version"])
        self.assertTrue(receipt["ok"])
        self.assertTrue(receipt["replayed"])
        self.assertEqual(os.stat(self.key).st_ino, avant)
        self.assertEqual(self.key.read_bytes(), CANARY)

    def test_rejeu_divergent(self):
        _, request = self.handle()
        self.key.write_bytes(b"autre-chose")
        os.chmod(self.key, 0o600)
        payload, _ = self.trame(version=request["version"])
        with self.assertRaises(coffre.Uncertain) as caught:
            self.installeur.handle(payload)
        self.assertEqual(caught.exception.category, "replay_mismatch")

    def test_fichier_non_gere_non_adopte(self):
        self.key.write_bytes(b"existant")
        os.chmod(self.key, 0o600)
        receipt, _ = self.handle()
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["category"], "unmanaged_file")
        self.assertEqual(self.key.read_bytes(), b"existant")

    def test_sante_absente_avant_effet(self):
        installeur = coffre.Installeur(self.helper_document, read_as=None)
        payload, _ = self.trame()
        receipt = json.loads(installeur.handle(payload))
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["category"], "health_unavailable")
        self.assertFalse(self.key.exists())

    def test_sante_rouge_restaure_la_precedente(self):
        _, premier = self.handle()
        installeur = coffre.Installeur(self.helper_document,
                                        read_as=lambda account, path: False)
        payload, _ = self.trame(version=os.urandom(16).hex())
        receipt = json.loads(installeur.handle(payload))
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["category"], "health")
        self.assertEqual(receipt["previous"], premier["version"])
        self.assertEqual(self.key.read_bytes(), CANARY)
        sidecar = json.loads(self.key.with_name(self.key.name + ".version").read_bytes())
        self.assertEqual(sidecar, {"version": premier["version"]})

    def test_sante_rouge_sans_precedente(self):
        installeur = coffre.Installeur(self.helper_document,
                                        read_as=lambda account, path: False)
        payload, _ = self.trame()
        receipt = json.loads(installeur.handle(payload))
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["category"], "health")
        self.assertFalse(self.key.exists())
        self.assertFalse(self.key.with_name(self.key.name + ".version").exists())

    def test_rotation_et_revocation(self):
        _, premier = self.handle()
        deuxieme, request = self.handle(value=b"sk-canary-version-2")
        self.assertEqual(deuxieme["previous"], premier["version"])
        gardee = self.key.with_name(self.key.name + ".previous")
        self.assertEqual(gardee.read_bytes(), CANARY)
        self.assertEqual(self.key.read_bytes(), b"sk-canary-version-2")
        revocation, _ = self.handle(b"", operation="revoke", version=premier["version"])
        self.assertTrue(revocation["ok"])
        self.assertEqual(revocation["category"], "revoked")
        self.assertFalse(gardee.exists())
        sidecar = json.loads(self.key.with_name(self.key.name + ".version").read_bytes())
        self.assertEqual(sidecar, {"version": request["version"]})
        courante, _ = self.handle(b"", operation="revoke", version=request["version"])
        self.assertFalse(courante["ok"])
        self.assertEqual(courante["category"], "current_version")
        inconnue, _ = self.handle(b"", operation="revoke", version=os.urandom(16).hex())
        self.assertFalse(inconnue["ok"])
        self.assertEqual(inconnue["category"], "version_unknown")

    def test_revocation_sans_valeur(self):
        receipt, _ = self.handle(b"valeur", operation="revoke",
                                 version=os.urandom(16).hex())
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["category"], "value_unexpected")

    def test_sans_conservation(self):
        document = json.loads(json.dumps(self.helper_document))
        document["destinations"][0]["keep_previous"] = 0
        cible = document["destinations"][0]
        installeur = coffre.Installeur(document, read_as=self.sante)
        premier = json.loads(installeur.handle(self.trame(destination=cible)[0]))
        deuxieme = json.loads(installeur.handle(
            self.trame(value=b"v2", destination=cible)[0]))
        self.assertTrue(premier["ok"], premier)
        self.assertTrue(deuxieme["ok"], deuxieme)
        self.assertEqual(deuxieme["previous"], premier["version"])
        self.assertFalse(self.key.with_name(self.key.name + ".previous").exists())
        sidecar = json.loads(self.key.with_name(self.key.name + ".version").read_bytes())
        self.assertEqual(sidecar, {"version": deuxieme["version"]})

    def test_destination_non_enrolee(self):
        autre = self.destination_document(path=str(self.account_dir / "autre.key"))
        receipt, _ = self.handle(destination=autre)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["category"], "destination_unknown")

    def test_hote_etranger(self):
        receipt, _ = self.handle(destination=self.destination_document(host="autre"))
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["category"], "destination_host")

    def test_version_invalide(self):
        payload, _ = self.trame(version="pas-une-version")
        receipt = json.loads(self.installeur.handle(payload))
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["category"], "version")

    def test_valeur_trop_grande(self):
        document = json.loads(json.dumps(self.helper_document))
        document["max_bytes"] = 8
        installeur = coffre.Installeur(document, read_as=self.sante)
        payload, _ = self.trame()
        receipt = json.loads(installeur.handle(payload))
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["category"], "value_size")
        self.assertFalse(self.key.exists())


class Transport:
    def __init__(self, installeur):
        self.installeur = installeur
        self.payloads = []

    def exchange(self, payload, timeout):
        self.payloads.append(payload)
        return self.installeur.handle(payload)


class LivraisonTest(Materiel):
    def setUp(self):
        super().setUp()
        self.transport = Transport(self.installeur)
        self.journal = coffre.Journal(self.root / "journal")
        self.livraison = coffre.Livraison(self.policy, self.transport, self.journal)

    def journal_lignes(self):
        path = self.root / "journal" / "rotations.jsonl"
        return [json.loads(ligne) for ligne in path.read_text().splitlines()]

    def test_livraison_et_journal(self):
        receipt = self.livraison.deliver(REFERENCE, HOST, UTILISATEUR)
        self.assertTrue(receipt["ok"])
        self.assertEqual(self.key.read_bytes(), CANARY)
        lignes = self.journal_lignes()
        self.assertEqual([ligne["category"] for ligne in lignes],
                         ["intent", "installed"])
        self.assertTrue(all(ligne["reference"] == REFERENCE for ligne in lignes))
        self.assertNotIn(CANARY, (self.root / "journal" / "rotations.jsonl").read_bytes())
        self.assertNotIn(CANARY, json.dumps(receipt).encode())
        entete = self.transport.payloads[0].split(b"\n", 1)[0]
        self.assertNotIn(CANARY, entete)
        self.assertEqual(self.transport.payloads[0].split(b"\n", 1)[1], CANARY)

    def test_transport_en_panne(self):
        class Panne:
            def exchange(self, payload, timeout):
                raise RuntimeError("connexion perdue")

        livraison = coffre.Livraison(self.policy, Panne(), self.journal)
        with self.assertRaises(coffre.Uncertain) as caught:
            livraison.deliver(REFERENCE, HOST, UTILISATEUR)
        self.assertEqual(caught.exception.category, "transport")
        self.assertEqual([ligne["category"] for ligne in self.journal_lignes()],
                         ["intent", "transport"])
        self.assertNotIn(CANARY, (self.root / "journal" / "rotations.jsonl").read_bytes())

    def test_frontiere_de_rotation(self):
        with self.assertRaises(coffre.Refused) as caught:
            self.livraison.deliver(REFERENCE, HOST, UTILISATEUR, operation="rotate")
        self.assertEqual(caught.exception.category, "boundary_unavailable")
        livraison = coffre.Livraison(self.policy, self.transport, self.journal,
                                     boundary=lambda host, account: False)
        with self.assertRaises(coffre.Refused) as caught:
            livraison.deliver(REFERENCE, HOST, UTILISATEUR, operation="rotate")
        self.assertEqual(caught.exception.category, "busy")
        self.assertFalse(self.key.exists())
        livraison = coffre.Livraison(self.policy, self.transport, self.journal,
                                     boundary=lambda host, account: True)
        receipt = livraison.deliver(REFERENCE, HOST, UTILISATEUR, operation="rotate")
        self.assertTrue(receipt["ok"])

    def test_recu_incoherent(self):
        class RecuFaux:
            def exchange(self, payload, timeout):
                request, _ = coffre.unframe(payload)
                receipt = {"protocol": coffre.PROTOCOL, "operation": request["operation"],
                           "reference": request["reference"], "version": "0" * 32,
                           "ok": True, "category": "installed", "replayed": False,
                           "destination": request["destination"], "previous": None}
                return json.dumps(receipt).encode()

        livraison = coffre.Livraison(self.policy, RecuFaux(), self.journal)
        with self.assertRaises(coffre.Uncertain) as caught:
            livraison.deliver(REFERENCE, HOST, UTILISATEUR)
        self.assertEqual(caught.exception.category, "receipt")

        class RecuTronque:
            def exchange(self, payload, timeout):
                return b'{"protocol":"ameesh-secret/1"}'

        livraison = coffre.Livraison(self.policy, RecuTronque(), self.journal)
        with self.assertRaises(coffre.Uncertain) as caught:
            livraison.deliver(REFERENCE, HOST, UTILISATEUR)
        self.assertEqual(caught.exception.category, "receipt")


DRIVER = """
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("coffre", sys.argv[1])
coffre = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coffre)
# pas de root dans le test : le contrôle de santé du compte est simulé.
coffre.default_read_as = lambda account, path: True
raise SystemExit(coffre.helper_main(Path(sys.argv[2]), stdin=sys.stdin.buffer,
                                    stdout=sys.stdout.buffer, stderr=sys.stderr))
"""


class HelperTest(Materiel):
    def enrollment(self, document=None, mode=0o600):
        path = self.root / "helper.json"
        path.write_text(json.dumps(document if document is not None
                                   else self.helper_document))
        os.chmod(path, mode)
        return path

    def driver(self, payload, enrollment):
        return subprocess.run([sys.executable, "-c", DRIVER, str(SCRIPT),
                               str(enrollment)], input=payload,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_processus_canari(self):
        enrollment = self.enrollment()
        payload, request = self.trame()
        done = self.driver(payload, enrollment)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn(CANARY, done.stdout)
        self.assertNotIn(CANARY, done.stderr)
        receipt = json.loads(done.stdout)
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["version"], request["version"])
        self.assertEqual(self.key.read_bytes(), CANARY)
        self.assertNotIn(CANARY, enrollment.read_bytes())

    def test_trame_invalide(self):
        enrollment = self.enrollment()
        done = self.driver(b"pas de trame\n" + CANARY, enrollment)
        self.assertEqual(done.returncode, 2)
        self.assertIn(b"refused:", done.stderr)
        self.assertNotIn(CANARY, done.stdout)
        self.assertNotIn(CANARY, done.stderr)

    def test_document_non_prive(self):
        enrollment = self.enrollment(mode=0o644)
        done = self.driver(self.trame()[0], enrollment)
        self.assertEqual(done.returncode, 2)
        self.assertIn(b"refused: file_mode", done.stderr)
        self.assertFalse(self.key.exists())


if __name__ == "__main__":
    unittest.main()
