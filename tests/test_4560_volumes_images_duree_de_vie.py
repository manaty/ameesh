# SPDX-License-Identifier: AGPL-3.0-only
"""#4560 : les ressources de test ont une DURÉE DE VIE déclarée et le ménage
les reprend.

Constat : 77 volumes Docker anonymes orphelins (5,4 GB) sur agents-2. Les
suites supprimaient leur conteneur Postgres avec `docker rm -f` sans `-v` ;
l'image `postgres:17` déclare `/var/lib/postgresql/data` en VOLUME, donc chaque
suite laissait un volume anonyme que rien ne rattachait plus au mesh. Le shim
d'un tour force désormais `rm -v` (le volume anonyme part avec le conteneur) ;
`scripts/pg-up.sh` pose `ameesh.lifetime=test` sur le volume nommé d'un banc de
lot déclaré jetable ; une image construite dans un tour porte le même label.

Témoins :
- un volume de test labellisé, orphelin et expiré est repris ;
- un volume de base nommé (`ameesh-pg-data`, `*-pg-data`) n'est JAMAIS touché,
  même étiqueté jetable ;
- une image de test SANS TAG, orpheline et expirée est reprise ; une image
  TAGUÉE ne l'est jamais ;
- un volume ou une image sans étiquette n'est jamais supprimé.
"""
from __future__ import annotations

import json
import subprocess
import time
import unittest

from ameesh import containers, menage, storage

from .support import PgTestCase


TEST = {containers.LIFETIME_LABEL: containers.LIFETIME_TEST}
JETABLE = {containers.BENCH_LABEL: containers.BENCH_DISPOSABLE}
ANONYME = {containers.ANONYMOUS_LABEL: ""}


class _Cfg:
    def __init__(self, state_dir: str, host: str = "pc"):
        self.state_dir = state_dir
        self.host = host


def _v(name, labels=None, driver="local"):
    return {"name": name, "driver": driver, "labels": dict(labels or {}),
            "mountpoint": "/var/lib/docker/volumes/%s/_data" % name}


def _i(ident, created, labels=None, tags=()):
    return {"id": ident, "created": created, "labels": dict(labels or {}),
            "tags": list(tags)}


class _FauxRessources:
    """Moteur docker minimal : volumes et images décrits par le test, leurs
    identités, leurs références et leurs suppressions."""

    binary = "/usr/bin/docker"

    def __init__(self, volumes=(), images=(), created=None,
                 created_unreadable=False, removal_refused=False, refs=()):
        self.volume_rows = list(volumes)
        self.image_rows = list(images)
        self.created = dict(created or {})
        self.created_unreadable = created_unreadable
        self.removal_refused = removal_refused
        self.refs = set(refs)
        self.inspected = None
        self.removed_volumes: list[str] = []
        self.removed_images: list[str] = []

    def list_all(self, stopped=False):
        return []

    def volumes(self, *, dangling=True):
        return [dict(r) for r in self.volume_rows
                if r["name"] not in self.removed_volumes
                and (not dangling or r["name"] not in self.refs)]

    def volume_created(self, names):
        self.inspected = list(names)
        if self.created_unreadable:
            return None
        return {n: self.created[n] for n in names if n in self.created}

    def remove_volume(self, name):
        if self.removal_refused:
            return False, "volume is in use"
        self.removed_volumes.append(name)
        return True, ""

    def images(self):
        return [dict(r) for r in self.image_rows
                if r["id"] not in self.removed_images]

    def remove_image(self, ident):
        if self.removal_refused:
            return False, "image is being used by stopped container"
        self.removed_images.append(ident)
        return True, ""


# ==========================================================================
# lecture du moteur : l'image, son label, son tag et sa date
# ==========================================================================

class ImagesMoteurTest(unittest.TestCase):
    def test_images_argv_labels_tags_et_created(self):
        appels = []

        def run(argv, **kw):
            appels.append(argv)
            if argv[1:3] == ["image", "ls"]:
                # le même Id peut apparaître une fois par tag
                return subprocess.CompletedProcess(
                    argv, 0, "sha256:aaa\nsha256:bbb\nsha256:aaa\n", "")
            return subprocess.CompletedProcess(argv, 0, "\n".join([
                json.dumps({"Id": "sha256:aaa",
                            "Created": "2026-10-06T01:33:02.828687149Z",
                            "RepoTags": None,
                            "Config": {"Labels": {"ameesh.lifetime": "test"}}}),
                json.dumps({"Id": "sha256:bbb",
                            "Created": "2026-10-07T00:00:00Z",
                            "RepoTags": ["postgres:17"],
                            "Config": None}),
            ]), "")

        moteur = containers.Runtime(binary="docker", timeout=4, run=run)
        rows = moteur.images()
        self.assertEqual(appels[0], ["docker", "image", "ls", "--no-trunc",
                                     "--format", "{{.ID}}"])
        self.assertEqual(appels[1][:5], ["docker", "image", "inspect",
                                         "--format", "{{json .}}"])
        self.assertEqual(appels[1][-2:], ["sha256:aaa", "sha256:bbb"])
        self.assertEqual([r["id"] for r in rows], ["sha256:aaa", "sha256:bbb"])
        self.assertEqual(rows[0]["labels"], {"ameesh.lifetime": "test"})
        self.assertEqual(rows[0]["tags"], [])          # null -> aucune étiquette
        self.assertEqual(rows[1]["tags"], ["postgres:17"])
        self.assertEqual(rows[1]["labels"], {})
        self.assertAlmostEqual(rows[0]["created"],
                               containers.parse_created("2026-10-06T01:33:02.828687Z"))

    def test_releve_illisible_ou_en_echec_ne_decide_rien(self):
        for sortie, code, attendu in (("{pas du json}\n", 0, None), ("", 0, []),
                                      ("", 1, None)):
            def run(argv, _sortie=sortie, _code=code, **kw):
                return subprocess.CompletedProcess(argv, _code, _sortie, "boom")

            with self.subTest(sortie=sortie, code=code):
                self.assertEqual(
                    containers.Runtime(binary="docker", run=run).images(), attendu)

        def run_ko(argv, **kw):
            raise OSError("docker absent")

        self.assertIsNone(containers.Runtime(binary="docker", run=run_ko).images())

        def run_ls_ko(argv, **kw):
            if argv[1:3] == ["image", "ls"]:
                return subprocess.CompletedProcess(argv, 0, "sha256:aaa\n", "")
            return subprocess.CompletedProcess(argv, 1, "", "no such image")

        self.assertIsNone(containers.Runtime(binary="docker", run=run_ls_ko).images())

    def test_remove_image_et_parse_created(self):
        appels = []

        def run(argv, **kw):
            appels.append(argv)
            return subprocess.CompletedProcess(argv, 0, "null", "")

        moteur = containers.Runtime(binary="docker", run=run)
        self.assertEqual(moteur.remove_image("sha256:aaa"), (True, ""))
        self.assertEqual(appels[0], ["docker", "image", "inspect", "--format",
                                     "{{json .RepoTags}}", "sha256:aaa"])
        self.assertEqual(appels[1], ["docker", "image", "rm", "sha256:aaa"])
        self.assertEqual(containers.parse_created("2026-10-06T01:33:02.828687149Z"),
                         containers.parse_created("2026-10-06T01:33:02.828687Z"))

    def test_remove_image_refuse_un_tag_apparu_depuis_le_releve(self):
        """La course avec un `docker tag` : `image rm <id>` retirerait aussi
        l'unique tag d'une image retaguée. L'inspection fraîche refuse."""
        appels = []

        def run(argv, **kw):
            appels.append(argv)
            return subprocess.CompletedProcess(argv, 0, '["repo:tag"]', "")

        moteur = containers.Runtime(binary="docker", run=run)
        ok, why = moteur.remove_image("sha256:aaa")
        self.assertFalse(ok)
        self.assertIn("tagged again", why)
        self.assertEqual(len(appels), 1)          # aucun `image rm` lancé

    def test_les_volumes_de_base_ne_sont_jamais_recoltes(self):
        for nom in ("ameesh-pg-data", "agent-mesh-pg-data",
                    "mty-amh-impl-06-pg-data", "essai-pg-data"):
            self.assertTrue(containers.is_base_volume(nom), nom)
        for nom in ("ameesh-pg-1333-data", "pg-essai", "banc-jetable", ""):
            self.assertFalse(containers.is_base_volume(nom), nom)


# ==========================================================================
# volumes : label de durée de vie, base protégée
# ==========================================================================

class ReapVolumesDureeDeVieTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM housekeeping_log")
        self.cfgl = _Cfg(self.state)

    def _journalise(self, result, host="pc"):
        storage.of(self.db).housekeeping.log(host, result["entries"], actor="t")

    def test_un_volume_de_test_orphelin_et_expire_est_repris(self):
        now = time.time()
        idle = 6 * 3600
        pol = menage.Policy(docker_volume_idle=idle)
        moteur = _FauxRessources([_v("banc-4560", TEST)],
                                 created={"banc-4560": now - 72 * 3600})
        premier = menage.reap_volumes(moteur, self.db, "pc", pol, now=now)
        self.assertEqual(moteur.removed_volumes, [])
        self.assertEqual([e["action"] for e in premier["entries"]], ["waiting"])
        self._journalise(premier)
        fini = menage.reap_volumes(moteur, self.db, "pc", pol, now=now + idle)
        self.assertEqual(moteur.removed_volumes, ["banc-4560"])
        self.assertEqual([e["action"] for e in fini["entries"]], ["removed"])
        self.assertIn("declared disposable", fini["entries"][0]["detail"])

    def test_l_ancien_label_de_banc_reste_reconnu(self):
        now = time.time()
        idle = 6 * 3600
        pol = menage.Policy(docker_volume_idle=idle)
        moteur = _FauxRessources([_v("banc-2003", JETABLE)],
                                 created={"banc-2003": now - 72 * 3600})
        premier = menage.reap_volumes(moteur, self.db, "pc", pol, now=now)
        self._journalise(premier)
        menage.reap_volumes(moteur, self.db, "pc", pol, now=now + idle)
        self.assertEqual(moteur.removed_volumes, ["banc-2003"])

    def test_un_volume_de_base_etiquete_jetable_n_est_jamais_touche(self):
        now = time.time()
        idle = 6 * 3600
        pol = menage.Policy(docker_volume_idle=idle)
        moteur = _FauxRessources(
            [_v("ameesh-pg-data", TEST),
             _v("mty-amh-impl-06-pg-data", JETABLE),
             _v("banc-4560-data", TEST)],
            created={"banc-4560-data": now - 72 * 3600})
        premier = menage.reap_volumes(moteur, self.db, "pc", pol, now=now)
        # le banc de lot, lui, attend son délai ; les bases sont gardées
        gardes = {e["path"]: e for e in premier["ours"] if e["action"] == "garde"}
        self.assertEqual(set(gardes), {"ameesh-pg-data", "mty-amh-impl-06-pg-data"})
        for ligne in gardes.values():
            self.assertIn("never reaped", ligne["detail"])
            self.assertNotIn("command", ligne)
        self._journalise(premier)
        fini = menage.reap_volumes(moteur, self.db, "pc", pol, now=now + idle)
        self.assertEqual(moteur.removed_volumes, ["banc-4560-data"])
        gardes = {e["path"]: e for e in fini["ours"] if e["action"] == "garde"}
        self.assertEqual(set(gardes), {"ameesh-pg-data", "mty-amh-impl-06-pg-data"})

    def test_le_label_de_duree_de_vie_est_aussi_signale_hors_jetable(self):
        now = time.time()
        moteur = _FauxRessources([_v("banc-autre", {containers.LIFETIME_LABEL: "24h"}),
                                  _v("anonyme", ANONYME)])
        essai = menage.reap_volumes(moteur, self.db, "pc", menage.Policy(), now=now)
        autres = {o["path"]: o for o in essai["others"]}
        self.assertIn("ameesh.lifetime=24h", autres["banc-autre"]["detail"])
        self.assertIn("no mesh provenance", autres["anonyme"]["detail"])
        self.assertEqual(moteur.removed_volumes, [])
        self.assertIsNone(moteur.inspected)

    def test_reference_et_created_illisible_ne_preservent_pas_l_episode(self):
        now = time.time()
        created = now - 72 * 3600
        pol = menage.Policy(docker_volume_idle=6 * 3600)
        moteur = _FauxRessources([_v("banc-4560", TEST)],
                                 created={"banc-4560": created})
        premier = menage.reap_volumes(moteur, self.db, "pc", pol, now=now)
        self.assertEqual([e["action"] for e in premier["entries"]], ["waiting"])
        self._journalise(premier)
        # le volume a été repris : la référence suffit à fermer l'absence,
        # même quand CreatedAt est illisible
        moteur.refs.add("banc-4560")
        moteur.created_unreadable = True
        repris = menage.reap_volumes(moteur, self.db, "pc", pol, now=now + 60)
        self.assertEqual([e["action"] for e in repris["entries"]], ["referenced"])


# ==========================================================================
# images : sans tag seulement, durée de vie déclarée
# ==========================================================================

class ReapImagesTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM housekeeping_log")

    def _journalise(self, result, host="pc"):
        storage.of(self.db).housekeeping.log(host, result["entries"], actor="t")

    def test_une_image_de_test_sans_tag_orpheline_et_expiree_est_reprise(self):
        now = time.time()
        idle = 6 * 3600
        pol = menage.Policy(docker_volume_idle=idle)
        image = _i("sha256:aaa", now - 72 * 3600, TEST)
        moteur = _FauxRessources(images=[image])
        premier = menage.reap_images(moteur, self.db, "pc", pol, now=now)
        self.assertEqual(moteur.removed_images, [])
        self.assertEqual([e["action"] for e in premier["entries"]], ["waiting"])
        self.assertEqual(premier["entries"][0]["data"]["episode_since"], now)
        self._journalise(premier)
        presque = menage.reap_images(moteur, self.db, "pc", pol, now=now + idle - 1)
        self.assertEqual(moteur.removed_images, [])
        fini = menage.reap_images(moteur, self.db, "pc", pol, now=now + idle)
        self.assertEqual(moteur.removed_images, ["sha256:aaa"])
        self.assertEqual([e["action"] for e in fini["entries"]], ["removed"])

    def test_une_image_taguee_n_est_jamais_supprimee(self):
        now = time.time()
        idle = 6 * 3600
        pol = menage.Policy(docker_volume_idle=idle)
        image = _i("sha256:bbb", now - 72 * 3600, TEST, tags=["postgres:17"])
        moteur = _FauxRessources(images=[image])
        essai = menage.reap_images(moteur, self.db, "pc", pol, now=now)
        self.assertEqual(moteur.removed_images, [])
        self.assertEqual(essai["entries"], [])
        # l'ancien épisode d'une image retaguée est refermé : elle ne sera pas
        # reprise à l'instant où elle redeviendrait orpheline
        image["tags"] = []
        self._journalise(menage.reap_images(moteur, self.db, "pc", pol, now=now))
        image["tags"] = ["postgres:17"]
        repris = menage.reap_images(moteur, self.db, "pc", pol, now=now + 1)
        self.assertEqual([e["action"] for e in repris["entries"]], ["referenced"])

    def test_une_image_sans_etiquette_ou_une_base_ne_sont_pas_reprises(self):
        now = time.time()
        idle = 6 * 3600
        pol = menage.Policy(docker_volume_idle=idle)
        moteur = _FauxRessources(images=[
            _i("sha256:libre", now - 72 * 3600),
            _i("sha256:autre", now - 72 * 3600, {containers.LIFETIME_LABEL: "24h"}),
        ])
        essai = menage.reap_images(moteur, self.db, "pc", pol, now=now)
        self.assertEqual(moteur.removed_images, [])
        self.assertEqual(essai["ours"], [])
        # les deux images dangling non déclarées : sans étiquette, et label
        # de durée de vie non reconnu ; aucune n'est supprimée
        self.assertEqual(essai["others_total"], 2)

    def test_identite_illisible_et_refus_du_moteur(self):
        now = time.time()
        idle = 6 * 3600
        pol = menage.Policy(docker_volume_idle=idle)
        illisible = _i("sha256:aaa", None, TEST)
        moteur = _FauxRessources(images=[illisible])
        essai = menage.reap_images(moteur, self.db, "pc", pol, now=now + idle)
        self.assertEqual(moteur.removed_images, [])
        self.assertEqual([e["action"] for e in essai["ours"]], ["signalé"])

        refus = _FauxRessources(images=[_i("sha256:bbb", now - 72 * 3600, TEST)],
                                removal_refused=True)
        self._journalise(menage.reap_images(refus, self.db, "pc", pol, now=now),
                         host="pc")
        fini = menage.reap_images(refus, self.db, "pc", pol, now=now + idle)
        self.assertEqual(refus.removed_images, [])
        self.assertEqual([e["action"] for e in fini["entries"]], ["kept"])
        self.assertIn("deletion refused", fini["entries"][0]["detail"])

    def test_politique_coupee_et_moteur_absent(self):
        now = time.time()
        image = _i("sha256:aaa", now - 72 * 3600, TEST)
        coupe = _FauxRessources(images=[image])
        vide = menage.reap_images(coupe, self.db, "pc",
                                  menage.Policy(docker_images=False), now=now)
        self.assertIsNone(vide["ours"])
        self.assertEqual(coupe.removed_images, [])
        self.assertIsNone(menage.reap_images(None, self.db, "pc",
                                             menage.Policy(), now=now)["ours"])
        podman = _FauxRessources(images=[image])
        podman.binary = "podman"
        self.assertIsNone(menage.reap_images(podman, self.db, "pc",
                                             menage.Policy(), now=now)["ours"])


# ==========================================================================
# bout en bout : run_pass récolte volume et image, et le relevé les porte
# ==========================================================================

class RunPassImagesEtVolumesTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM housekeeping_log")
        self.cfgl = _Cfg(self.state)

    def test_run_pass_recolte_les_deux_et_les_rapporte(self):
        now = time.time()
        idle = menage.DEFAULT_DOCKER_VOLUME_IDLE_S
        created = now - 72 * 3600
        moteur = _FauxRessources(
            volumes=[_v("banc-4560", TEST), _v("ameesh-pg-data", TEST),
                     _v("hors-mesh")],
            images=[_i("sha256:img", created, TEST)],
            created={"banc-4560": created, "ameesh-pg-data": created})
        for kind, path in (("volume", "banc-4560"), ("image", "sha256:img")):
            storage.of(self.db).housekeeping.log("pc", [{
                "kind": kind, "action": "waiting", "path": path,
                "detail": "relevé antérieur",
                "data": {"created": created, "episode_since": now - idle - 1}}],
                actor="t")
        rapport = menage.run_pass(self.cfgl, self.db, menage.Policy(),
                                  host="pc", actor="t", runtime=moteur, now=now)
        self.assertEqual(moteur.removed_volumes, ["banc-4560"])
        self.assertEqual(moteur.removed_images, ["sha256:img"])
        self.assertEqual({e["path"] for e in rapport["entries"]
                          if e["kind"] == "image"}, {"sha256:img"})
        mesure = storage.of(self.db).housekeeping.last_measure("pc")[0]["data"]
        self.assertEqual({e["path"] for e in mesure["volumes"]["ameesh"]},
                         {"banc-4560", "ameesh-pg-data"})
        self.assertEqual({e["path"] for e in mesure["images"]["ameesh"]},
                         {"sha256:img"})
        # la base nommée reste dans le relevé, gardée, sans commande
        garde = [e for e in mesure["volumes"]["ameesh"]
                 if e["path"] == "ameesh-pg-data"]
        self.assertEqual([e["action"] for e in garde], ["garde"])


if __name__ == "__main__":
    unittest.main()
