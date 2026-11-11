# SPDX-License-Identifier: AGPL-3.0-only
"""#6871 (EP8.5a) : `db status`, le doctor et les alertes `db_*`.

Aucune grappe réelle : les lignes de `pg_stat_replication` et les réponses de
l'API Patroni sont des faux. Le seul témoin qui touche une base lit le
mono-nœud du banc (`in_recovery = false`, aucun secours), comme l'hôte
d'aujourd'hui : statut lisible, aucune alerte de grappe.
"""
from __future__ import annotations

import dataclasses
import io
import json
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ameesh import config, db as db_mod, db_status, exploitation, notify

from .support import EMPTY_CONFIG, PgTestCase

LOCAL_PRIMARY = {
    "in_recovery": False,
    "server_version": "17.11 (Debian 17.11-1.pgdg12+2)",
    "synchronous_standby_names": "",
    "synchronous_commit": "on",
    "started_at": "2026-11-10T00:00:00+00:00",
    "server_addr": "10.77.0.1",
    "server_port": 5432,
    "database": "ameesh",
    "role_name": "ameesh",
    "read_all_stats": True,
    "superuser": True,
}


def cfg(**changes):
    return dataclasses.replace(config.Config(), **changes)


def standby(name, *, sync="async", lag=0.0, state="streaming", logical=False):
    return {"application_name": name, "client_addr": "10.77.0.3",
            "state": state, "sync_state": sync,
            "sync_priority": 0 if sync != "sync" else 1,
            "write_lag_s": lag, "flush_lag_s": lag, "replay_lag_s": lag,
            "sent_lsn": "0/10", "replay_lsn": "0/10",
            "logical": logical}


def masked_standby(name="n2", pid=1234, logical=False):
    """Une ligne telle que PostgreSQL 17 la rend sans pg_read_all_stats.

    `application_name` et `client_addr` viennent de `pg_stat_get_activity`
    et restent visibles ; `state`, `sync_state` et les retards viennent de
    `pg_stat_get_wal_senders` et sont NULL (system_views.sql,
    REL_17_STABLE).
    """
    return {"pid": pid, "application_name": name, "client_addr": "10.77.0.3",
            "state": None, "sync_state": None, "sync_priority": None,
            "write_lag_s": None, "flush_lag_s": None, "replay_lag_s": None,
            "sent_lsn": None, "replay_lsn": None, "logical": logical}


class FakeDb:
    """Les deux lectures de `read`, sans base ni réseau."""

    name = "psycopg"

    def __init__(self, *, local=None, standbys=()):
        self.local = dict(LOCAL_PRIMARY if local is None else local)
        self.standbys = [dict(row) for row in standbys]
        self.cfg = types.SimpleNamespace(backend="pg")

    def query(self, sql, params=()):
        if "pg_stat_replication" in sql:
            return [dict(row) for row in self.standbys]
        return [dict(self.local)]


def patroni_reader(*, local=None, cluster=None):
    """Un lecteur Patroni de faux : une réponse par chemin, ou une erreur."""
    def reader(url, path):
        payload = local if path == "/patroni" else cluster
        if isinstance(payload, BaseException):
            raise payload
        if payload is None:
            raise OSError("connection refused")
        return payload
    return reader


def patroni_view(*, name="n1", role="primary", leader="n1", members=None,
                 paused=False):
    members = members or [
        {"name": "n1", "role": "leader" if leader == "n1" else "replica",
         "state": "running", "host": "10.77.0.4"},
        {"name": "n2", "role": "leader" if leader == "n2" else "replica",
         "state": "running", "host": "10.77.0.3"},
    ]
    return db_status.patroni(
        cfg(patroni_url="http://127.0.0.1:8008"),
        reader=patroni_reader(
            local={"role": role, "state": "running", "timeline": 3,
                   "dcs_last_seen": 1794393000,
                   "patroni": {"name": name, "scope": "ameesh",
                               "version": "4.0.0"}},
            cluster={"members": members, "scope": "ameesh", "pause": paused}))


class MonoNodeTest(unittest.TestCase):
    """Le défaut d'aujourd'hui : lisible, et aucune alerte de grappe."""

    def test_status_readable_sans_patroni_ni_secours(self):
        status = db_status.read(cfg(), FakeDb())
        self.assertEqual(status["schema"], db_status.STATUS_SCHEMA)
        self.assertEqual(status["node"]["role"], "primary")
        self.assertEqual(status["node"]["name"], "10.77.0.1")
        self.assertEqual(status["standbys"], [])
        self.assertIsNone(status["replication"]["max_lag_s"])
        self.assertEqual(status["replication"]["expected"], 0)
        self.assertIsNone(status["patroni"])
        self.assertEqual(status["primary"], {"name": "10.77.0.1", "source": "local"})
        self.assertEqual(db_status.alerts_from_status(cfg(), status), [])
        self.assertEqual(db_status.alerts(cfg(), FakeDb()), [])

    def test_doctor_line_et_texte_de_commande(self):
        status = db_status.read(cfg(), FakeDb())
        line = db_status.line(status)
        self.assertIn("mono-node", line)
        self.assertIn("Patroni not configured", line)
        lines = db_status.doctor_lines(cfg(), FakeDb())
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("db         : "))
        self.assertNotIn("DEGRADED", lines[0])
        text = "\n".join(db_status.format_lines(dict(status, alerts=[])))
        self.assertIn("standbys   : none (mono-node)", text)
        self.assertIn("verdict    : OK", text)

    def test_un_secours_ne_surveille_pas_les_secours_du_primaire(self):
        status = db_status.read(
            cfg(), FakeDb(local=dict(LOCAL_PRIMARY, in_recovery=True,
                                     synchronous_standby_names="n1")))
        self.assertEqual(status["node"]["role"], "standby")
        self.assertIsNone(status["primary"])
        self.assertEqual(db_status.alerts_from_status(cfg(), status), [])

    def test_backend_medie_ne_produit_pas_d_alerte(self):
        db = FakeDb()
        db.cfg = types.SimpleNamespace(backend="mediated")
        self.assertEqual(db_status.alerts(cfg(), db), [])
        with self.assertRaises(db_status.StatusUnavailable):
            db_status.read(cfg(), db)

    def test_deux_primaires_se_lit_aussi_d_un_secours(self):
        status = db_status.read(
            cfg(), FakeDb(local=dict(LOCAL_PRIMARY, in_recovery=True)))
        status["patroni"] = patroni_view(members=[
            {"name": "n1", "role": "leader"}, {"name": "n2", "role": "leader"}])
        self.assertEqual([a["type"] for a in
                          db_status.alerts_from_status(cfg(), status)],
                         ["db_two_primaries"])


class ClusterStatusTest(unittest.TestCase):
    def test_lecture_des_secours_et_du_retard(self):
        status = db_status.read(
            cfg(), FakeDb(
                local=dict(LOCAL_PRIMARY,
                           synchronous_standby_names="FIRST 1 (n2, n3)"),
                standbys=[standby("n2", sync="sync", lag=0.2),
                          standby("n3", lag=2.5)]))
        self.assertEqual(status["replication"]["count"], 2)
        self.assertEqual(status["replication"]["sync_count"], 1)
        self.assertEqual(status["replication"]["max_lag_s"], 2.5)
        self.assertEqual(status["replication"]["expected"], 1)
        self.assertIn("n2 sync replay 0.2 s", db_status.line(status))
        self.assertIn("n3 async replay 2.5 s", db_status.line(status))
        self.assertEqual(db_status.alerts_from_status(cfg(), status), [])

    def test_attendu_explicite_et_seuil(self):
        expected_cfg = cfg(db_min_standbys=2, db_lag_threshold_s=10.0)
        status = db_status.read(
            expected_cfg, FakeDb(standbys=[standby("n2", lag=12.0)]))
        self.assertEqual(status["replication"]["expected"], 2)
        self.assertEqual(status["replication"]["threshold_s"], 10.0)
        kinds = [a["type"] for a in
                 db_status.alerts_from_status(expected_cfg, status)]
        self.assertIn("db_no_standby", kinds)
        self.assertIn("db_replication_lag", kinds)

    def test_un_abonne_logique_n_est_pas_un_secours(self):
        # Un seul abonné logique : aucun secours physique, l'alerte reste.
        expected_cfg = cfg(db_min_standbys=1)
        status = db_status.read(
            expected_cfg, FakeDb(standbys=[standby("sub1", logical=True)]))
        self.assertEqual(status["replication"]["count"], 0)
        self.assertEqual(status["replication"]["logical"], 1)
        self.assertEqual([a["type"] for a in
                          db_status.alerts_from_status(expected_cfg, status)],
                         ["db_no_standby"])
        self.assertIn("1 logical walsender excluded", db_status.line(status))

    def test_logique_et_physique_meles(self):
        expected_cfg = cfg(db_min_standbys=1)
        status = db_status.read(
            expected_cfg, FakeDb(
                local=dict(LOCAL_PRIMARY,
                           synchronous_standby_names="FIRST 1 (n2)"),
                standbys=[standby("sub1", logical=True),
                          standby("n2", sync="sync")]))
        self.assertEqual(status["replication"]["count"], 1)
        self.assertEqual(status["replication"]["sync_count"], 1)
        self.assertEqual(status["replication"]["logical"], 1)
        self.assertEqual([s["name"] for s in status["standbys"]], ["n2"])
        self.assertEqual(db_status.alerts_from_status(expected_cfg, status), [])


class MaskedStatsTest(unittest.TestCase):
    """PostgreSQL masque `pg_stat_replication` hors `pg_read_all_stats`.

    Une ligne masquée ne doit être ni un secours, ni un retard, ni une
    absence : elle se dit, et rien ne s'interprète à partir d'elle.
    """

    def unprivileged(self, **changes):
        return dict(LOCAL_PRIMARY, read_all_stats=False, superuser=False,
                    role_name="app", **changes)

    def test_la_lecture_sonde_le_droit_de_voir_les_details(self):
        self.assertIn("pg_read_all_stats", db_status._LOCAL_SQL)
        self.assertIn("is_superuser", db_status._LOCAL_SQL)

    def test_ligne_masquee_n_est_pas_un_secours(self):
        local = self.unprivileged(synchronous_standby_names="n2")
        row = masked_standby()
        # Le nom reste visible (pg_stat_get_activity) : c'est `state` NULL,
        # et lui seul, qui prouve le masque.
        self.assertEqual(row["application_name"], "n2")
        status = db_status.read(
            cfg(db_min_standbys=1), FakeDb(local=local, standbys=[row]))
        self.assertFalse(status["visibility"]["privileged"])
        self.assertEqual(status["visibility"]["masked_rows"], 1)
        self.assertEqual(status["replication"]["masked"], 1)
        self.assertEqual(status["standbys"], [])
        self.assertEqual(status["replication"]["count"], 0)
        self.assertIsNone(status["replication"]["max_lag_s"])
        alerts = db_status.alerts_from_status(cfg(), status)
        self.assertEqual([a["type"] for a in alerts], ["db_stats_masked"])
        self.assertEqual(alerts[0]["masked"], 1)
        self.assertIn("pg_read_all_stats", alerts[0]["detail"])
        self.assertIn("masked", db_status.line(status))
        text = "\n".join(db_status.format_lines(dict(status, alerts=[])))
        self.assertIn("1 walsender(s) with masked statistics", text)
        self.assertIn("pg_read_all_stats no", text)

    def test_absence_reste_certaine_sans_privilege(self):
        # Zéro ligne : le filtrage des lignes n'est pas masqué ; le secours
        # attendu par synchronous_standby_names manque vraiment.
        status = db_status.read(
            cfg(), FakeDb(local=self.unprivileged(
                synchronous_standby_names="n2")))
        self.assertEqual(status["visibility"]["masked_rows"], 0)
        self.assertEqual([a["type"] for a in
                          db_status.alerts_from_status(cfg(), status)],
                         ["db_no_standby"])

    def test_retard_inconnu_visible_n_est_pas_du_masque(self):
        status = db_status.read(
            cfg(), FakeDb(standbys=[standby("n2", lag=None)]))
        self.assertEqual(status["visibility"]["masked_rows"], 0)
        self.assertEqual(status["replication"]["count"], 1)
        self.assertIsNone(status["replication"]["max_lag_s"])
        self.assertEqual(db_status.alerts_from_status(cfg(), status), [])

    def test_sauvegarde_masquee_dit_la_visibilite_pas_l_absence(self):
        # Une sauvegarde masquée ne se distingue pas d'un secours : on ne
        # compte ni l'une ni l'autre, et on dit le masque.
        local = self.unprivileged(synchronous_standby_names="n2")
        status = db_status.read(
            cfg(), FakeDb(local=local, standbys=[masked_standby()]))
        kinds = [a["type"] for a in
                 db_status.alerts_from_status(cfg(), status)]
        self.assertEqual(kinds, ["db_stats_masked"])
        self.assertNotIn("db_no_standby", kinds)
        self.assertNotIn("db_sync_lost", kinds)

    def test_un_abonne_logique_masque_ne_crie_pas_au_masque(self):
        # Le classement logique vient des slots, pas des colonnes masquées :
        # un abonné logique n'est pas un secours et n'en cache aucun.
        expected_cfg = cfg(db_min_standbys=1)
        status = db_status.read(
            expected_cfg, FakeDb(
                local=self.unprivileged(),
                standbys=[masked_standby("sub1", logical=True)]))
        self.assertEqual(status["replication"]["masked"], 0)
        self.assertEqual(status["replication"]["logical"], 1)
        self.assertEqual([a["type"] for a in
                          db_status.alerts_from_status(expected_cfg, status)],
                         ["db_no_standby"])


class AlertTest(unittest.TestCase):
    def status(self, *, local=None, standbys=(), **changes):
        return db_status.read(cfg(**changes), FakeDb(
            local=local if local is not None else LOCAL_PRIMARY,
            standbys=standbys))

    def kinds(self, status, **changes):
        return [a["type"] for a in db_status.alerts_from_status(
            cfg(**changes), status)]

    def test_secours_absent_quand_la_grappe_est_attendue(self):
        names = dict(LOCAL_PRIMARY, synchronous_standby_names="n2")
        status = self.status(local=names)
        alerts = db_status.alerts_from_status(cfg(), status)
        self.assertEqual([a["type"] for a in alerts], ["db_no_standby"])
        self.assertEqual(alerts[0]["value"], 0)
        self.assertEqual(alerts[0]["threshold"], 1)
        # un secours attendu par configuration, même sans synchrone
        status = self.status(**{"db_min_standbys": 1})
        self.assertEqual(self.kinds(status, db_min_standbys=1), ["db_no_standby"])
        # mono-nœud : rien
        self.assertEqual(self.kinds(self.status()), [])

    def test_retard_au_dessus_du_seuil(self):
        status = self.status(standbys=[standby("n2", lag=45.2)])
        alerts = db_status.alerts_from_status(cfg(), status)
        self.assertEqual([a["type"] for a in alerts], ["db_replication_lag"])
        self.assertEqual(alerts[0]["value"], 45.2)
        self.assertEqual(alerts[0]["threshold"], 30.0)
        self.assertEqual(alerts[0]["standby"], "n2")
        # strictement au seuil : pas d'alerte
        self.assertEqual(self.kinds(self.status(
            standbys=[standby("n2", lag=30.0)])), [])
        # un seuil plus large l'absorbe
        self.assertEqual(self.kinds(self.status(
            standbys=[standby("n2", lag=45.2)]),
            db_lag_threshold_s=60.0), [])

    def test_retard_inconnu_ne_fait_pas_alerter(self):
        rows = [standby("n2", lag=None)]
        status = self.status(standbys=rows)
        self.assertIsNone(status["replication"]["max_lag_s"])
        self.assertEqual(self.kinds(status), [])

    def test_plus_de_synchrone(self):
        names = dict(LOCAL_PRIMARY, synchronous_standby_names="n2")
        status = self.status(local=names, standbys=[standby("n2")])
        self.assertEqual(self.kinds(status), ["db_sync_lost"])
        # le secours synchrone est revenu
        status = self.status(local=names, standbys=[standby("n2", sync="sync")])
        self.assertEqual(self.kinds(status), [])
        status = self.status(local=names, standbys=[standby("n2", sync="quorum")])
        self.assertEqual(self.kinds(status), [])
        # aucun secours : secours absent, pas une seconde alerte pour la même cause
        status = self.status(local=names)
        self.assertEqual(self.kinds(status), ["db_no_standby"])

    def test_une_sauvegarde_de_base_n_est_pas_un_secours(self):
        names = dict(LOCAL_PRIMARY, synchronous_standby_names="n2")
        status = self.status(local=names,
                             standbys=[standby("n2", state="backup")])
        self.assertEqual(self.kinds(status), ["db_no_standby"])

    def test_deux_primaires(self):
        members = [{"name": "n1", "role": "leader"},
                   {"name": "n2", "role": "leader"}]
        status = self.status()
        status["patroni"] = patroni_view(members=members)
        alerts = db_status.alerts_from_status(cfg(), status)
        self.assertEqual([a["type"] for a in alerts], ["db_two_primaries"])
        self.assertEqual(sorted(alerts[0]["nodes"]), ["n1", "n2"])

    def test_primaire_local_et_leader_dcs_ailleurs(self):
        status = self.status()
        status["patroni"] = patroni_view(leader="n2",
                                         members=[{"name": "n2", "role": "leader"}])
        self.assertEqual([a["type"] for a in
                          db_status.alerts_from_status(cfg(), status)],
                         ["db_two_primaries"])

    def test_un_seul_leader_et_ce_noeud_replique_ne_disent_rien(self):
        status = self.status()
        status["patroni"] = patroni_view(leader="n2", role="replica",
                                         members=[{"name": "n2", "role": "leader"},
                                                  {"name": "n1", "role": "replica"}])
        self.assertEqual(self.kinds(status), [])

    def test_patroni_illisible_ninvente_pas_deux_primaires(self):
        status = self.status()
        status["patroni"] = db_status.patroni(
            cfg(patroni_url="http://127.0.0.1:8008"),
            reader=patroni_reader(local=OSError("refused")))
        self.assertTrue(status["patroni"]["error"])
        self.assertEqual(self.kinds(status), [])

    def test_cluster_a_deux_leaders_malgre_patroni_en_erreur(self):
        # /patroni échoue mais /cluster répond : la preuve disponible suffit.
        status = self.status()
        status["patroni"] = db_status.patroni(
            cfg(patroni_url="http://127.0.0.1:8008"),
            reader=patroni_reader(
                local=OSError("refused"),
                cluster={"scope": "ameesh", "members": [
                    {"name": "n1", "role": "leader"},
                    {"name": "n2", "role": "leader"}]}))
        self.assertEqual(status["patroni"]["errors"]["local"], "refused")
        self.assertIsNone(status["patroni"]["errors"]["cluster"])
        self.assertEqual([a["type"] for a in
                          db_status.alerts_from_status(cfg(), status)],
                         ["db_two_primaries"])
        text = "\n".join(db_status.format_lines(dict(status, alerts=[])))
        self.assertIn("leader n1, 2 members", text)
        self.assertIn("local unreadable", text)

    def test_cluster_illisible_ne_compare_pas_le_role_local(self):
        # Le rôle local ne dit rien sans la vue DCS : aucune inférence.
        status = self.status()
        status["patroni"] = db_status.patroni(
            cfg(patroni_url="http://127.0.0.1:8008"),
            reader=patroni_reader(
                local={"role": "primary", "patroni": {"name": "n1"}},
                cluster=OSError("refused")))
        self.assertEqual(status["patroni"]["errors"]["cluster"], "refused")
        self.assertEqual(self.kinds(status), [])

    def test_un_seul_leader_et_patroni_en_erreur_n_invente_rien(self):
        status = self.status()
        status["patroni"] = db_status.patroni(
            cfg(patroni_url="http://127.0.0.1:8008"),
            reader=patroni_reader(
                local=OSError("refused"),
                cluster={"members": [{"name": "n2", "role": "leader"}]}))
        self.assertEqual(self.kinds(status), [])


class PatroniTest(unittest.TestCase):
    def test_lecture_de_l_api_et_du_leader(self):
        view = patroni_view(paused=True)
        self.assertEqual(view["name"], "n1")
        self.assertEqual(view["scope"], "ameesh")
        self.assertTrue(view["paused"])
        self.assertEqual(view["local"]["role"], "primary")
        self.assertEqual(view["members"][0]["name"], "n1")

    def test_l_url_est_masquee(self):
        view = db_status.patroni(
            cfg(patroni_url="http://user:secret@127.0.0.1:8008/"),
            reader=patroni_reader(local={"patroni": {"name": "n1"}},
                                  cluster={"members": []}))
        self.assertEqual(view["url"], "http://127.0.0.1:8008/")
        self.assertNotIn("secret", json.dumps(view))

    def test_patroni_non_configure(self):
        self.assertIsNone(db_status.patroni(cfg()))

    def test_une_requete_de_l_url_ne_sort_pas(self):
        self.assertEqual(
            db_status.mask_url("http://127.0.0.1:8008?token=secret"),
            "http://127.0.0.1:8008")

    def test_port_invalide_ne_livre_pas_l_url_brute(self):
        for raw in ("http://user:secret@localhost:bad",
                    "http://user:secret@127.0.0.1:70000"):
            with self.subTest(raw=raw):
                masked = db_status.mask_url(raw)
                self.assertEqual(masked, "(invalid URL)")
                self.assertNotIn("secret", masked)
                self.assertNotIn("user", masked)

    def test_ipv6_mal_formee_ne_livre_pas_l_url_brute(self):
        masked = db_status.mask_url("http://user:secret@[::1")
        self.assertEqual(masked, "(invalid URL)")
        self.assertNotIn("secret", masked)

    def test_ipv6_valide_garde_les_crochets(self):
        self.assertEqual(
            db_status.mask_url("http://user:secret@[::1]:8008/"),
            "http://[::1]:8008/")

    def test_une_erreur_reseau_ne_recopie_pas_l_url(self):
        raw = "http://user:secret@127.0.0.1:8008"

        def reader(url, path):
            raise OSError("connection to %s refused" % url)

        view = db_status.patroni(cfg(patroni_url=raw), reader=reader)
        self.assertNotIn("secret", json.dumps(view))
        self.assertIn("http://127.0.0.1:8008", view["errors"]["local"])

    def test_une_erreur_sans_schema_ne_recopie_pas_le_mot_de_passe(self):
        def reader(url, path):
            raise OSError("auth user:secret@host refused")

        view = db_status.patroni(
            cfg(patroni_url="http://127.0.0.1:8008"), reader=reader)
        self.assertNotIn("secret", json.dumps(view))
        self.assertIn("[redacted]@host", view["errors"]["local"])

    def test_une_url_illisible_dans_le_json_ne_livre_rien(self):
        raw = "http://user:secret@localhost:bad"
        view = db_status.patroni(
            cfg(patroni_url=raw),
            reader=patroni_reader(local=OSError("refused")))
        self.assertEqual(view["url"], "(invalid URL)")
        self.assertNotIn("secret", json.dumps(view))


class RegistrationTest(unittest.TestCase):
    def test_les_types_sont_declares_et_urgents(self):
        kinds = ("db_no_standby", "db_replication_lag",
                 "db_sync_lost", "db_two_primaries", "db_stats_masked")
        for kind in kinds:
            with self.subTest(kind=kind):
                self.assertIn(kind, exploitation.ALERT_TYPES)
                self.assertIn(kind, notify.DEFAULT_TYPES)
                self.assertIn(kind, notify.TYPE_LABELS)
        for kind in ("db_no_standby", "db_sync_lost", "db_two_primaries"):
            self.assertIn(kind, notify.URGENT_TYPES)
        # Une visibilité masquée est un diagnostic de surveillance, pas une
        # panne de service : elle ne réveille pas.
        self.assertNotIn("db_stats_masked", notify.URGENT_TYPES)


class ConfigurationTest(unittest.TestCase):
    def load(self, **env):
        base = {"AMEESH_CONFIG": EMPTY_CONFIG}
        base.update(env)
        return config.load(env=base)

    def test_defauts_mono_noeud(self):
        cfg = config.Config()
        self.assertEqual(cfg.patroni_url, "")
        self.assertEqual(cfg.db_min_standbys, 0)
        self.assertEqual(cfg.db_lag_threshold_s, 30.0)

    def test_environnement(self):
        cfg = self.load(AMEESH_PATRONI_URL="http://127.0.0.1:8008",
                        AMEESH_DB_MIN_STANDBYS="2",
                        AMEESH_DB_LAG_THRESHOLD_S="15")
        self.assertEqual(cfg.patroni_url, "http://127.0.0.1:8008")
        self.assertEqual(cfg.db_min_standbys, 2)
        self.assertEqual(cfg.db_lag_threshold_s, 15.0)

    def test_valeurs_illisibles_gardent_le_defaut(self):
        cfg = self.load(AMEESH_DB_MIN_STANDBYS="-1",
                        AMEESH_DB_LAG_THRESHOLD_S="illisible")
        self.assertEqual(cfg.db_min_standbys, 0)
        self.assertEqual(cfg.db_lag_threshold_s, 30.0)


class MonoNodeBenchTest(PgTestCase):
    """Le vrai mono-nœud du banc, en lecture seule : lisible et silencieux."""

    def test_le_statut_est_lisible_et_sans_alerte(self):
        status = db_status.read(self.cfg, self.db)
        self.assertIn(status["node"]["role"], ("primary", "standby"))
        self.assertEqual(status["replication"]["expected"], 0)
        self.assertEqual(db_status.alerts(self.cfg, self.db), [])
        self.assertFalse(db_status.doctor_lines(self.cfg, self.db)[0].startswith(
            "db         : not checked"))

    def test_la_commande_sort_son_json(self):
        conn = self.connect()
        out = io.StringIO()
        with mock.patch.object(config, "load", return_value=self.cfg), \
                mock.patch.object(db_mod, "connect", return_value=conn), \
                redirect_stdout(out):
            code = db_status.main(["status", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["schema"], db_status.STATUS_SCHEMA)
        self.assertEqual(payload["alerts"], [])
        self.assertIn("standbys", payload)

    def test_la_passe_d_alertes_porte_l_alerte_de_grappe(self):
        status = db_status.read(
            self.cfg, FakeDb(
                local=dict(LOCAL_PRIMARY, synchronous_standby_names="n2"),
                standbys=[standby("n2", sync="sync", lag=45.0)]))
        with mock.patch.object(db_status, "read", return_value=status):
            rows = db_mod.batched(
                self.db, lambda d: exploitation.alerts(self.cfg, d))
        types = {row["type"] for row in rows}
        self.assertIn("db_replication_lag", types)
        self.assertNotIn("db_two_primaries", types)

    def test_la_commande_sort_1_sur_une_alerte(self):
        status = db_status.read(
            self.cfg, FakeDb(
                local=dict(LOCAL_PRIMARY, synchronous_standby_names="n2"),
                standbys=[standby("n2", sync="sync", lag=45.0)]))
        conn = self.connect()
        out = io.StringIO()
        with mock.patch.object(config, "load", return_value=self.cfg), \
                mock.patch.object(db_mod, "connect", return_value=conn), \
                mock.patch.object(db_status, "read", return_value=status), \
                redirect_stdout(out):
            code = db_status.main(["status", "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(out.getvalue())
        self.assertEqual([a["type"] for a in payload["alerts"]],
                         ["db_replication_lag"])

    def test_le_doctor_imprime_la_ligne_et_voit_la_degradation(self):
        from ameesh import cli, db_capacity, venv_guard
        status = db_status.read(
            self.cfg, FakeDb(
                local=dict(LOCAL_PRIMARY, synchronous_standby_names="n2"),
                standbys=[standby("n2", sync="sync", lag=45.0)]))
        self.assertEqual([a["type"] for a in
                          db_status.alerts_from_status(self.cfg, status)],
                         ["db_replication_lag"])
        out = io.StringIO()
        with mock.patch.object(db_status, "read", return_value=status), \
                mock.patch.object(cli, "_profile_audit", return_value=None), \
                mock.patch.object(venv_guard, "installation_problems",
                                  return_value=[]), \
                mock.patch.object(db_capacity, "read", return_value=None), \
                redirect_stdout(out):
            code = cli.cmd_doctor(self.cfg, notify_test=False)
        text = out.getvalue()
        self.assertIn("db         : DEGRADED (db_replication_lag)", text)
        self.assertEqual(code, 0, "the db line is a signal, not a verdict")
