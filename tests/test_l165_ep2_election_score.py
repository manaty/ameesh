# SPDX-License-Identifier: AGPL-3.0-only
"""L165 (EP2) : score des candidats, délai par rang, hystérésis, `role status`.

Les décisions de `ameesh/election.py` sont jugées sur une base migrée réelle
(`role_standings`, `host_resources`, `canon_state`, `agent_candidates`) : un
hôte candidat ne publie que sa partie LOCALE (harnais, compte, budget) ; la
porte, le canon, le placement et la fraîcheur sont recalculés par le lecteur
depuis la base. La disponibilité se calcule sur les sept jours de relevés ;
l'hystérésis exige une heure de supériorité sans trou. Le CAS du bail reste
celui d'EP1 (aucun test ici ne le remplace).
"""
from __future__ import annotations

import dataclasses
import json
import time
import unittest
from unittest import mock

from ameesh import election, registry, role_standings_pg as pg, runner, storage

from .support import PgTestCase


class _RoleCase(PgTestCase):
    """Deux hôtes candidats (`banc`, `atelier`) et une fiche de rôle élue."""

    HOST = "banc"

    def setUp(self) -> None:
        super().setUp()
        self.cfg = dataclasses.replace(self.cfg, host=self.HOST)
        for table in ("role_standings", "agent_candidates", "canon_state",
                      "host_lifecycle", "host_holds", "credential_health",
                      "host_resources"):
            self.db.execute("DELETE FROM " + table)
        # Un descripteur de harnais factice : ces témoins ne dépendent pas des
        # binaires installés sur la machine de CI (éprouvé séparément).
        adapter = mock.patch("ameesh.adapters.adapter_for", return_value=object())
        adapter.start()
        self.addCleanup(adapter.stop)

    # -- fixtures ----------------------------------------------------------
    def role(self, name: str = "pilote", hosts: tuple[str, ...] = ("banc", "atelier"),
             harness: str = "claude") -> dict:
        registry.upsert(self.db, name, harness=harness, host=hosts[0], cwd="/work")
        self.db.execute(
            "UPDATE agent_registry SET releve = 'election', candidats = %s::text[]"
            " WHERE name = %s", ("{%s}" % ",".join(hosts), name))
        return registry.get(self.db, name)

    def admit(self, name: str, host: str, *, ok: bool = True,
              diagnostic: str = "") -> None:
        storage.of(self.db).canon.write_candidate(
            name, host, canon="", cwd="/work/%s" % host, ok=ok,
            diagnostic=diagnostic, ref="canon:placements/test@abc", harness="claude",
            provider=None, model=None, credential_mode=None)

    def canon(self, host: str, status: str = "ok") -> None:
        self.db.execute(
            "INSERT INTO canon_state (host, canon, status, root, source, diagnostic)"
            " VALUES (%s, '', %s, '/canon', 'test', '')"
            " ON CONFLICT (host, canon) DO UPDATE SET status = excluded.status",
            (host, status))

    def reading(self, host: str, *, sampled_ts: float | None = None,
                load: float = 0.1, cpus: int = 4,
                mem: int = 8 * 1024 ** 3,
                gate_state: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO host_resources"
            " (host, sampled_at, load1, cpu_count, mem_available_bytes,"
            "  turns_in_progress, gate_state)"
            " VALUES (%s, coalesce(to_timestamp(%s), now()), %s, %s, %s, 0, %s)",
            (host, sampled_ts, load, cpus, mem, gate_state))

    def ready_host(self, name: str, host: str, **reading) -> None:
        self.admit(name, host)
        self.canon(host)
        self.reading(host, **reading)

    def ready_pair(self, name: str = "pilote") -> None:
        for host in ("banc", "atelier"):
            self.ready_host(name, host)

    def standing(self, name: str, host: str, score: float, *,
                 ready: bool = True, capacity: float = 0.5,
                 evaluated_ts: float | None = None, diagnostic: str = "") -> None:
        self.db.execute(
            "INSERT INTO role_standings"
            " (name, host, evaluated_at, availability, capacity, score, ready, diagnostic)"
            " VALUES (%s, %s, coalesce(to_timestamp(%s), clock_timestamp()),"
            "         %s, %s, %s, %s, %s)",
            (name, host, evaluated_ts, score, capacity, score + capacity,
             ready, diagnostic))

    def hold(self, name: str, host: str = HOST, *, epoch: int = 7,
             expires_in: float = 300.0) -> dict:
        self.db.execute(
            "UPDATE agent_registry SET host = %s, lease_owner = 'runner-test',"
            " lease_epoch = %s, lease_expires_at = clock_timestamp()"
            " + make_interval(secs => %s) WHERE name = %s",
            (host, epoch, expires_in, name))
        return registry.get(self.db, name)


class AvailabilityTest(_RoleCase):
    def samples(self, host: str, *, start: float, minutes: int,
                step: float = 60.0, skip: range | None = None) -> None:
        where = ""
        params = [host, start, step, minutes - 1]
        if skip is not None:
            where = " WHERE NOT (g >= %s AND g < %s)"
            params += [skip.start, skip.stop]
        self.db.execute(
            "INSERT INTO host_resources (host, sampled_at, load1, cpu_count)"
            " SELECT %s, to_timestamp(%s + g * %s), 0.1, 4"
            "   FROM generate_series(0, %s) g" + where, tuple(params))

    def test_hote_neuf_une_journee_sur_sept(self):
        now = 1_700_000_000.0
        self.samples("banc", start=now - 86400.0, minutes=1440)
        av = election.availability(self.db, "banc", now=now)
        # 1 jour couvert sur une fenêtre de 7 : la dénomination est la fenêtre
        # entière, un hôte neuf n'a pas encore de passé.
        self.assertAlmostEqual(av["availability"], 100.0 / 7.0, delta=0.3)
        self.assertGreater(av["samples"], 1400)

    def test_un_hote_d_une_heure_ne_bat_pas_une_vm_de_sept_jours(self):
        now = 1_700_000_000.0
        self.samples("neuf", start=now - 3600.0, minutes=60)
        self.samples("vm", start=now - 7 * 86400.0, minutes=7 * 1440 - 15)
        neuf = election.availability(self.db, "neuf", now=now)
        vm = election.availability(self.db, "vm", now=now)
        self.assertLess(neuf["availability"], vm["availability"])
        self.assertLess(neuf["availability"], 1.0)
        self.assertGreater(vm["availability"], 99.0)

    def test_une_semaine_pleine_et_un_trou(self):
        now = 1_700_000_000.0
        self.samples("banc", start=now - 7 * 86400.0, minutes=7 * 1440,
                     skip=range(3000, 3060))
        av = election.availability(self.db, "banc", now=now)
        # 60 minutes de trou sur 7 jours : ~99,4 %, et le trou pèse vraiment.
        self.assertGreater(av["availability"], 99.0)
        self.assertLess(av["availability"], 99.8)

    def test_un_releve_ne_couvre_que_le_seuil_de_fraicheur(self):
        now = 1_700_000_000.0
        self.samples("banc", start=now - 600.0, minutes=1)
        av = election.availability(self.db, "banc", now=now)
        self.assertAlmostEqual(av["covered_s"], 180.0, delta=0.01)
        self.assertAlmostEqual(av["availability"], 100.0 * 180.0 / (7 * 86400),
                               delta=0.01)

    def test_aucun_releve(self):
        self.assertEqual(election.availability(self.db, "banc")["availability"], 0.0)

    def test_fraicheur_suit_la_cadence_de_l_hote(self):
        now = 1_700_000_000.0
        self.samples("soixante", start=now - 3600.0, minutes=60)
        self.samples("dix-minutes", start=now - 3600.0, minutes=7, step=600.0)
        self.assertEqual(election.freshness_s(self.db, "soixante", now=now), 180.0)
        self.assertEqual(election.freshness_s(self.db, "dix-minutes", now=now), 1800.0)

    def test_un_releve_du_futur_ne_compte_pas(self):
        now = pg.now_ts(self.db)
        self.db.execute(
            "INSERT INTO host_resources (host, sampled_at, load1, cpu_count)"
            " VALUES ('banc', to_timestamp(%s), 0.1, 4)", (now + 300.0,))
        av = election.availability(self.db, "banc", now=now)
        self.assertEqual(av["availability"], 0.0)
        self.assertEqual(av["samples"], 0)

    def test_un_releve_du_futur_rend_l_hote_non_pret(self):
        self.role()
        self.admit("pilote", "banc")
        self.canon("banc")
        self.reading("banc", sampled_ts=time.time() + 300.0)
        state = election.candidate_state(self.db, registry.get(self.db, "pilote"),
                                         "banc")
        self.assertFalse(state["ready"])
        self.assertIn("future", state["diagnostic"])

    def test_un_standing_du_futur_n_est_pas_classe(self):
        role = self.role()
        self.ready_pair()
        self.standing("pilote", "banc", 90.0)
        self.standing("pilote", "atelier", 99.0, evaluated_ts=time.time() + 100.0)
        items = election.candidacy(self.db, "pilote")
        atelier = next(item for item in items if item["host"] == "atelier")
        self.assertFalse(atelier["fresh"])
        self.assertIsNone(atelier["rank"])
        self.assertIn("future", atelier["diagnostic"])


class ReadinessTest(_RoleCase):
    def setUp(self) -> None:
        super().setUp()
        self.role_row = self.role()
        self.ready_host("pilote", "banc")
        self.standing("pilote", "banc", 99.0)

    def candidate(self):
        return election.candidate_state(
            self.db, registry.get(self.db, "pilote"), "banc")

    def local(self):
        return election.local_readiness(
            self.cfg, self.db, registry.get(self.db, "pilote"), "banc")

    def test_pret_quand_tout_est_la(self):
        ready, diagnostic = self.local()
        self.assertTrue(ready, diagnostic)
        self.assertEqual(diagnostic, "")
        state = self.candidate()
        self.assertTrue(state["ready"], state["diagnostic"])
        self.assertEqual(state["diagnostic"], "")

    def test_porte_de_l_hote_fermee(self):
        self.db.execute(
            "INSERT INTO host_lifecycle (host, mode, actor) VALUES ('banc', 'draining', 'test')")
        state = self.candidate()
        self.assertFalse(state["ready"])
        self.assertIn("host gate", state["diagnostic"])

    def test_hote_retenu_par_le_proprietaire(self):
        self.db.execute(
            "INSERT INTO host_holds (host, reason, author, until)"
            " VALUES ('banc', 'maintenance', 'human:alice',"
            "         clock_timestamp() + interval '1 hour')")
        state = self.candidate()
        self.assertFalse(state["ready"])
        self.assertIn("held", state["diagnostic"])

    def test_porte_locale_publiee_dans_le_releve(self):
        self.reading("banc", gate_state="stopped")
        state = self.candidate()
        self.assertFalse(state["ready"])
        self.assertIn("host gate stopped", state["diagnostic"])

    def test_canon_invalide(self):
        self.canon("banc", "invalid")
        state = self.candidate()
        self.assertFalse(state["ready"])
        self.assertIn("canon", state["diagnostic"])

    def test_placement_non_admis(self):
        self.admit("pilote", "banc", ok=False, diagnostic="profil divergent")
        state = self.candidate()
        self.assertFalse(state["ready"])
        self.assertIn("placement refused", state["diagnostic"])

    def test_verdict_de_placement_d_un_autre_canon(self):
        # Le CAS d'EP1 exige le verdict du canon COURANT de la fiche : un
        # verdict resté sur l'ancien canon ne doit pas être classé prêt.
        self.db.execute(
            "UPDATE agent_registry SET canon = 'home' WHERE name = 'pilote'")
        self.db.execute(
            "INSERT INTO canon_state (host, canon, status, root, source, diagnostic)"
            " VALUES ('banc', 'home', 'ok', '/home', 'test', '')"
            " ON CONFLICT (host, canon) DO UPDATE SET status = excluded.status")
        state = self.candidate()
        self.assertFalse(state["ready"])
        self.assertIn("another canon", state["diagnostic"])

    def test_verdict_de_placement_anterieur_a_la_fiche(self):
        # Profil de placement divergé depuis l'évaluation (le CAS refuse) :
        # le classement ne doit pas faire attendre le candidat suivant.
        self.db.execute(
            "UPDATE agent_registry SET model = 'claude-opus' WHERE name = 'pilote'")
        state = self.candidate()
        self.assertFalse(state["ready"])
        self.assertIn("predates the current card", state["diagnostic"])

    def test_releve_de_ressources_perime(self):
        self.db.execute("DELETE FROM host_resources")
        self.reading("banc", sampled_ts=time.time() - 3600)
        state = self.candidate()
        self.assertFalse(state["ready"])
        self.assertIn("stale resource reading", state["diagnostic"])

    def test_connexion_du_harnais_en_echec(self):
        storage.of(self.db).credentials.report(
            "banc", "claude", "", "failing", reason="token expired", source="turn")
        ready, diagnostic = self.local()
        self.assertFalse(ready)
        self.assertIn("harness login failing", diagnostic)

    def test_tenue_de_compte_ou_de_budget(self):
        with mock.patch("ameesh.account_turn.turn_hold",
                        return_value="account secondaire 5.00/5.00 $ until 00:00"):
            ready, diagnostic = self.local()
        self.assertFalse(ready)
        self.assertIn("no turn could start", diagnostic)

    def test_harnais_absent_de_l_hote(self):
        with mock.patch("ameesh.adapters.adapter_for",
                        side_effect=RuntimeError("no binary")):
            ready, diagnostic = self.local()
        self.assertFalse(ready)
        self.assertIn("harness claude unavailable", diagnostic)

    def test_la_base_ne_croit_pas_l_hote_sur_la_porte_ni_le_canon(self):
        # Correction d'arbitrage : la porte et le canon se recalculent côté
        # lecteur. L'hôte publie sa partie locale (prête), la base le refuse.
        self.db.execute(
            "INSERT INTO host_lifecycle (host, mode, actor) VALUES ('banc', 'draining', 'test')")
        published = election.publish_self(self.cfg, self.db, "banc", force=True)
        self.assertEqual(published, 1)
        row = pg.latest(self.db, "pilote")["banc"]
        self.assertTrue(row["ready"], row["diagnostic"])
        state = self.candidate()
        self.assertFalse(state["ready"])
        self.assertIn("host gate", state["diagnostic"])
        self.canon("banc", "invalid")
        self.db.execute("DELETE FROM host_lifecycle")
        state = self.candidate()
        self.assertFalse(state["ready"])
        self.assertIn("canon invalid", state["diagnostic"])

    def test_pause_operateur_du_role(self):
        self.db.execute(
            "UPDATE agent_registry SET operator_paused = true,"
            " operator_pause_reason = 'maintenance' WHERE name = 'pilote'")
        standing = election.evaluate(self.cfg, self.db,
                                     registry.get(self.db, "pilote"), "banc")
        self.assertFalse(standing["ready"])
        self.assertIn("operator pause: maintenance", standing["diagnostic"])


class ScoreTest(_RoleCase):
    def setUp(self) -> None:
        super().setUp()
        self.role_row = self.role()
        self.ready_pair()

    def test_capacite_ne_departage_qu_a_score_egal(self):
        self.standing("pilote", "banc", 90.0, capacity=0.9)
        self.standing("pilote", "atelier", 90.0, capacity=0.1)
        ranked = election.candidacy(self.db, "pilote")
        self.assertEqual([item["host"] for item in ranked], ["banc", "atelier"])
        self.assertEqual([item["delay_s"] for item in ranked], [0.0, 20.0])
        # Un score supérieur passe AVANT la capacité : elle ne le rattrape pas.
        self.standing("pilote", "atelier", 91.0, capacity=0.0)
        ranked = election.candidacy(self.db, "pilote")
        self.assertEqual(ranked[0]["host"], "atelier")

    def test_delai_par_rang(self):
        self.assertEqual(election.rank_delay_s(0), 0.0)
        self.assertEqual(election.rank_delay_s(1), 20.0)
        self.assertEqual(election.rank_delay_s(2), 40.0)

    def test_claim_wait_respecte_le_rang(self):
        self.standing("pilote", "atelier", 99.0, capacity=0.5)
        self.standing("pilote", "banc", 90.0, capacity=0.9)
        allowed, reason, mine = election.claim_wait(
            self.db, self.role_row, "banc", 1000.0, now=1010.0)
        self.assertFalse(allowed)
        self.assertEqual(mine["rank"], 1)
        self.assertIn("rank 1", reason)
        allowed, _, _ = election.claim_wait(
            self.db, self.role_row, "banc", 1000.0, now=1020.0)
        self.assertTrue(allowed)
        # Le meilleur part tout de suite, et gagne le CAS de toute façon.
        allowed, _, _ = election.claim_wait(
            self.db, self.role_row, "atelier", 1000.0, now=1000.0)
        self.assertTrue(allowed)

    def test_un_meilleur_mort_ne_fait_pas_attendre_le_second(self):
        # Correction d'arbitrage 5 : un hôte frais et prêt, même moins bon, est
        # rang 0 si le meilleur n'a plus de relevé frais ; il n'attend pas 20 s.
        self.standing("pilote", "banc", 90.0)
        self.standing("pilote", "atelier", 99.0, evaluated_ts=time.time() - 3600)
        ranked = election.candidacy(self.db, "pilote")
        self.assertEqual(ranked[0]["host"], "banc")
        self.assertEqual(ranked[0]["rank"], 0)
        self.assertEqual(ranked[0]["delay_s"], 0.0)
        allowed, _, _ = election.claim_wait(
            self.db, self.role_row, "banc", 1000.0, now=1000.0)
        self.assertTrue(allowed)

    def test_claim_wait_refuse_un_hote_non_pret(self):
        self.standing("pilote", "banc", 90.0, ready=False, diagnostic="canon invalid")
        allowed, reason, mine = election.claim_wait(
            self.db, self.role_row, "banc", 0.0, now=1000.0)
        self.assertFalse(allowed)
        self.assertIn("not ready", reason)
        self.assertIn("canon invalid", reason)
        self.assertIsNotNone(mine)

    def test_claim_wait_refuse_un_releve_perime(self):
        self.standing("pilote", "banc", 90.0, evaluated_ts=time.time() - 3600)
        allowed, reason, _ = election.claim_wait(
            self.db, self.role_row, "banc", 0.0, now=1000.0)
        self.assertFalse(allowed)
        self.assertIn("not ready", reason)

    def test_publication_limitee_a_une_ligne_par_minute(self):
        self.ready_host("pilote", "banc")
        published = election.publish_self(self.cfg, self.db, "banc")
        self.assertEqual(published, 1)
        self.assertEqual(election.publish_self(self.cfg, self.db, "banc"), 0)
        self.assertEqual(len(pg.latest(self.db, "pilote")), 1)
        # `force` republie sans attendre : l'outil de diagnostic peut le faire.
        self.assertEqual(
            election.publish_self(self.cfg, self.db, "banc", force=True), 1)


class HysteresisTest(_RoleCase):
    def setUp(self) -> None:
        super().setUp()
        self.role_row = self.role()
        self.ready_pair()

    def history(self, name: str, host: str, score: float, *, minutes: int = 70,
                now: float | None = None, skip: range | None = None) -> float:
        # Le temps de la BASE : une lecture et un classement comparés à
        # l'horloge de l'hôte divergent de quelques millisecondes et feraient
        # un relevé « futur » (l'arbitrage refuse les dates futures).
        now = pg.now_ts(self.db) if now is None else now
        for minute in range(minutes):
            if skip is not None and minute in skip:
                continue
            self.standing(name, host, score, evaluated_ts=now - minute * 60.0)
        return now

    def test_bascule_apres_une_heure_de_superiorite(self):
        now = self.history("pilote", "banc", 90.0)
        self.history("pilote", "atelier", 96.0, now=now)
        due = election.handover(self.db, self.role_row, "banc", now=now)
        self.assertIsNotNone(due)
        self.assertEqual(due["challenger"], "atelier")
        self.assertGreaterEqual(due["seconds"], 3600.0)
        self.assertAlmostEqual(due["gap"], 6.0, delta=0.01)

    def test_pas_de_bascule_avant_une_heure(self):
        now = self.history("pilote", "banc", 90.0, minutes=45)
        self.history("pilote", "atelier", 96.0, minutes=45, now=now)
        self.assertIsNone(election.handover(self.db, self.role_row, "banc", now=now))

    def test_pas_de_bascule_sous_cinq_points(self):
        now = self.history("pilote", "banc", 90.0)
        self.history("pilote", "atelier", 94.0, now=now)
        self.assertIsNone(election.handover(self.db, self.role_row, "banc", now=now))

    def test_un_trou_dans_l_heure_annule_la_bascule(self):
        now = self.history("pilote", "banc", 90.0)
        self.history("pilote", "atelier", 96.0, now=now, skip=range(30, 45))
        self.assertIsNone(election.handover(self.db, self.role_row, "banc", now=now))

    def test_un_detenteur_silencieux_ne_bascule_pas(self):
        now = self.history("pilote", "atelier", 96.0)
        # le détenteur n'a plus publié depuis dix minutes : pas de preuve
        for minute in range(10, 70):
            self.standing("pilote", "banc", 90.0, evaluated_ts=now - minute * 60.0)
        self.assertIsNone(election.handover(self.db, self.role_row, "banc", now=now))

    def test_une_bascule_due_exige_un_bail_vivant(self):
        now = self.history("pilote", "banc", 90.0)
        self.history("pilote", "atelier", 96.0, now=now)
        role_row = self.hold("pilote", "banc", epoch=7, expires_in=300)
        self.assertIsNotNone(
            election.handover_due(self.db, role_row, "banc", now=now))
        role_row = self.hold("pilote", "banc", epoch=8, expires_in=-300)
        self.assertIsNone(
            election.handover_due(self.db, role_row, "banc", now=now))


class StatusTest(_RoleCase):
    def setUp(self) -> None:
        super().setUp()
        self.role_row = self.role()
        self.ready_pair()

    def test_detenteur_epoque_et_classement(self):
        self.standing("pilote", "banc", 90.0)
        self.standing("pilote", "atelier", 96.0)
        self.hold("pilote", "banc", epoch=12)
        report = election.status(self.db, "pilote")
        self.assertEqual(report["holder"], "banc")
        self.assertEqual(report["epoch"], 12)
        self.assertTrue(report["elected"])
        ranked = [item for item in report["candidates"] if item["rank"] is not None]
        self.assertEqual([item["host"] for item in ranked], ["atelier", "banc"])
        self.assertEqual(ranked[0]["rank"], 0)
        self.assertEqual(ranked[1]["rank"], 1)
        self.assertTrue(ranked[1]["holder"])
        text = "\n".join(election.format_status(report))
        self.assertIn("holder banc (epoch 12", text)
        self.assertIn("atelier", text)

    def test_releve_perime_non_classe_et_diagnostic(self):
        self.standing("pilote", "banc", 90.0)
        self.standing("pilote", "atelier", 99.0, evaluated_ts=time.time() - 3600,
                      diagnostic="canon invalid")
        report = election.status(self.db, "pilote")
        atelier = next(item for item in report["candidates"]
                       if item["host"] == "atelier")
        self.assertFalse(atelier["fresh"])
        self.assertIsNone(atelier["rank"])
        self.assertIn("standing", atelier["diagnostic"])

    def test_cli_role_status_json(self):
        self.standing("pilote", "banc", 90.0)
        self.hold("pilote", "banc", epoch=3)
        proc = self.mesh("role", "status", "pilote", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["holder"], "banc")
        self.assertEqual(payload["epoch"], 3)

    def test_cli_role_status_texte(self):
        self.standing("pilote", "banc", 90.0)
        self.hold("pilote", "banc", epoch=3)
        proc = self.mesh("role", "status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pilote: holder banc (epoch 3", proc.stdout)


class RunnerWiringTest(_RoleCase):
    def setUp(self) -> None:
        super().setUp()
        self.role_row = self.role()
        self.ready_pair()

    def runner(self):
        obj = runner.Runner(self.cfg, self.db, once=True)
        self.addCleanup(obj.stop.set)
        return obj

    def test_le_runner_attend_son_rang_avant_de_reclamer(self):
        self.standing("pilote", "atelier", 99.0)
        self.standing("pilote", "banc", 90.0)
        obj = self.runner()
        self.assertFalse(
            obj.election_claim_allowed(self.role_row)[0])
        # le délai part de la première observation du bail libre
        obj._election_free_since["pilote"] = obj.clock.monotonic() - 21.0
        allowed, _, mine = obj.election_claim_allowed(self.role_row)
        self.assertTrue(allowed)
        self.assertEqual(mine["rank"], 1)

    def test_l_ancien_detenteur_reprend_apres_la_marge(self):
        # Correction d'arbitrage 4 : challenger mort après la libération, le
        # bail ne reste pas libre — l'ancien détenteur reprend après la marge.
        self.standing("pilote", "atelier", 99.0)
        self.standing("pilote", "banc", 90.0)
        obj = self.runner()
        obj.note_handover_release("pilote", "atelier", 0.0)
        now = obj.clock.monotonic()
        obj._election_free_since["pilote"] = now - 21.0
        allowed, reason, _ = obj.election_claim_allowed(self.role_row)
        self.assertFalse(allowed)
        self.assertIn("handover", reason)
        obj._handover_released["pilote"] = (now + 1.0, "atelier")
        self.assertFalse(obj.election_claim_allowed(self.role_row)[0])
        obj._handover_released["pilote"] = (now - 1.0, "atelier")
        allowed, reason, _ = obj.election_claim_allowed(self.role_row)
        self.assertTrue(allowed, reason)

    def test_le_runner_publie_le_score_de_l_hote(self):
        self.ready_host("pilote", "banc")
        obj = self.runner()
        self.assertEqual(obj.role_evaluation_once(), 1)
        rows = pg.latest(self.db, "pilote")
        self.assertIn("banc", rows)
        self.assertTrue(rows["banc"]["ready"], rows["banc"]["diagnostic"])
        # pas de seconde ligne dans la même minute
        self.assertIsNone(obj.role_evaluation_once())
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM role_standings")[0]["n"], 1)


if __name__ == "__main__":
    unittest.main()
