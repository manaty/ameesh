# SPDX-License-Identifier: AGPL-3.0-only
"""#1604 — chemin rapide de `agent-mail statusline` et `agent-mail hook`.

Un hook se déclenche après CHAQUE outil, une barre d'état à chaque rendu : ces
deux chemins ne doivent ni importer tout le paquet (base, psycopg) ni ouvrir la
base quand il n'y a rien à faire. Ce module ne dépend que de `ameesh.config` :
il lit des marqueurs locaux et des caches courts, rien de plus.

Marqueur de boîte — `state_dir/mail/<agent>.json` :

    {"pending": bool, "checked": <epoch>, "unread": int}

`pending` dit qu'une remise par le hook est peut-être à faire (non-lu connu,
envoi local, doute). Le hook ne saute la base que si `pending` est faux ET que
`checked` est récent : la remise du courrier n'est jamais décidée ici, et un
marqueur absent, illisible ou périmé ramène au chemin complet.

Cache de barre d'état — `state_dir/statusline/<clé>.json` : la ligne déjà
rendue, quelques secondes (`AMEESH_STATUSLINE_TTL`, 5 s).

Rien ici n'écrit le registre ni ne remet de courrier : un chemin rapide ne peut
donc pas se tromper d'identité ni de session. Au pire il retarde une remise, que
le prochain chemin complet rattrape — les événements Stop, SessionStart et
UserPromptSubmit passent TOUJOURS par la base, et un PostToolUse y repasse au
plus tard au bout du TTL (`AMEESH_HOOK_FAST_TTL`, 15 s) ou à l'inscription due
(60 s).
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from .config import NAME_RE, Config

#: L61 : un hook ne réinscrit pas l'agent plus d'une fois par cette durée
REGISTER_EVERY_S = 60.0

#: âge maximal d'un marqueur « rien à faire » pour le chemin rapide du hook
HOOK_TTL_S = 15.0

#: âge maximal du cache de la barre d'état
STATUSLINE_TTL_S = 5.0

#: âge maximal du cache de liaison de session (l'identité du hook)
LIAISON_TTL_S = 600.0


def _delai(nom: str, defaut: float) -> float:
    """Le TTL réglé par l'environnement, sinon le défaut (jamais d'exception)."""
    try:
        return float(os.environ.get(nom) or defaut)
    except (TypeError, ValueError):
        return defaut


# --------------------------------------------------------------------------
# fichiers locaux (atomiques, silencieux : un cache ne fait jamais échouer)
# --------------------------------------------------------------------------

def _ecrire_json(path: str, data: dict) -> bool:
    tmp = None
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
        return True
    except OSError:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


def _lire_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# marqueur de boîte
# --------------------------------------------------------------------------

def mail_dir(cfg: Config) -> str:
    return os.path.join(cfg.state_dir, "mail")


def marker_path(cfg: Config, name: str) -> str:
    return os.path.join(mail_dir(cfg), name + ".json")


def read_marker(cfg: Config, name: str) -> dict | None:
    return _lire_json(marker_path(cfg, name))


def note(cfg: Config, name: str, *, pending: bool, unread: int = 0,
         maintenant: float | None = None) -> None:
    """Le résultat d'un passage COMPLET par la base (rien n'est décidé ici)."""
    _ecrire_json(marker_path(cfg, name), {
        "pending": bool(pending),
        "unread": max(0, int(unread)),
        "checked": time.time() if maintenant is None else float(maintenant),
    })


def bump(cfg: Config, name: str) -> None:
    """Un envoi local touche cette boîte : le prochain hook ira voir la base.

    On ne crée le marqueur que s'il existe déjà : un destinataire qui n'a jamais
    tourné sur cet hôte n'a pas de chemin rapide à invalider (et sa boîte reste
    lue par les événements complets).
    """
    if not NAME_RE.match(name or ""):
        return
    path = marker_path(cfg, name)
    if not os.path.exists(path):
        return
    actuel = _lire_json(path) or {}
    try:
        checked = float(actuel.get("checked") or 0.0)
        unread = int(actuel.get("unread") or 0)
    except (TypeError, ValueError):
        checked, unread = 0.0, 0
    _ecrire_json(path, {"pending": True, "unread": max(1, unread), "checked": checked})


def rien_a_faire(cfg: Config, name: str, ttl: float | None = None) -> bool:
    """Vrai si le marqueur autorise le hook à ne pas ouvrir la base."""
    marque = read_marker(cfg, name)
    if not marque or marque.get("pending"):
        return False
    if int(marque.get("unread") or 0) > 0:
        return False
    try:
        checked = float(marque.get("checked") or 0.0)
    except (TypeError, ValueError):
        return False
    age = time.time() - checked
    limite = _delai("AMEESH_HOOK_FAST_TTL", HOOK_TTL_S) if ttl is None else float(ttl)
    return 0 <= age < limite


# --------------------------------------------------------------------------
# inscription du hook (même format que cli.py, lu sans base)
# --------------------------------------------------------------------------

def register_stamp(cfg: Config, name: str) -> str:
    return os.path.join(cfg.state_dir, "hooks", name + ".inscription")


def register_due(cfg: Config, name: str, key: list) -> bool:
    """L61 : l'inscription de cette session est-elle à refaire ? Un état local
    illisible : oui."""
    try:
        with open(register_stamp(cfg, name), encoding="utf-8") as fh:
            last = json.load(fh)
        return (last.get("key") != key
                or not 0 <= time.time() - float(last.get("ts") or 0) < REGISTER_EVERY_S)
    except (OSError, ValueError, TypeError, AttributeError):
        return True


def register_done(cfg: Config, name: str, key: list) -> None:
    path = register_stamp(cfg, name)
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = "%s.%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"key": key, "ts": time.time()}, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def inscription_key(tool: str, data: dict, *, runner_id: str | None = None,
                    epoch: int | None = None) -> list:
    """La clé d'inscription telle que `cli.cmd_hook` la construit.

    Depuis l'environnement (identité de bail) : le couple (runner, epoch) ; une
    session externe liée : rien (la clé ne porte que harnais, dossier, session).
    Sans bail, `runner_id` et `epoch` valent None — jamais "" (la clé est
    comparée telle quelle au fichier d'inscription).
    """
    # L166: sous bail, cli.cmd_hook laisse la session au flux du runner et
    # inscrit None, même si le harnais fournit sa propre session au hook.
    session_id = None if runner_id and epoch is not None else data.get("session_id")
    return [tool, data.get("cwd"), session_id, runner_id, epoch]


# --------------------------------------------------------------------------
# identité du hook sans base : liaison de session locale
# --------------------------------------------------------------------------

def liaison_path(cfg: Config, harness: str, session_id: str) -> str:
    cle = hashlib.sha256(("%s\0%s\0%s" % (
        cfg.host, harness, session_id)).encode()).hexdigest()[:24]
    return os.path.join(cfg.state_dir, "hooks", "liaison-%s.json" % cle)


def remember_liaison(cfg: Config, name: str, harness: str, session_id: str,
                     *, pid=None) -> None:
    """Note la liaison qui vient d'être résolue en base, pour les hooks rapides."""
    if not (NAME_RE.match(name or "") and session_id):
        return
    _ecrire_json(liaison_path(cfg, harness, session_id), {
        "name": name, "harness": harness, "session": session_id,
        "pid": int(pid) if pid else None, "ts": time.time(),
    })


def _nom_liaison(cfg: Config, harness: str, session_id: str,
                 ttl: float | None = None) -> str:
    row = _lire_json(liaison_path(cfg, harness, session_id))
    if not row or row.get("harness") != harness or row.get("session") != session_id:
        return ""
    try:
        age = time.time() - float(row.get("ts") or 0.0)
    except (TypeError, ValueError):
        return ""
    limite = _delai("AMEESH_LIAISON_TTL", LIAISON_TTL_S) if ttl is None else float(ttl)
    if not 0 <= age < limite:
        return ""
    name = row.get("name") or ""
    return name if NAME_RE.match(name) else ""


def identite(cfg: Config, tool: str, data: dict) -> str:
    """Le nom d'agent de CE hook, sans base : environnement, sinon liaison notée."""
    name = os.environ.get("AGENT_MAIL_NAME") or ""
    if name and NAME_RE.match(name):
        return name
    return _nom_liaison(cfg, tool, str(data.get("session_id") or ""))


# --------------------------------------------------------------------------
# chemin rapide du hook
# --------------------------------------------------------------------------

def hook_rapide(cfg: Config, tool: str, raw: str) -> bool:
    """Traite le hook sans base ; False : continuer par le chemin complet.

    Ne remet jamais de courrier, n'écrit jamais le registre : le seul effet est
    de rendre la main tôt quand la boîte locale dit qu'il n'y a rien à faire.
    """
    if getattr(cfg, "backend", "auto") in ("mediated", "file"):
        # L109 : pas de remise par le hook dans la VM ; et le repli fichier
        # réinscrit l'agent à chaque hook (aucun tampon d'inscription) : rien
        # à accélérer ici sans changer ce contrat.
        return False
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("agent_id"):
        # L133 : sous-agent — rien n'est lu, remis, marqué ni écrit.
        return True
    if (data.get("hook_event_name") or "") != "PostToolUse":
        # Stop, SessionStart, UserPromptSubmit : toujours le chemin complet
        # (le Stop doit pouvoir bloquer, la remise ne se décide pas ici).
        return False
    attendu = os.environ.get("AMEESH_HARNESS") or ""
    if attendu and attendu != tool:
        return False  # harnais étranger : le chemin complet refuse et journalise
    name = identite(cfg, tool, data)
    if not name:
        return False
    runner_id = os.environ.get("AMEESH_RUNNER_ID") or os.environ.get("AGENT_MESH_RUNNER_ID") or ""
    epoch_txt = os.environ.get("AMEESH_LEASE_EPOCH") or os.environ.get("AGENT_MESH_LEASE_EPOCH") or ""
    en_bail = bool(runner_id and epoch_txt)
    if en_bail:
        try:
            epoch = int(epoch_txt)
        except ValueError:
            return False
        key = inscription_key(tool, data, runner_id=runner_id, epoch=epoch)
    else:
        key = inscription_key(tool, data)
    if register_due(cfg, name, key):
        return False  # l'inscription (et la session) se rafraîchit en base
    return rien_a_faire(cfg, name)


# --------------------------------------------------------------------------
# cache de la barre d'état
# --------------------------------------------------------------------------

def statusline_key(data: dict) -> str:
    """La clé du cache : le dossier et la session, rien d'autre (affichage)."""
    espace = data.get("workspace")
    cwd = ((espace or {}).get("current_dir") if isinstance(espace, dict) else None) \
        or data.get("cwd") or os.getcwd()
    session = str(data.get("session_id") or "")
    try:
        cwd = os.path.realpath(cwd)
    except (OSError, TypeError, ValueError):
        cwd = str(cwd)
    return hashlib.sha256(("%s\0%s" % (cwd, session)).encode()).hexdigest()[:24]


def _statusline_path(cfg: Config, key: str) -> str:
    return os.path.join(cfg.state_dir, "statusline", key + ".json")


def read_statusline(cfg: Config, key: str, ttl: float | None = None) -> str | None:
    row = _lire_json(_statusline_path(cfg, key))
    if not row or not isinstance(row.get("out"), str):
        return None
    try:
        age = time.time() - float(row.get("ts") or 0.0)
    except (TypeError, ValueError):
        return None
    limite = _delai("AMEESH_STATUSLINE_TTL", STATUSLINE_TTL_S) if ttl is None else float(ttl)
    if 0 <= age < limite:
        return row["out"]
    return None


def write_statusline(cfg: Config, key: str, out: str) -> None:
    _ecrire_json(_statusline_path(cfg, key), {"ts": time.time(), "out": out})
