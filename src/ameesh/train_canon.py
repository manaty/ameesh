# SPDX-License-Identifier: AGPL-3.0-only
"""#2914 — the train's pinned canon commit against the gate's, on this host.

The train reads its rules at the commit pinned in the host configuration
(``train.projects.<p>.canon_commit``, qualified by the pilot, #1467). The merge
gate and the reviewer designation read the host's audited canon commit
(``canon_sync.audited_commit``: ``canon_state.last_good_commit``). When the two
differ, the train and the gate do not ask the same number of reviews for the
same lot (agents-2, 21/10: pin 91abace7, gate 20c5a630).

At each passage the train compares both commits:

* **shadow**: the drift is written in the plan and the pilot is alerted;
* **active**: the train refuses, by name, to compose a new packet; a packet
  already in flight runs to its terminal outcome.

The pin never follows the gate by itself: a new commit must be qualified and
configured by the pilot (#1467). The gate's commit is read, never written.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass

#: the three outcomes of a comparison
ALIGNED, DRIFT, UNKNOWN = "aligned", "drift", "unknown"
SENDER = "ameesh"

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


@dataclass(frozen=True)
class Reading:
    """One comparison of the pinned commit with the gate's commit."""

    pin: str
    gate: str
    state: str
    why: str = ""

    @property
    def aligned(self):
        return self.state == ALIGNED

    def describe(self):
        if self.state == DRIFT:
            return ("canon épinglé %s ≠ canon de la porte %s"
                    % (self.pin[:12], self.gate[:12]))
        if self.state == UNKNOWN:
            return ("canon de la porte illisible (épinglé %s) : %s"
                    % (self.pin[:12], self.why or "?"))
        return "canon épinglé %s = canon de la porte" % self.pin[:12]

    def to_json(self):
        return asdict(self)


def _same_repo(left, right):
    try:
        return os.path.realpath(left) == os.path.realpath(right)
    except (TypeError, ValueError):
        return False


def gate_commit(db, cfg, canon_path):
    """The audited canon commit the gate reads on this host, and why not.

    Returns ``(commit, why)``: ``commit`` is empty when no exact commit can be
    read, ``why`` then names the cause. The configured canon cloned at
    ``canon_path`` is preferred; with another path, the single configured Git
    canon is the gate's. Several canons and none at that path are ambiguous:
    no commit is guessed.
    """
    from . import canon as canon_mod
    from . import canon_sync

    if cfg is None or not str(getattr(cfg, "canon", "") or "").strip():
        return "", "no canon configured for the gate on this host"
    try:
        canons = canon_mod.load_configured(cfg)
    except Exception as exc:  # noqa: BLE001 - unreadable canon: named, never guessed
        return "", "cannot read the configured canon: %s: %s" % (
            type(exc).__name__, str(exc)[:200])
    sources = [(canon, source) for canon in canons
               for source in getattr(canon, "sources", ()) or ()
               if getattr(source, "mode", "") == "git" and getattr(source, "repo", "")
               and getattr(source, "commit", "") and not getattr(source, "prefix", "")]
    if not sources:
        return "", "no configured canon is an exact Git commit"
    same = [pair for pair in sources if _same_repo(pair[1].repo, canon_path)]
    if same:
        canon, source = same[0]
    elif len(sources) == 1:
        canon, source = sources[0]
    else:
        return "", ("%d configured canons and none at %s: the gate's canon is "
                    "ambiguous" % (len(sources), canon_path))
    try:
        commit = canon_sync.audited_commit(db, cfg, canon, source)
    except Exception as exc:  # noqa: BLE001 - unreadable state: named, never the ref
        return "", "cannot read the audited canon state: %s: %s" % (
            type(exc).__name__, str(exc)[:200])
    commit = str(commit or "").strip().lower()
    if not _SHA.fullmatch(commit):
        return "", "no exact audited commit for %s" % source.repo
    return commit, ""


def compare(pin, commit, why=""):
    """The reading of a pinned commit against the gate's (``""`` = unknown)."""
    pin = str(pin or "").strip().lower()
    commit = str(commit or "").strip().lower()
    if not commit:
        return Reading(pin=pin, gate="", state=UNKNOWN,
                       why=why or "no gate canon commit")
    return Reading(pin=pin, gate=commit, state=ALIGNED if commit == pin else DRIFT)


class Watch:
    """Compare at each passage; alert the pilot once per distinct reading.

    ``reader(db)`` returns ``(commit, why)`` (``gate_commit`` bound to the
    host's configuration and the train's canon path). ``pilot(db)`` names the
    project's orchestrator ("" = none known). The alert is a mail, sent once
    per process for each distinct (pin, gate, state); the passage's own
    ``notify`` journals every non-aligned reading.
    """

    def __init__(self, project, pin, reader, *, pilot=None, mailer=None):
        if not callable(reader):
            raise TypeError("explicit gate canon reader required")
        self.project, self.pin, self.reader = project, pin, reader
        self.pilot = pilot
        self.mailer = mailer
        self.alerted = set()

    def read(self, db):
        try:
            commit, why = self.reader(db)
        except Exception as exc:  # noqa: BLE001 - a reader failure is an unknown reading
            commit, why = "", "%s: %s" % (type(exc).__name__, str(exc)[:200])
        return compare(self.pin, commit, why)

    def alert(self, db, reading, mode):
        """Mail the pilot once for this reading; return the recipient or ""."""
        if reading.aligned:
            return ""
        key = (reading.pin, reading.gate, reading.state, mode)
        if key in self.alerted:
            return ""
        recipient = ""
        try:
            recipient = self.pilot(db) if self.pilot is not None else ""
            if recipient:
                self._send(db, recipient, reading, mode)
        except Exception:  # noqa: BLE001 - the plan/refusal stays recorded
            return ""
        self.alerted.add(key)
        return recipient

    def _send(self, db, recipient, reading, mode):
        effect = ("le train refuse de composer un nouveau paquet"
                  if mode == "active" else
                  "le plan d'ombre porte l'écart ; aucun effet")
        body = "\n".join([
            "Train %s (%s) : %s." % (self.project, mode, reading.describe()),
            "",
            "- Effet : %s." % effect,
            "- Le train lit ses règles au commit épinglé (train.projects.%s."
            "canon_commit) ; la porte et la désignation lisent le canon audité "
            "de l'hôte." % self.project,
            "- Aucun suivi automatique (#1467) : qualifier le commit de la porte, "
            "puis l'épingler dans la configuration de l'hôte.",
        ])
        if self.mailer is not None:
            self.mailer(db, recipient, body, reading)
            return
        from . import mail
        mail.send(db, SENDER, recipient, body,
                  payload={"train_canon": dict(reading.to_json(), project=self.project,
                                               mode=mode)})


def pilot_of(cfg, project):
    """A reader of the project's orchestrator, the one the designation mails."""
    def read(db):
        from . import registry, review_designation_activation
        return review_designation_activation._notify(cfg, db, project,
                                                     registry.overview(db))
    return read
