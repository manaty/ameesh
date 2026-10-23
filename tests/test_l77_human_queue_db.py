# SPDX-License-Identifier: AGPL-3.0-only
"""L77 (#286): trace L78 des messages humains servis — base réelle.

Le journal ne porte aucun état servi/ack : l'entrée `human_message` est écrite
à la REMISE, avec `event_id = mail:<id>` pour qu'une remise incertaine rejouée
ne duplique pas la trace.
"""
from __future__ import annotations

import dataclasses
import json
import os

from ameesh import conversation, registry
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase


def _make_worker(test, name, prompt=None):
    cwd = os.path.join(test.tmp, "work", name)
    os.makedirs(cwd, exist_ok=True)
    kwargs = {"prompt": prompt} if prompt is not None else {}
    test.register(name, "claude", cwd=cwd, **kwargs)
    runner = Runner(dataclasses.replace(test.cfg), test.db, once=True)
    lease = registry.claim(test.db, name, runner.runner_id, 3600)
    test.assertIsNotNone(lease)
    worker = AgentWorker(runner, registry.get(test.db, name), lease)
    test.addCleanup(worker.watchdog_stop.set)
    test.addCleanup(worker._close_peek)
    return worker


def _capped(original):
    """Borne une lecture de boîte à UNREAD_LIMIT, comme le serveur médié."""
    from ameesh import mail

    def appel(*args, **kwargs):
        if len(args) >= 3:
            args = list(args)
            args[2] = min(int(args[2]), mail.UNREAD_LIMIT)
        elif "limit" in kwargs:
            kwargs = dict(kwargs)
            kwargs["limit"] = min(int(kwargs["limit"]), mail.UNREAD_LIMIT)
        return original(*args, **kwargs)

    return appel


class HumanJournalTest(PgTestCase):
    def _worker(self, name="worker"):
        return _make_worker(self, name)

    def _spec(self, mid=11, sender="human:alice", body="hello"):
        return {"human": True,
                "rows": [{"id": mid, "sender": sender, "body": body,
                          "payload": {"human": True}, "created_ts": 1.0}]}

    def _human_entries(self, name):
        page = conversation.history(self.db, name, limit=50)
        return [e for e in page["entries"] if e["kind"] == "human_message"]

    def test_human_message_journaled_once_per_delivery(self):
        name = "worker-once"
        worker = self._worker(name)
        worker._journal_humans(self._spec(), [11])
        # Une remise incertaine rejouée ne duplique pas l'entrée.
        worker._journal_humans(self._spec(), [11])
        entries = self._human_entries(name)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["author"], "human:alice")
        self.assertEqual(entries[0]["text"], "hello")
        self.assertEqual(entries[0]["event_id"], "mail:11")

    def test_bare_human_name_is_prefixed(self):
        name = "worker-bare"
        worker = self._worker(name)
        worker._journal_humans(self._spec(mid=12, sender="alice"), [12])
        entries = self._human_entries(name)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["author"], "human:alice")

    def test_non_human_spec_writes_nothing(self):
        name = "worker-plain"
        worker = self._worker(name)
        spec = self._spec()
        spec["human"] = False
        worker._journal_humans(spec, [11])
        self.assertEqual(self._human_entries(name), [])


class PickHumanFirstTest(PgTestCase):
    """L77 : `_pick` sert l'humain d'abord et ne le consomme pas.

    At-least-once : la construction du spec ne réserve ni ne remet rien ;
    seule la remise (lancement du harnais) solde la réservation.
    """

    def _bulk(self, name, sender, count, payload):
        """Dépose `count` messages d'un coup (les boucles de 200 coûtent cher)."""
        self.db.execute(
            "INSERT INTO agent_mailbox (sender, recipient, body, kind, payload)"
            " SELECT %s, %s, 'bulk ' || g, 'request', %s::jsonb"
            " FROM generate_series(1, %s) AS g",
            (sender, name, json.dumps(payload), count))

    def test_pick_serves_human_first_without_delivering(self):
        from ameesh import mail
        name = "worker-pick"
        worker = _make_worker(self, name)
        mail.send(self.db, "human:alice", name, "from a human",
                  payload={"human": True})
        mail.send(self.db, "agent:bob", name, "from an agent", payload={})
        spec = worker._pick()
        self.assertIsNotNone(spec)
        self.assertEqual(spec["kind"], "mail")
        self.assertTrue(spec.get("human"))
        self.assertEqual(len(spec["ids"]), 1)
        row = mail.get(self.db, spec["ids"][0])
        self.assertEqual(row["sender"], "human:alice")
        self.assertIsNone(row.get("delivered_at"))
        # Re-pick : la construction du spec n'a rien consommé.
        again = worker._pick()
        self.assertIsNotNone(again)
        self.assertEqual(again["ids"], spec["ids"])

    def test_human_beyond_the_unread_prefix_is_served_before_lower_ranks(self):
        """B13 : 200 actifs ordinaires devant l'humain ne le cachent pas.

        Le préfixe `UNREAD_LIMIT` est saturé par du courrier ordinaire ; sans
        recherche dédiée, `_pick` retombait sur la consigne en attente (rang 5)
        alors qu'un humain attendait (rang 2). Les deux formes d'humain sont
        couvertes : `payload.human` et l'ancien expéditeur `human:*`.
        """
        from ameesh import mail, registry
        name = "worker-pick-deep"
        worker = _make_worker(self, name)
        for index in range(mail.UNREAD_LIMIT):
            mail.send(self.db, "agent:bob", name,
                      "ordinary active %d" % index, payload={})
        mail.send(self.db, "human:alice", name, "marked human",
                  payload={"human": True})
        legacy = mail.send(self.db, "human:carol", name, "legacy human", payload={})
        registry.set_pending_prompt(self.db, name, "a queued instruction")
        spec = worker._pick()
        self.assertIsNotNone(spec)
        self.assertEqual(spec["kind"], "mail",
                         "the pending prompt (rank 5) hid the human (rank 2)")
        self.assertTrue(spec.get("human"))
        self.assertEqual(set(spec["ids"]), set(
            int(row["id"]) for row in self.db.query(
                "SELECT id FROM agent_mailbox WHERE recipient = %s"
                " AND (payload @> '{\"human\": true}'::jsonb"
                "      OR starts_with(sender, 'human:'))", (name,))))
        self.assertIn(legacy, spec["ids"])

    def test_human_survives_a_full_page_shrunk_by_an_expired_request(self):
        """B13/cx2 : la page LUE est pleine, `_live_mail` retire l'expirée.

        200 non-lus dont une requête expirée : `_live_mail` n'en laisse que
        199 vivants. Le déclencheur de la recherche humaine doit porter sur la
        page lue (200), pas sur le reste filtré (199), sinon la consigne en
        attente passe encore devant l'humain de la position 201.
        """
        from ameesh import mail, registry
        name = "worker-pick-expired"
        worker = _make_worker(self, name)
        for index in range(mail.UNREAD_LIMIT - 1):
            mail.send(self.db, "agent:bob", name,
                      "ordinary active %d" % index, payload={})
        mail.send(self.db, "agent:bob", name, "expired request",
                  payload={"request_expires_ts": 1.0})
        human = mail.send(self.db, "human:alice", name, "from a human",
                          payload={"human": True})
        registry.set_pending_prompt(self.db, name, "a queued instruction")
        spec = worker._pick()
        self.assertIsNotNone(spec)
        self.assertEqual(spec["kind"], "mail",
                         "the expired request shrank the page and hid the human")
        self.assertTrue(spec.get("human"))
        self.assertEqual(spec["ids"], [human])

    def test_active_human_behind_passive_human_copies_is_served(self):
        """B14/cx3 : 200 copies humaines passives ne remplissent pas la fenêtre.

        `mailbox.unread_human` doit filtrer les passifs DANS sa requête, sinon
        le premier humain actif (position 401) reste invisible et la consigne
        en attente passe devant lui.
        """
        from ameesh import mail, registry
        name = "worker-pick-passive"
        worker = _make_worker(self, name)
        self._bulk(name, "agent:bob", mail.UNREAD_LIMIT, {})
        self._bulk(name, "human:alice", mail.UNREAD_LIMIT,
                   {"human": True, "cc": "alice"})
        human = mail.send(self.db, "human:carol", name, "active human",
                          payload={"human": True})
        registry.set_pending_prompt(self.db, name, "a queued instruction")
        spec = worker._pick()
        self.assertIsNotNone(spec)
        self.assertEqual(spec["kind"], "mail",
                         "passive human copies filled the window before the active human")
        self.assertTrue(spec.get("human"))
        self.assertEqual(spec["ids"], [human])

    def test_human_after_200_retained_humans_is_served(self):
        """B14/cx2 : le curseur franchit 200 humains ACTIFS retenus.

        Le serveur médié borne `unread_human` à 200 : sans `after_id`, les 200
        premiers humains actifs (tous d'un lot retenu) remplissent la fenêtre
        et l'humain remisable en 201 reste invisible, derrière la consigne.
        """
        from unittest import mock
        from ameesh import lot_workdir, mail, registry
        name = "worker-pick-retained-humans"
        worker = _make_worker(self, name)
        # Deux projets : `deliverable_unread` filtre alors les lots retenus.
        self.db.execute(
            "UPDATE agent_registry SET projects = ARRAY['ameesh', 'second']"
            " WHERE name = %s", (name,))
        self._bulk(name, "human:alice", mail.UNREAD_LIMIT,
                   {"human": True, "retained": True})
        human = mail.send(self.db, "human:carol", name, "deliverable human",
                          payload={"human": True})
        registry.set_pending_prompt(self.db, name, "a queued instruction")

        def garder(cfg, db, agent, rows, *, deferred=None, canons=None):
            return [row for row in rows
                    if not (row.get("payload") or {}).get("retained")]

        real_unread = lot_workdir.mail.unread
        real_human = lot_workdir.mail.unread_human
        real_page = lot_workdir.mail.unread_human_page
        # #2940 : borner aussi le secours `active` — sinon il trouve humain:carol
        # directement derrière les 200 retenus et le curseur humain n'est pas
        # exercé (fausse régression verte).
        real_active = lot_workdir.mail.unread_active
        curseur = []

        def page_suivie(*args, **kwargs):
            curseur.append(1)
            return _capped(real_page)(*args, **kwargs)

        with mock.patch.object(lot_workdir.mail, "unread",
                               side_effect=_capped(real_unread)), \
                mock.patch.object(lot_workdir.mail, "unread_active",
                                  side_effect=_capped(real_active)), \
                mock.patch.object(lot_workdir.mail, "unread_human",
                                  side_effect=_capped(real_human)), \
                mock.patch.object(lot_workdir.mail, "unread_human_page",
                                  side_effect=page_suivie), \
                mock.patch.object(lot_workdir, "deliverable", side_effect=garder):
            spec = worker._pick()
            again = worker._pick()
        self.assertTrue(curseur,
                        "le curseur humain (unread_human_page) n'a pas ete exerce")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["kind"], "mail",
                         "200 retained humans filled the page before human 201")
        self.assertTrue(spec.get("human"))
        self.assertEqual(spec["ids"], [human])
        self.assertIsNotNone(again)
        self.assertEqual((again["kind"], again["ids"]), ("mail", [human]))

    def test_human_after_250_retained_humans_is_served(self):
        """B14/cx2 : plus de 200 retenus, le curseur lit plusieurs pages.

        250 humains actifs retenus : la première page (200) et la deuxième
        (50 retenus + l'humain remisable) doivent être lues avant la consigne.
        """
        from unittest import mock
        from ameesh import lot_workdir, mail, registry
        name = "worker-pick-retained-many"
        worker = _make_worker(self, name)
        self.db.execute(
            "UPDATE agent_registry SET projects = ARRAY['ameesh', 'second']"
            " WHERE name = %s", (name,))
        self._bulk(name, "human:alice", 250,
                   {"human": True, "retained": True})
        human = mail.send(self.db, "human:carol", name, "deliverable human",
                          payload={"human": True})
        registry.set_pending_prompt(self.db, name, "a queued instruction")

        def garder(cfg, db, agent, rows, *, deferred=None, canons=None):
            return [row for row in rows
                    if not (row.get("payload") or {}).get("retained")]

        real_unread = lot_workdir.mail.unread
        real_human = lot_workdir.mail.unread_human
        real_page = lot_workdir.mail.unread_human_page
        # #2940 : même borne du secours `active` qu'au cas 200 ; deux picks
        # rendent le même humain sans livraison.
        real_active = lot_workdir.mail.unread_active
        curseur = []

        def page_suivie(*args, **kwargs):
            curseur.append(1)
            return _capped(real_page)(*args, **kwargs)

        with mock.patch.object(lot_workdir.mail, "unread",
                               side_effect=_capped(real_unread)), \
                mock.patch.object(lot_workdir.mail, "unread_active",
                                  side_effect=_capped(real_active)), \
                mock.patch.object(lot_workdir.mail, "unread_human",
                                  side_effect=_capped(real_human)), \
                mock.patch.object(lot_workdir.mail, "unread_human_page",
                                  side_effect=page_suivie), \
                mock.patch.object(lot_workdir, "deliverable", side_effect=garder):
            spec = worker._pick()
            again = worker._pick()
        self.assertTrue(curseur,
                        "le curseur humain (unread_human_page) n'a pas ete exerce")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["kind"], "mail",
                         "more than 200 retained humans hid the deliverable human")
        self.assertTrue(spec.get("human"))
        self.assertEqual(spec["ids"], [human])
        self.assertIsNotNone(again)
        self.assertEqual((again["kind"], again["ids"]), ("mail", [human]))

    def test_human_after_a_truncated_page_is_served(self):
        """B14 : une page bornée (médié, 200 lignes) ne referme pas la recherche.

        300 non-lus, dont 150 d'un lot retenu parmi les 200 premiers ; un
        humain en position 251. La page bornée ne rend que 50 remisables :
        `page_pleine` doit venir de la lecture TRONQUÉE, pas du compte des
        remisables, sinon la consigne en attente passe avant l'humain.
        """
        from unittest import mock
        from ameesh import lot_workdir, mail, registry
        name = "worker-pick-truncated"
        worker = _make_worker(self, name)
        self._bulk(name, "agent:bob", 150, {"retained": True})
        self._bulk(name, "agent:bob", 100, {})
        human = mail.send(self.db, "human:alice", name, "active human",
                          payload={"human": True})
        registry.set_pending_prompt(self.db, name, "a queued instruction")

        real_unread = lot_workdir.mail.unread

        def borne(db, recipient, limit=mail.UNREAD_LIMIT):
            # Backend médié : la page est ramenée à 200 lignes, sans décalage.
            return real_unread(db, recipient, limit=min(int(limit), mail.UNREAD_LIMIT))

        def garder(cfg, db, agent, rows, *, deferred=None, canons=None):
            return [row for row in rows
                    if not (row.get("payload") or {}).get("retained")]

        with mock.patch.object(lot_workdir.mail, "unread", side_effect=borne), \
                mock.patch.object(lot_workdir, "deliverable", side_effect=garder):
            spec = worker._pick()
        self.assertIsNotNone(spec)
        self.assertEqual(spec["kind"], "mail",
                         "a truncated page hid the human behind the retained prefix")
        self.assertTrue(spec.get("human"))
        self.assertEqual(spec["ids"], [human])

    def test_old_server_without_deep_human_search_does_not_kill_pick(self):
        """N : un serveur 1.4 refuse `mailbox.unread_human` ; le fil survit.

        La recherche profonde est alors indisponible : lecture comme avant 1.5
        (ici la consigne en attente), sans laisser remonter
        `NotSupportedRemotely`.
        """
        from unittest import mock
        from ameesh import lot_workdir, mail, registry
        from ameesh.mediated_executor.contract import NotSupportedRemotely
        name = "worker-pick-old-server"
        worker = _make_worker(self, name)
        self._bulk(name, "agent:bob", mail.UNREAD_LIMIT, {})
        registry.set_pending_prompt(self.db, name, "a queued instruction")

        original = lot_workdir.deliverable_unread

        def lecture(cfg, db, agent, *, besoin, mode="unread", **kwargs):
            if mode == "human":
                raise NotSupportedRemotely("op_not_allowed")
            return original(cfg, db, agent, besoin=besoin, mode=mode, **kwargs)

        with mock.patch.object(lot_workdir, "deliverable_unread",
                               side_effect=lecture):
            spec = worker._pick()
        self.assertIsNotNone(spec)
        self.assertEqual(spec["kind"], "prompt")
        self.assertEqual(spec["prompt"], "a queued instruction")


class EndOfTurnServiceTest(PgTestCase):
    """L77 : le message humain n'est soldé qu'à la FIN du tour."""

    def _reserve(self, worker, sender, payload):
        from ameesh import mail
        mail.send(self.db, sender, worker.name, "body of the message", payload=payload)
        mid = int(self.db.query(
            "SELECT id FROM agent_mailbox WHERE recipient = %s ORDER BY id DESC LIMIT 1",
            (worker.name,))[0]["id"])
        token = mail.new_token()
        reserves = mail.reserve(self.db, worker.name, worker.runner.runner_id,
                                worker.epoch, token, ids=[mid], porteur="consigne",
                                ttl_seconds=mail.RESERVATION_TTL)
        self.assertTrue(reserves)
        return mid, token, reserves

    def _delivered_at(self, mid):
        return self.db.query("SELECT delivered_at FROM agent_mailbox WHERE id = %s",
                             (mid,))[0]["delivered_at"]

    def test_human_is_settled_only_at_the_end_of_the_turn(self):
        from ameesh import conversation
        name = "worker-serve"
        worker = _make_worker(self, name)
        mid, token, reserves = self._reserve(worker, "human:alice", {"human": True})
        spec = {"human": True, "ids": [mid], "jeton": token, "rows": list(reserves)}
        worker._livre_courrier(spec)
        self.assertIsNone(self._delivered_at(mid))  # pas soldé au lancement
        worker._solde_humains(spec)
        self.assertIsNotNone(self._delivered_at(mid))  # soldé à la fin du tour
        page = conversation.history(self.db, name, limit=10)
        self.assertIn("human_message", [e["kind"] for e in page["entries"]])

    def test_non_human_mail_is_still_settled_at_launch(self):
        name = "worker-agent-mail"
        worker = _make_worker(self, name)
        mid, token, reserves = self._reserve(worker, "agent:bob", {})
        spec = {"ids": [mid], "jeton": token, "rows": list(reserves)}
        worker._livre_courrier(spec)
        self.assertIsNotNone(self._delivered_at(mid))


class HumanReservationTtlTest(PgTestCase):
    """L77/B6 : la réservation humaine couvre tout le tour, pas 10 min."""

    def test_the_human_reservation_outlives_the_turn(self):
        from ameesh import mail
        name = "worker-b6"
        worker = _make_worker(self, name)
        worker.agent = dict(worker.agent or {}, turn_max_seconds=1800)
        self.assertEqual(worker._reservation_ttl({"human": True}),
                         1800.0 + mail.RESERVATION_TTL)
        self.assertEqual(worker._reservation_ttl({}), mail.RESERVATION_TTL)

        mail.send(self.db, "human:alice", name, "a long turn",
                  payload={"human": True})
        mid = int(self.db.query(
            "SELECT id FROM agent_mailbox WHERE recipient = %s ORDER BY id DESC LIMIT 1",
            (name,))[0]["id"])
        token = mail.new_token()
        self.assertTrue(mail.reserve(
            self.db, name, worker.runner.runner_id, worker.epoch, token,
            ids=[mid], porteur="consigne",
            ttl_seconds=worker._reservation_ttl({"human": True})))
        row = self.db.query(
            "SELECT (meta->'consigne'->>'expire')::float8"
            "     - (meta->'consigne'->>'ts')::float8 AS ttl"
            "  FROM agent_mailbox WHERE id = %s", (mid,))[0]
        self.assertGreaterEqual(float(row["ttl"]),
                                1800.0 + mail.RESERVATION_TTL - 1.0)
        # Le hook (autre jeton, même bail) ne reprend pas un message actif :
        # c'est l'expiration du TTL qui le lui permettait.
        self.assertEqual(mail.reserve(
            self.db, name, worker.runner.runner_id, worker.epoch,
            mail.new_token(), ids=[mid], porteur="hook",
            ttl_seconds=mail.HOOK_RESERVATION_TTL), [])

        worker.agent = dict(worker.agent, turn_max_seconds=0)
        self.assertEqual(worker._reservation_ttl({"human": True}),
                         mail.HUMAN_RESERVATION_MAX)
