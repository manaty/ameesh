# SPDX-License-Identifier: AGPL-3.0-only
"""Les deux backends de la CLI : Postgres (nominal) et fichiers (repli v0).

Le repli fichier écrit exactement là où la v0 écrit
(`~/.local/state/agent-mail`), avec les mêmes noms de fichiers et le même JSON :
si Postgres est absent, les agents continuent de se parler, et la v0 peut
reprendre la main sans migration de données.

Dans les deux cas, le message est aussi écrit dans son fil lisible (R12) ; un
envoi à « all » donne une seule entrée par projet, pas une par destinataire.
"""
from __future__ import annotations

import json
import os
import socket
import time

from . import config as config_mod
from . import db as db_mod
from . import fil, identity, mail, registry
from .config import Config


def _mk(path: str) -> str:
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _send_payload(dest: str, urgent: bool, payload: dict | None) -> dict:
    """Le payload d'un envoi (L125) : `urgent`, et `broadcast` pour une
    diffusion à « all » qui ne l'est pas — lue au prochain tour de chacun,
    sans réveiller personne (`mail.passive_reason`). Le courrier d'un humain
    (`human`) réveille toujours, diffusion comprise."""
    out = dict(payload or {})
    if urgent:
        out["urgent"] = True
    if dest == "all" and not out.get("urgent") and not out.get("human"):
        out["broadcast"] = True
    return out


def _send_thread_meta(thread_meta: dict | None, payload: dict, cc) -> dict | None:
    """L125 : le fil d'un message dit ses copies et s'il est un accusé."""
    meta = dict(thread_meta or {})
    if cc:
        meta["cc"] = list(cc)
    if payload.get("ack"):
        meta["ack"] = True
    return meta or None


class FileBackend:
    """Backend v0 : un fichier JSON par message, un dossier par agent."""

    kind = "file"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = cfg.v0_state

    # -- chemins -----------------------------------------------------------
    def inbox_dir(self, name: str) -> str:
        return _mk(os.path.join(self.state, "inbox", name))

    def agents_dir(self) -> str:
        return _mk(os.path.join(self.state, "agents"))

    def status_path(self, name: str) -> str:
        return os.path.join(self.agents_dir(), name + ".status")

    # -- messages ----------------------------------------------------------
    def send(self, sender: str, dest: str, text: str, host: str | None = None,
             signed: dict | None = None, work_item_id: str | None = None,
             allow_structured: bool = False, kind: str = "notify",
             urgent: bool = False, thread_meta: dict | None = None,
             include_stopped: bool = False, skipped: list | None = None,
             payload: dict | None = None, cc=()) -> list[str]:
        # Le repli fichier ne stocke pas de signature (format v0 strict) : la
        # CLI prévient l'appelant, le message part quand même. Sans registre,
        # rien ne dit qu'un agent est arrêté : `include_stopped` et `skipped`
        # sont sans effet ici.
        fil.ensure_readable(text, allow_structured)
        payload = _send_payload(dest, urgent, payload)
        if dest == "all":
            targets = [
                f[:-5]
                for f in os.listdir(self.agents_dir())
                if f.endswith(".json") and f[:-5] != sender
            ]
            # L36 (0030) : limité au chantier de l'expéditeur, comme la v0
            own = identity.chantier_of(sender, self.cfg)
            if own:
                targets = [t for t in targets if identity.chantier_of(t, self.cfg) == own]
        else:
            targets = [dest]
        now = time.time()
        written: list[tuple[str, str]] = []
        # L125 : les copies (--cc) partent en événement passif, sans réveil
        envois = [(target, kind, payload, False) for target in targets]
        envois += [(name, "event", {"cc": dest}, True) for name in cc or ()]
        for target, genre, charge, copie in envois:
            filename = "%d-%s-%d.json" % (int(now * 1000), sender, os.getpid())
            tmp = os.path.join(self.inbox_dir(target), "." + filename)
            message = {"from": sender, "to": target, "ts": now, "text": text,
                       "host": host or socket.gethostname()}
            if genre != "notify" or charge:
                # format v0 inchangé pour un message ordinaire ; kind/payload
                # n'apparaissent que pour un événement, un urgent (C9) ou un
                # message passif (L125).
                message["kind"] = genre
                message["payload"] = charge
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(message, fh, ensure_ascii=False)
            os.replace(tmp, os.path.join(self.inbox_dir(target), filename))
            if not copie:
                written.append((target, filename))
        # Le fil, après le dépôt : une erreur ici ne perd rien (fil.record ne
        # lève pas). `v0` permet à import-v0 de ne pas réécrire ces messages.
        def chantier(name: str) -> str:
            try:
                return identity.chantier_of(name, self.cfg)
            except (OSError, ValueError):  # alias illisible : projet par défaut
                return ""

        sender_chantier = chantier(sender)
        groups: dict[str, list[tuple[str, str]]] = {}
        for target, filename in written:
            project = fil.project_for(self.cfg, sender_chantier, chantier(target))
            groups.setdefault(project, []).append((target, filename))
        for project, sent in groups.items():
            fil.record(
                self.cfg, None, sender=sender, recipients=[t for t, _ in sent], text=text,
                ts=now, project=project, lot=work_item_id,
                meta=dict(_send_thread_meta(thread_meta, payload, cc) or {}, repli="fichier",
                          host=host or socket.gethostname(),
                          diffusion="all" if dest == "all" else None,
                          v0=["%s/%s" % (t, f) for t, f in sent]))
        return targets

    def unread(self, name: str) -> list[dict]:
        out: list[dict] = []
        directory = self.inbox_dir(name)
        for filename in sorted(os.listdir(directory)):
            if not filename.endswith(".json"):
                continue
            try:
                with open(os.path.join(directory, filename), encoding="utf-8") as fh:
                    msg = json.load(fh)
            except (OSError, ValueError):
                continue
            out.append({
                "id": None,
                "from": msg.get("from", "?"),
                "to": msg.get("to", name),
                "ts": msg.get("ts", 0),
                "text": msg.get("text", ""),
                "kind": msg.get("kind", "notify"),
                "payload": msg.get("payload") or {},
                "host": msg.get("host"),
                "_file": filename,
            })
        return out

    def mark_read(self, name: str, msgs: list[dict]) -> int:
        directory = self.inbox_dir(name)
        read_dir = _mk(os.path.join(directory, "read"))
        done = 0
        for msg in msgs:
            filename = msg.get("_file")
            if not filename:
                continue
            try:
                os.replace(os.path.join(directory, filename), os.path.join(read_dir, filename))
                done += 1
            except OSError:
                pass
        return done

    # -- agents ------------------------------------------------------------
    def register(self, name: str, tool: str, cwd: str | None, session_id: str | None,
                 *, leased: bool = True) -> None:
        try:
            path = os.path.join(self.agents_dir(), name + ".json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"name": name, "tool": tool, "cwd": cwd,
                           "session_id": session_id, "last_seen": time.time()},
                          fh, ensure_ascii=False)
        except OSError:
            pass

    def agents(self) -> list[dict]:
        rows: list[dict] = []
        directory = self.agents_dir()
        for filename in os.listdir(directory):
            if not filename.endswith(".json"):
                continue
            try:
                with open(os.path.join(directory, filename), encoding="utf-8") as fh:
                    row = json.load(fh)
            except (OSError, ValueError):
                continue
            row = dict(row)
            row.setdefault("name", filename[:-5])
            row["host"] = socket.gethostname().split(".")[0]
            row["unread"] = len(self.unread(row["name"]))
            row["status"] = "?"
            row["status_text"] = self.get_status(row["name"])
            row["lease_owner"] = None
            row["lease_expires_ts"] = None
            row["chantier"] = identity.chantier_of(row["name"], self.cfg)
            rows.append(row)
        return sorted(rows, key=lambda r: -(r.get("last_seen") or 0))

    def get_status(self, name: str) -> str:
        try:
            with open(self.status_path(name), encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def set_status(self, name: str, text: str) -> None:
        try:
            with open(self.status_path(name), "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass

    # -- état local --------------------------------------------------------
    def stop_counter(self, name: str, reset: bool = False, bump: bool = False) -> int:
        path = os.path.join(_mk(os.path.join(self.cfg.state_dir, "hooks")), name + ".stops")
        count = 0
        try:
            with open(path, encoding="utf-8") as fh:
                count = int(fh.read().strip() or 0)
        except (OSError, ValueError):
            pass
        if reset:
            count = 0
        if bump:
            count += 1
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(str(count))
        except OSError:
            pass
        return count

    def close(self) -> None:
        pass


class PgBackend:
    """Backend Postgres : registre + boîte aux lettres + notifications."""

    kind = "pg"

    def __init__(self, cfg: Config, db: db_mod.Db):
        self.cfg = cfg
        self.db = db

    # -- messages ----------------------------------------------------------
    def send(self, sender: str, dest: str, text: str, host: str | None = None,
             signed: dict | None = None, work_item_id: str | None = None,
             allow_structured: bool = False, kind: str = "notify",
             urgent: bool = False, thread_meta: dict | None = None,
             include_stopped: bool = False, skipped: list | None = None,
             payload: dict | None = None, cc=()) -> list[str]:
        """Dépose le message. N'inscrit JAMAIS le destinataire au registre :
        un envoi à un nom inconnu créait un agent fantôme (et réécrivait
        l'hôte d'un agent existant) — la CLI refuse désormais ces envois
        avant d'arriver ici (`undeliverable.check_send`).

        « all » écarte les agents arrêtés, sauf `include_stopped` (`--queue`) ;
        leurs noms sont ajoutés à `skipped` si l'appelant en passe une liste."""
        fil.ensure_readable(text, allow_structured)
        payload = _send_payload(dest, urgent, payload)
        thread_meta = _send_thread_meta(thread_meta, payload, cc)
        projects: dict[str, str] = {}
        if dest == "all":
            rows = registry.overview(self.db)
            # projet du fil : équipe du canon, sinon chantier (fil.agent_project)
            projects = {row["name"]: fil.agent_project(row) for row in rows}
            # L36 (0030) : « all » = l'équipe (ou le chantier) de l'expéditeur ;
            # un expéditeur sans équipe ni chantier garde la diffusion globale.
            own = projects.get(sender)
            team = [row for row in rows if row["name"] != sender
                    and (not own or projects.get(row["name"]) == own)]
            # un agent arrêté ne lit pas sa boîte : la diffusion ne la remplit pas
            stopped = [row["name"] for row in team
                       if row.get("status") == "stopped" and not include_stopped]
            if skipped is not None:
                skipped.extend(stopped)
            targets = [row["name"] for row in team if row["name"] not in stopped]
        else:
            targets = [dest]
        groups: dict[str, list[tuple[str, int]]] = {}
        for target in targets:
            # `signed` vient de authority.sign_message : signature, payload,
            # nonce, horodatage et échéance signés. Un envoi simple écrit son
            # fil dans mail.send ; un envoi à « all » est regroupé ci-dessous.
            message_id = mail.send(
                self.db, sender, target, text, host=host, work_item_id=work_item_id,
                allow_structured=allow_structured, thread=(dest != "all"),
                kind=kind, payload=payload or None,
                thread_meta=thread_meta, **dict(signed or {}))
            if dest == "all":
                project = fil.project_for(self.cfg, projects.get(sender), projects.get(target))
                groups.setdefault(project, []).append((target, message_id))
        for project, sent in groups.items():
            fil.record(
                self.cfg, self.db, sender=sender, recipients=[t for t, _ in sent], text=text,
                project=project, lot=work_item_id, mailbox_ids=[i for _, i in sent],
                meta={"host": host, "diffusion": "all"})
        for name in cc or ():
            # L125 : la copie part en événement passif — lue au prochain tour
            # de son destinataire, sans le réveiller. Sans lot (elle ne change
            # pas le lot de sa session) ; le fil la cite sur l'entrée du message.
            mail.send(self.db, sender, name, text, host=host, kind="event",
                      payload={"cc": dest}, allow_structured=allow_structured,
                      thread=False)
        return targets

    def unread(self, name: str) -> list[dict]:
        return [mail.normalize(row) for row in mail.unread(self.db, name)]

    def mark_read(self, name: str, msgs: list[dict]) -> int:
        return mail.mark_delivered(self.db, [m["id"] for m in msgs if m.get("id") is not None])

    # -- agents ------------------------------------------------------------
    def register(self, name: str, tool: str, cwd: str | None, session_id: str | None,
                 *, leased: bool = True) -> None:
        if not leased:
            # L36 → L46 : un hook sans bail (session externe, shell qui a hérité
            # de AGENT_MAIL_NAME) ne touche à la ligne d'un agent `execute` que
            # pour `last_seen` : ni session, ni dossier, ni hôte, ni harnais —
            # l'exécuteur reprendrait sinon une session étrangère, ailleurs.
            # Un agent NÉ d'un hook sans bail est `externe` (L37, 0030). Une
            # seule instruction : pas de course lecture → écriture.
            registry.upsert_unleased(
                self.db, name, chantier=identity.chantier_of(name, self.cfg),
                harness=tool, host=self.cfg.host, cwd=cwd, session_id=session_id)
            return
        registry.upsert(
            self.db, name,
            chantier=identity.chantier_of(name, self.cfg),
            harness=tool, host=self.cfg.host, cwd=cwd, session_id=session_id,
        )

    def agents(self) -> list[dict]:
        rows = registry.overview(self.db)
        out = []
        for row in rows:
            out.append({
                "name": row["name"],
                "tool": row.get("harness") or "?",
                "cwd": row.get("cwd") or "",
                "session_id": row.get("session_id"),
                "last_seen": row.get("last_seen_ts") or 0.0,
                "host": row.get("host") or "",
                "status": row.get("status") or "?",
                "status_text": row.get("status_text") or "",
                "unread": int(row.get("unread") or 0),
                "lease_owner": row.get("lease_owner"),
                "lease_expires_ts": row.get("lease_expires_ts"),
                "chantier": row.get("chantier") or "",
                "model": row.get("model"),
                "budget_usd": row.get("budget_usd"),
                "spent_usd": row.get("spent_usd"),
                "turns": row.get("turns"),
            })
        return out

    def get_status(self, name: str) -> str:
        row = registry.get(self.db, name)
        return (row or {}).get("status_text") or ""

    def set_status(self, name: str, text: str) -> None:
        registry.upsert(self.db, name, host=self.cfg.host)
        registry.set_status(self.db, name, "idle", status_text=text)

    # -- état local --------------------------------------------------------
    def stop_counter(self, name: str, reset: bool = False, bump: bool = False) -> int:
        # Le compteur de relances est local à la machine (comme la v0), pas partagé.
        return FileBackend(self.cfg).stop_counter(name, reset=reset, bump=bump)

    def close(self) -> None:
        self.db.close()


def open_backend(cfg: Config) -> tuple[FileBackend | PgBackend, str | None]:
    """Ouvre le backend voulu. Renvoie (backend, avertissement lisible ou None).

    `auto` : Postgres si la base répond et qu'elle est migrée ; si la base est
    absente, repli fichier avec un message qui dit quoi faire. Un schéma
    incomplet n'est pas un fallback silencieux : c'est une erreur à corriger
    (`agent-mesh migrate`), sauf pour les hooks qui ne doivent jamais échouer.
    """
    mode = cfg.backend
    if mode == "file":
        return FileBackend(cfg), None
    try:
        db = db_mod.connect(cfg)
        db_mod.require_schema(db, defer=True)
        return PgBackend(cfg, db), None
    except db_mod.Unavailable as exc:
        if mode == "pg":
            raise
        warning = (
            "agent-mail : base injoignable (%s) — repli sur les fichiers %s ; détail : %s"
            % (config_mod.mask_dsn(cfg.dsn), cfg.v0_state, exc)
        )
        return FileBackend(cfg), warning
