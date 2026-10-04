# SPDX-License-Identifier: AGPL-3.0-only
"""Connecteur `shell-noop` : tests et démonstration (spec §7.3).

L'« effet » est l'écriture d'un fichier `<dossier>/<clé d'idempotence>.json`
qui décrit l'action. La création est atomique et exclusive (fichier
temporaire puis `link`, qui échoue si la cible existe) : une seconde
exécution avec la même clé ne refait rien et rend l'issue de la première —
le connecteur **garantit** la déduplication (`dedupe = guaranteed`).
La réconciliation lit la présence du fichier.

Pour démontrer le protocole, `args.simulate` (facultatif, signé comme le
reste des args) provoque :

* `fail` : échec certain, aucun fichier écrit ;
* `lose-response` : le fichier est écrit, puis la réponse se perd
  (`ConnectionError`) : la porte enregistre `unknown` ;
* `hang` : le fichier est écrit, puis l'appel ne rend pas la main avant
  `args.hang_seconds` (défaut 3600) : délai dépassé, `unknown`.
"""
from __future__ import annotations

import os
import re
import time

from .. import jcs
from . import Action, ConnectorError, Outcome

_KEY_RE = re.compile(r"^act_[0-9A-Za-z]{26}$")

#: classes par défaut (le canon prime) ; une opération inconnue est gardée
OPERATIONS = {
    "read": "read",
    "note": "reversible",
    "send": "irreversible",
    "merge": "irreversible",
    "refund": "costly",
    "label": "costly",
}


class ShellNoopConnector:
    name = "shell-noop"
    dedupe = "guaranteed"

    def __init__(self, directory: str | None = None, timeout: float = 30.0):
        directory = directory or os.environ.get("AMEESH_NOOP_DIR")
        if not directory:
            raise ConnectorError("shell-noop : dossier requis (directory= ou AMEESH_NOOP_DIR)")
        self.directory = os.path.abspath(os.path.expanduser(directory))
        self.timeout = float(timeout)

    # -- protocole ---------------------------------------------------------
    def classify(self, operation: str, args: dict) -> str:
        return OPERATIONS.get(operation, "irreversible")

    def path(self, idempotency_key: str) -> str:
        if not isinstance(idempotency_key, str) or not _KEY_RE.fullmatch(idempotency_key):
            raise ConnectorError("shell-noop : clé d'idempotence invalide %r" % (idempotency_key,))
        return os.path.join(self.directory, idempotency_key + ".json")

    def execute(self, action: Action, idempotency_key: str) -> Outcome:
        path = self.path(idempotency_key)
        if os.path.exists(path):
            return Outcome.confirmed(path, "déjà fait : même clé d'idempotence, aucun nouvel effet")
        simulate = action.args.get("simulate") if isinstance(action.args, dict) else None
        if simulate == "fail":
            return Outcome.failed("échec simulé : aucun effet")
        os.makedirs(self.directory, exist_ok=True)
        payload = jcs.dumps({
            "idempotency_key": idempotency_key, "action_id": action.action_id,
            "project": action.project, "operation": action.operation, "target": action.target,
            "args": action.args, "amount": action.amount, "currency": action.currency,
            "attempt": action.attempt,
        }) + "\n"
        tmp = os.path.join(self.directory, ".%s.%d.%s.tmp" % (
            idempotency_key, os.getpid(), os.urandom(4).hex()))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)
            created = True
        except FileExistsError:
            created = False
        finally:
            os.unlink(tmp)
        if not created:
            return Outcome.confirmed(path, "déjà fait : même clé d'idempotence, aucun nouvel effet")
        if simulate == "lose-response":
            raise ConnectionError("shell-noop : réponse perdue après l'effet (simulé)")
        if simulate == "hang":
            time.sleep(float(action.args.get("hang_seconds", 3600)))
        return Outcome.confirmed(path)

    def reconcile(self, action: Action) -> Outcome | None:
        path = self.path(action.action_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                written = jcs.loads(fh.read())
        except (OSError, jcs.JcsError):
            return None
        if not isinstance(written, dict) or written.get("action_id") != action.action_id:
            return None
        return Outcome.confirmed(path, "fichier présent : l'effet a eu lieu")
