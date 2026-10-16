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
import threading
import time
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
        payload, _ = self.trame(version=os.urandom(16).hex(), operation="rotate")
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
        deuxieme, request = self.handle(value=b"sk-canary-version-2",
                                        operation="rotate")
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
            self.trame(value=b"v2", destination=cible, operation="rotate")[0]))
        self.assertTrue(premier["ok"], premier)
        self.assertTrue(deuxieme["ok"], deuxieme)
        self.assertEqual(deuxieme["previous"], premier["version"])
        self.assertFalse(self.key.with_name(self.key.name + ".previous").exists())
        sidecar = json.loads(self.key.with_name(self.key.name + ".version").read_bytes())
        self.assertEqual(sidecar, {"version": deuxieme["version"]})

    def test_install_sur_destination_geree_refuse_avant_effet(self):
        _, premier = self.handle()
        avant = self.key.read_bytes()
        receipt, request = self.handle(value=b"sk-canary-autre", operation="install")
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["category"], "rotation_required")
        self.assertEqual(request["operation"], "install")
        self.assertEqual(self.key.read_bytes(), avant)
        sidecar = json.loads(self.key.with_name(self.key.name + ".version").read_bytes())
        self.assertEqual(sidecar, {"version": premier["version"]})
        self.assertFalse(self.key.with_name(self.key.name + ".previous").exists())

    def test_rotate_sur_destination_non_geree_installe(self):
        # une rotation sur une destination non gérée ne remplace aucune valeur :
        # la frontière de tour reste tenue par le chemin rotate côté mesh.
        receipt, _ = self.handle(operation="rotate")
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(self.key.read_bytes(), CANARY)

    def test_reprise_apres_crash_entre_valeur_et_commit(self):
        class Arret(Exception):
            pass

        _, premier = self.handle()
        etat = {"casse": True}

        def sante(destination):
            if etat["casse"]:
                raise Arret()
            return True

        installeur = coffre.Installeur(self.helper_document, read_as=self.sante)
        installeur._health = sante
        payload, request = self.trame(value=b"sk-canary-v2", operation="rotate")
        with self.assertRaises(Arret):
            installeur.handle(payload)
        sidecar = self.key.with_name(self.key.name + ".version")
        pending = self.key.with_name(self.key.name + ".pending")
        rollback = self.key.with_name(self.key.name + ".rollback")
        # la valeur non commitée est sur le disque, l'état lu reste l'ancien.
        self.assertEqual(self.key.read_bytes(), b"sk-canary-v2")
        self.assertEqual(json.loads(sidecar.read_bytes()), {"version": premier["version"]})
        self.assertTrue(pending.exists() and rollback.exists())
        # reprise sous la même identité : l'intention se défait, puis se refait.
        etat["casse"] = False
        receipt = json.loads(installeur.handle(payload))
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(receipt["version"], request["version"])
        self.assertFalse(receipt["replayed"])
        self.assertEqual(self.key.read_bytes(), b"sk-canary-v2")
        self.assertEqual(json.loads(sidecar.read_bytes()),
                         {"version": request["version"], "previous": premier["version"]})
        self.assertEqual(self.key.with_name(self.key.name + ".previous").read_bytes(), CANARY)
        self.assertFalse(pending.exists() or rollback.exists())

    def test_reprise_apres_crash_entre_intention_et_valeur(self):
        class Arret(Exception):
            pass

        installeur = coffre.Installeur(self.helper_document, read_as=self.sante)
        original = installeur._atomic_write

        def ecriture(path, data, destination, *, mode, owner, group):
            if path == self.key:
                raise Arret()
            return original(path, data, destination, mode=mode, owner=owner,
                            group=group)

        installeur._atomic_write = ecriture
        payload, request = self.trame()
        with self.assertRaises(Arret):
            installeur.handle(payload)
        pending = self.key.with_name(self.key.name + ".pending")
        self.assertTrue(pending.exists())
        self.assertFalse(self.key.exists())
        # reprise : l'intention sans effet se défait, puis le même install passe.
        installeur._atomic_write = original
        receipt = json.loads(installeur.handle(payload))
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(self.key.read_bytes(), CANARY)
        sidecar = json.loads(self.key.with_name(self.key.name + ".version").read_bytes())
        self.assertEqual(sidecar, {"version": request["version"]})
        self.assertFalse(pending.exists())

    def test_reprise_apres_copie_de_retour_sans_intention(self):
        class Arret(Exception):
            pass

        _, premier = self.handle()
        installeur = coffre.Installeur(self.helper_document, read_as=self.sante)
        original = installeur._atomic_write
        pending = self.key.with_name(self.key.name + ".pending")

        def ecriture(path, data, destination, *, mode, owner, group):
            if path == pending:
                raise Arret()
            return original(path, data, destination, mode=mode, owner=owner,
                            group=group)

        installeur._atomic_write = ecriture
        payload, request = self.trame(value=b"sk-canary-v2", operation="rotate")
        with self.assertRaises(Arret):
            installeur.handle(payload)
        rollback = self.key.with_name(self.key.name + ".rollback")
        # la copie de retour existe sans intention : l'ancien etat fait foi.
        self.assertTrue(rollback.exists())
        self.assertFalse(pending.exists())
        self.assertEqual(self.key.read_bytes(), CANARY)
        sidecar = self.key.with_name(self.key.name + ".version")
        self.assertEqual(json.loads(sidecar.read_bytes()), {"version": premier["version"]})
        # la reprise rejoue la rotation et n'ecrase pas la precedente.
        installeur._atomic_write = original
        receipt = json.loads(installeur.handle(payload))
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(self.key.read_bytes(), b"sk-canary-v2")
        self.assertEqual(json.loads(sidecar.read_bytes()),
                         {"version": request["version"], "previous": premier["version"]})
        self.assertEqual(self.key.with_name(self.key.name + ".previous").read_bytes(), CANARY)
        self.assertFalse(rollback.exists() or pending.exists())

    def test_reprise_apres_commit_sans_menage(self):
        class Arret(Exception):
            pass

        _, premier = self.handle()
        installeur = coffre.Installeur(self.helper_document, read_as=self.sante)
        menage = installeur._finish

        def finish(destination, keep):
            raise Arret()

        installeur._finish = finish
        payload, request = self.trame(value=b"sk-canary-v2", operation="rotate")
        with self.assertRaises(Arret):
            installeur.handle(payload)
        sidecar = self.key.with_name(self.key.name + ".version")
        pending = self.key.with_name(self.key.name + ".pending")
        rollback = self.key.with_name(self.key.name + ".rollback")
        self.assertEqual(json.loads(sidecar.read_bytes()),
                         {"version": request["version"], "previous": premier["version"]})
        self.assertTrue(pending.exists() and rollback.exists())
        # reprise : le commit est constaté, le ménage se termine sans réécriture.
        installeur._finish = menage
        receipt = json.loads(installeur.handle(payload))
        self.assertTrue(receipt["ok"], receipt)
        self.assertTrue(receipt["replayed"])
        self.assertFalse(pending.exists() or rollback.exists())
        self.assertEqual(self.key.read_bytes(), b"sk-canary-v2")
        self.assertEqual(json.loads(sidecar.read_bytes()),
                         {"version": request["version"], "previous": premier["version"]})
        self.assertEqual(self.key.with_name(self.key.name + ".previous").read_bytes(), CANARY)

    def test_reprise_deux_arrets_successifs_dont_un_dans_la_reprise(self):
        class Arret(Exception):
            pass

        _, premier = self.handle()
        etat = {"casse": True}

        def sante(destination):
            if etat["casse"]:
                raise Arret()
            return True

        installeur = coffre.Installeur(self.helper_document, read_as=self.sante)
        installeur._health = sante
        pending = self.key.with_name(self.key.name + ".pending")
        rollback = self.key.with_name(self.key.name + ".rollback")
        sidecar = self.key.with_name(self.key.name + ".version")
        payload, request = self.trame(value=b"sk-canary-v2", operation="rotate")

        # premier arret : la rotation a publie la valeur sans commit.
        with self.assertRaises(Arret):
            installeur.handle(payload)
        self.assertEqual(self.key.read_bytes(), b"sk-canary-v2")
        self.assertEqual(json.loads(sidecar.read_bytes()),
                         {"version": premier["version"]})
        self.assertTrue(pending.exists() and rollback.exists())

        # second arret, dans la reprise : la valeur et le record sont restaures
        # durablement, mais l'intention n'est pas encore effacee.
        etat["casse"] = False
        menage = installeur._unlink_pending

        def arret(destination):
            raise Arret()

        installeur._unlink_pending = arret
        with self.assertRaises(Arret):
            installeur.handle(payload)
        self.assertEqual(self.key.read_bytes(), CANARY)
        self.assertEqual(json.loads(sidecar.read_bytes()),
                         {"version": premier["version"]})
        self.assertTrue(pending.exists())
        # la copie de retour ne part qu'apres l'effacement de l'intention :
        # la deuxieme reprise reste decidable au lieu de manquer sa copie.
        self.assertTrue(rollback.exists())

        # troisieme invocation : la reprise se rejoue a l'identique, puis la
        # rotation aboutit.
        installeur._unlink_pending = menage
        receipt = json.loads(installeur.handle(payload))
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(receipt["version"], request["version"])
        self.assertFalse(receipt["replayed"])
        self.assertEqual(self.key.read_bytes(), b"sk-canary-v2")
        self.assertEqual(json.loads(sidecar.read_bytes()),
                         {"version": request["version"],
                          "previous": premier["version"]})
        self.assertEqual(self.key.with_name(self.key.name + ".previous").read_bytes(), CANARY)
        self.assertFalse(pending.exists() or rollback.exists())

    def test_reprise_apres_intention_effacee_copie_orpheline(self):
        class Arret(Exception):
            pass

        _, premier = self.handle()
        etat = {"casse": True}

        def sante(destination):
            if etat["casse"]:
                raise Arret()
            return True

        installeur = coffre.Installeur(self.helper_document, read_as=self.sante)
        installeur._health = sante
        pending = self.key.with_name(self.key.name + ".pending")
        rollback = self.key.with_name(self.key.name + ".rollback")
        sidecar = self.key.with_name(self.key.name + ".version")
        payload, request = self.trame(value=b"sk-canary-v2", operation="rotate")

        # premier arret : la valeur est publiee, le record ne l'est pas.
        with self.assertRaises(Arret):
            installeur.handle(payload)
        etat["casse"] = False

        # la reprise efface l'intention durablement, puis meurt avant de
        # retirer sa copie de retour.
        menage = installeur._discard_rollback

        def arret(destination):
            raise Arret()

        installeur._discard_rollback = arret
        with self.assertRaises(Arret):
            installeur.handle(payload)
        self.assertFalse(pending.exists())
        self.assertTrue(rollback.exists())
        self.assertEqual(self.key.read_bytes(), CANARY)
        self.assertEqual(json.loads(sidecar.read_bytes()),
                         {"version": premier["version"]})

        # la copie orpheline est inerte : l'operation suivante la retire et
        # refait la rotation depuis l'etat restaure.
        installeur._discard_rollback = menage
        receipt = json.loads(installeur.handle(payload))
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(self.key.read_bytes(), b"sk-canary-v2")
        self.assertEqual(json.loads(sidecar.read_bytes()),
                         {"version": request["version"],
                          "previous": premier["version"]})
        self.assertEqual(self.key.with_name(self.key.name + ".previous").read_bytes(), CANARY)
        self.assertFalse(pending.exists() or rollback.exists())

    def test_menage_les_transitions_sont_fsyncees(self):
        class Arret(Exception):
            pass

        _, premier = self.handle()
        etat = {"casse": True}

        def sante(destination):
            if etat["casse"]:
                raise Arret()
            return True

        installeur = coffre.Installeur(self.helper_document, read_as=self.sante)
        installeur._health = sante
        pending = self.key.with_name(self.key.name + ".pending")
        rollback = self.key.with_name(self.key.name + ".rollback")
        payload, _ = self.trame(value=b"sk-canary-v2", operation="rotate")

        # premier arret : la valeur est publiee, le record ne l'est pas.
        with self.assertRaises(Arret):
            installeur.handle(payload)
        etat["casse"] = False

        # la reprise restaure, efface l'intention puis retire la copie : chaque
        # transition de menage est observee avec le fsync du repertoire.
        evenements = []
        menage = installeur._unlink_pending
        copie = installeur._discard_rollback

        def unlink_pending(destination):
            evenements.append(("avant-intention", pending.exists(),
                               rollback.exists()))
            menage(destination)
            evenements.append(("apres-intention", pending.exists(),
                               rollback.exists()))

        def discard_rollback(destination):
            evenements.append(("avant-copie", pending.exists(),
                               rollback.exists()))
            copie(destination)
            evenements.append(("apres-copie", pending.exists(),
                               rollback.exists()))

        vrai = os.fsync
        dossier = os.stat(self.account_dir)

        def fsync(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and info.st_ino == dossier.st_ino:
                evenements.append(("fsync", pending.exists(), rollback.exists()))
            return vrai(fd)

        installeur._unlink_pending = unlink_pending
        installeur._discard_rollback = discard_rollback
        os.fsync = fsync
        try:
            receipt = json.loads(installeur.handle(payload))
        finally:
            os.fsync = vrai
        self.assertTrue(receipt["ok"], receipt)
        debut = evenements.index(("avant-intention", True, True))
        milieu = evenements.index(("avant-copie", False, True))
        fin = evenements.index(("apres-copie", False, False))
        # l'effacement de l'intention est fsynce avant que la copie ne parte.
        self.assertIn(("fsync", False, True), evenements[debut:milieu])
        # le retrait de la copie est fsynce a son tour.
        self.assertIn(("fsync", False, False), evenements[milieu:fin])

    def test_temporaires_orphelins_retires(self):
        reste = self.key.with_name("." + self.key.name + ".tmp-abcdef")
        reste.write_bytes(b"reste d'un arret brutal")
        os.chmod(reste, 0o600)
        receipt, _ = self.handle()
        self.assertTrue(receipt["ok"])
        self.assertFalse(reste.exists())

    def test_rotations_concurrentes_serialisees(self):
        self.handle()
        depart = threading.Barrier(2, timeout=15)
        resultats = []

        def tour(value, version):
            payload, _ = self.trame(value=value, operation="rotate", version=version)
            installeur = coffre.Installeur(self.helper_document, read_as=self.sante)
            depart.wait()
            receipt = json.loads(installeur.handle(payload))
            resultats.append((receipt, value, version))

        threads = [threading.Thread(target=tour, args=(b"valeur-A", "a" * 32)),
                   threading.Thread(target=tour, args=(b"valeur-B", "b" * 32))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(resultats), 2)
        self.assertTrue(all(receipt["ok"] for receipt, _, _ in resultats), resultats)
        sidecar = json.loads(self.key.with_name(self.key.name + ".version").read_bytes())
        valeurs = {version: value for _, value, version in resultats}
        self.assertIn(sidecar["version"], valeurs)
        self.assertEqual(self.key.read_bytes(), valeurs[sidecar["version"]])
        autre = [version for version in valeurs if version != sidecar["version"]]
        self.assertEqual(len(autre), 1)
        self.assertEqual(sidecar["previous"], autre[0])
        self.assertEqual(self.key.with_name(self.key.name + ".previous").read_bytes(),
                         valeurs[autre[0]])
        self.assertFalse(self.key.with_name(self.key.name + ".pending").exists())
        self.assertFalse(self.key.with_name(self.key.name + ".rollback").exists())

    def test_rotation_et_revocation_concurrentes(self):
        _, premier = self.handle()
        depart = threading.Barrier(2, timeout=15)
        resultats = {}

        def rotation():
            payload, _ = self.trame(value=b"sk-canary-v1", operation="rotate",
                                    version="c" * 32)
            depart.wait()
            resultats["rotation"] = json.loads(
                coffre.Installeur(self.helper_document, read_as=self.sante).handle(payload))

        def revocation():
            payload, _ = self.trame(b"", operation="revoke", version=premier["version"])
            depart.wait()
            resultats["revocation"] = json.loads(
                coffre.Installeur(self.helper_document, read_as=self.sante).handle(payload))

        threads = [threading.Thread(target=rotation),
                   threading.Thread(target=revocation)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertTrue(resultats["rotation"]["ok"], resultats["rotation"])
        self.assertEqual(self.key.read_bytes(), b"sk-canary-v1")
        sidecar = json.loads(self.key.with_name(self.key.name + ".version").read_bytes())
        self.assertEqual(sidecar["version"], "c" * 32)
        gardee = self.key.with_name(self.key.name + ".previous")
        if resultats["revocation"]["ok"]:
            # la rotation a précédé la révocation : la précédente est révoquée.
            self.assertEqual(sidecar, {"version": "c" * 32})
            self.assertFalse(gardee.exists())
        else:
            # la révocation a précédé la rotation : elle vise la version
            # courante, refusée sans effet, et la rotation garde la précédente.
            self.assertEqual(resultats["revocation"]["category"], "current_version")
            self.assertEqual(sidecar,
                             {"version": "c" * 32, "previous": premier["version"]})
            self.assertEqual(gardee.read_bytes(), CANARY)

    def test_verrou_inter_processus(self):
        import fcntl

        self.handle()
        verrou = self.key.with_name(self.key.name + ".lock")
        programme = (
            "import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_RDWR)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('tenu', flush=True)\n"
            "time.sleep(1.5)\n")
        detenteur = subprocess.Popen([sys.executable, "-c", programme, str(verrou)],
                                     stdout=subprocess.PIPE)
        try:
            self.assertEqual(detenteur.stdout.readline().strip(), b"tenu")
            debut = time.monotonic()
            receipt, _ = self.handle(value=b"valeur-v2", operation="rotate")
            duree = time.monotonic() - debut
        finally:
            detenteur.wait(10)
        self.assertTrue(receipt["ok"], receipt)
        # le helper a attendu le detenteur : le verrou traverse les processus.
        self.assertGreaterEqual(duree, 1.0)
        self.assertEqual(self.key.read_bytes(), b"valeur-v2")

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


class CompteIsoleTest(Materiel):
    """B1 R2 dev4 : root n'agit pas par chemin dans le répertoire du compte.

    Hors root, le fils abandonne ses droits vers le même compte : le chemin
    réel (fork, setresuid, reçu par tube) est exercé sans privilège.
    """

    def setUp(self):
        super().setUp()
        self.installeur = coffre.Installeur(self.helper_document, read_as=None,
                                            isolate=True)

    def test_installation_rotation_revocation_dans_le_fils(self):
        premier, _ = self.handle()
        self.assertTrue(premier["ok"], premier)
        self.assertEqual(self.key.read_bytes(), CANARY)
        rejeu, _ = self.installer(premier["version"])
        self.assertTrue(rejeu["replayed"])
        second, _ = self.handle(value=b"sk-canary-v2", operation="rotate")
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["previous"], premier["version"])
        revoque, _ = self.handle(b"", operation="revoke", version=premier["version"])
        self.assertTrue(revoque["ok"], revoque)
        self.assertFalse(self.key.with_name(self.key.name + ".previous").exists())

    def test_refus_du_fils_rendu_en_categorie(self):
        self.handle()
        receipt, _ = self.handle(value=b"autre")
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["category"], "rotation_required")

    def test_sante_lue_par_le_compte(self):
        # la santé du fils est l'ouverture réelle par le compte nommé : un
        # autre compte que celui du fils ne la rend pas, et l'installation
        # se défait.
        original = coffre._read_as_self

        def sabote(account, path):
            self.assertTrue(original(account, path))
            return original("compte-absent", path)

        coffre._read_as_self = sabote
        self.addCleanup(setattr, coffre, "_read_as_self", original)
        receipt, _ = self.handle()
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["category"], "health")
        self.assertFalse(self.key.exists())

    def test_fils_mort_issue_incertaine(self):
        original = coffre.Installeur.install
        coffre.Installeur.install = lambda *a, **k: os._exit(9)
        self.addCleanup(setattr, coffre.Installeur, "install", original)
        payload, _ = self.trame()
        with self.assertRaises(coffre.Uncertain) as caught:
            self.installeur.handle(payload)
        self.assertEqual(caught.exception.category, "account_transaction")

    def test_compte_root_refuse(self):
        document = self.destination_document(account="root", owner="root",
                                             group="root")
        helper = dict(self.helper_document, destinations=[document])
        # uid injecté à 0 : le refus tombe avant le fils et avant tout effet.
        installeur = coffre.Installeur(helper, read_as=None, uid=0, isolate=True)
        payload, _ = self.trame(destination=document)
        receipt = json.loads(installeur.handle(payload))
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["category"], "destination_bridge")


class LiensEtSanteTest(Materiel):
    def test_lien_dur_refuse(self):
        self.handle()
        ailleurs = self.root / "ailleurs"
        os.link(self.key, ailleurs)
        receipt, _ = self.handle(value=b"v2", operation="rotate")
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["category"], "file_links")
        self.assertEqual(ailleurs.read_bytes(), CANARY)

    def test_refus_de_sante_apres_effet_defait(self):
        # N2 R2 dev4 : un refus levé par le contrôle après l'écriture ne rend
        # pas un reçu « sans effet » sur une valeur écrite.
        def refuse(account, path):
            raise coffre.Refused("health_unavailable")

        installeur = coffre.Installeur(self.helper_document, read_as=refuse)
        payload, _ = self.trame()
        receipt = json.loads(installeur.handle(payload))
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["category"], "health")
        self.assertFalse(self.key.exists())
        self.assertFalse(self.key.with_name(self.key.name + ".pending").exists())


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

    def test_deliver_install_ne_contourne_pas_la_frontiere(self):
        premier = self.livraison.deliver(REFERENCE, HOST, UTILISATEUR)
        self.assertTrue(premier["ok"])
        avant = self.key.read_bytes()
        receipt = self.livraison.deliver(REFERENCE, HOST, UTILISATEUR)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["category"], "rotation_required")
        self.assertEqual(self.key.read_bytes(), avant)
        self.assertEqual(self.journal_lignes()[-1]["category"], "rotation_required")
        # rotate sans frontière reste refusé avant tout effet.
        with self.assertRaises(coffre.Refused) as caught:
            self.livraison.deliver(REFERENCE, HOST, UTILISATEUR, operation="rotate")
        self.assertEqual(caught.exception.category, "boundary_unavailable")
        self.assertEqual(self.key.read_bytes(), avant)

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
