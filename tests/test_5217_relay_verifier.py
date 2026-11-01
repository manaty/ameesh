# SPDX-License-Identifier: AGPL-3.0-only
"""#5217 — le relais autonome compose le vérificateur sandbox.

Le serveur médié (`build_relay`) et `relay serve` doivent monter le **même**
vérificateur : le vérificateur de bail configuré, enveloppé par
`sandbox_relay.compose` (jetons `amsb1.` vérifiés par le tour, jeton de bail
d'un agent sandbox refusé). Sans cette composition, `relay serve` refuse les
jetons sandbox ou accepte un bail sans fencing (#5140, N relevé en relecture).

Les compléments N1/N3/N5 du panier sont couverts ici : le lien au spend key
passe par `turn_id` (docstring de `sandbox_relay`), l'échéance de l'appelant
n'est jamais l'expiration du jeton, et seul un tour vivant fend le bail.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest import TestCase, mock

from ameesh import relay, sandbox_relay

from .support import PgTestCase

IDENTITY = relay.Identity("exec", "host", "agent", "owner", 7)


class FakeTransaction:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def query(self, sql, args=()):
        return list(self.rows)

    def execute(self, sql, args=()):
        return None


class FakeDb:
    """Assez de `db` pour `RelayTokens` : `transaction()` pour le jeton
    sandbox, `query()` pour le fencing du jeton de bail."""

    def __init__(self, sandbox_rows=()):
        self.sandbox_rows = list(sandbox_rows)
        self.sandbox_queries = 0

    def query(self, sql, args=()):
        self.sandbox_queries += 1
        return list(self.sandbox_rows)

    def transaction(self):
        return FakeTransaction()


class ComposeTest(TestCase):

    def test_compose_wraps_the_configured_legacy_verifier(self):
        db = FakeDb()
        legacy = relay.StaticTokens({"lease": IDENTITY})
        verifier = sandbox_relay.compose(db, legacy)
        self.assertIsInstance(verifier, sandbox_relay.RelayTokens)
        self.assertIs(verifier.legacy, legacy)
        self.assertIs(verifier.db, db)
        self.assertEqual(verifier.verify_token("lease"), IDENTITY)

    def test_sandbox_bearer_never_reaches_the_legacy_verifier(self):
        legacy = mock.Mock()
        verifier = sandbox_relay.compose(FakeDb(), legacy)
        for token in (sandbox_relay.PREFIX + "a" * 43, sandbox_relay.PREFIX + "broken"):
            with self.assertRaises(relay.TokenError):
                verifier.verify_token(token)
        legacy.verify_token.assert_not_called()

    def test_lease_bearer_fenced_when_the_agent_holds_a_sandbox_turn(self):
        legacy = relay.StaticTokens({"lease": IDENTITY})
        allowed = sandbox_relay.compose(FakeDb(), legacy)
        self.assertEqual(allowed.verify_token("lease"), IDENTITY)
        fenced = sandbox_relay.compose(FakeDb([{"?": 1}]), legacy)
        with self.assertRaises(relay.TokenError):
            fenced.verify_token("lease")


class LoadTest(TestCase):

    def test_load_relay_verifier_composes_the_configured_verifier(self):
        db = FakeDb()
        legacy = relay.StaticTokens({"lease": IDENTITY})
        with mock.patch.object(relay, "load_verifier", return_value=legacy) as load:
            verifier = relay.load_relay_verifier(db, "module:objet")
        load.assert_called_once_with("module:objet")
        self.assertIsInstance(verifier, sandbox_relay.RelayTokens)
        self.assertIs(verifier.legacy, legacy)


class MainTest(TestCase):

    def test_serve_mounts_the_composed_verifier(self):
        """`relay serve` ne passe plus `load_verifier(spec)` nu à `Relay`."""
        db, verifier_db = FakeDb(), FakeDb()
        legacy = relay.StaticTokens({"lease": IDENTITY})
        relayed = mock.Mock()
        server = mock.Mock(server_address=("127.0.0.1", 0))
        with mock.patch.dict(os.environ, {"AMEESH_RELAY_VERIFIER": "module:objet"},
                             clear=True), \
                mock.patch("ameesh.platform.child_environment.install"), \
                mock.patch("ameesh.platform.child_environment.harden", return_value=True), \
                mock.patch("ameesh.config.load", return_value=SimpleNamespace()), \
                mock.patch("ameesh.db.connect", side_effect=[db, verifier_db]), \
                mock.patch.object(relay, "load_verifier", return_value=legacy), \
                mock.patch.object(relay, "Relay", return_value=relayed) as built, \
                mock.patch.object(relay, "make_server", return_value=server):
            code = relay.main(["serve", "--bind", "127.0.0.1:0"])
        self.assertEqual(code, 0)
        verifier = built.call_args.kwargs["verifier"]
        self.assertIsInstance(verifier, sandbox_relay.RelayTokens)
        self.assertIs(verifier.legacy, legacy)
        self.assertIs(verifier.db, verifier_db)
        server.serve_forever.assert_called_once_with()


class MediatedEntryTest(TestCase):

    def test_mediated_relay_uses_the_same_composition(self):
        from ameesh.mediated_executor import server as server_mod
        cfg = SimpleNamespace()
        pair = (FakeDb(), FakeDb())
        composed = mock.Mock()
        with mock.patch("ameesh.db.connect", side_effect=pair), \
                mock.patch("ameesh.relay.Relay") as built, \
                mock.patch("ameesh.sandbox_relay.compose", return_value=composed) as compose:
            server_mod.build_relay(cfg, mock.Mock(), mock.Mock())
        compose.assert_called_once()
        self.assertIs(compose.call_args.args[0], pair[0])
        self.assertEqual(compose.call_args.args[1].__class__.__name__, "ExecutorTokens")
        self.assertIs(built.call_args.kwargs["verifier"], composed)


class CallerDeadlineTest(TestCase):
    """#5217/N3 : l'échéance de l'appelant est un argument de contrat, jamais
    l'échéance du jeton : l'émetteur relit la ligne admise sous verrou et prend
    l'horloge de la base."""

    def test_caller_deadline_is_never_forwarded_as_expiry(self):
        from ameesh.runner import Runner
        owner = object.__new__(Runner)
        owner.db, owner.host = object(), 'host'
        owner.runner_id, owner.mediated = 'owner', False
        owner.cfg = SimpleNamespace(exec_url='https://relay.test')
        issued = relay.Identity('exec', 'host', 'agent', 'owner', 7,
                                turn='turn', expires_ts=2_000_000_000.0)
        with mock.patch.object(relay, 'relay_url',
                               return_value='https://relay.test'), \
                mock.patch.object(sandbox_relay, 'issue',
                                  return_value=(sandbox_relay.PREFIX + 'a' * 43,
                                                issued)) as issue:
            material = owner.sandbox_credentials_for(
                'agent', 'owner', 7, 'turn', '2000-01-01T00:00:00+00:00')
        self.assertIs(material.identity, issued)
        issue.assert_called_once_with(owner.db, agent='agent', owner='owner',
                                      epoch=7, turn_id='turn', host='host')


class LeaseFencingTest(PgTestCase):
    """#5217/N5 : seul un tour sandbox vivant fend le jeton de bail.

    Un tour terminal ne vérifie plus son propre jeton : le bail ordinaire
    redevient utilisable. Le fencing est par (agent, owner, epoch), donc un bail
    partagé par plusieurs agents n'est fendu que pour le porteur du tour."""

    def setUp(self):
        super().setUp()
        for table in ('sandbox_relay_tokens', 'sandbox_turns'):
            self.db.execute('delete from ' + table)
        self.db.execute(
            "insert into agent_registry (name, harness, host, lease_owner,"
            " lease_epoch, lease_expires_at)"
            " values ('fence-agent','deepseek','host-fence','owner',7,"
            " now() + interval '1 hour')"
            " on conflict (name) do update set host = excluded.host,"
            " lease_owner = excluded.lease_owner,"
            " lease_epoch = excluded.lease_epoch,"
            " lease_expires_at = excluded.lease_expires_at")
        self.identity = relay.Identity('exec', 'host-fence', 'fence-agent', 'owner', 7)

    def verifier(self, identity=None):
        return sandbox_relay.RelayTokens(
            self.db, relay.StaticTokens({'lease': identity or self.identity}))

    def seed(self, turn_id, state):
        self.db.execute(
            "insert into sandbox_turns (turn_id, spend_key, agent, owner, lease_epoch,"
            " scope, scope_digest, retry_family, scope_generation, attempt, max_attempts,"
            " profile_ref, profile_digest, placement_ref, placement_digest, canon_ref,"
            " limits, image_digest, provider, namespace, job_name, state, deadline_ts,"
            " source)"
            " values (%s,%s,'fence-agent','owner',7,'{}'::jsonb,%s,%s,1,1,1,"
            " 'p','pd','pl','pld','canon','{}'::jsonb,'img','kubernetes','ns',%s,%s,"
            " clock_timestamp() + interval '1 hour','{}'::jsonb)",
            (turn_id, 'sk-' + turn_id, 'sd-' + turn_id, 'rf-' + turn_id,
             'job-' + turn_id, state))

    def test_live_turn_fences_then_a_terminal_turn_releases_the_lease_bearer(self):
        self.seed('live', 'running')
        with self.assertRaises(relay.TokenError):
            self.verifier().verify_token('lease')
        self.db.execute("update sandbox_turns set state = 'succeeded',"
                        " completed_at = clock_timestamp() where turn_id = 'live'")
        self.assertEqual(self.verifier().verify_token('lease'), self.identity)

    def test_a_shared_lease_fences_only_the_agent_holding_the_live_turn(self):
        self.seed('live', 'running')
        other = relay.Identity('exec', 'host-fence', 'other-agent', 'owner', 7)
        self.assertEqual(self.verifier(other).verify_token('lease'), other)
