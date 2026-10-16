# SPDX-License-Identifier: AGPL-3.0-only
"""#2003 : ménage des volumes Docker — seuls les volumes que le mesh peut
ATTRIBUER sont récoltés (banc étiqueté `ameesh.bench` par `scripts/pg-up.sh`,
volume anonyme d'un tour fini de cet hôte), après `docker_volume_idle`. Tout
le reste est listé, borné, avec sa commande `docker volume rm` : jamais
supprimé (précédent #411, les volumes nommés existants sont conservés)."""
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
    """Moteur docker minimal : aucun conteneur, des volumes décrits par le
    test, leurs dates de création et leurs suppressions."""

    binary = "/usr/bin/docker"

    def __init__(self, volumes, created=None, created_unreadable=False,
                 removal_refused=False):
        self.rows = list(volumes)
        self.created = dict(created or {})
        self.created_unreadable = created_unreadable
        self.removal_refused = removal_refused
        self.inspected: list[str] | None = None
        self.removed: list[str] = []

    def list_all(self, stopped=False):
        return []

    def volumes(self, *, dangling=True):
        return [dict(r) for r in self.rows if r["name"] not in self.removed]

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


BANC = {containers.BENCH_LABEL: "postgres"}
ANONYME = {containers.ANONYMOUS_LABEL: ""}


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
                "Labels": "ameesh.bench=postgres,autre=1",
                "Mountpoint": "/var/lib/docker/volumes/pg-banc/_data"}) + "\n", "")

        moteur = containers.Runtime(binary="docker", timeout=4, run=run)
        self.assertEqual(moteur.volumes(), [{
            "name": "pg-banc", "driver": "local",
            "labels": {"ameesh.bench": "postgres", "autre": "1"},
            "mountpoint": "/var/lib/docker/volumes/pg-banc/_data"}])
        self.assertEqual(appels[0][0], ["docker", "volume", "ls", "--filter",
                                        "dangling=true", "--format", "{{json .}}"])
        self.assertEqual(appels[0][1]["timeout"], 4)

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
# attribution et récolte (base)
# ==========================================================================

class ReapVolumesTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM turn_resources")
        self.db.execute("DELETE FROM housekeeping_log")
        self.cfgl = _Cfg(self.state)

    def _turns(self):
        return storage.of(self.db).turn_resources

    def test_turn_at_intervalle_et_fin_de_grace(self):
        turns = self._turns()
        turns.open_turn("t1", "a1", "pc", pgid=None, label=None)
        turns.begin_grace("t1")
        ligne = turns.get("t1")
        self.assertEqual(ligne["status"], "running")
        self.assertIsNotNone(ligne["ended_ts"])
        milieu = (ligne["started_ts"] + ligne["ended_ts"]) / 2
        self.assertEqual(turns.turn_at("pc", milieu)["turn_id"], "t1")
        # la grâce ne couvre plus rien après son `ended_at`
        self.assertIsNone(turns.turn_at("pc", ligne["ended_ts"] + 1))
        self.assertIsNone(turns.turn_at("pc", ligne["started_ts"] - 1))
        # un autre hôte n'attribue rien
        self.assertIsNone(turns.turn_at("autre", milieu))

    def test_attribution_recolte_et_signalement(self):
        now = time.time()
        turns = self._turns()
        turns.open_turn("fini", "a1", "pc", pgid=None, label=None)
        turns.open_turn("encours", "a2", "pc", pgid=None, label=None)
        turns.close_turn("fini", orphan=True)
        self.db.execute(
            "UPDATE turn_resources SET started_at = to_timestamp(%s),"
            " ended_at = to_timestamp(%s) WHERE turn_id = 'fini'",
            (now - 48 * 3600, now - 40 * 3600))
        self.db.execute(
            "UPDATE turn_resources SET started_at = to_timestamp(%s)"
            " WHERE turn_id = 'encours'", (now - 2 * 3600,))
        moteur = _FauxVolumes(
            [_v("banc-vieux", BANC), _v("banc-jeune", BANC), _v("banc-sans-date", BANC),
             _v("anonyme-fini", ANONYME), _v("anonyme-encours", ANONYME),
             _v("anonyme-hors-tour", ANONYME), _v("anonyme-sans-date", ANONYME),
             _v("nomme-sans-etiquette")],
            created={"banc-vieux": now - 48 * 3600, "banc-jeune": now - 3600,
                     "anonyme-fini": now - 44 * 3600, "anonyme-encours": now - 3600,
                     "anonyme-hors-tour": now - 30 * 3600})
        essai = menage.reap_volumes(moteur, self.db, "pc", menage.Policy(),
                                    dry_run=True, now=now)
        self.assertEqual(moteur.removed, [])
        self.assertEqual(essai["entries"], [])
        self.assertEqual({p["path"] for p in essai["plan"] if p["action"] == "suppression"},
                         {"banc-vieux", "anonyme-fini"})
        self.assertEqual({p["path"] for p in essai["plan"] if p["action"] == "signalé"},
                         {"banc-sans-date"})
        self.assertEqual({o["path"]: o["action"] for o in essai["ours"]},
                         {"banc-vieux": "suppression", "banc-jeune": "attend",
                          "banc-sans-date": "signalé", "anonyme-fini": "suppression",
                          "anonyme-encours": "attend"})
        # l'anonyme d'un tour en cours attend, avec le nom du tour
        attente = {o["path"]: o["detail"] for o in essai["ours"]
                   if o["action"] == "attend"}
        self.assertIn("turn encours in progress", attente["anonyme-encours"])
        self.assertIn("under 24 h", attente["banc-jeune"])
        # les non attribués sont listés, bornés, avec leur commande : jamais supprimés
        autres = {o["path"]: o for o in essai["others"]}
        self.assertEqual(set(autres), {"nomme-sans-etiquette", "anonyme-hors-tour",
                                       "anonyme-sans-date"})
        self.assertEqual(autres["nomme-sans-etiquette"]["command"],
                         "docker volume rm nomme-sans-etiquette")
        self.assertIn("outside any recorded turn",
                      autres["anonyme-hors-tour"]["detail"])
        self.assertIn("creation time unreadable", autres["anonyme-sans-date"]["detail"])
        self.assertEqual(essai["others_total"], 3)
        # réalisation : seuls les attribués assez vieux partent
        moteur.removed = []
        realise = menage.reap_volumes(moteur, self.db, "pc", menage.Policy(), now=now)
        self.assertEqual(sorted(moteur.removed), ["anonyme-fini", "banc-vieux"])
        self.assertEqual({e["path"]: e["action"] for e in realise["entries"]},
                         {"banc-vieux": "removed", "anonyme-fini": "removed"})
        self.assertEqual(moteur.inspected.count("nomme-sans-etiquette"), 0)

    def test_jeune_ou_hors_delai_attend_et_refus_garde(self):
        now = time.time()
        turns = self._turns()
        turns.open_turn("jeune", "a1", "pc", pgid=None, label=None)
        turns.close_turn("jeune", orphan=True)
        self.db.execute(
            "UPDATE turn_resources SET started_at = to_timestamp(%s),"
            " ended_at = to_timestamp(%s) WHERE turn_id = 'jeune'",
            (now - 3 * 3600, now - 2 * 3600))
        moteur = _FauxVolumes(
            [_v("anonyme-recent", ANONYME), _v("banc", BANC)],
            created={"anonyme-recent": now - 2.5 * 3600, "banc": now - 1 * 3600})
        essai = menage.reap_volumes(moteur, self.db, "pc",
                                    menage.Policy(docker_volume_idle=6 * 3600),
                                    dry_run=True, now=now)
        self.assertEqual({o["path"]: o["action"] for o in essai["ours"]},
                         {"anonyme-recent": "attend", "banc": "attend"})
        self.assertEqual(essai["plan"], [])
        # un moteur qui refuse la suppression : le volume est gardé, jamais perdu
        vieux = _FauxVolumes([_v("banc-vieux", BANC)],
                             created={"banc-vieux": now - 48 * 3600},
                             removal_refused=True)
        realise = menage.reap_volumes(vieux, self.db, "pc", menage.Policy(), now=now)
        self.assertEqual(vieux.removed, [])
        self.assertEqual([e["action"] for e in realise["entries"]], ["kept"])
        self.assertIn("deletion refused", realise["entries"][0]["detail"])

    def test_releve_illisible_et_politique_coupée(self):
        now = time.time()
        illisible = _FauxVolumes([_v("anonyme", ANONYME), _v("banc", BANC)],
                                 created_unreadable=True)
        essai = menage.reap_volumes(illisible, self.db, "pc", menage.Policy(), now=now)
        self.assertEqual(illisible.removed, [])
        self.assertEqual({o["action"] for o in essai["ours"]}, {"signalé"})
        self.assertEqual({o["path"] for o in essai["others"]}, {"anonyme"})
        # `docker_volumes: false` : le moteur n'est même pas interrogé
        coupe = _FauxVolumes([_v("banc-vieux", BANC)],
                             created={"banc-vieux": now - 48 * 3600})
        vide = menage.reap_volumes(coupe, self.db, "pc",
                                   menage.Policy(docker_volumes=False), now=now)
        self.assertEqual(vide, {"entries": [], "plan": [], "ours": None,
                                "others": None, "others_total": None})
        self.assertIsNone(coupe.inspected)
        self.assertEqual(coupe.removed, [])
        # podman et moteur absent : jamais de suppression (comme la mesure #411)
        podman = _FauxVolumes([_v("banc-vieux", BANC)])
        podman.binary = "podman"
        self.assertIsNone(menage.reap_volumes(podman, self.db, "pc",
                                              menage.Policy(), now=now)["ours"])
        self.assertIsNone(menage.reap_volumes(None, self.db, "pc",
                                              menage.Policy(), now=now)["ours"])

    def test_releve_des_non_attribues_borne(self):
        moteur = _FauxVolumes([_v("v-%d" % i) for i in range(5)])
        with mock.patch.object(menage, "VOLUMES_TOP", 2):
            essai = menage.reap_volumes(moteur, self.db, "pc", menage.Policy())
        self.assertEqual([o["path"] for o in essai["others"]], ["v-0", "v-1"])
        self.assertEqual(essai["others_total"], 5)
        self.assertEqual(essai["ours"], [])

    def test_run_pass_recolte_journalise_et_rapporte(self):
        now = time.time()
        turns = self._turns()
        turns.open_turn("fini", "a1", "pc", pgid=None, label=None)
        turns.close_turn("fini", orphan=True)
        self.db.execute(
            "UPDATE turn_resources SET started_at = to_timestamp(%s),"
            " ended_at = to_timestamp(%s) WHERE turn_id = 'fini'",
            (now - 48 * 3600, now - 40 * 3600))
        moteur = _FauxVolumes(
            [_v("banc-vieux", BANC), _v("hors-mesh"), _v("anonyme-fini", ANONYME)],
            created={"banc-vieux": now - 48 * 3600, "anonyme-fini": now - 44 * 3600})
        rapport = menage.run_pass(self.cfgl, self.db, menage.Policy(), host="pc",
                                  actor="t", runtime=moteur, now=now)
        self.assertEqual(sorted(moteur.removed), ["anonyme-fini", "banc-vieux"])
        self.assertEqual({e["path"] for e in rapport["entries"] if e["kind"] == "volume"},
                         {"banc-vieux", "anonyme-fini"})
        mesure = storage.of(self.db).housekeeping.last_measure("pc")[0]["data"]["volumes"]
        self.assertEqual({e["path"] for e in mesure["ameesh"]},
                         {"banc-vieux", "anonyme-fini"})
        self.assertEqual([o["path"] for o in mesure["others"]], ["hors-mesh"])
        self.assertEqual(mesure["others"][0]["command"], "docker volume rm hors-mesh")
        journal = storage.of(self.db).housekeeping.recent("pc", 3600)
        self.assertEqual(sorted(r["path"] for r in journal if r["kind"] == "volume"),
                         ["anonyme-fini", "banc-vieux"])
        sortie = io.StringIO()
        with contextlib.redirect_stdout(sortie):
            menage.print_report(rapport)
        self.assertIn("docker volume rm hors-mesh", sortie.getvalue())
        # chaque volume récolté est dit une fois : pas de doublon plan/entrées
        self.assertEqual(sortie.getvalue().count("banc-vieux"), 1)
        self.assertEqual(sortie.getvalue().count("anonyme-fini"), 1)


if __name__ == "__main__":
    unittest.main()
