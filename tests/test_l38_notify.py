# SPDX-License-Identifier: AGPL-3.0-only
"""L38 (décision 0030, point 4) : `ameesh notify`, les alertes POUSSÉES à
l'humain responsable.

Aucun envoi réel : ntfy et Slack sont de faux serveurs HTTP sur la boucle
locale, `notify-send` est le faux binaire de tests/fakebin (jamais le vrai :
`AMEESH_NOTIFY_SEND_BIN` le désigne explicitement, sans repli sur le PATH).
"""
from __future__ import annotations

import dataclasses
import http.server
import json
import os
import tempfile
import threading
import unittest

from ameesh import fil, mail, notify, registry, storage, work

from .support import FAKEBIN, PgTestCase
from .test_canon import _TmpMixin, write

FAKE_NOTIFY_SEND = os.path.join(FAKEBIN, "notify-send")


class FakeHttp:
    """Faux ntfy / faux webhook Slack : retient chaque POST, rend `status`."""

    def __init__(self, status: int = 200):
        self.status = status
        self.requests: list = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - API de http.server
                size = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(size).decode("utf-8")
                owner.requests.append({"path": self.path, "headers": dict(self.headers),
                                       "body": body})
                self.send_response(owner.status)
                self.end_headers()
                self.wfile.write(b"ok" if owner.status < 400 else b"panne simulee")

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _Http:
    def http(self, status=200) -> FakeHttp:
        server = FakeHttp(status)
        self.addCleanup(server.close)
        return server


def _sender_env(log_path: str, **extra) -> dict:
    env = {"AMEESH_NOTIFY_SEND_BIN": FAKE_NOTIFY_SEND, "AMEESH_FAKE_NOTIFY_LOG": log_path,
           "PATH": os.environ.get("PATH", "")}
    env.update(extra)
    return env


def _desktop_log(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _alert(kind="stopped_with_mail", agent="ouvrier", since=1000.0, **extra) -> dict:
    out = {"schema": "ameesh-alert/1", "type": kind, "agent": agent, "since": since,
           "value": 2, "threshold": 300, "detail": "arrêté (bail_expire) avec 2 message(s) "
                                                   "non lu(s) depuis 600s"}
    out.update(extra)
    return out


# ==========================================================================
# configuration, texte, canaux (sans base)
# ==========================================================================

class ConfigTest(unittest.TestCase):
    def test_vide_et_exemple(self):
        ncfg = notify.parse_config({})
        self.assertEqual(ncfg.types, notify.DEFAULT_TYPES)
        self.assertIn("delegation_expired", ncfg.types)
        self.assertFalse(ncfg.configured)
        ncfg = notify.parse_config({
            "default_human": "human:smichea",
            "routes": {"human:smichea": ["desktop", "ntfy", "slack"]},
            "default": ["desktop"],
            "channels": {"ntfy": {"url": "https://ntfy.example.org", "topic": "ameesh-sm",
                                  "token_env": "AMEESH_NTFY_TOKEN"},
                         "slack": {"webhook_env": "AMEESH_SLACK_WEBHOOK"}},
            "types": ["stopped_with_mail", "orphan_lot"], "rate_per_minute": 5})
        self.assertEqual([c.kind for c in ncfg.channels_for("human:smichea")],
                         ["desktop", "ntfy", "slack"])
        self.assertEqual([c.name for c in ncfg.channels_for("human:autre")], ["desktop"])
        self.assertEqual(ncfg.types, ("stopped_with_mail", "orphan_lot"))
        self.assertEqual(ncfg.rate_per_minute, 5)
        # un canal nommé librement, typé
        ncfg = notify.parse_config({
            "routes": {"human:bruno": ["ntfy-bruno"]},
            "channels": {"ntfy-bruno": {"type": "ntfy", "url": "http://127.0.0.1:9",
                                        "topic": "b"}}})
        self.assertEqual(ncfg.channels_for("human:bruno")[0].kind, "ntfy")

    def test_secrets_et_erreurs_refuses(self):
        cas = [
            {"channels": {"ntfy": {"url": "https://n", "topic": "t", "token": "tk_x"}}},
            {"channels": {"slack": {"url": "https://hooks.slack.com/services/X"}}},
            {"channels": {"slack": {"webhook": "https://hooks.slack.com/services/X"}}},
            {"channels": {"ntfy": {"url": "http://ntfy.example.org", "topic": "t"}}},
            {"channels": {"ntfy": {"topic": "t"}}},
            {"routes": {"human:x": ["ntfy"]}},
            {"routes": {"human:x": ["inconnu"]}},
            {"routes": {"smichea": ["desktop"]}},
            {"channels": {"desktop": {"commande": "rm"}}},
            {"inconnue": 1},
            {"default_human": "agent:robot"},
            {"types": ["Pas Un Type"]},
        ]
        for raw in cas:
            with self.subTest(raw=raw), self.assertRaises(notify.NotifyConfigError):
                notify.parse_config(raw)
        with self.assertRaises(notify.NotifyConfigError) as ctx:
            notify.parse_config({"channels": {"slack": {"url": "https://hooks/X"}}})
        self.assertNotIn("hooks/X", str(ctx.exception))

    def test_humain_normalise(self):
        self.assertEqual(notify.normalize_human("human:proprio"), "human:proprio")
        self.assertEqual(notify.normalize_human("proprio"), "human:proprio")
        self.assertIsNone(notify.normalize_human("agent:robot"))
        self.assertIsNone(notify.normalize_human(""))

    def test_config_de_l_hote(self):
        from ameesh import config as config_mod
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"notify": {"default": ["desktop"]}}, fh)
            cfg = config_mod.load(env={"AMEESH_CONFIG": path})
        self.assertEqual(cfg.notify, {"default": ["desktop"]})


class TexteTest(unittest.TestCase):
    def test_levee_et_resolution_lisibles(self):
        alerte = _alert("orphan_lot", agent="coord", since=None, lot=12, title="refonte",
                        detail="lot #12 (build) orphelin : assigné à coord, arrêté (manuel)")
        msg = notify.render(alerte, "raised", 5000.0, raised_ts=4000.0)
        self.assertIn("lot orphelin", msg.title)
        self.assertIn("[orphan_lot] agent coord · lot #12 « refonte »", msg.body)
        self.assertIn("orphelin : assigné à coord", msg.body)
        self.assertIn("constatée le", msg.body)
        self.assertTrue(msg.urgent)
        self.assertIsNone(fil.unreadable_reason(msg.text))
        msg = notify.render(_alert(since=1000.0), "raised", 1000.0 + 25 * 60)
        self.assertIn("(25 min)", msg.body)
        msg = notify.render(_alert(since=1000.0), "resolved", 1000.0 + 3 * 3600)
        self.assertIn("résolue", msg.title)
        self.assertIn("résolue après 3 h 00", msg.body)
        self.assertTrue(msg.low)

    def test_detail_illisible_retire(self):
        blob = "QUJD" * 80
        msg = notify.render(_alert(detail="session " + blob), "raised", 2000.0)
        self.assertNotIn(blob[:250], msg.body)
        self.assertIn("détail retiré", msg.body)
        self.assertIsNone(fil.unreadable_reason(msg.text))
        # caractères de contrôle retirés (couleurs de terminal)
        msg = notify.render(_alert(detail="\x1b[31mrouge\x1b[0m"), "raised", 2000.0)
        self.assertNotIn("\x1b", msg.text)


class CanauxTest(_Http, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l38-")
        self.log = os.path.join(self.tmp, "desktop.log")
        self.msg = notify.Message("ameesh : agent arrêté avec du courrier (ouvrier)",
                                  "[stopped_with_mail] agent ouvrier\ndétail", urgent=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_desktop_faux_notify_send(self):
        sender = notify.Sender(env=_sender_env(self.log))
        sender.send(notify.Channel("desktop", "desktop"), self.msg)
        argv = _desktop_log(self.log)
        self.assertEqual(len(argv), 1)
        self.assertIn("--urgency=critical", argv[0])
        self.assertEqual(argv[0][-2:], [self.msg.title, self.msg.body])
        # échec du binaire : constat lisible, retentable
        sender = notify.Sender(env=_sender_env(self.log, AMEESH_FAKE_NOTIFY_EXIT="1"))
        with self.assertRaises(notify.ChannelError) as ctx:
            sender.send(notify.Channel("desktop", "desktop"), self.msg)
        self.assertIn("pas de bus de session", str(ctx.exception))
        self.assertFalse(ctx.exception.permanent)

    def test_desktop_absent(self):
        env = _sender_env(self.log, AMEESH_NOTIFY_SEND_BIN=os.path.join(self.tmp, "absent"))
        with self.assertRaises(notify.ChannelError) as ctx:
            notify.Sender(env=env).send(notify.Channel("desktop", "desktop"), self.msg)
        self.assertIn("notify-send introuvable", str(ctx.exception))
        self.assertTrue(ctx.exception.permanent)
        self.assertEqual(_desktop_log(self.log), [])

    def test_ntfy_jeton_par_env_et_fichier(self):
        server = self.http()
        canal = notify.parse_config({"channels": {"ntfy": {
            "url": server.url, "topic": "ameesh-test", "token_env": "TEST_NTFY_TOKEN"}},
            "default": ["ntfy"]}).channels["ntfy"]
        notify.Sender(env={"TEST_NTFY_TOKEN": "tk_secret"}).send(canal, self.msg)
        req = server.requests[0]
        self.assertEqual(req["path"], "/ameesh-test")
        self.assertEqual(req["headers"]["Authorization"], "Bearer tk_secret")
        self.assertEqual(req["headers"]["Priority"], "high")
        self.assertTrue(req["headers"]["Title"].startswith("=?UTF-8?B?"))
        self.assertEqual(req["body"], self.msg.body)
        # jeton nommé mais absent : échec permanent, rien n'est posté
        with self.assertRaises(notify.ChannelError) as ctx:
            notify.Sender(env={}).send(canal, self.msg)
        self.assertTrue(ctx.exception.permanent)
        self.assertEqual(len(server.requests), 1)
        # fichier 0600 accepté, 0644 refusé
        jeton = os.path.join(self.tmp, "ntfy.token")
        with open(jeton, "w", encoding="utf-8") as fh:
            fh.write("tk_fichier\n")
        os.chmod(jeton, 0o644)
        canal = notify.Channel("ntfy", "ntfy", {"url": server.url, "topic": "t",
                                                "token_file": jeton})
        with self.assertRaises(notify.ChannelError) as ctx:
            notify.Sender(env={}).send(canal, self.msg)
        self.assertIn("chmod 600", str(ctx.exception))
        os.chmod(jeton, 0o600)
        notify.Sender(env={}).send(canal, self.msg)
        self.assertEqual(server.requests[-1]["headers"]["Authorization"], "Bearer tk_fichier")

    def test_slack_webhook_par_env(self):
        server = self.http()
        canal = notify.parse_config({"default": ["slack"]}).channels["slack"]
        url = server.url + "/services/T000/B000/XXXX"
        notify.Sender(env={"AMEESH_SLACK_WEBHOOK": url}).send(canal, self.msg)
        payload = json.loads(server.requests[0]["body"])
        self.assertTrue(payload["text"].startswith("*ameesh : agent arrêté"))
        with self.assertRaises(notify.ChannelError) as ctx:
            notify.Sender(env={}).send(canal, self.msg)
        self.assertIn("AMEESH_SLACK_WEBHOOK", str(ctx.exception))
        # l'URL du webhook (un secret) n'apparaît jamais dans une erreur
        server.status = 500
        with self.assertRaises(notify.ChannelError) as ctx:
            notify.Sender(env={"AMEESH_SLACK_WEBHOOK": url}).send(canal, self.msg)
        self.assertIn("HTTP 500", str(ctx.exception))
        self.assertNotIn("XXXX", str(ctx.exception))

    def test_send_test(self):
        ntfy = self.http()
        ncfg = notify.parse_config({
            "routes": {"human:proprio": ["desktop", "ntfy"]},
            "channels": {"ntfy": {"url": ntfy.url, "topic": "t"}}})
        results = notify.send_test(ncfg, "human:proprio", "atelier",
                                   sender=notify.Sender(env=_sender_env(self.log)))
        self.assertEqual(results, [("desktop", None), ("ntfy", None)])
        self.assertIn("message de test", _desktop_log(self.log)[0][-2])
        self.assertIn("human:proprio", ntfy.requests[0]["body"])


# ==========================================================================
# le service (base réelle)
# ==========================================================================

class _Base(_Http, PgTestCase):
    def setUp(self):
        super().setUp()
        self.desk = os.path.join(self.tmp, "desktop.log")
        self.logs: list = []
        self.now = 100000.0

    def ncfg(self, **raw):
        raw.setdefault("default", ["desktop"])
        return notify.parse_config(raw)

    def notifier(self, ncfg, state=None, cfg=None, **kw):
        kw.setdefault("sender", notify.Sender(env=_sender_env(self.desk)))
        return notify.Notifier(cfg or self.cfg, ncfg, state, log=self.logs.append,
                               clock=lambda: self.now, **kw)

    def passe(self, notifier, current=None, **kw):
        return notifier.run_pass(self.db, current=current, **kw)

    def _courrier_ancien(self, dest):
        mail.send(self.db, "orch", dest, "à lire")
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '10 minutes'")


class AcheminementTest(_Base):
    def test_chaque_niveau_de_repli(self):
        self.register("ouvrier", "claude", cwd=self.tmp)
        self.register("sans-resp", "claude", cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:agent-resp' "
                        "WHERE name = 'ouvrier'")
        router = notify.Router(self.cfg, self.db, self.ncfg(default_human="human:defaut"))
        # 1. le champ `responsible` de l'alerte prime
        self.assertEqual(router.resolve(_alert(responsible="human:alerte")),
                         ("human:alerte", "alerte"))
        # 2. le responsable de l'agent
        self.assertEqual(router.resolve(_alert()), ("human:agent-resp", "agent"))
        # 3. le responsable du paquet de travail du lot (en remontant au parent)
        packages = storage.of(self.db).packages
        packages.upsert({"id": "E1", "kind": "epic", "title": "epic",
                         "responsible": "human:lot-resp", "canon_ref": "plan/e1.md"})
        packages.upsert({"id": "L1", "kind": "lot", "title": "lot", "parent": "E1",
                         "canon_ref": "plan/l1.md"})
        lot = work.add(self.db, title="lot rattaché", assignee="sans-resp")
        self.db.execute("UPDATE work_items SET package_id = 'L1' WHERE id = %s",
                        (lot["id"],))
        router = notify.Router(self.cfg, self.db, self.ncfg(default_human="human:defaut"))
        self.assertEqual(router.resolve(_alert("orphan_lot", agent="sans-resp",
                                               lot=lot["id"])),
                         ("human:lot-resp", "lot"))
        # 5. l'humain par défaut de la configuration
        self.assertEqual(router.resolve(_alert(agent="sans-resp")),
                         ("human:defaut", "defaut"))
        # personne : (None, None)
        router = notify.Router(self.cfg, self.db, self.ncfg())
        self.assertEqual(router.resolve(_alert(agent="sans-resp")), (None, None))

    def test_responsable_de_l_hote(self):
        mixin = _TmpMixin()
        mixin.addCleanup = self.addCleanup
        mixin.skipTest = self.skipTest
        root = mixin.example_copy()
        write(root, "hotes/atelier.md",
              "---\ntype: Host\ntitle: atelier\nresponsible: human:hote-resp\npolicy:\n"
              "  harnesses: [claude]\n  max_agents: 2\n---\n\n# atelier\n")
        cfg = dataclasses.replace(self.cfg, canon=root, canon_untrusted=True)
        self.register("ouvrier", "claude", cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET host = 'atelier', responsible = NULL")
        router = notify.Router(cfg, self.db, self.ncfg(default_human="human:defaut"))
        self.assertEqual(router.resolve(_alert()), ("human:hote-resp", "hote"))
        self.assertEqual(router.resolve(_alert("host_pressure", agent=None, host="atelier")),
                         ("human:hote-resp", "hote"))
        self.assertEqual(router.resolve(_alert("host_pressure", agent=None, host="ailleurs")),
                         ("human:defaut", "defaut"))

    def test_sans_destinataire_journalise_une_fois(self):
        notifier = self.notifier(self.ncfg())
        records = self.passe(notifier, [_alert(agent="fantome")])
        self.assertEqual([r["delivery"] for r in records], ["sans_destinataire"])
        self.assertEqual(len([l for l in self.logs if "SANS DESTINATAIRE" in l]), 1)
        self.assertEqual(self.passe(notifier, [_alert(agent="fantome")]), [])
        self.assertEqual(len([l for l in self.logs if "SANS DESTINATAIRE" in l]), 1)
        self.assertEqual(_desktop_log(self.desk), [])
        # un responsable apparaît : l'alerte en cours part enfin
        notifier.ncfg = self.ncfg(default_human="human:proprio")
        records = self.passe(notifier, [_alert(agent="fantome")])
        self.assertEqual([(r["delivery"], r["human"]) for r in records],
                         [("envoyee", "human:proprio")])
        self.assertEqual(len(_desktop_log(self.desk)), 1)

    def test_route_par_humain(self):
        ntfy = self.http()
        ncfg = self.ncfg(routes={"human:proprio": ["ntfy"]},
                         channels={"ntfy": {"url": ntfy.url, "topic": "t"}})
        notifier = self.notifier(ncfg)
        self.passe(notifier, [_alert(responsible="human:proprio"),
                              _alert(agent="autre", responsible="human:autre")])
        self.assertEqual(len(ntfy.requests), 1)
        self.assertIn("agent ouvrier", ntfy.requests[0]["body"])
        self.assertEqual(len(_desktop_log(self.desk)), 1)   # autre → notify.default
        self.assertIn("agent autre", _desktop_log(self.desk)[0][-1])


class AlertesReellesTest(_Base):
    def test_stopped_with_mail_envoye_au_responsable_puis_resolu(self):
        self.register("ouvrier", "claude", cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:proprio'")
        self._courrier_ancien("ouvrier")
        registry.set_status(self.db, "ouvrier", "dead", status_text="bail expiré",
                            stop_reason="bail_expire")
        notifier = self.notifier(self.ncfg())
        import time as time_mod
        self.now = time_mod.time()
        records = self.passe(notifier)
        envoyes = [r for r in records if r["event"] == "raised"]
        self.assertEqual([(r["type"], r["agent"], r["human"], r["source"], r["delivery"])
                          for r in envoyes],
                         [("stopped_with_mail", "ouvrier", "human:proprio", "alerte",
                           "envoyee")])
        argv = _desktop_log(self.desk)
        self.assertEqual(len(argv), 1)
        self.assertIn("agent arrêté avec du courrier", argv[0][-2])
        self.assertIn("bail_expire", argv[0][-1])
        self.assertEqual(self.passe(notifier), [])     # une alerte qui dure : rien
        self.db.execute("UPDATE agent_mailbox SET delivered_at = now()")
        records = self.passe(notifier)
        self.assertEqual([(r["event"], r["delivery"]) for r in records],
                         [("resolved", "envoyee")])
        self.assertIn("résolue", _desktop_log(self.desk)[-1][-2])

    def test_types_filtres(self):
        alertes = [_alert("long_turn", responsible="human:p"),
                   _alert("stale_lot", agent="a", lot=3, responsible="human:p"),
                   _alert("delegation_expired", agent="b", lot=4, responsible="human:p"),
                   _alert("idle_with_mail", agent="c", responsible="human:p")]
        records = self.passe(self.notifier(self.ncfg()), alertes)
        self.assertEqual(sorted(r["type"] for r in records),
                         ["delegation_expired", "idle_with_mail"])
        records = self.passe(self.notifier(self.ncfg(types=["long_turn"])), alertes)
        self.assertEqual([r["type"] for r in records], ["long_turn"])


class FiabiliteTest(_Base):
    def test_dedoublonnage_et_redemarrage(self):
        path = os.path.join(self.tmp, "notify", "state.json")
        ncfg = self.ncfg(default_human="human:proprio")
        a1, a2 = _alert(), _alert("orphan_lot", agent="coord", since=None, lot=7)
        notifier = self.notifier(ncfg)
        self.assertEqual(len(self.passe(notifier, [a1, a2])), 2)
        self.assertEqual(self.passe(notifier, [a1, a2]), [])
        notify.save_state(path, notifier.state)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        # redémarrage : une alerte qui dure ne repart pas
        notifier = self.notifier(ncfg, notify.load_state(path))
        self.assertEqual(self.passe(notifier, [a1, a2]), [])
        self.assertEqual(len(_desktop_log(self.desk)), 2)
        # résolue pendant l'arrêt : la résolution part à la reprise
        notify.save_state(path, notifier.state)
        notifier = self.notifier(ncfg, notify.load_state(path))
        self.now += 120
        records = self.passe(notifier, [a2])
        self.assertEqual([(r["event"], r["type"]) for r in records],
                         [("resolved", "stopped_with_mail")])
        self.assertIn("résolue après", records[0]["text"])
        # une nouvelle occurrence (autre `since`) est une nouvelle alerte
        records = self.passe(notifier, [a2, _alert(since=5000.0)])
        self.assertEqual([(r["event"], r["type"]) for r in records],
                         [("raised", "stopped_with_mail")])

    def test_etat_illisible_mis_de_cote(self):
        path = os.path.join(self.tmp, "notify", "state.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{pas du json")
        # `--dry-run` : constat, mais rien n'est déplacé
        state = notify.load_state(path, log=self.logs.append, move_aside=False)
        self.assertEqual(state["active"], {})
        self.assertTrue(os.path.exists(path))
        state = notify.load_state(path, log=self.logs.append)
        self.assertEqual(state["active"], {})
        self.assertTrue(any("illisible" in l for l in self.logs))
        self.assertFalse(os.path.exists(path))

    def test_limite_de_debit_et_resume(self):
        ncfg = self.ncfg(default_human="human:proprio", rate_per_minute=3)
        notifier = self.notifier(ncfg)
        alertes = [_alert(agent="a%d" % i) for i in range(5)]
        records = self.passe(notifier, alertes)
        self.assertEqual([r["delivery"] for r in records if r["event"] == "raised"],
                         ["envoyee"] * 3 + ["resumee"] * 2)
        resumes = [r for r in records if r["event"] == "summary"]
        self.assertEqual(len(resumes), 1)
        self.assertIn("2 alerte(s) non détaillée(s)", resumes[0]["title"])
        self.assertIn("2 stopped_with_mail", resumes[0]["text"])
        self.assertEqual(len(_desktop_log(self.desk)), 4)
        # un autre humain n'est pas freiné
        records = self.passe(notifier, alertes + [_alert(agent="x", responsible="human:b")])
        self.assertEqual([(r["human"], r["delivery"]) for r in records],
                         [("human:b", "envoyee")])
        # fenêtre pleine, résumé récent : la suivante attend le prochain résumé
        self.now += 10
        autre = _alert(agent="x", responsible="human:b")
        records = self.passe(notifier, alertes + [autre, _alert(agent="a9")])
        self.assertEqual([(r["event"], r["delivery"]) for r in records],
                         [("raised", "resumee")])
        # deux alertes résumées se résolvent : elles comptent dans le résumé
        self.now += 61
        restantes = alertes[:3] + [_alert(agent="a9")]
        records = self.passe(notifier, restantes + [autre])
        self.assertEqual([r["event"] for r in records], ["summary"])
        self.assertIn("1 stopped_with_mail", records[0]["text"])
        self.assertIn("résolues : 2", records[0]["text"])
        self.assertEqual(notifier.state["summary"], {})

    def test_canal_en_echec_isole_et_retente(self):
        ntfy, slack = self.http(status=500), self.http()
        ncfg = self.ncfg(default_human="human:proprio", max_attempts=3,
                         routes={"human:proprio": ["desktop", "ntfy", "slack"]},
                         channels={"ntfy": {"url": ntfy.url, "topic": "t"},
                                   "slack": {"webhook_env": "TEST_SLACK"}})
        sender = notify.Sender(env=_sender_env(self.desk, TEST_SLACK=slack.url + "/hook"))
        notifier = self.notifier(ncfg, sender=sender)
        records = self.passe(notifier, [_alert()])
        self.assertEqual(records[0]["delivery"], "envoyee")
        self.assertEqual(records[0]["channels"]["desktop"], "ok")
        self.assertEqual(records[0]["channels"]["slack"], "ok")
        self.assertIn("HTTP 500", records[0]["channels"]["ntfy"])
        self.assertTrue(any("canal ntfy" in l and "retenté" in l for l in self.logs))
        # passage suivant : seul ntfy est retenté
        records = self.passe(notifier, [_alert()])
        self.assertEqual(list(records[0]["channels"]), ["ntfy"])
        self.assertEqual((len(slack.requests), len(_desktop_log(self.desk))), (1, 1))
        # rétabli : il passe, puis plus rien
        ntfy.status = 200
        records = self.passe(notifier, [_alert()])
        self.assertEqual(records[0]["channels"], {"ntfy": "ok"})
        self.assertEqual(self.passe(notifier, [_alert()]), [])
        self.assertEqual(len(ntfy.requests), 3)

    def test_retentatives_bornees(self):
        ntfy = self.http(status=503)
        ncfg = self.ncfg(default_human="human:proprio", max_attempts=2, default=["ntfy"],
                         channels={"ntfy": {"url": ntfy.url, "topic": "t"}})
        notifier = self.notifier(ncfg)
        for _ in range(4):
            self.passe(notifier, [_alert()])
        self.assertEqual(len(ntfy.requests), 2)
        self.assertTrue(any("ABANDON" in l for l in self.logs))
        # résolue sans aucun envoi réussi : pas de résolution envoyée
        records = self.passe(notifier, [])
        self.assertEqual([r["delivery"] for r in records], ["abandonnee"])
        self.assertEqual(len(ntfy.requests), 2)

    def test_canal_absent_permanent(self):
        sender = notify.Sender(env=_sender_env(
            self.desk, AMEESH_NOTIFY_SEND_BIN=os.path.join(self.tmp, "absent")))
        notifier = self.notifier(self.ncfg(default_human="human:proprio"), sender=sender)
        records = self.passe(notifier, [_alert()])
        self.assertIn("notify-send introuvable", records[0]["channels"]["desktop"])
        self.assertTrue(any("ABANDON" in l for l in self.logs))
        self.assertEqual(self.passe(notifier, [_alert()]), [])

    def test_dry_run_n_envoie_rien(self):
        notifier = self.notifier(self.ncfg(default_human="human:proprio"), dry_run=True,
                                 sender=notify.DrySender())
        records = self.passe(notifier, [_alert()])
        self.assertEqual(records[0]["delivery"], "a_blanc")
        self.assertIn("agent arrêté avec du courrier", records[0]["title"])
        self.assertEqual(_desktop_log(self.desk), [])


class CliTest(_Base):
    def config(self, notify_raw: dict) -> str:
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"notify": notify_raw}, fh)
        return path

    def env_cli(self, notify_raw: dict, **extra):
        return self.env(AMEESH_CONFIG=self.config(notify_raw),
                        AMEESH_NOTIFY_SEND_BIN=FAKE_NOTIFY_SEND,
                        AMEESH_FAKE_NOTIFY_LOG=self.desk, **extra)

    def _arrete_avec_courrier(self):
        self.register("ouvrier", "claude", cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:proprio'")
        self._courrier_ancien("ouvrier")
        registry.set_status(self.db, "ouvrier", "dead", status_text="bail expiré",
                            stop_reason="bail_expire")

    def test_once_puis_pas_de_renvoi(self):
        self._arrete_avec_courrier()
        env = self.env_cli({"default": ["desktop"]})
        proc = self.mesh("notify", "--once", "--json", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lignes = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
        self.assertEqual([(l["schema"], l["type"], l["human"], l["delivery"]) for l in lignes],
                         [("ameesh-notify/1", "stopped_with_mail", "human:proprio",
                           "envoyee")])
        self.assertTrue(os.path.exists(os.path.join(self.state, "notify", "state.json")))
        proc = self.mesh("notify", "--once", "--json", env=env)
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, ""), proc.stderr)
        self.assertEqual(len(_desktop_log(self.desk)), 1)

    def test_dry_run(self):
        self._arrete_avec_courrier()
        proc = self.mesh("notify", "--once", "--dry-run",
                         env=self.env_cli({"default": ["desktop"]}))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("levée [stopped_with_mail] agent ouvrier → human:proprio : a blanc",
                      proc.stdout)
        self.assertIn("agent arrêté avec du courrier", proc.stdout)
        self.assertEqual(_desktop_log(self.desk), [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "notify", "state.json")))

    def test_test_human(self):
        ntfy, slack = self.http(), self.http()
        raw = {"routes": {"human:proprio": ["desktop", "ntfy", "slack"]},
               "channels": {"ntfy": {"url": ntfy.url, "topic": "ameesh-proprio"}}}
        proc = self.mesh("notify", "--test", "human:proprio",
                         env=self.env_cli(raw, AMEESH_SLACK_WEBHOOK=slack.url + "/h"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.count("envoyé"), 3)
        self.assertEqual(len(_desktop_log(self.desk)), 1)
        self.assertEqual(ntfy.requests[0]["path"], "/ameesh-proprio")
        self.assertIn("message de test", json.loads(slack.requests[0]["body"])["text"])
        # Slack sans webhook : échec lisible, les autres canaux passent
        env = self.env_cli(raw)
        env.pop("AMEESH_SLACK_WEBHOOK", None)
        proc = self.mesh("notify", "--test", "human:proprio", env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("slack → human:proprio : ÉCHEC", proc.stdout)
        self.assertEqual(len(ntfy.requests), 2)
        proc = self.mesh("notify", "--test", "proprio", env=env)
        self.assertEqual(proc.returncode, 2)

    def test_configuration_secrete_refusee(self):
        proc = self.mesh("notify", "--once", env=self.env_cli(
            {"channels": {"slack": {"url": "https://hooks.slack.com/services/X"}}}))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("secret", proc.stderr)
        self.assertNotIn("services/X", proc.stderr)

    def test_aide(self):
        proc = self.mesh("--help")
        self.assertIn("ameesh notify", proc.stdout)
        proc = self.mesh("notify", "--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--dry-run", proc.stdout)


if __name__ == "__main__":
    unittest.main()
