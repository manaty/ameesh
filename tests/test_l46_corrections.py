# SPDX-License-Identifier: AGPL-3.0-only
"""L46 : corrections issues de la relecture indépendante des lots L36–L45."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from ameesh import canon, canon_sync, mail, registry
from ameesh.storage.postgres import canon as pg_canon

from .support import PgTestCase
from .test_l39_adoption_reprise import SID, _Base as _BaseL39, _rollout
from .test_l42_plusieurs_canons import HOST, ID_A, ID_B, _DeuxCanons


class ExterneJamaisReclameTest(PgTestCase):
    """Point 1 : un agent `externe` n'est jamais réclamé par l'exécuteur."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "humain-codex", harness="codex", host=self.cfg.host,
                        cwd=self.tmp, session_id="s-ext", mode="externe")
        registry.upsert(self.db, "ouvrier", harness="claude", host=self.cfg.host,
                        cwd=self.tmp)
        mail.send(self.db, "orch", "humain-codex", "à lire")
        mail.send(self.db, "orch", "ouvrier", "à lire")

    def test_claimable_et_claim_ignorent_l_externe(self):
        noms = [r["name"] for r in registry.claimable(self.db, self.cfg.host)]
        self.assertEqual(noms, ["ouvrier"])
        self.assertEqual(
            registry.claimable(self.db, self.cfg.host, ["humain-codex"]), [])
        self.assertIsNone(registry.claim(self.db, "humain-codex", "r1", 60))
        row = registry.get(self.db, "humain-codex")
        self.assertIsNone(row["lease_owner"])
        self.assertEqual(row["session_id"], "s-ext")
        self.assertTrue(registry.claim(self.db, "ouvrier", "r1", 60))

    def test_attach_reste_permis_sur_un_externe(self):
        # session humaine interactive : permise, et le mode ne change pas
        self.assertTrue(registry.attach_claim(self.db, "humain-codex", "attach:x", 60))
        self.assertEqual(registry.get(self.db, "humain-codex")["mode"], "externe")


class HookSansBailTest(PgTestCase):
    """Point 2 : écritures sans bail sur la ligne d'un agent `execute`."""

    def _hook(self, name, session="s-etrangere", event="SessionStart", cwd="/ailleurs"):
        return self.cli("hook", "codex", env=self.env(AGENT_MAIL_NAME=name),
                        stdin=json.dumps({"hook_event_name": event,
                                          "session_id": session, "cwd": cwd}))

    def test_ligne_execute_seul_last_seen_avance(self):
        # agent de l'exécuteur, encore sans session : rien ne s'écrit sauf last_seen
        registry.upsert(self.db, "ouvrier", harness="claude", host="autre-hote",
                        cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET last_seen = now() - interval '1 hour'")
        avant = registry.get(self.db, "ouvrier")
        proc = self._hook("ouvrier")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        apres = registry.get(self.db, "ouvrier")
        for champ in ("host", "harness", "session_id", "cwd", "chantier", "mode"):
            self.assertEqual(apres[champ], avant[champ], champ)
        self.assertGreater(apres["last_seen_ts"], avant["last_seen_ts"])

    def test_ligne_externe_suit_la_session(self):
        registry.upsert(self.db, "humain", harness="claude", mode="externe")
        proc = self._hook("humain", session="s-1", cwd=self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = registry.get(self.db, "humain")
        self.assertEqual((row["harness"], row["session_id"], row["host"]),
                         ("codex", "s-1", self.cfg.host))

    def test_source_explicite_refusee_si_bail_vivant(self):
        registry.upsert(self.db, "ouvrier", harness="claude", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET lease_owner = 'runner-x', lease_epoch = 1, "
                        "lease_expires_at = now() + interval '1 hour' WHERE name = 'ouvrier'")
        mail.send(self.db, "orch", "ouvrier", "Pour le prochain tour.")
        proc = self._hook("ouvrier", event="UserPromptSubmit")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("mené par l'exécuteur", proc.stderr)
        # et la réservation sans bail le refuse elle-même, sous verrou
        self.assertEqual(mail.reserve(self.db, "ouvrier", None, None, mail.new_token(),
                                      porteur="hook"), [])
        n = self.db.query("SELECT count(*)::int AS n FROM agent_mailbox "
                          "WHERE recipient = 'ouvrier' AND delivered_at IS NULL")[0]["n"]
        self.assertEqual(n, 1)
        # bail échu : la remise explicite redevient possible
        self.db.execute("UPDATE agent_registry SET lease_expires_at = now() - interval '1 s'")
        proc = self._hook("ouvrier", event="UserPromptSubmit")
        self.assertIn("Pour le prochain tour.", proc.stdout)


class CanonParDefautRejoueTest(_DeuxCanons):
    """Point 3 : le changement de canon par défaut n'est jamais rejoué."""

    def _defaut(self):
        return self.db.query("SELECT canon_id, status FROM canon_state "
                             "WHERE host = %s AND canon = ''", (HOST,))[0]

    def _canons(self):
        return {n: self.row(n)["canon"] for n in ("a1", "a2", "b1", "b2")}

    def test_exception_apres_rebase_puis_rejeu_idempotent(self):
        self.sync_a()
        self.sync_b()
        self.assertEqual(self._defaut()["canon_id"], ID_A)
        # un éphémère du futur canon par défaut (B), que sa synchronisation
        # ne réécrit pas
        registry.upsert(self.db, "b-eph", harness="claude", host=HOST)
        self.db.execute("UPDATE agent_registry SET canon = %s, ephemeral = true, "
                        "ephemeral_expires_at = now() + interval '1 hour' "
                        "WHERE name = 'b-eph'", (ID_B,))
        b_defaut, a_second = canon.load(self.b), canon.load(self.a, default=False)
        original = pg_canon.Canon.rebase_default

        def puis_panne(store, *args, **kwargs):
            original(store, *args, **kwargs)
            raise RuntimeError("panne simulée après le rebase")

        with mock.patch.object(pg_canon.Canon, "rebase_default", puis_panne):
            with self.assertRaises(RuntimeError):
                canon_sync.adopt_default(self.db, HOST, [b_defaut, a_second])
        # transaction annulée : rien n'a bougé, l'ancien canon_id reste
        self.assertEqual(self._defaut()["canon_id"], ID_A)
        self.assertEqual(self._canons(), {"a1": None, "a2": None, "b1": ID_B, "b2": ID_B})
        # rejeu : le rebase a lieu une fois, et le nouveau canon_id avec lui
        done = canon_sync.adopt_default(self.db, HOST, [b_defaut, a_second])
        self.assertEqual(done["agents"], ["a1", "a2"])
        self.assertIn("b-eph", done["adopted"])
        self.assertEqual(self._defaut()["canon_id"], ID_B)
        self.assertIsNone(self.row("b-eph")["canon"])
        self.assertEqual(self._canons(), {"a1": ID_A, "a2": ID_A, "b1": None, "b2": None})
        # B synchronisé (ou non) : un nouvel appel — `canon sync` manuelle
        # concurrente — ne réétiquette JAMAIS B en A
        self.assertIsNone(canon_sync.adopt_default(self.db, HOST, [b_defaut, a_second]))
        canon_sync.sync(self.db, b_defaut, HOST)
        self.assertIsNone(canon_sync.adopt_default(self.db, HOST, [b_defaut, a_second]))
        self.assertEqual(self._canons(), {"a1": ID_A, "a2": ID_A, "b1": None, "b2": None})
        self.assertEqual(self._defaut()["canon_id"], ID_B)

    def test_ordre_change_sans_canon_id_refuse(self):
        self.sync_a()
        self.sync_b()
        self.db.execute("UPDATE canon_state SET canon_id = NULL WHERE canon = ''")
        b_defaut, a_second = canon.load(self.b), canon.load(self.a, default=False)
        with self.assertRaises(canon_sync.DefaultCanonError) as ctx:
            canon_sync.adopt_default(self.db, HOST, [b_defaut, a_second])
        self.assertIn("pas d'identifiant enregistré", str(ctx.exception))
        # rien n'est donné à B ; le canon par défaut est fermé (non réclamable)
        self.assertEqual(self._canons(), {"a1": None, "a2": None, "b1": ID_B, "b2": ID_B})
        self.assertEqual(self._defaut()["status"], canon_sync.CANON_INVALID)
        self.assertNotIn("a1", self.claimable())
        # même ordre qu'avant (même racine) : l'identifiant est inscrit, rien n'est refusé
        a_defaut, b_second = canon.load(self.a), canon.load(self.b, default=False)
        self.assertIsNone(canon_sync.adopt_default(self.db, HOST, [a_defaut, b_second]))
        self.assertEqual(self._defaut()["canon_id"], ID_A)


class CanonDActionImmuableTest(PgTestCase):
    """Point 4 (0036) : `actions.canon` ne change que par le rebase."""

    def _action(self):
        from ameesh import actions
        action_id = actions.new_action_id()
        self.db.query(
            "INSERT INTO actions (action_id, project, proposed_by, connector, operation, "
            "target, args, class, digest, dedupe, requires_receipt) VALUES (%s, 'demo', "
            "'agent:ouvrier', 'shell-noop', 'send', 'client:42', '{}', 'irreversible', %s, "
            "'none', true) RETURNING action_id", (action_id, "sha256:" + "0" * 64))
        return action_id

    def _canon(self, action_id):
        return self.db.query("SELECT canon FROM actions WHERE action_id = %s",
                             (action_id,))[0]["canon"]

    def test_update_refuse_hors_rebase_accepte_pendant(self):
        from ameesh.db import DbError
        action_id = self._action()
        self.assertEqual(self._canon(action_id), "")
        with self.assertRaises(DbError) as ctx:
            self.db.query("UPDATE actions SET canon = 'autre' WHERE action_id = %s "
                          "RETURNING action_id", (action_id,))
        self.assertIn("canon d'action immuable", str(ctx.exception))
        # un réglage de session hors transaction ne suffit pas (SET LOCAL)
        with self.assertRaises(DbError):
            with self.db.transaction() as tx:
                tx.query("UPDATE actions SET canon = 'autre' WHERE action_id = %s "
                         "RETURNING action_id", (action_id,))
        self.assertEqual(self._canon(action_id), "")
        # pendant le rebase du canon par défaut : accepté
        with self.db.transaction() as tx:
            from ameesh import storage
            done = storage.of(tx).canon.rebase_default("h", "ancien", "nouveau")
        self.assertEqual(done["actions"], 1)
        self.assertEqual(self._canon(action_id), "ancien")
        # et le réglage ne survit pas à la transaction
        with self.assertRaises(DbError):
            self.db.query("UPDATE actions SET canon = '' WHERE action_id = %s "
                          "RETURNING action_id", (action_id,))
        # les autres colonnes restent modifiables comme avant
        self.db.query("UPDATE actions SET state = 'cancelled' WHERE action_id = %s "
                      "RETURNING action_id", (action_id,))


class AttributionTest(PgTestCase):
    """Point 5 : `work add --assignee` / `work assign` moins stricts, refus utiles."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "ouvrier", harness="claude", host=self.cfg.host,
                        cwd=self.tmp)

    def test_nom_nu_d_un_humain_normalise(self):
        import dataclasses
        from ameesh import work
        cfg = dataclasses.replace(self.cfg, humans="alice")
        lot = work.add(self.db, title="pour alice", assignee="alice", cfg=cfg)
        self.assertEqual(lot["assignee"], "human:alice")
        check = work.check_assignee(self.db, "alice", cfg=cfg)
        self.assertIn("humain connu", check["warning"])
        # humain déjà résolu par le canon (responsable d'un agent)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:bob' "
                        "WHERE name = 'ouvrier'")
        self.assertEqual(work.check_assignee(self.db, "bob", cfg=self.cfg)["assignee"],
                         "human:bob")
        proc = self.mesh("work", "assign", str(lot["id"]), "bob")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("humain connu", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "human:bob")
        # mais jamais un délégué
        with self.assertRaises(work.WorkError):
            work.delegate(self.db, lot["id"], "bob", within="30m", cfg=self.cfg)

    def test_nom_inconnu_refuse_avec_la_marche_a_suivre(self):
        proc = self.mesh("work", "add", "--title", "x", "--assignee", "fantome")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("agent inconnu", proc.stderr)
        self.assertIn("agent-runner register", proc.stderr)
        self.assertIn("--externe", proc.stderr)

    def test_canon_momentanement_invalide_ne_bloque_pas(self):
        from ameesh import work
        self.db.execute(
            "UPDATE agent_registry SET canon_ref = 'agents/ouvrier.md', "
            "responsible = 'human:proprio', placement_ok = true, "
            "placement_profile = ameesh_placement_profile(host, harness, provider, model, "
            "credential_mode) WHERE name = 'ouvrier'")
        self.db.execute("DELETE FROM canon_state")
        self.db.execute("INSERT INTO canon_state (host, canon, status, root, source, diagnostic, "
                        "checked_at) VALUES (%s, '', 'invalid', '', '', 'en panne', now())",
                        (self.cfg.host,))
        self.assertEqual(registry.wakeable(self.db, self.cfg, "ouvrier"), (True, ""))
        check = work.check_assignee(self.db, "ouvrier", cfg=self.cfg)
        self.assertEqual(check["assignee"], "ouvrier")
        self.assertIn("momentanément invalid", check["warning"])
        # la réclamation, elle, reste fermée
        self.assertIsNone(registry.claim(self.db, "ouvrier", "r1", 60))
        # un placement refusé, lui, bloque toujours
        self.db.execute("UPDATE agent_registry SET placement_ok = false, "
                        "placement_diagnostic = 'hôte non admis' WHERE name = 'ouvrier'")
        with self.assertRaises(work.WorkError) as ctx:
            work.check_assignee(self.db, "ouvrier", cfg=self.cfg)
        self.assertIn("hôte non admis", str(ctx.exception))


class WebhookSecretTest(unittest.TestCase):
    """Point 6 : une URL de webhook piégée ne fuit jamais (journal, JSON, état)."""

    SECRET = "T0SECRET/B0SECRET/XXXXSECRETXXXX"

    def setUp(self):
        from ameesh import notify
        self.notify = notify
        self.msg = notify.Message("titre", "corps")
        self.canal = notify.parse_config({"default": ["slack"]}).channels["slack"]

    def _assert_propre(self, text):
        for morceau in ("SECRET", "T0SECRET", "XXXX"):
            self.assertNotIn(morceau, text)

    def test_commentaire_en_fin_de_ligne_refuse_sans_citer_l_url(self):
        notify = self.notify
        for piege in ("https://hooks.slack.com/services/%s # webhook prod" % self.SECRET,
                      "https://hooks.slack.com/services/%s\x07" % self.SECRET,
                      "https://hooks.slack.com/serv ices/%s" % self.SECRET):
            with self.assertRaises(notify.ChannelError) as ctx:
                notify.Sender(env={"AMEESH_SLACK_WEBHOOK": piege}).send(self.canal, self.msg)
            self.assertIn("URL du webhook invalide", str(ctx.exception))
            self.assertTrue(ctx.exception.permanent)
            self._assert_propre(str(ctx.exception))

    def test_post_url_secrete_invalide_message_fixe(self):
        notify = self.notify
        # même si la lecture laissait passer : http.client cite le chemin
        with self.assertRaises(notify.ChannelError) as ctx:
            notify._post("https://127.0.0.1:9/services/%s x" % self.SECRET, b"{}", {}, 1.0,
                         show_url=False)
        self.assertEqual(str(ctx.exception), notify.SECRET_URL_INVALID)

    def test_jeton_ntfy_piege(self):
        notify = self.notify
        canal = notify.Channel("ntfy", "ntfy", {"url": "https://127.0.0.1:9", "topic": "t",
                                                "token_env": "TOK"})
        with self.assertRaises(notify.ChannelError) as ctx:
            notify.Sender(env={"TOK": "tk_SECRET\nX-Injecte: 1"}).send(canal, self.msg)
        self._assert_propre(str(ctx.exception))
        self.assertIn("jeton ntfy invalide", str(ctx.exception))

    def test_erreur_inattendue_nettoyee(self):
        notify = self.notify
        url = "https://hooks.slack.com/services/%s" % self.SECRET

        class Panne:
            env = {"AMEESH_SLACK_WEBHOOK": url}

            def send(self, channel, message):
                raise RuntimeError("explosion sur %s" % url)

        ncfg = notify.parse_config({"routes": {"human:p": ["slack"]}})
        [(nom, erreur)] = notify.send_test(ncfg, "human:p", "hote", sender=Panne())
        self.assertEqual(nom, "slack")
        self.assertIn("erreur inattendue", erreur)
        self._assert_propre(erreur)


class AdoptRevoqueLesLiaisonsTest(_BaseL39):
    """Point 8 (L39 × L41) : `ameesh adopt` révoque les liaisons de session."""

    def test_adoption_revoque_les_liaisons_actives(self):
        from ameesh import reprise, session_bindings
        self.db.execute("TRUNCATE session_bindings RESTART IDENTITY")
        comptes = self._codex()
        _rollout(comptes["codex"][1]["path"], SID, self.travail)
        self._externe("coord")
        session_bindings.bind(self.cfg, self.db, "coord", session_id="s-ext",
                              harness="claude", by="human:proprio")
        self.assertEqual(len(session_bindings.listing(self.db)), 1)
        out = reprise.adopt(self._cfg(comptes), self.db, "coord", session_id=SID,
                            harness="codex", actor="human:proprio")
        self.assertEqual(session_bindings.listing(self.db), [])
        self.assertTrue(any("liaison(s) de session révoquée(s) : claude:s-ext" in n
                            for n in out["notes"]))
        self.assertIn("révoquée(s) par human:proprio (adoption)", self._fil())


class OrphanLotTest(PgTestCase):
    """Point 8 (L37) : `orphan_lot` — ni `sans_tour` pour un externe, ni
    `inconnu` pour un nom nu d'humain connu."""

    def _vieillir(self):
        for sql in ("UPDATE work_items SET created_at = now() - interval '2 hours',"
                    " updated_at = now() - interval '2 hours'",
                    "UPDATE work_item_events SET created_at = now() - interval '2 hours'",
                    "UPDATE work_item_milestones SET at = now() - interval '2 hours'"):
            self.db.execute(sql)

    def _orphelins(self, cfg=None):
        from ameesh import exploitation
        return [a for a in exploitation.alerts(cfg or self.cfg, self.db)
                if a["type"] == "orphan_lot"]

    def test_externe_jamais_sans_tour(self):
        from ameesh import work
        registry.upsert(self.db, "coord", harness="codex", mode="externe")
        self.db.execute("UPDATE agent_registry SET responsible = 'human:proprio' "
                        "WHERE name = 'coord'")
        work.add(self.db, title="pour la session", assignee="coord", externe=True)
        self._vieillir()
        self.assertEqual(self._orphelins(), [])

    def test_nom_nu_d_un_humain_n_est_pas_inconnu(self):
        import dataclasses
        from ameesh import work
        lot = work.add(self.db, title="nu", assignee="human:alice")
        self.db.execute("UPDATE work_items SET assignee = 'alice' WHERE id = %s",
                        (lot["id"],))
        self.assertEqual([a["reason"] for a in self._orphelins()], ["inconnu"])
        cfg = dataclasses.replace(self.cfg, humans="alice")
        self.assertEqual(self._orphelins(cfg), [])


class DelegationAnnuleeTest(PgTestCase):
    """Point 8 (L40) : une délégation annulée ne laisse ni délégant ni date."""

    def test_reassignation_hors_work_assign_puis_echeance(self):
        from ameesh import work
        registry.upsert(self.db, "orch", harness="claude", host=self.cfg.host)
        registry.upsert(self.db, "relais", harness="claude", host=self.cfg.host)
        lot = work.add(self.db, title="à confier", assignee="orch")
        work.delegate(self.db, lot["id"], "relais", within="30m")
        # réassigné à la main (SQL, outil tiers) : l'échéance annule
        self.db.execute("UPDATE work_items SET assignee = 'orch' WHERE id = %s", (lot["id"],))
        self.db.execute("UPDATE work_item_delegations SET due_at = now() - interval '1 min'")
        self.db.execute("UPDATE work_items SET due_at = now() - interval '1 min'")
        [done] = work.expire_delegations(self.db)
        self.assertEqual(done["outcome"], "annulee")
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["delegated_by"], item.get("delegated_ts"), item.get("due_ts")),
                         (None, None, None))
        self.assertIsNone(work.delegation_view(item))


class PidRecycleTest(PgTestCase):
    """Point 8 (L41, 0036) : l'heure de démarrage du PID lié est contrôlée."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE session_bindings RESTART IDENTITY")

    def test_heure_de_demarrage_enregistree_et_verifiee(self):
        import os
        from ameesh import identity, session_bindings as sb
        moi = os.getpid()
        debut = sb.pid_start(moi)
        self.assertIsInstance(debut, int)
        self.assertIsNone(sb.pid_start(2 ** 22 + 12345))
        sb.bind(self.cfg, self.db, "alpha", session_id="s-1", harness="claude", pid=moi,
                by="human:proprio")
        [row] = sb.listing(self.db)
        self.assertEqual((row["pid"], row["pid_start"]), (moi, debut))
        chaine = sb.ancestors()
        self.assertEqual(sb.for_hook(self.cfg, self.db, "claude", "s-1", chaine)[1], "")
        self.assertEqual(sb.by_ancestry(self.cfg, self.db, chaine)["agent"], "alpha")
        # même PID, autre processus (PID recyclé) : rien
        self.db.execute("UPDATE session_bindings SET pid_start = pid_start + 1")
        row, why = sb.for_hook(self.cfg, self.db, "claude", "s-1", chaine)
        self.assertIn("réutilisé", why)
        self.assertIsNone(sb.by_ancestry(self.cfg, self.db, chaine))
        binding = identity._session_binding(self.cfg, self.db, "claude", "s-1")
        self.assertFalse(binding.ok)
        # re-lier avec --pid remet l'heure de démarrage à jour
        out = sb.bind(self.cfg, self.db, "alpha", session_id="s-1", harness="claude",
                      pid=moi, by="human:proprio")
        self.assertEqual(out.status, "updated")
        self.assertEqual(sb.for_hook(self.cfg, self.db, "claude", "s-1", chaine)[1], "")
        # liaison antérieure (sans heure de démarrage) : seul le PID est contrôlé
        self.db.execute("UPDATE session_bindings SET pid_start = NULL")
        self.assertEqual(sb.for_hook(self.cfg, self.db, "claude", "s-1", chaine)[1], "")


if __name__ == "__main__":
    unittest.main()
