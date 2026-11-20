# SPDX-License-Identifier: AGPL-3.0-only
"""#7861 : le courrier arrivé pendant un attach atteint la session.

Constat (manaty/ameesh#6089) : `ameesh attach` lance le harnais interactif
sans tour de courrier ; le hook `agent-mail` ne remet le courrier que sur
`UserPromptSubmit`/`Stop`/`PostToolUse`, donc une session immobile ne voit
rien. Le fil `check_inputs` d'`_run_attach` lisait `attach.waiting_since`
toutes les 30 s, mais ne journalisait qu'après 10 min, et seulement dans le
journal de fichier de l'attach.

Deux chemins, sans jamais écrire dans la TUI ni marquer lu avant remise :

* **pendant la session** : `new_mail` signale chaque message NON PASSIF
  arrivé depuis le début de l'attach au canal notify de l'hôte
  (`platform.notify`, le même `notify-send`/`osascript` que les alertes de
  bureau). La notification donne l'expéditeur, le numéro et une ligne
  d'objet, une seule fois par message ; elle ne réserve rien et ne marque
  rien lu ;
* **au détachement** : `mark_due` lit les non-lus non passifs encore en
  boîte et écrit un marqueur local (`attach-mail-due.json` dans le dossier
  de l'agent), avec l'id maximal. L'exécuteur le lit dans `_mail_spec` :
  ces messages sont dus tout de suite, sans la fenêtre de regroupement
  L125, donc le tour de courrier part sans attendre le cycle suivant. Le
  marqueur est retiré dès qu'aucun message marqué n'est plus non lu, ce qui
  évite toute double remise (le hook a pu livrer entre-temps).

Le marqueur est un fichier de l'état local de l'agent, pas une écriture en
base : aucun schéma nouveau, et l'attach comme l'exécuteur de l'hôte le
partagent. Un NOTIFY `agent_mail` explicite réveille l'exécuteur, en plus du
déclencheur de bail que `registry.release` émet déjà.
"""
from __future__ import annotations

import json
import os
import time

from . import mail
from .config import CHANNEL_MAIL

#: nom du marqueur dans `<état>/<agent>/`
MARKER_NAME = "attach-mail-due.json"
MARKER_SCHEMA = "ameesh-attach-mail-due/1"

#: une notification de bureau ne doit jamais retarder le fil d'attach
NOTICE_TIMEOUT_S = 2.0
#: une ligne d'objet reste courte (et rien du corps au-delà)
SUBJECT_LIMIT = 120


def _one_line(value, limit: int) -> str:
    """Une seule ligne, espaces compactés, coupée proprement à `limit`."""
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[:max(0, limit - 1)].rstrip() + "…"
    return text


def subject_line(row) -> str:
    """Première ligne non vide du corps : l'« objet » d'un message ameesh.

    Aucun champ `subject` n'existe en base ; la première ligne est la ligne
    d'objet utile, et la notification n'en montre pas davantage.
    """
    body = row.get("body")
    if not isinstance(body, str):
        return ""
    for line in body.splitlines():
        line = " ".join(line.split())
        if line:
            return line
    return ""


def notice(name: str, row) -> tuple[str, str]:
    """(titre, corps) courts d'une notification de nouveau courrier.

    Expéditeur, numéro, une ligne d'objet : rien d'autre. Un corps multiligne
    ne livre jamais sa suite par ce canal.
    """
    sender = _one_line(row.get("sender") or "?", 80)
    number = row.get("id")
    objet = _one_line(subject_line(row), SUBJECT_LIMIT) or "(sans objet)"
    return ("Nouveau courrier pour %s" % name,
            "de %s — #%s : %s" % (sender, number if number is not None else "?",
                                  objet))


def notify_mail(name: str, row) -> tuple[bool, str]:
    """Affiche la notification de bureau de l'hôte ; ne lève jamais.

    Rend `(True, "")` si le backend a répondu, `(False, raison)` sinon : le
    fil d'attach journalise l'échec une fois par message, sans interrompre
    la session. Aucune remise n'est faite ici : le message reste non lu.
    """
    from .platform import notify as platform_notify
    title, body = notice(name, row)
    try:
        platform_notify.send(title, body, timeout=NOTICE_TIMEOUT_S)
    except platform_notify.NotifyError as exc:
        return False, str(exc)
    except OSError as exc:  # défensif : un lancement impossible n'est pas fatal
        return False, str(exc)
    return True, ""


def new_mail(db, name: str, *, since: float, notified: set[int],
             notify=notify_mail) -> list[tuple[int, str]]:
    """Signale une fois chaque message non passif arrivé depuis `since`.

    `notified` est la mémoire du fil d'attach (ids déjà signalés) ; elle est
    mise à jour même si l'affichage échoue, pour ne pas réessayer toutes les
    30 s un backend absent. Rend les échecs `(id, raison)` pour le journal.
    """
    failures: list[tuple[int, str]] = []
    for row in mail.unread_active(db, name):
        if mail.passive_reason(row):
            # `unread_active` filtre déjà en SQL ; on refiltre ici avec la
            # règle unique (`passive_reason`), comme le fait le hook.
            continue
        try:
            message_id = int(row["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if message_id in notified:
            continue
        try:
            created = float(row.get("created_ts") or 0.0)
        except (TypeError, ValueError):
            created = 0.0
        if created < float(since):
            # déjà là avant l'attach : le hook le remettra au prochain prompt,
            # ou l'exécuteur le prendra au détachement ; pas de doublon ici.
            continue
        notified.add(message_id)
        try:
            ok, reason = notify(name, row)
        except Exception as exc:  # noqa: BLE001 — une notification ne tue pas le fil
            ok, reason = False, "%s: %s" % (type(exc).__name__, exc)
        if not ok:
            failures.append((message_id, reason))
    return failures


def marker_path(cfg, name: str) -> str:
    """Chemin du marqueur de courrier dû d'un agent."""
    return os.path.join(cfg.agent_dir(name), MARKER_NAME)


def due_marker(cfg, name: str) -> int | None:
    """L'id maximal marqué au détachement, ou None (absent/illisible)."""
    try:
        with open(marker_path(cfg, name), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != MARKER_SCHEMA:
        return None
    try:
        value = int(data.get("max_id"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def mark_due(cfg, db, name: str) -> int | None:
    """Écrit le marqueur des non-lus présents au détachement.

    Lit les non-lus non passifs AVANT d'être appelé par le `finally` de
    `_run_attach` (donc avant que le bail ne soit rendu) et retient l'id
    maximal. Rend cet id, ou None s'il n'y avait rien à traiter. Une erreur
    d'écriture remonte à l'appelant, qui la journalise : le tour de courrier
    ordinaire reste le filet.
    """
    rows = mail.unread_active(db, name)
    ids = []
    for row in rows:
        if mail.passive_reason(row):
            continue  # même règle que `new_mail` : le passif n'ouvre pas de tour
        try:
            ids.append(int(row["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not ids:
        return None
    max_id = max(ids)
    path = marker_path(cfg, name)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    temporary = "%s.tmp-%d" % (path, os.getpid())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"schema": MARKER_SCHEMA, "max_id": max_id,
                       "at": time.time()}, handle)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return max_id


def clear(cfg, name: str) -> None:
    """Retire le marqueur (idempotent) : son office est fait."""
    try:
        os.unlink(marker_path(cfg, name))
    except FileNotFoundError:
        pass
    except OSError:
        pass


def wake(db, name: str, max_id: int) -> bool:
    """Réveille l'exécuteur de l'hôte pour le tour de courrier du détachement.

    Le déclencheur `agent_lease` de `registry.release` réveille déjà la passe
    de l'exécuteur ; ce NOTIFY explicite porte l'intention et suit le
    marqueur, au cas où un auditeur l'aurait manqué. Best effort : le
    marqueur reste la source durable, le sondage de l'exécuteur le lira.
    """
    from . import db as db_mod, storage
    try:
        storage.of(db).wakeups.notify(
            CHANNEL_MAIL,
            json.dumps({"to": name, "attach_end": True, "max_id": int(max_id)}))
    except db_mod.DbError:
        return False
    return True
