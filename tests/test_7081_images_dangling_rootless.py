# SPDX-License-Identifier: AGPL-3.0-only
"""#7081 : les images dangling sans étiquette du docker rootless d'ameesh sont
récoltées.

Constat (#6783) : le bureau a supprimé à la main playwright:<none>,
actionlint:<none>, caddy:<none> sur AG1 et AG2. Ce sont les anciennes versions
d'images TIRÉES (ou construites hors du shim) : aucune étiquette ne peut y être
posée, et `reap_images` (#4560) ne faisait que les compter. Dans un docker sans
racine, rien d'autre que l'utilisateur du mesh n'y tire ni n'y construit : ces
images suivent le même délai d'absence continue et la même voie de retrait.

Témoins :
- une image dangling sans étiquette est récoltée après le délai (rootless) ;
- une image taguée est gardée ;
- une image dont descend un conteneur (même arrêté) est gardée, et gardée
  aussi quand cette lecture échoue ;
- hors rootless, elle reste seulement comptée ;
- une autre valeur de `ameesh.lifetime` n'est pas visée ;
- `Runtime.rootless` et `Runtime.image_users` lisent le moteur.
"""
from __future__ import annotations

import subprocess
import time
import unittest

from ameesh import containers, menage, storage

from .support import PgTestCase
from .test_4560_volumes_images_duree_de_vie import _FauxRessources, _i


IDLE = 6 * 3600


class _Rootless(_FauxRessources):
    def __init__(self, *args, rootless=True, users=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_rootless = rootless
        self.users = dict(users or {})
        self.probes = 0

    def rootless(self):
        self.probes += 1
        return self.is_rootless

    def image_users(self, ident):
        return self.users.get(ident, [])


class ReapDanglingRootlessTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM housekeeping_log")
        self.pol = menage.Policy(docker_volume_idle=IDLE)

    def _passes(self, moteur, now):
        """Un relevé journalisé (ouvre l'épisode), puis le passage au délai."""
        premier = menage.reap_images(moteur, self.db, "pc", self.pol, now=now)
        storage.of(self.db).housekeeping.log("pc", premier["entries"], actor="t")
        return premier, menage.reap_images(moteur, self.db, "pc", self.pol,
                                           now=now + IDLE)

    def test_une_image_dangling_sans_etiquette_est_recoltee_apres_le_delai(self):
        now = time.time()
        moteur = _Rootless(images=[_i("sha256:caddy", now - 72 * 3600)])
        premier, fini = self._passes(moteur, now)
        self.assertEqual([e["action"] for e in premier["entries"]], ["waiting"])
        self.assertIn("rootless", premier["entries"][0]["detail"])
        self.assertEqual(moteur.removed_images, ["sha256:caddy"])
        self.assertEqual([e["action"] for e in fini["entries"]], ["removed"])
        self.assertEqual(fini["others_total"], 0)
        self.assertTrue(fini["rootless"])

    def test_une_image_taguee_est_gardee(self):
        now = time.time()
        moteur = _Rootless(images=[_i("sha256:pg", now - 72 * 3600,
                                      tags=["postgres:17"])])
        _, fini = self._passes(moteur, now)
        self.assertEqual(moteur.removed_images, [])
        self.assertEqual(fini["entries"], [])
        # aucune image dangling : le moteur n'est même pas interrogé
        self.assertEqual(moteur.probes, 0)

    def test_une_image_en_usage_est_gardee(self):
        now = time.time()
        moteur = _Rootless(images=[_i("sha256:pw", now - 72 * 3600),
                                   _i("sha256:muet", now - 72 * 3600)],
                           users={"sha256:pw": ["c0ffee"], "sha256:muet": None})
        _, fini = self._passes(moteur, now)
        self.assertEqual(moteur.removed_images, [])
        par_image = {e["path"]: e for e in fini["entries"]}
        self.assertEqual(par_image["sha256:pw"]["action"], "kept")
        self.assertIn("in use by 1 container", par_image["sha256:pw"]["detail"])
        self.assertIn("unreadable", par_image["sha256:muet"]["detail"])
        self.assertEqual(fini["plan"], [])
        # l'épisode reste ouvert : retirée au passage suivant, une fois libre
        storage.of(self.db).housekeeping.log("pc", fini["entries"], actor="t")
        moteur.users = {}
        apres = menage.reap_images(moteur, self.db, "pc", self.pol, now=now + IDLE + 1)
        self.assertEqual(sorted(moteur.removed_images), ["sha256:muet", "sha256:pw"])
        self.assertEqual({e["action"] for e in apres["entries"]}, {"removed"})

    def test_hors_rootless_elle_reste_comptee(self):
        now = time.time()
        moteur = _Rootless(images=[_i("sha256:caddy", now - 72 * 3600)],
                           rootless=False)
        _, fini = self._passes(moteur, now)
        self.assertEqual(moteur.removed_images, [])
        self.assertEqual(fini["entries"], [])
        self.assertEqual(fini["others_total"], 1)
        # moteur sans la lecture rootless (ancien faux, autre moteur) : idem
        ancien = _FauxRessources(images=[_i("sha256:caddy", now - 72 * 3600)])
        essai = menage.reap_images(ancien, self.db, "pc", self.pol, now=now + IDLE)
        self.assertEqual(essai["others_total"], 1)
        self.assertEqual(ancien.removed_images, [])

    def test_une_autre_duree_de_vie_declaree_n_est_pas_visee(self):
        now = time.time()
        moteur = _Rootless(images=[
            _i("sha256:24h", now - 72 * 3600, {containers.LIFETIME_LABEL: "24h"})])
        _, fini = self._passes(moteur, now)
        self.assertEqual(moteur.removed_images, [])
        self.assertEqual(fini["others_total"], 1)

    def test_essai_annonce_sans_supprimer(self):
        now = time.time()
        moteur = _Rootless(images=[_i("sha256:al", now - 72 * 3600)])
        menage.reap_images(moteur, self.db, "pc", self.pol, now=now)
        storage.of(self.db).housekeeping.log(
            "pc", menage.reap_images(moteur, self.db, "pc", self.pol, now=now)["entries"],
            actor="t")
        essai = menage.reap_images(moteur, self.db, "pc", self.pol,
                                   now=now + IDLE, dry_run=True)
        self.assertEqual(moteur.removed_images, [])
        self.assertEqual([p["action"] for p in essai["plan"]], ["suppression"])
        self.assertIn("rootless", essai["plan"][0]["detail"])


class _Proc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


class RuntimeLecturesTest(unittest.TestCase):
    def _runtime(self, *replies):
        calls: list = []
        replies = list(replies)

        def run(argv, **kwargs):
            calls.append(list(argv))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        rt = containers.Runtime(binary="docker", run=run)
        return rt, calls

    def test_rootless_lu_dans_security_options(self):
        rt, calls = self._runtime(
            _Proc('["name=seccomp,profile=builtin","name=rootless","name=cgroupns"]'))
        self.assertTrue(rt.rootless())
        self.assertEqual(calls[0], ["docker", "info", "--format",
                                    "{{json .SecurityOptions}}"])
        rt, _ = self._runtime(_Proc('["name=apparmor","name=seccomp,profile=builtin"]'))
        self.assertFalse(rt.rootless())
        for reply in (_Proc("", returncode=1), _Proc("pas du json"),
                      _Proc('{"a": 1}'), subprocess.TimeoutExpired("docker", 1)):
            rt, _ = self._runtime(reply)
            self.assertIsNone(rt.rootless())

    def test_image_users_filtre_ancestor_arretes_compris(self):
        rt, calls = self._runtime(_Proc("abc\n\ndef\n"))
        self.assertEqual(rt.image_users("sha256:x"), ["abc", "def"])
        self.assertEqual(calls[0], ["docker", "ps", "-a", "--no-trunc", "--quiet",
                                    "--filter", "ancestor=sha256:x"])
        rt, _ = self._runtime(_Proc("", returncode=1))
        self.assertIsNone(rt.image_users("sha256:x"))


if __name__ == "__main__":
    unittest.main()
