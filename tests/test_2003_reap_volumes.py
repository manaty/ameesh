# SPDX-License-Identifier: AGPL-3.0-only
"""#2003 : ménage des volumes Docker — seuls les volumes dont le mesh a DÉCLARÉ
le cycle de vie jetable (`ameesh.bench=disposable`, posée par `pg-up.sh` sur un
banc de lot) sont récoltés, après `docker_volume_idle` d'ABSENCE CONTINUE
mesurée par les relevés successifs. Le banc partagé persistant, les volumes non
étiquetés et les volumes anonymes — sans provenance du mesh — ne sont jamais
supprimés : listés, bornés, avec leur commande `docker volume rm` (précédent
#411). Témoins discriminants des trois défauts bloquants de la relecture cx2 :
B1 aucune attribution par proximité d'heure, B2 le banc persistant reste
persistant, B3 l'âge de création ne tient pas lieu de délai sans conteneur."""
from __future__ import annotations

import contextlib
import datetime
import io
import json
import subprocess
import time
import unittest
from unittest import mock

from ameesh import containers, menage, storage

from .support import PgTestCase


class _Cfg:
    def __init__(self, state_dir: str, host: str = "pc"):
        self.state_dir = state_dir
        self.host = host


class _FauxVolumes:
    """Moteur docker minimal : aucun conteneur réel, des volumes décrits par le
    test, un jeu de volumes RÉFÉRENCÉS par un conteneur (`refs`), leurs
    identités de création et leurs suppressions."""

    binary = "/usr/bin/docker"

    def __init__(self, volumes, created=None, created_unreadable=False,
                 removal_refused=False, refs=()):
        self.rows = list(volumes)
        self.created = dict(created or {})
        self.created_unreadable = created_unreadable
        self.removal_refused = removal_refused
        self.refs = set(refs)
        self.inspected: list[str] | None = None
        self.removed: list[str] = []

    def list_all(self, stopped=False):
        return []

    def volumes(self, *, dangling=True):
        return [dict(r) for r in self.rows
                if r["name"] not in self.removed
                and (not dangling or r["name"] not in self.refs)]

    def volume_created(self, names):
        self.inspected = list(names)
        if self.created_unreadable:
            return None
        return {n: self.created[n] for n in names if n in self.created}

    def remove_volume(self, name):
        if self.removal_refused:
            return False, "volume is in use"
        self.removed.append(name)
        return True, ""


def _v(name, labels=None, driver="local"):
    return {"name": name, "driver": driver, "labels": dict(labels or {}),
            "mountpoint": "/var/lib/docker/volumes/%s/_data" % name}


JETABLE = {containers.BENCH_LABEL: containers.BENCH_DISPOSABLE}
NON_JETABLE = {containers.BENCH_LABEL: "postgres"}
ANONYME = {containers.ANONYMOUS_LABEL: ""}
SANS_ETIQUETTE: dict = {}
BANC_PARTAGE = "ameesh-pg-data"


# ==========================================================================
# lecture du moteur
# ==========================================================================

class MoteurVolumesTest(unittest.TestCase):
    def test_volumes_argv_et_lecture(self):
        appels = []

        def run(argv, **kw):
            appels.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "Name": "pg-banc", "Driver": "local",
                "Labels": "ameesh.bench=disposable,autre=1",
                "Mountpoint": "/var/lib/docker/volumes/pg-banc/_data"}) + "\n", "")

        moteur = containers.Runtime(binary="docker", timeout=4, run=run)
        self.assertEqual(moteur.volumes(), [{
            "name": "pg-banc", "driver": "local",
            "labels": {"ameesh.bench": "disposable", "autre": "1"},
            "mountpoint": "/var/lib/docker/volumes/pg-banc/_data"}])
        self.assertEqual(appels[0][0], ["docker", "volume", "ls", "--filter",
                                        "dangling=true", "--format", "{{json .}}"])
        self.assertEqual(appels[0][1]["timeout"], 4)
        # `dangling=False` : tous les volumes, sans filtre (reprise d'un volume)
        moteur.volumes(dangling=False)
        self.assertEqual(appels[1][0], ["docker", "volume", "ls",
                                        "--format", "{{json .}}"])

    def test_releve_illisible_ou_en_echec_ne_decide_rien(self):
        for sortie, code, attendu in (("{pas du json}\n", 0, None), ("", 0, []),
                                      ("", 1, None)):
            def run(argv, _sortie=sortie, _code=code, **kw):
                return subprocess.CompletedProcess(argv, _code, _sortie, "boom")

            with self.subTest(sortie=sortie, code=code):
                self.assertEqual(
                    containers.Runtime(binary="docker", run=run).volumes(), attendu)

        def run_ko(argv, **kw):
            raise OSError("docker absent")

        self.assertIsNone(containers.Runtime(binary="docker", run=run_ko).volumes())

    def test_volume_created_par_lots_et_refus(self):
        appels = []

        def run(argv, **kw):
            appels.append(argv)
            if argv[1] == "volume" and argv[2] == "inspect":
                noms = list(argv[argv.index("--format") + 2:])
                if "absent" in noms:
                    return subprocess.CompletedProcess(argv, 1, "", "no such volume")
                return subprocess.CompletedProcess(argv, 0, "\n".join(
                    json.dumps({"Name": n, "CreatedAt": "2026-10-16T09:00:00Z"})
                    for n in noms), "")
            return subprocess.CompletedProcess(argv, 1, "", "boom")

        moteur = containers.Runtime(binary="docker", run=run)
        with mock.patch.object(containers, "INSPECT_BATCH", 1):
            stamps = moteur.volume_created(["a", "b"])
        self.assertEqual(len(appels), 2)
        self.assertEqual(appels[0][-1], "a")     # un nom par appel, lots bornés
        self.assertEqual(appels[1][-1], "b")
        attendu = datetime.datetime(2026, 10, 16, 9, 0,
                                    tzinfo=datetime.timezone.utc).timestamp()
        self.assertEqual(set(stamps), {"a", "b"})
        self.assertEqual(stamps["a"], attendu)
        # un nom disparu fait refuser tout l'appel : None, jamais une liste partielle
        self.assertIsNone(moteur.volume_created(["a", "absent"]))

    def test_remove_volume_et_parse_created(self):
        appels = []

        def run(argv, **kw):
            appels.append(argv)
            return subprocess.CompletedProcess(argv, 0 if argv[-1] != "monte" else 1,
                                               "", "volume is in use")

        moteur = containers.Runtime(binary="docker", run=run)
        self.assertEqual(moteur.remove_volume("libre"), (True, ""))
        self.assertEqual(appels[0], ["docker", "volume", "rm", "libre"])
        self.assertEqual(moteur.remove_volume("monte"), (False, "volume is in use"))
        attendu = datetime.datetime(2026, 10, 16, 9, 0,
                                    tzinfo=datetime.timezone.utc).timestamp()
        self.assertEqual(containers.parse_created("2026-10-16T09:00:00Z"), attendu)
        self.assertEqual(containers.parse_created("2026-10-16T11:00:00+02:00"), attendu)
        # sans fuseau : UTC (Docker en pose toujours un)
        self.assertEqual(containers.parse_created("2026-10-16T09:00:00"), attendu)
        for mauvais in ("", None, "hier", "2026-13-45T99:00:00Z"):
            self.assertIsNone(containers.parse_created(mauvais))


# ==========================================================================
# politique
# ==========================================================================

class PolitiqueVolumesTest(unittest.TestCase):
    def test_cles_valeurs_et_erreurs(self):
        parsed, problems, unknown = menage.parse_policy(
            {"docker_volumes": False, "docker_volume_idle": "2h"})
        self.assertEqual(parsed, {"docker_volumes": False, "docker_volume_idle": 7200.0})
        self.assertEqual((problems, unknown), ([], []))
        self.assertEqual(menage.parse_policy({"docker_volume_idle": "1h"})[0],
                         {"docker_volume_idle": 3600.0})
        for mauvais in ("bientôt", 0, -1, True):
            self.assertTrue(menage.parse_policy({"docker_volume_idle": mauvais})[1],
                            mauvais)
        for mauvais in ("non", 1, "oui"):
            self.assertTrue(menage.parse_policy({"docker_volumes": mauvais})[1], mauvais)
        self.assertTrue(menage.Policy().docker_volumes)

    def test_fusion_le_plus_long_et_le_false_gagne(self):
        class Fiche:
            def __init__(self, hk):
                self.policy = type("P", (), {"housekeeping": hk})()

        class Canon:
            def __init__(self, ident, hk):
                self.id, self._f = ident, Fiche(hk)

            def host(self, name):
                return self._f if name == "pc" else None

        a = Canon("a", {"docker_volume_idle": 12 * 3600})
        b = Canon("b", {"docker_volume_idle": 2 * 24 * 3600, "docker_volumes": False})
        pol = menage.effective([a, b], "pc")
        self.assertEqual(pol.docker_volume_idle, 2 * 24 * 3600.0)
        self.assertIs(pol.docker_volumes, False)
        self.assertEqual(pol.origin["docker_volume_idle"], "b")
        self.assertEqual(pol.origin["docker_volumes"], "b")
        # une seule fiche qui coupe suffit, et la plus longue inactivité gagne
        self.assertIs(menage.effective([a, Canon("c", {"docker_volumes": False})],
                                       "pc").docker_volumes, False)


# ==========================================================================
# récolte : déclaration explicite, absence continue, jamais le reste
# ==========================================================================

class ReapVolumesObservationTest(unittest.TestCase):
    def test_reference_closes_episode_even_when_creation_inspection_fails(self):
        """A known reference ends absence even when CreatedAt is unreadable."""
        now = 1_800_000_000.0
        created = now - 72 * 3600
        previous = [{"path": "bench", "action": "waiting",
                     "data": {"created": created,
                              "episode_since": now - 48 * 3600}}]
        journal = mock.Mock()
        journal.latest_by_path.side_effect = lambda *args: previous
        store = mock.Mock(housekeeping=journal)
        runtime = _FauxVolumes([_v("bench", JETABLE)],
                               created={"bench": created},
                               created_unreadable=True, refs={"bench"})
        with mock.patch.object(menage.storage, "of", return_value=store):
            referenced = menage.reap_volumes(
                runtime, object(), "pc", menage.Policy(), now=now)
            # Persist just like run_pass before the next observation.
            previous[:] = referenced["entries"] or previous
            runtime.refs.clear()
            runtime.created_unreadable = False
            detached = menage.reap_volumes(
                runtime, object(), "pc", menage.Policy(), now=now + 60)
        self.assertEqual(runtime.removed, [])
        self.assertEqual([e["action"] for e in referenced["entries"]], ["referenced"])
        self.assertEqual([e["action"] for e in detached["entries"]], ["waiting"])
        self.assertEqual(detached["entries"][0]["data"]["episode_since"], now + 60)


class ReapVolumesTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM turn_resources")
        self.db.execute("DELETE FROM housekeeping_log")
        self.cfgl = _Cfg(self.state)

    def _journalise(self, result, host="pc"):
        """Écrit les relevés du passage, comme `run_pass` le fait."""
        storage.of(self.db).housekeeping.log(host, result["entries"], actor="t")

    def _episode_ancien(self, path, created, since, host="pc"):
        storage.of(self.db).housekeeping.log(host, [{
            "kind": "volume", "action": "waiting", "path": path,
            "detail": "relevé antérieur", "data": {"created": created,
                                                   "episode_since": since}}], actor="t")

    # -- B1 : aucune attribution par proximité d'heure ---------------------

    def test_b1_volume_anonyme_jamais_attribue_par_l_heure(self):
        """Témoin B1 : un volume anonyme créé pendant un tour FINI de l'hôte
        (l'ancien modèle le supprimait) reste sans provenance du mesh : il est
        signalé avec sa commande, jamais supprimé, même jamais inspecté."""
        now = time.time()
        turns = storage.of(self.db).turn_resources
        turns.open_turn("fini", "a1", "pc", pgid=None, label=None)
        turns.close_turn("fini", orphan=True)
        self.db.execute(
            "UPDATE turn_resources SET started_at = to_timestamp(%s),"
            " ended_at = to_timestamp(%s) WHERE turn_id = 'fini'",
            (now - 48 * 3600, now - 40 * 3600))
        moteur = _FauxVolumes(
            [_v("anonyme-dans-un-tour", ANONYME), _v("owner-anonymous-data", ANONYME)],
            created={"anonyme-dans-un-tour": now - 44 * 3600,
                     "owner-anonymous-data": now - 72 * 3600})
        essai = menage.reap_volumes(moteur, self.db, "pc", menage.Policy(), now=now)
        self.assertEqual(moteur.removed, [])
        self.assertIsNone(moteur.inspected)          # rien n'est même inspecté
        self.assertEqual([o["path"] for o in essai["ours"]], [])
        autres = {o["path"]: o for o in essai["others"]}
        self.assertEqual(set(autres), {"anonyme-dans-un-tour", "owner-anonymous-data"})
        self.assertIn("no mesh provenance", autres["anonyme-dans-un-tour"]["detail"])
        self.assertEqual(autres["owner-anonymous-data"]["command"],
                         "docker volume rm owner-anonymous-data")
        self.assertEqual(essai["others_total"], 2)

    # -- B2 : le banc partagé/persistant reste persistant ------------------

    def test_b2_banc_partage_et_etiquette_non_jetable_jamais_recoltes(self):
        """Témoin B2 : `ameesh-pg-data` (volume par défaut de `pg-up.sh`, sans
        étiquette depuis #2003) et un volume étiqueté `postgres` (ancienne
        valeur, non déclarée jetable) ne sont jamais supprimés, même vieux de
        72 h et sans conteneur ; ils ne sont pas même inspectés."""
        now = time.time()
        moteur = _FauxVolumes(
            [_v(BANC_PARTAGE, SANS_ETIQUETTE), _v("banc-etiquette-interim", NON_JETABLE)],
            created={BANC_PARTAGE: now - 72 * 3600,
                     "banc-etiquette-interim": now - 72 * 3600})
        essai = menage.reap_volumes(moteur, self.db, "pc", menage.Policy(), now=now)
        self.assertEqual(moteur.removed, [])
        self.assertIsNone(moteur.inspected)
        autres = {o["path"]: o for o in essai["others"]}
        self.assertEqual(set(autres), {BANC_PARTAGE, "banc-etiquette-interim"})
        self.assertIn("not declared disposable",
                      autres[BANC_PARTAGE]["detail"])
        self.assertIn("ameesh.bench=postgres",
                      autres["banc-etiquette-interim"]["detail"])
        self.assertEqual(essai["others_total"], 2)

    # -- B3 : l'absence continue, pas l'âge de création --------------------

    def test_b3_absence_continue_mesuree_pas_l_age_de_creation(self):
        """Témoin B3 : un volume jetable créé il y a 72 h mais vu SANS
        conteneur pour la première fois n'est pas supprimé au premier relevé ;
        il faut `docker_volume_idle` d'absence continue, relevé après relevé."""
        now = time.time()
        created = now - 72 * 3600
        moteur = _FauxVolumes([_v("banc-vieux", JETABLE)], created={"banc-vieux": created})
        idle = 6 * 3600
        pol = menage.Policy(docker_volume_idle=idle)

        premier = menage.reap_volumes(moteur, self.db, "pc", pol, now=now)
        self.assertEqual(moteur.removed, [])
        self.assertEqual([e["action"] for e in premier["entries"]], ["waiting"])
        self.assertEqual(premier["entries"][0]["data"]["episode_since"], now)
        self.assertEqual(premier["entries"][0]["data"]["created"], created)
        self.assertIn("under 6 h", premier["ours"][0]["detail"])
        self._journalise(premier)

        presque = menage.reap_volumes(moteur, self.db, "pc", pol, now=now + idle - 1)
        self.assertEqual(moteur.removed, [])
        self.assertEqual([e["action"] for e in presque["entries"]], ["waiting"])
        self.assertEqual(presque["entries"][0]["data"]["episode_since"], now)  # pas l'âge
        self._journalise(presque)

        fini = menage.reap_volumes(moteur, self.db, "pc", pol, now=now + idle)
        self.assertEqual(moteur.removed, ["banc-vieux"])
        self.assertEqual([e["action"] for e in fini["entries"]], ["removed"])
        self.assertTrue(fini["entries"][0]["detail"].startswith("declared disposable"))

    def test_reference_referme_l_episode(self):
        """Une reprise du volume referme l'épisode : un nouveau relevé sans
        conteneur repart de zéro, même si le volume est ancien."""
        now = time.time()
        idle = 6 * 3600
        pol = menage.Policy(docker_volume_idle=idle)
        created = now - 72 * 3600
        moteur = _FauxVolumes([_v("banc-x", JETABLE)], created={"banc-x": created})
        self._journalise(menage.reap_volumes(moteur, self.db, "pc", pol, now=now))

        moteur.refs.add("banc-x")                    # repris par un conteneur
        repris = menage.reap_volumes(moteur, self.db, "pc", pol, now=now + idle / 2)
        self.assertEqual([e["action"] for e in repris["entries"]], ["referenced"])
        self.assertEqual(moteur.removed, [])
        self._journalise(repris)

        moteur.refs.clear()                          # de nouveau sans conteneur
        apres = now + idle + 1
        neuf = menage.reap_volumes(moteur, self.db, "pc", pol, now=apres)
        self.assertEqual(moteur.removed, [])
        self.assertEqual([e["action"] for e in neuf["entries"]], ["waiting"])
        self.assertEqual(neuf["entries"][0]["data"]["episode_since"], apres)

    def test_volume_recree_sous_le_meme_nom_ouvre_un_episode(self):
        """Un nom recréé est un AUTRE volume (identité `created`) : il repart
        d'un épisode neuf au lieu d'hériter du précédent."""
        now = time.time()
        idle = 6 * 3600
        pol = menage.Policy(docker_volume_idle=idle)
        self._episode_ancien("banc-y", now - 72 * 3600, now - idle - 1)
        moteur = _FauxVolumes([_v("banc-y", JETABLE)],
                              created={"banc-y": now - 60})     # recréé
        neuf = menage.reap_volumes(moteur, self.db, "pc", pol, now=now)
        self.assertEqual(moteur.removed, [])
        self.assertEqual([e["action"] for e in neuf["entries"]], ["waiting"])
        self.assertEqual(neuf["entries"][0]["data"]["episode_since"], now)

    # -- signalements, refus, bornage, politique ---------------------------

    def test_identite_illisible_signale_sans_suppression(self):
        now = time.time()
        illisible = _FauxVolumes([_v("banc-jetable", JETABLE)],
                                 created_unreadable=True)
        essai = menage.reap_volumes(illisible, self.db, "pc", menage.Policy(), now=now)
        self.assertEqual(illisible.removed, [])
        self.assertEqual([o["action"] for o in essai["ours"]], ["signalé"])
        self.assertIn("identity unreadable", essai["ours"][0]["detail"])
        self.assertEqual([p["action"] for p in essai["plan"]], ["signalé"])

    def test_refus_du_moteur_garde_le_volume(self):
        now = time.time()
        idle = 6 * 3600
        self._episode_ancien("banc-refuse", now - 72 * 3600, now - idle - 1)
        moteur = _FauxVolumes([_v("banc-refuse", JETABLE)],
                              created={"banc-refuse": now - 72 * 3600},
                              removal_refused=True)
        realise = menage.reap_volumes(moteur, self.db, "pc",
                                      menage.Policy(docker_volume_idle=idle), now=now)
        self.assertEqual(moteur.removed, [])
        self.assertEqual([e["action"] for e in realise["entries"]], ["kept"])
        self.assertIn("deletion refused", realise["entries"][0]["detail"])

    def test_releve_illisible_et_politique_coupee(self):
        now = time.time()
        # moteur qui ne répond pas : rien n'est décidé
        for moteur in (None, _FauxVolumes([])):
            if moteur is not None:
                def ko(*a, **k):
                    return None
                moteur.volumes = ko
            self.assertIsNone(menage.reap_volumes(moteur, self.db, "pc",
                                                  menage.Policy(), now=now)["ours"])
        # `docker_volumes: false` : le moteur n'est même pas interrogé
        coupe = _FauxVolumes([_v("banc-jetable", JETABLE)],
                             created={"banc-jetable": now - 72 * 3600})
        vide = menage.reap_volumes(coupe, self.db, "pc",
                                   menage.Policy(docker_volumes=False), now=now)
        self.assertEqual(vide, {"entries": [], "plan": [], "ours": None,
                                "others": None, "others_total": None})
        self.assertIsNone(coupe.inspected)
        self.assertEqual(coupe.removed, [])
        # podman et moteur absent : jamais de suppression (comme la mesure #411)
        podman = _FauxVolumes([_v("banc-jetable", JETABLE)])
        podman.binary = "podman"
        self.assertIsNone(menage.reap_volumes(podman, self.db, "pc",
                                              menage.Policy(), now=now)["ours"])
        self.assertIsNone(menage.reap_volumes(None, self.db, "pc",
                                              menage.Policy(), now=now)["ours"])

    def test_releve_des_non_declares_borne(self):
        moteur = _FauxVolumes([_v("v-%d" % i) for i in range(5)])
        with mock.patch.object(menage, "VOLUMES_TOP", 2):
            essai = menage.reap_volumes(moteur, self.db, "pc", menage.Policy())
        self.assertEqual([o["path"] for o in essai["others"]], ["v-0", "v-1"])
        self.assertEqual(essai["others_total"], 5)
        self.assertEqual(essai["ours"], [])

    # -- bout en bout : run_pass, journal, rapport -------------------------

    def test_run_pass_recolte_journalise_et_rapporte(self):
        now = time.time()
        idle = menage.DEFAULT_DOCKER_VOLUME_IDLE_S
        self._episode_ancien("banc-jetable", now - 72 * 3600, now - idle - 1)
        moteur = _FauxVolumes(
            [_v("banc-jetable", JETABLE), _v("hors-mesh"), _v(BANC_PARTAGE)],
            created={"banc-jetable": now - 72 * 3600},
            refs={"hors-mesh"})          # référencé : pas un candidat au relevé
        rapport = menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc",
                                  actor="t", runtime=moteur, now=now)
        self.assertEqual(moteur.removed, ["banc-jetable"])
        self.assertEqual({e["path"] for e in rapport["entries"] if e["kind"] == "volume"},
                         {"banc-jetable"})
        mesure = storage.of(self.db).housekeeping.last_measure("pc")[0]["data"]["volumes"]
        self.assertEqual({e["path"] for e in mesure["ameesh"]}, {"banc-jetable"})
        self.assertEqual([o["path"] for o in mesure["others"]], [BANC_PARTAGE])
        self.assertEqual(mesure["others"][0]["command"],
                         "docker volume rm %s" % BANC_PARTAGE)
        journal = [r for r in storage.of(self.db).housekeeping.recent("pc", 3600)
                   if r["kind"] == "volume"]
        # l'épisode ensemencé par le test, puis la suppression journalisée
        self.assertEqual([r["action"] for r in journal], ["removed", "waiting"])
        self.assertEqual({r["path"] for r in journal}, {"banc-jetable"})
        sortie = io.StringIO()
        with contextlib.redirect_stdout(sortie):
            menage.print_report(rapport)
        self.assertIn("docker volume rm %s" % BANC_PARTAGE, sortie.getvalue())
        # chaque volume récolté est dit une fois : pas de doublon plan/entrées
        self.assertEqual(sortie.getvalue().count("banc-jetable"), 1)

    def test_run_pass_ecrit_l_episode_pour_le_passage_suivant(self):
        """L'épisode naît au premier relevé du ménage : `run_pass` le
        journalise, et le passage suivant mesure l'absence depuis ce début."""
        now = time.time()
        idle = 6 * 3600
        pol = menage.Policy(docker_volume_idle=idle)
        moteur = _FauxVolumes([_v("banc-neuf", JETABLE)],
                              created={"banc-neuf": now - 72 * 3600})
        menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t",
                        runtime=moteur, now=now)
        self.assertEqual(moteur.removed, [])
        menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t",
                        runtime=moteur, now=now + idle - 1)
        self.assertEqual(moteur.removed, [])
        menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t",
                        runtime=moteur, now=now + idle)
        self.assertEqual(moteur.removed, ["banc-neuf"])


if __name__ == "__main__":
    unittest.main()
