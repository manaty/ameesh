"""Les deux backends de la CLI : Postgres (nominal) et fichiers (repli v0).

Le repli fichier écrit exactement là où la v0 écrit
(`~/.local/state/agent-mail`), avec les mêmes noms de fichiers et le même JSON :
si Postgres est absent, les agents continuent de se parler, et la v0 peut
reprendre la main sans migration de données.
"""
from __future__ import annotations

import json
import os
import socket
import time

from . import config as config_mod
from . import db as db_mod
from . import identity, mail, registry
from .config import Config


def _mk(path: str) -> str:
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


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
             signed: dict | None = None) -> list[str]:
        # Le repli fichier ne stocke pas de signature (format v0 strict) : la
        # CLI prévient l'appelant, le message part quand même.
        if dest == "all":
            targets = [
                f[:-5]
                for f in os.listdir(self.agents_dir())
                if f.endswith(".json") and f[:-5] != sender
            ]
        else:
            targets = [dest]
        now = time.time()
        for target in targets:
            filename = "%d-%s-%d.json" % (int(now * 1000), sender, os.getpid())
            tmp = os.path.join(self.inbox_dir(target), "." + filename)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    {"from": sender, "to": target, "ts": now, "text": text,
                     "host": host or socket.gethostname()},
                    fh, ensure_ascii=False,
                )
            os.replace(tmp, os.path.join(self.inbox_dir(target), filename))
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
                "kind": "notify",
                "payload": {},
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
    def register(self, name: str, tool: str, cwd: str | None, session_id: str | None) -> None:
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
             signed: dict | None = None) -> list[str]:
        if dest == "all":
            targets = [
                row["name"]
                for row in registry.overview(self.db)
                if row["name"] != sender
            ]
        else:
            targets = [dest]
        for target in targets:
            # `signed` vient de authority.sign_message : signature, payload,
            # nonce, horodatage et échéance signés.
            mail.send(self.db, sender, target, text, host=host, **dict(signed or {}))
            registry.upsert(self.db, target, host=host)
        return targets

    def unread(self, name: str) -> list[dict]:
        return [mail.normalize(row) for row in mail.unread(self.db, name)]

    def mark_read(self, name: str, msgs: list[dict]) -> int:
        return mail.mark_delivered(self.db, [m["id"] for m in msgs if m.get("id") is not None])

    # -- agents ------------------------------------------------------------
    def register(self, name: str, tool: str, cwd: str | None, session_id: str | None) -> None:
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
        db_mod.require_schema(db)
        return PgBackend(cfg, db), None
    except db_mod.Unavailable as exc:
        if mode == "pg":
            raise
        warning = (
            "agent-mail : base injoignable (%s) — repli sur les fichiers %s ; détail : %s"
            % (config_mod.mask_dsn(cfg.dsn), cfg.v0_state, exc)
        )
        return FileBackend(cfg), warning
