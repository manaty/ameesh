# SPDX-License-Identifier: AGPL-3.0-only
"""#7869 : le résolveur d'incident reconnaît un attach distant.

Un attach local inscrit `attach:<user>@<host>` dans le bail ; un attach
distant inscrit `attach:<tenant>:<appareil>` (conception 5322 §5.4), où
l'appareil est le `credential_id` borné d'un authentificateur `device-es256`.
Le résolveur d'incident (#7866) ne comparait que la première forme à
`Member.local_accounts` : un humain attaché à distance sur un agent épinglé
était invisible. Le témoin ci-dessous rattache le bail distant à son humain
par `authenticators.approver`, jamais par un hôte : un appareil d'un autre
humain, un appareil révoqué, une forme inconnue ou une base muette restent une
absence de signal, pas une présence (N2 de l'auditeur, lot #7866).
"""
from __future__ import annotations

import datetime as _dt
import unittest
from types import SimpleNamespace

from ameesh import attach_session, incident_routing, responder

from .support import PgTestCase


class _Db:
    """Un faux `Db` qui répond par requête et retient le SQL lu."""

    def __init__(self, *, leases=(), devices=(), devices_boom=False):
        self.leases = list(leases)
        self.devices = list(devices)
        self.devices_boom = devices_boom
        self.sql = []

    def query(self, sql, params=()):
        self.sql.append((sql, params))
        if "agent_registry" in sql:
            return [{"lease_owner": owner} for owner in self.leases]
        if "authenticators" in sql:
            if self.devices_boom:
                raise RuntimeError("database unavailable")
            return [{"credential_id": device} for device in self.devices]
        return []


def _member(title="alice", accounts=(), timezone="Asia/Shanghai"):
    """Un répondant dont le repli fuseau est hors horaires (02:00 à Shanghai)."""
    return SimpleNamespace(title=title, local_accounts=list(accounts),
                           timezone=timezone, incident_eligible=True,
                           fiche=SimpleNamespace(data={}))


class RemoteAttachSignalTest(unittest.TestCase):
    """Le bail distant est rattaché par l'appareil, jamais par l'hôte."""

    def test_a_remote_attach_of_the_members_device_is_recognised(self):
        db = _Db(leases=["attach:t1:device:alice"], devices=["device:alice"])
        self.assertTrue(responder.LiveSignals(db=db, env={}).attach(_member()))
        self.assertEqual(len(db.sql), 2)
        self.assertIn("agent_registry", db.sql[0][0])
        self.assertIn("authenticators", db.sql[1][0])
        self.assertEqual(db.sql[1][1], ("human:alice",))
        self.assertIn("facade = 'device-es256'", db.sql[1][0])
        self.assertIn("revoked_at is null", db.sql[1][0])

    def test_a_shared_host_without_a_tenant_is_recognised(self):
        owner = attach_session.remote_owner("", "device:alice")
        self.assertTrue(owner.startswith("attach:-:"))
        db = _Db(leases=[owner], devices=["device:alice"])
        self.assertTrue(responder.LiveSignals(db=db, env={}).attach(_member()))

    def test_a_remote_attach_of_another_human_is_not_attributed(self):
        db = _Db(leases=["attach:t1:device:bob"], devices=["device:alice"])
        self.assertIsNone(responder.LiveSignals(db=db, env={}).attach(_member()))

    def test_a_local_attach_still_matches_without_reading_devices(self):
        db = _Db(leases=["attach:alice@pc-smichea"])
        self.assertTrue(responder.LiveSignals(db=db, env={}).attach(
            _member(accounts=("alice@pc-smichea",))))
        self.assertEqual(len(db.sql), 1)

    def test_the_bounded_device_label_matches_the_owner_the_host_wrote(self):
        credential = "device:" + "x" * 200
        owner = attach_session.remote_owner("t1", credential)
        self.assertIn("~", owner)
        db = _Db(leases=[owner], devices=[credential])
        self.assertTrue(responder.LiveSignals(db=db, env={}).attach(_member()))

    def test_a_failing_device_lookup_is_an_absence(self):
        db = _Db(leases=["attach:t1:device:alice"], devices_boom=True)
        self.assertIsNone(responder.LiveSignals(db=db, env={}).attach(_member()))

    def test_a_local_lease_of_someone_else_does_not_query_devices(self):
        db = _Db(leases=["attach:bob@pc-smichea"])
        self.assertIsNone(responder.LiveSignals(db=db, env={}).attach(_member()))
        self.assertEqual(len(db.sql), 1)

    def test_an_unknown_lease_shape_is_not_an_appareil(self):
        db = _Db(leases=["attach:t1:", "attach:t1"])
        self.assertIsNone(responder.LiveSignals(db=db, env={}).attach(_member()))
        self.assertEqual(len(db.sql), 1)

    def test_no_live_lease_or_no_database_is_an_absence(self):
        self.assertIsNone(responder.LiveSignals(db=_Db(), env={}).attach(_member()))
        self.assertIsNone(responder.LiveSignals(env={}).attach(_member()))


class ResolverPicksRemoteAttachedTest(unittest.TestCase):
    """Le résolveur choisit l'humain dont l'appareil tient le bail distant."""

    NOW = _dt.datetime(2026, 11, 19, 18, 0, tzinfo=_dt.timezone.utc)

    class _Canon:
        def __init__(self, member):
            self.members = [member]

        @staticmethod
        def resolve_human(reference):
            return reference if reference == "human:alice" else None

    @staticmethod
    def _application():
        return SimpleNamespace(incident=incident_routing.IncidentPolicy(
            severities=("P1",),
            responders=(incident_routing.Responder(member="human:alice"),)))

    def test_the_remote_attached_human_is_chosen(self):
        member = _member()
        db = _Db(leases=["attach:t1:device:alice"], devices=["device:alice"])
        result = responder.resolve(self._Canon(member), self._application(), "P1",
                                   responder.LiveSignals(db=db, env={}), self.NOW)
        self.assertIs(result.responder, member)
        self.assertEqual(result.reason, "attached")
        self.assertEqual(result.signals, ("attach",))
        self.assertEqual(result.consulted, ("attach",))

    def test_another_humans_device_leaves_the_chain_without_a_responder(self):
        member = _member()
        db = _Db(leases=["attach:t1:device:bob"], devices=["device:alice"])
        result = responder.resolve(self._Canon(member), self._application(), "P1",
                                   responder.LiveSignals(db=db, env={}), self.NOW)
        self.assertIsNone(result.responder)
        self.assertEqual(result.reason, "no-responder")
        self.assertNotIn("attach", result.consulted)


class RemoteAttachPgTest(PgTestCase):
    """Le SQL réel : bail vivant, appareil actif du même humain, autre humain."""

    def member(self, title):
        return SimpleNamespace(title=title, local_accounts=[],
                               timezone="Asia/Shanghai", incident_eligible=True,
                               fiche=SimpleNamespace(data={}))

    def test_a_live_remote_lease_is_attached_only_to_its_own_human(self):
        self.db.execute(
            "insert into agent_registry (name, host, lease_owner, lease_expires_at)"
            " values ('pinned-7869', 'ameesh-agents-2', %s,"
            "         clock_timestamp() + interval '60 seconds')",
            ("attach:t1:device:alice",))
        canon_ref = "canon@%s" % ("a" * 40)
        self.db.execute(
            "insert into authenticators (approver, facade, credential_id,"
            " public_key, key_fingerprint, canon_ref)"
            " values ('human:alice', 'device-es256', 'device:alice',"
            "         'public', 'fingerprint', %s)", (canon_ref,))
        signals = responder.LiveSignals(db=self.db)
        self.assertTrue(signals.attach(self.member("alice")))
        self.assertIsNone(signals.attach(self.member("bob")))

    def test_a_revoked_device_does_not_hold_the_attachment(self):
        self.db.execute(
            "insert into agent_registry (name, host, lease_owner, lease_expires_at)"
            " values ('pinned-7869-revoked', 'ameesh-agents-2', %s,"
            "         clock_timestamp() + interval '60 seconds')",
            ("attach:t1:device:carol",))
        self.db.execute(
            "insert into authenticators (approver, facade, credential_id,"
            " public_key, key_fingerprint, canon_ref, revoked_at)"
            " values ('human:carol', 'device-es256', 'device:carol',"
            "         'public', 'fingerprint', %s, now())",
            ("canon@%s" % ("b" * 40),))
        self.assertIsNone(responder.LiveSignals(db=self.db).attach(
            self.member("carol")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
