# SPDX-License-Identifier: AGPL-3.0-only
"""#7833 : bascule d'un avatar quand le runner du poste s'arrête.

Un arrêt automatique (`stop_reason = 'erreur'`, L48) d'un avatar
`releve: preference` n'est pas un arrêt humain : après la grâce de 10 min,
l'hôte cloud prend la conversation par un déplacement `recovered`, le poste
reste absent tant que son runner n'est pas réparé et l'humain reçoit un avis
unique. Un arrêt `manuel` n'est jamais repris par cette bascule.
"""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from ameesh import (avatar_failover, conversation_move as M, election,
                    exploitation, registry, storage)

from .test_l165_ep2_election_score import _RoleCase

READY = {c: True for c in M.CHECKS}


def ready(_move):
    return READY


class _AvatarCase(_RoleCase):
    POSTE = "banc"
    CLOUD = "atelier"

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM conversation_moves")
        self.role_row = self.role(hosts=(self.POSTE, self.CLOUD))
        self.db.execute(
            "UPDATE agent_registry SET releve = 'preference',"
            " admitted_hosts = %s::text[], cwd = '/work',"
            " responsible = 'human:alice' WHERE name = 'pilote'",
            ("{%s,%s}" % (self.POSTE, self.CLOUD),))
        for rank, host in enumerate((self.POSTE, self.CLOUD)):
            self.ready_host("pilote", host)
            self.standing("pilote", host, 10.0)
            self.db.execute(
                "UPDATE agent_candidates SET preference_rank = %s"
                " WHERE name = 'pilote' AND host = %s", (rank, host))

    # -- fixtures ----------------------------------------------------------
    def row(self) -> dict:
        return registry.get(self.db, "pilote")

    def stop(self, *, age_s: float = 0.0, reason: str = "erreur",
             status: str = "stopped") -> dict:
        """Pose l'arrêt puis recule `status_since` (le trigger le remet à now)."""
        self.db.execute(
            "UPDATE agent_registry SET status = %s, stop_reason = %s,"
            " status_text = 'stopped after 5 consecutive fast failures',"
            " last_error = 'Failed to authenticate: this login reached its"
            " time limit and cannot be renewed' WHERE name = 'pilote'",
            (status, reason))
        self.db.execute(
            "UPDATE agent_registry SET status_since = clock_timestamp()"
            " - make_interval(secs => %s) WHERE name = 'pilote'", (age_s,))
        return self.row()

    def stale_poste(self) -> None:
        """Le runner arrêté ne publie plus de standing sur le poste."""
        self.db.execute(
            "UPDATE role_standings SET evaluated_at = clock_timestamp()"
            " - interval '1 hour' WHERE name = 'pilote' AND host = %s",
            (self.POSTE,))

    def runner(self, host: str | None = None) -> SimpleNamespace:
        from ameesh.runner import Runner
        runner = SimpleNamespace(
            db=self.db, host=host or self.CLOUD,
            runner_id="runner-%s" % (host or "cloud"),
            dry_run=False, mediated=False, agents_filter=None,
            clock=SimpleNamespace(monotonic=time.monotonic),
            _preference_next=0.0, _preference_seen_since={},
            _preference_plan_next={}, _preference_error=None)
        for name in ("preference_return_pass", "advance_conversation_moves"):
            setattr(runner, name, getattr(Runner, name).__get__(runner))
        return runner

    def cloud_item(self) -> dict:
        return next(item for item in election.candidacy(self.db, "pilote")
                    if item["host"] == self.CLOUD)

    def poste_item(self) -> dict:
        return next(item for item in election.candidacy(self.db, "pilote")
                    if item["host"] == self.POSTE)

    def failover_to_cloud(self) -> None:
        """Le chemin #7833 : le poste s'arrête, la grâce passe, le cloud prend."""
        self.stale_poste()
        self.stop(age_s=avatar_failover.GRACE_S + 1.0)
        self.assertEqual(self.runner().preference_return_pass(), ["pilote"])
        steps = M.advance(self.db, self.CLOUD, probes=ready,
                          cwd_for=lambda _move: "/work/cloud")
        self.assertEqual([step for _ident, step in steps],
                         ["prepared", "switched"])
        lease = registry.claim(self.db, "pilote", "runner-cloud", 120,
                               host=self.CLOUD)
        self.assertIsNotNone(lease)
        # le premier tour cloud a eu lieu : la tentative de bascule n'est plus
        # ouverte, `switched_at` garde la trace de l'arrivée.
        self.db.execute(
            "UPDATE conversation_moves SET state = 'failed',"
            " failed_at = clock_timestamp(), error = 'test: turn served'"
            " WHERE agent = 'pilote'")

    def cloud_dies(self, *, lease: bool = True, standing: bool = True) -> None:
        """L'hôte cloud meurt : bail expiré (ou plus de bail), ligne non stopped."""
        if lease:
            self.db.execute(
                "UPDATE agent_registry SET status = 'running',"
                " lease_owner = 'runner-cloud', lease_expires_at ="
                " clock_timestamp() - interval '5 seconds'"
                " WHERE name = 'pilote'")
        else:
            self.db.execute(
                "UPDATE agent_registry SET status = 'idle', lease_owner = NULL,"
                " lease_expires_at = NULL WHERE name = 'pilote'")
        if standing:
            self.db.execute(
                "UPDATE role_standings SET evaluated_at = clock_timestamp()"
                " - interval '1 hour' WHERE name = 'pilote' AND host = %s",
                (self.CLOUD,))


class GraceTest(_AvatarCase):
    def test_grace_then_the_cloud_is_rank_zero(self):
        self.stale_poste()
        self.stop(age_s=60.0)
        self.assertFalse(avatar_failover.due(self.row(), time.time() + 0))
        cloud = self.cloud_item()
        self.assertFalse(cloud["ready"])
        self.assertIn("failover in", cloud["diagnostic"])
        self.assertFalse(self.poste_item()["ready"])

        self.stop(age_s=avatar_failover.GRACE_S + 1.0)
        cloud = self.cloud_item()
        self.assertTrue(cloud["ready"])
        self.assertEqual(cloud["rank"], 0)
        self.assertFalse(self.poste_item()["ready"])

    def test_a_manual_stop_never_fails_over(self):
        self.stale_poste()
        self.stop(age_s=avatar_failover.GRACE_S * 10, reason="manuel")
        self.assertFalse(avatar_failover.due(self.row(), time.time()))
        self.assertFalse(self.cloud_item()["ready"])
        self.assertIn("role stopped", self.cloud_item()["diagnostic"])

    def test_no_other_declared_host_means_no_failover(self):
        self.db.execute("UPDATE agent_registry SET candidats = %s::text[],"
                        " admitted_hosts = %s::text[] WHERE name = 'pilote'",
                        ("{banc}", "{banc}"))
        self.stale_poste()
        row = self.stop(age_s=avatar_failover.GRACE_S + 1.0)
        self.assertFalse(avatar_failover.due(row, time.time()))
        self.assertEqual(avatar_failover.target_host(row), "")

    def test_a_stopped_avatar_is_published_by_the_other_host_only(self):
        from ameesh import role_standings_pg as pg
        self.stop(age_s=60.0)
        cloud = pg.elected_roles(self.db, self.CLOUD, releves=("preference",))
        poste = pg.elected_roles(self.db, self.POSTE, releves=("preference",))
        self.assertEqual([row["name"] for row in cloud], ["pilote"])
        self.assertIn("status_since_ts", cloud[0])
        self.assertEqual(poste, [])


class RecoveryTest(_AvatarCase):
    def test_before_the_grace_the_preferred_host_does_nothing(self):
        self.stale_poste()
        self.stop(age_s=60.0)
        self.assertIsNone(election.preference_return(
            self.db, self.row(), self.CLOUD))

    def test_after_the_grace_the_cloud_plans_and_takes_the_conversation(self):
        self.stale_poste()
        self.stop(age_s=avatar_failover.GRACE_S + 1.0)
        verdict = election.preference_return(self.db, self.row(), self.CLOUD)
        self.assertEqual((verdict["state"], verdict["recovery"]), ("plan", True))
        self.assertEqual(verdict["holder"], self.POSTE)
        self.assertEqual(verdict.get("reason"), "failover")

        runner = self.runner()
        self.assertEqual(runner.preference_return_pass(), ["pilote"])
        move = storage.of(self.db).conversation_moves.open_for("pilote") or {}
        self.assertEqual((move.get("target_host"), move.get("state")),
                         (self.CLOUD, "planned"))
        self.assertEqual(move.get("reason"), "failover")
        step = M.advance(self.db, self.CLOUD, probes=ready,
                         cwd_for=lambda m: "/work/cloud")
        self.assertEqual([value for _ident, value in step],
                         ["prepared", "switched"])
        row = self.row()
        self.assertEqual((row["host"], row["status"], row["stop_reason"]),
                         (self.CLOUD, "idle", None))
        self.assertIn("pilote", [item["name"] for item in
                                 registry.claimable(self.db, self.CLOUD,
                                                    ["pilote"])])
        lease = registry.claim(self.db, "pilote", "runner-cloud", 120,
                               host=self.CLOUD)
        self.assertIsNotNone(lease)
        self.assertEqual(lease["host"], self.CLOUD)
        # #7680: a preference claim never carries a brief, so the row keeps
        # `idle` (only an elected cross-host takeover opens `queued`); the
        # live lease is what lets the cloud executor run its first turn.
        row = self.row()
        self.assertEqual((row["status"], row["stop_reason"]), ("idle", None))
        self.assertEqual(row["lease_owner"], "runner-cloud")

    def test_after_a_failover_the_poste_presence_does_not_plan_a_return(self):
        """#7833 B (relecture 13) : pas de ping-pong poste ↔ cloud.

        Une fois la conversation basculée par `failover`, le poste peut
        republier un standing frais : le retour automatique de présence (B2)
        ne doit pas reprendre l'avatar tant qu'un humain ne l'a pas déplacé.
        """
        self.stale_poste()
        self.stop(age_s=avatar_failover.GRACE_S + 1.0)
        self.assertEqual(self.runner().preference_return_pass(), ["pilote"])
        M.advance(self.db, self.CLOUD, probes=ready, cwd_for=lambda m: "/work/cloud")
        lease = registry.claim(self.db, "pilote", "runner-cloud", 120,
                               host=self.CLOUD)
        self.assertIsNotNone(lease)
        # la tentative de bascule n'est plus ouverte (le premier tour cloud a
        # eu lieu) ; `switched_at` garde la trace de l'arrivée.
        self.db.execute(
            "UPDATE conversation_moves SET state = 'failed',"
            " failed_at = clock_timestamp(), error = 'test: turn served'"
            " WHERE agent = 'pilote'")
        # le poste redevient présent et frais, rang 0
        self.standing("pilote", self.POSTE, 10.0)
        verdict = election.preference_return(
            self.db, self.row(), self.POSTE,
            seen_since=time.monotonic() - 200.0)
        self.assertEqual(verdict["state"], "plan")
        self.assertFalse(verdict["recovery"])
        # le retour par préférence est refusé par la retenue de bascule
        self.assertIsNone(M.plan(
            self.db, "pilote", self.POSTE, requested_by="test",
            reason="preference"))
        self.assertIsNone(storage.of(self.db).conversation_moves.open_for("pilote"))

    def test_a_manual_move_revives_a_stopped_preference_avatar(self):
        """N (relecture 13) : le réveil dépend de la relève, pas de la raison."""
        self.stale_poste()
        self.stop(age_s=0.0, reason="erreur")
        move = M.plan(self.db, "pilote", self.CLOUD,
                      requested_by="test", reason="manual")
        self.assertIsNotNone(move)
        M.advance(self.db, self.CLOUD, probes=ready,
                  cwd_for=lambda m: "/work/cloud")
        row = self.row()
        self.assertEqual((row["host"], row["status"], row["stop_reason"]),
                         (self.CLOUD, "idle", None))

    def test_a_manual_stop_is_not_revived_by_the_switch(self):
        self.stale_poste()
        self.stop(age_s=0.0, reason="manuel")
        move = M.plan(self.db, "pilote", self.CLOUD,
                      requested_by="test", reason="preference")
        self.assertIsNotNone(move)
        M.advance(self.db, self.CLOUD, probes=ready,
                  cwd_for=lambda m: "/work/cloud")
        row = self.row()
        self.assertEqual((row["host"], row["status"], row["stop_reason"]),
                         (self.CLOUD, "stopped", "manuel"))


class DeadHostTest(_AvatarCase):
    """#7900 : la retenue de bascule ne vaut que sur un hôte cloud vivant."""

    THIRD = "troisieme"

    def test_an_expired_cloud_lease_releases_the_avatar_to_the_poste(self):
        # Le bail expire (signature `dead_runner`, ligne non `stopped`) alors
        # que le standing du cloud est encore frais : le bail expiré suffit à
        # déclarer l'hôte mort et à libérer l'avatar.
        self.failover_to_cloud()
        self.cloud_dies(standing=False)
        self.standing("pilote", self.POSTE, 10.0)
        verdict = election.preference_return(
            self.db, self.row(), self.POSTE,
            seen_since=time.monotonic() - election.PREFERENCE_DWELL_S - 1.0)
        self.assertEqual((verdict["state"], verdict["recovery"]), ("plan", True))
        move = M.plan(self.db, "pilote", self.POSTE, requested_by="test",
                      reason="preference")
        self.assertIsNotNone(move)
        self.assertEqual(move["reason"], "preference")
        steps = M.advance(self.db, self.POSTE, probes=ready,
                          cwd_for=lambda _move: "/work/poste")
        self.assertEqual([step for _ident, step in steps],
                         ["prepared", "recovered"])
        row = self.row()
        self.assertEqual((row["host"], row["status"], row["lease_owner"]),
                         (self.POSTE, "idle", None))
        self.assertIsNotNone(registry.claim(
            self.db, "pilote", "runner-poste", 120, host=self.POSTE))

    def test_a_silent_cloud_host_releases_the_avatar(self):
        # Entre deux tours, sans bail : seul le standing prouve la vie. Muet,
        # l'hôte est mort et ne retient plus l'avatar.
        self.failover_to_cloud()
        self.cloud_dies(lease=False)
        self.standing("pilote", self.POSTE, 10.0)
        verdict = election.preference_return(self.db, self.row(), self.POSTE)
        self.assertEqual((verdict["state"], verdict["recovery"]), ("plan", True))
        move = M.plan(self.db, "pilote", self.POSTE, requested_by="test",
                      reason="preference")
        self.assertIsNotNone(move)
        steps = M.advance(self.db, self.POSTE, probes=ready,
                          cwd_for=lambda _move: "/work/poste")
        self.assertEqual([step for _ident, step in steps],
                         ["prepared", "switched"])
        self.assertEqual(self.row()["host"], self.POSTE)
        self.assertIsNotNone(registry.claim(
            self.db, "pilote", "runner-poste", 120, host=self.POSTE))

    def test_the_poste_runner_plans_the_return_from_a_dead_cloud(self):
        # Le chemin réel : la passe B2 du poste planifie elle-même la reprise
        # quand le cloud est mort (bail expiré), sans attendre un agent move.
        self.failover_to_cloud()
        self.cloud_dies(standing=False)
        self.standing("pilote", self.POSTE, 10.0)
        self.assertEqual(self.runner(self.POSTE).preference_return_pass(),
                         ["pilote"])
        move = storage.of(self.db).conversation_moves.open_for("pilote") or {}
        self.assertEqual((move.get("state"), move.get("target_host"),
                          move.get("reason")),
                         ("planned", self.POSTE, "preference"))

    def test_a_living_cloud_keeps_the_avatar(self):
        # Cloud vivant et au repos (pas de bail, standing frais) : la retenue
        # #7833 tient, pas de ping-pong poste ↔ cloud.
        self.failover_to_cloud()
        self.db.execute(
            "UPDATE agent_registry SET status = 'idle', lease_owner = NULL,"
            " lease_expires_at = NULL WHERE name = 'pilote'")
        self.standing("pilote", self.CLOUD, 10.0)
        self.standing("pilote", self.POSTE, 10.0)
        verdict = election.preference_return(
            self.db, self.row(), self.POSTE,
            seen_since=time.monotonic() - election.PREFERENCE_DWELL_S - 1.0)
        self.assertEqual((verdict["state"], verdict["recovery"]), ("plan", True))
        self.assertIsNone(M.plan(
            self.db, "pilote", self.POSTE, requested_by="test",
            reason="preference"))

    def test_another_eligible_host_takes_the_avatar(self):
        # Le cloud meurt, le poste est muet aussi : le troisième hôte éligible
        # est rang 0 et prend l'avatar ; la retenue ne le bloque plus.
        hosts = (self.POSTE, self.CLOUD, self.THIRD)
        self.db.execute(
            "UPDATE agent_registry SET candidats = %s::text[],"
            " admitted_hosts = %s::text[] WHERE name = 'pilote'",
            ("{%s}" % ",".join(hosts), "{%s}" % ",".join(hosts)))
        self.ready_host("pilote", self.THIRD)
        self.standing("pilote", self.THIRD, 10.0)
        self.db.execute(
            "UPDATE agent_candidates SET preference_rank = 2"
            " WHERE name = 'pilote' AND host = %s", (self.THIRD,))
        self.failover_to_cloud()
        self.cloud_dies()
        self.db.execute(
            "UPDATE role_standings SET evaluated_at = clock_timestamp()"
            " - interval '1 hour' WHERE name = 'pilote' AND host = %s",
            (self.POSTE,))
        verdict = election.preference_return(self.db, self.row(), self.THIRD)
        self.assertEqual((verdict["state"], verdict["recovery"]), ("plan", True))
        move = M.plan(self.db, "pilote", self.THIRD, requested_by="test",
                      reason="preference")
        self.assertIsNotNone(move)
        steps = M.advance(self.db, self.THIRD, probes=ready,
                          cwd_for=lambda _move: "/work/troisieme")
        self.assertEqual([step for _ident, step in steps],
                         ["prepared", "recovered"])
        self.assertEqual(self.row()["host"], self.THIRD)


class AlertTest(_AvatarCase):
    def alerts(self) -> list[dict]:
        return exploitation.alerts(self.cfg, self.db)

    def test_one_alert_with_the_reason_and_the_target(self):
        self.stale_poste()
        self.stop(age_s=60.0)
        found = [alert for alert in self.alerts()
                 if alert["type"] == "avatar_failover"]
        self.assertEqual(len(found), 1, found)
        alert = found[0]
        self.assertEqual(alert["agent"], "pilote")
        self.assertEqual(alert["host"], self.POSTE)
        self.assertEqual(alert["target"], self.CLOUD)
        self.assertEqual(alert["responsible"], "human:alice")
        self.assertIn("failover to %s" % self.CLOUD, alert["detail"])
        self.assertIn("log in again", alert["detail"])
        # the same episode keeps the same `since` (one notification)
        again = [item for item in self.alerts()
                 if item["type"] == "avatar_failover"]
        self.assertEqual(again[0]["since"], alert["since"])

    def test_the_alert_names_the_missing_fallback(self):
        self.db.execute("UPDATE agent_registry SET candidats = %s::text[],"
                        " admitted_hosts = %s::text[] WHERE name = 'pilote'",
                        ("{banc}", "{banc}"))
        self.stale_poste()
        self.stop(age_s=60.0)
        found = [alert for alert in self.alerts()
                 if alert["type"] == "avatar_failover"]
        self.assertEqual(len(found), 1, found)
        self.assertIn("no fallback host", found[0]["detail"])

    def test_notify_pushes_the_type_by_default(self):
        from ameesh import notify
        self.assertIn("avatar_failover", notify.DEFAULT_TYPES)
        self.assertEqual(notify.type_label("avatar_failover"),
                         "avatar failed over after its runner stopped")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
