# SPDX-License-Identifier: AGPL-3.0-only
"""#2914 — the train's pinned canon commit against the gate's, on this host.

The train reads its rules at the commit pinned in the host configuration
(``train.projects.<p>.canon_commit``, qualified by the pilot, #1467). The merge
gate and the reviewer designation read the host's audited canon commit
(``canon_sync.audited_commit``: ``canon_state.last_good_commit``), from the
first configured canon whose policy applies to the project
(``review_designation_activation.policy_sources``). When the two
differ, the train and the gate do not ask the same number of reviews for the
same lot (agents-2, 21/10: pin 91abace7, gate 20c5a630).

At each passage the train compares both commits:

The commits are compared first; with equal commits, the gate's light floor
and family flags, the strictest over every configured canon (applicable to
the project or not), are compared with the pinned canon's.

* **shadow**: the drift is written in the plan and the pilot is alerted;
* **active**: the train refuses, by name, to compose a new packet; a packet
  already in flight runs to its terminal outcome.

The pin never follows the gate by itself: a new commit must be qualified and
configured by the pilot (#1467). The gate's commit is read, never written.
"""
from __future__ import annotations

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
        if self.state == DRIFT and self.gate == self.pin:
            return ("canon épinglé %s = canon de la porte, mais règles d'avis "
                    "de la porte différentes : %s" % (self.pin[:12], self.why))
        if self.state == DRIFT:
            return ("canon épinglé %s ≠ canon de la porte %s"
                    % (self.pin[:12], self.gate[:12]))
        if self.state == UNKNOWN:
            return ("canon de la porte illisible (épinglé %s) : %s"
                    % (self.pin[:12], self.why or "?"))
        return "canon épinglé %s = canon de la porte" % self.pin[:12]

    def to_json(self):
        return asdict(self)


def gate_commit(db, cfg, project):
    """The audited canon commit the designation reads for ``project``, and why not.

    Returns ``(commit, why)``: ``commit`` is empty when no exact commit can be
    read, ``why`` then names the cause. The sources are the designation's
    own (``review_designation_activation.policy_sources``: configured canons
    in order, each at its audited commit, kept when its compiled policy for
    the project loads), never a parallel selection. Several applicable canons
    at different commits are ambiguous: which one the designation reads then
    depends on the lot's paths, and no commit is guessed.
    """
    from . import review_designation_activation as rda

    errors, found = [], []
    try:
        for source, commit, _loaded in rda.policy_sources(cfg, db, project, errors):
            found.append((source.repo, str(commit or "").strip().lower()))
    except rda.NoCanon as exc:
        return "", str(exc)
    except Exception as exc:  # noqa: BLE001 - unreadable source: named, never guessed
        return "", "%s: %s" % (type(exc).__name__, str(exc)[:200])
    if not found:
        why = "; ".join(errors[:4]) or "no canon declares a policy for %r" % (project,)
        return "", "no applicable canon for the gate: %s" % why
    commits = sorted({commit for _repo, commit in found})
    if len(commits) > 1:
        return "", ("%d applicable canons at different commits (%s): the gate's "
                    "canon is ambiguous" % (len(found), ", ".join(
                        "%s@%s" % (repo, commit[:12]) for repo, commit in found)))
    commit = commits[0]
    if not _SHA.fullmatch(commit):
        return "", "no exact audited commit for %s" % found[0][0]
    return commit, ""


def _pinned_flags(repo, commit):
    """The pinned canon's light floor and family flags, as the gate reads one."""
    from . import mechanics
    try:
        light = mechanics.light_reviews_at(repo, commit)
        exclude, distinct = mechanics.review_flags_at(repo, commit)
    except mechanics.MechanicsAbsent:
        return 0, False, True  # the gate's historical reading of a canon without it
    return light, exclude, distinct


def gate_rules_gap(cfg, canon_path, pin):
    """What the merge gate reads differently from the pin, across all canons.

    The gate's light floor and family flags (``lot_class._light_reviews``,
    ``lot_class._review_flags``) are the strictest over **every** configured
    canon, applicable to the project or not: a canon that does not declare the
    project's policy can still raise them. Returns "" when they equal the
    pinned canon's, otherwise the named difference (an unreadable value is a
    difference too, never a reassuring zero).
    """
    from . import canon as canon_mod
    from . import lot_class, mechanics

    canons = canon_mod.load_configured(cfg)
    light, problem = lot_class._light_reviews(canons)
    if problem:
        return "plancher léger de la porte illisible : %s" % problem
    exclude, distinct, problem = lot_class._review_flags(canons)
    if problem:
        return "drapeaux d'avis de la porte illisibles : %s" % problem
    try:
        pinned = _pinned_flags(canon_path, pin)
    except mechanics.MechanicsError as exc:
        return "règles d'avis du commit épinglé illisibles : %s" % str(exc)[:200]
    gate = (light, exclude, distinct)
    if gate == pinned:
        return ""
    names = ("light_reviews", "exclude_author_family", "distinct_families")
    return ", ".join("%s porte %s / train %s" % (name, mine, theirs)
                     for name, mine, theirs in zip(names, gate, pinned)
                     if mine != theirs)


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
    host's configuration and the project). ``pilot(db)`` names the
    project's orchestrator ("" = none known, retried at the next passage). The
    alert is a mail, sent once per process for each distinct reading and
    mode; the passage's own
    ``notify`` journals every non-aligned reading.
    """

    def __init__(self, project, pin, reader, *, rules=None, pilot=None, mailer=None):
        if not callable(reader):
            raise TypeError("explicit gate canon reader required")
        self.project, self.pin, self.reader = project, pin, reader
        #: ``rules(db)`` -> "" or the gate's review-rule difference across all
        #: configured canons (``gate_rules_gap``); None skips this reading
        self.rules = rules
        self.pilot = pilot
        self.mailer = mailer
        self.alerted = set()

    def read(self, db):
        try:
            commit, why = self.reader(db)
        except Exception as exc:  # noqa: BLE001 - a reader failure is an unknown reading
            commit, why = "", "%s: %s" % (type(exc).__name__, str(exc)[:200])
        reading = compare(self.pin, commit, why)
        if not reading.aligned or self.rules is None:
            return reading
        try:
            gap = self.rules(db)
        except Exception as exc:  # noqa: BLE001 - unreadable rules: never aligned
            return Reading(pin=reading.pin, gate="", state=UNKNOWN,
                           why="règles d'avis de la porte illisibles : %s: %s"
                           % (type(exc).__name__, str(exc)[:200]))
        if gap:
            return Reading(pin=reading.pin, gate=reading.gate, state=DRIFT, why=gap)
        return reading

    def alert(self, db, reading, mode):
        """Mail the pilot once for this reading; return the recipient or ""."""
        if reading.aligned:
            return ""
        # a rule gap is named by its why; an unknown reading's why can vary
        # with each failure and must not resend the same alert
        key = (reading.pin, reading.gate, reading.state,
               reading.why if reading.state == DRIFT else "", mode)
        if key in self.alerted:
            return ""
        try:
            recipient = self.pilot(db) if self.pilot is not None else ""
            if not recipient:
                return ""  # no pilot known yet: the next passage retries
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
