# SPDX-License-Identifier: AGPL-3.0-only
"""#5424: identify a review quorum held by a whole budget-paused family.

Pure calculation shared by alerts and debit. Registry eligibility and the
latest freeze's designation are evidence; names and prose holds are not.
"""
from __future__ import annotations

import datetime
import json
import re
import time

from . import accounts, budget, review_designation as rd, work_lifecycle as lifecycle

#: #6990 : la marque de journal qui prouve le repli, et que la relecture après
#: fusion relit. Le préfixe nomme la décision owner (#213032, option a).
FALLBACK_PREFIX = "paused-family-review-fallback:#213032:"
FALLBACK_SCHEMA = "ameesh-paused-family-fallback/1"

#: #8264 : les décisions closes relues pour trouver une couverture de pause.
#: La table ne porte que quelques centaines de décisions ; la borne évite une
#: lecture sans fin sur une base ancienne. Une décision hors de cette fenêtre
#: ne couvre rien : le doute laisse l'attente ordinaire, jamais l'inverse.
DECISION_SCAN = 2000

#: L'échéance que la décision signée donne à sa règle : « (expire 26/11
#: 18:00Z) », « (expire: 2026-11-26 18:00Z) » ou une date seule, tenue
#: jusqu'à la fin du jour. Sans ce marqueur explicite, rien ne couvre la
#: pause : une date voisine (« jusqu'au 26/11 vers 16:00Z ») n'est pas une
#: échéance de règle et ne doit pas écourter la couverture.
_EXPIRES = re.compile(
    r"\bexpir\w*[^0-9]{0,40}?"
    r"(?:(?P<iso>\d{4}-\d{2}-\d{2})(?:[T ](?P<ih>\d{1,2}):(?P<im>\d{2}))?"
    r"|(?P<day>\d{1,2})/(?P<month>\d{1,2})(?:/(?P<year>\d{4}))?"
    r"(?:[ ]+(?:à[ ]+|vers[ ]+)?(?P<h>\d{1,2}):(?P<mi>\d{2}))?)"
    r"[ ]*(?:Z|UTC)?",
    re.IGNORECASE)

#: Une décision ne couvre une pause que si elle en parle : la question ou la
#: réponse retenue nomme la pause, le plafond ou l'indisponibilité. Une
#: décision de compte sans rapport, qui cite la famille et une échéance, ne
#: doit pas éteindre l'attente.
_PAUSE_CUE = re.compile(
    r"\bpause\b|\bpaused\b|\bindisponib\w*|\bplafond\b|\bquota\b|\bbudget\b"
    r"|\boverage\b|\bcap\b", re.IGNORECASE)


def _nature(row):
    reason = str(row.get("status_text") or "")[len(budget.PAUSE_PREFIX):].strip()
    if reason.lower().startswith(budget.CRITICAL_MARK.lower()):
        reason = reason[len(budget.CRITICAL_MARK):].strip()
    return accounts.pause_nature(reason).split("|")


def _account(row):
    nature = _nature(row)
    return nature[1] if nature[0] == "pause" else row.get("session_account")


def _window(row):
    nature = _nature(row)
    return nature[2] if nature[0] == "pause" else ""


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def _names(text, family):
    """La famille est nommée comme un mot : « codex » ne vaut pas pour
    « codex2 », et la casse du canon (« Codex ») est indifférente."""
    return re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(str(family)),
                     str(text or ""), re.IGNORECASE) is not None


def _label_of(view):
    """Le libellé de l'option retenue, ou son libellé d'origine s'il manque
    (réponses antérieures à `option_label`)."""
    answer = view.get("answer") or {}
    label = str(answer.get("option_label") or "").strip()
    if label:
        return label
    option = str(answer.get("option") or "")
    for row in view.get("options") or ():
        if str((row or {}).get("key") or "") == option:
            return str((row or {}).get("label") or "")
    return ""


def decision_until(view):
    """L'échéance d'une décision owner signée qui couvre une pause, ou None.

    Preuve lue : réponse `answered`, canal `signed` avec sa preuve, décision
    non remplacée, et une échéance explicite dans le périmètre **retenu** (le
    libellé de l'option et le texte de la réponse). La question n'est pas lue
    pour l'échéance : elle décrit les options, dont celles qui n'ont pas été
    choisies. La **validité** de la preuve n'est pas jugée ici : elle se
    revérifie à part (`verified_coverages`, B #46462), sans quoi le canal
    `signed` ne serait qu'une déclaration."""
    if not isinstance(view, dict):
        return None
    answer = view.get("answer") or {}
    if str(view.get("state") or "") != "answered":
        return None
    if str(answer.get("channel") or "") != "signed" or not answer.get("proof"):
        return None
    if view.get("superseded_by"):
        return None
    scope = " ".join(part for part in (_label_of(view),
                                       str(answer.get("text") or "")) if part)
    match = _EXPIRES.search(scope)
    if match is None:
        return None
    created = float(view.get("created_ts") or time.time())
    try:
        if match.group("iso"):
            year, month, day = (int(part) for part in match.group("iso").split("-"))
            hour = int(match.group("ih") or 23)
            minute = int(match.group("im") or 59)
            inferred = False
        else:
            day, month = int(match.group("day")), int(match.group("month"))
            year = int(match.group("year") or 0) or datetime.datetime.fromtimestamp(
                created, datetime.timezone.utc).year
            hour = int(match.group("h") or 23)
            minute = int(match.group("mi") or 59)
            inferred = not match.group("year")
        until = datetime.datetime(year, month, day, hour, minute,
                                  tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None
    # Une date sans année qui tombe avant la décision est celle de l'année
    # suivante (une règle de décembre court jusqu'en janvier).
    if inferred and until.timestamp() < created - 86400:
        try:
            until = until.replace(year=until.year + 1)
        except ValueError:
            return None
    return until.timestamp()


def coverages(decisions, *, families, now, verify=None):
    """#8264 : les familles en pause qu'une décision owner signée couvre.

    Rend `{famille: {id, option, until, until_iso, by}}` pour les décisions
    `answered`, signées, non remplacées et non échues dont le périmètre retenu
    nomme la famille et une échéance. Deux décisions pour la même famille :
    la plus proche échéance tranche, pour ne jamais prolonger une couverture
    par une décision plus tardive.

    `verify` (#46462) : revérifie la preuve de la réponse
    (`decisions.verify_proof`). Sans elle, le canal `signed` et la présence
    d'une preuve ne sont qu'une déclaration : ni la clé owner, ni l'empreinte
    de la réponse, ni l'approbation ne sont prouvées, et rien ne couvre. Un
    refus ne couvre rien ; une exception remonte à l'appelant, qui la
    consigne comme lecture impossible."""
    now = float(now)
    out = {}
    for view in decisions or ():
        until = decision_until(view)
        if until is None or until <= now:
            continue
        answer = view.get("answer") or {}
        scope = " ".join(part for part in (_label_of(view),
                                           str(answer.get("text") or "")) if part)
        context = " ".join(part for part in (str(view.get("question") or ""),
                                             scope) if part)
        if not _PAUSE_CUE.search(context):
            continue
        try:
            ident = int(view.get("id"))
        except (TypeError, ValueError):
            continue
        if verify is None or not verify(view):
            continue
        for family in families or ():
            family = str(family or "").strip()
            if not family or not _names(scope, family):
                continue
            current = out.get(family)
            if current is not None and current["until"] <= until:
                continue
            out[family] = {"id": ident, "option": answer.get("option"),
                           "until": until, "until_iso": _iso(until),
                           "by": answer.get("by") or None}
    return out


def decisions_history(db, *, now):
    """Les décisions closes relues par le débit (voir `DECISION_SCAN`)."""
    from . import decisions
    return decisions.history(db, now=now, limit=DECISION_SCAN)


def verified_coverages(db, views, *, families, now):
    """Les couvertures dont la preuve signée est revérifiée (B #46462).

    `decisions.verify_proof` refuse une empreinte altérée, une approbation qui
    n'est pas celle de cette décision, une clé owner absente, révoquée ou
    remplacée, et une signature Ed25519 invalide. Une preuve refusée ne couvre
    rien ; une lecture en échec remonte, l'appelant la consigne."""
    from . import decisions
    return coverages(views, families=families, now=now,
                     verify=lambda view: bool(decisions.verify_proof(db, view)[0]))


def blocked(designation, listing, *, project, families, now, pilot="", closed_hosts=(),
            decisions=None):
    """Return indispensable paused families, never infer an unknown author.

    The pool is the designation's (`review_designation_activation._candidates`):
    the project's pilot and agents on closed hosts are never reviewers, so they
    cannot make a paused family look reachable (N6 of #5424).

    `decisions` (#8264) : `{famille: couverture}` de `coverages`. Une pause
    couverte garde son calcul et porte en plus `decision` (id, option,
    échéance) ; sans cette lecture, aucune couverture n'est supposée.
    """
    required = designation.get("required")
    authors = set(designation.get("author_families") or ())
    if (not required or not authors or designation.get("unknown_authors")
            or designation.get("quorum") or not designation.get("shortfall")):
        return []
    candidates = rd.candidates_from_registry(listing, project=project, families=families,
                                             closed_hosts=closed_hosts)
    by_name = {row["name"]: row for row in listing}
    closed = {str(host).strip() for host in closed_hosts or () if str(host).strip()}
    # Out of the pool, not merely unavailable: a closed-host reviewer is no
    # budget evidence either, and must not stop the family's pause scan.
    candidates = [candidate for candidate in candidates if candidate.name != pilot
                  and str(by_name[candidate.name].get("host") or "").strip() not in closed]
    by_family = {}
    for candidate in candidates:
        if candidate.family and candidate.family not in authors:
            by_family.setdefault(candidate.family, []).append(candidate)
    acquired = set(designation.get("acquired_families") or ()) - authors
    reachable = acquired | {family for family, rows in by_family.items()
                            if any(row.available for row in rows)}
    if len(reachable) >= required or len(set(by_family) | acquired) < required:
        return []
    out = []
    for family, candidates in sorted(by_family.items()):
        if family in reachable:
            continue
        pauses = []
        for candidate in candidates:
            row = by_name[candidate.name]
            text = str(row.get("status_text") or "")
            # A dead/retired/operator-held reviewer is not evidence of a
            # budget pause. Short account windows remain eligible (#3876).
            resumed = dict(row, status="idle", status_text="")
            if (not rd.is_available(resumed)
                    or str(row.get("status") or "").lower() in ("dead", "stopped")
                    or not text.lower().startswith(budget.PAUSE_PREFIX)
                    or rd.is_short_window_pause(text)):
                break
            deadline = accounts.pause_deadline(text)
            if deadline is not None and deadline <= now:
                break
            account = _account(row)
            pauses.append({"name": candidate.name,
                           "account": account,
                           "resume_ts": deadline, "reason": text})
        else:
            if pauses:
                deadlines = [p["resume_ts"] for p in pauses if p["resume_ts"] is not None]
                entry = {"family": family, "pauses": pauses,
                         "resume_ts": min(deadlines) if deadlines else None}
                coverage = (decisions or {}).get(family)
                if coverage:
                    entry["decision"] = dict(coverage)
                out.append(entry)
    return out


def fallback(designation, listing, *, project, families, now, min_hours,
             pilot="", closed_hosts=()):
    """#6990 : le repli de la porte quand une famille requise est en pause.

    Décision owner #213032 option a : la porte admet un lot sur **un** avis
    d'une famille autre que celle de l'auteur, plus la CI complète du paquet,
    quand la famille qui manque est entièrement en pause budget et que sa
    reprise est à plus de `min_hours` heures. Rend la preuve du repli, ou
    `None` — jamais un repli supposé.

    Le cas est étroit à dessein : le barème exige exactement deux avis, il en
    manque exactement un, et cette unique famille manquante est en pause. Un
    siège manquant pour une autre raison (agent indisponible, pool
    incomplet), une famille en pause multiple, un auteur inconnu ou un quorum
    déjà acquis ne l'ouvrent pas : le repli ne couvre que la pause budget
    prouvée par `blocked` (#5424), avec une échéance lisible.
    """
    if type(min_hours) is not int or min_hours <= 0:
        return None
    designation = designation or {}
    if designation.get("required") != 2:
        return None
    if int(designation.get("shortfall") or 0) != 1:
        return None
    if designation.get("quorum") or designation.get("unknown_authors"):
        return None
    pauses = blocked(designation, listing, project=project, families=families,
                     now=now, pilot=pilot, closed_hosts=closed_hosts)
    if len(pauses) != 1:
        return None
    pause = pauses[0]
    # La reprise de la famille est la plus tôt de ses comptes ; un seul compte
    # sans échéance lisible rend la famille « peut-être de retour bientôt » :
    # le délai n'est alors pas prouvé, et le repli ne s'ouvre pas.
    resumes = []
    for row in pause.get("pauses") or ():
        try:
            resumes.append(float(row.get("resume_ts")))
        except (TypeError, ValueError):
            return None
    if not resumes:
        return None
    resume = min(resumes)
    if resume <= float(now) + min_hours * 3600:
        return None
    accounts_ = sorted({str(row.get("account") or "").strip()
                        for row in pause.get("pauses") or ()
                        if str(row.get("account") or "").strip()})
    return {"schema": FALLBACK_SCHEMA, "family": str(pause.get("family") or ""),
            "pauses": list(pause.get("pauses") or ()),
            "resume_ts": resume, "accounts": accounts_,
            "min_hours": min_hours, "required": 2, "shortfall": 1}


def fallback_note(evidence, frozen: str) -> str:
    """La marque de journal du repli, lisible et sans secret (#6990).

    Le texte est stable pour la même preuve : la porte l'écrit avec
    `once=True`, une seconde admission ne duplique donc pas la ligne. Le gel
    exact y figure, pour que la relecture après fusion ne rattache le repli
    qu'à la tête qu'il a réellement couverte.
    """
    payload = {"schema": FALLBACK_SCHEMA,
               "freeze": str(frozen or "").strip().lower(),
               "family": str(evidence.get("family") or ""),
               "required": 2, "admitted": 1,
               "resume_ts": float(evidence.get("resume_ts") or 0.0),
               "accounts": sorted(str(account) for account in
                                  evidence.get("accounts") or () if str(account))}
    return FALLBACK_PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True)


def recorded(notes):
    """Les preuves de repli lisibles dans des notes de journal (#6990).

    Une note illisible est ignorée : elle ne prouve ni n'interdit le repli, et
    la relecture après fusion ne s'ouvre que sur une preuve complète. Rend les
    charges utiles décodées, dans l'ordre reçu.
    """
    out = []
    for note in notes or ():
        text = note if isinstance(note, str) else str((note or {}).get("note") or "")
        at = text.find(FALLBACK_PREFIX)
        if at < 0:
            continue
        try:
            payload = json.loads(text[at + len(FALLBACK_PREFIX):].strip())
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("schema") == FALLBACK_SCHEMA:
            out.append(payload)
    return out


def exclusions(cfg, db, projects, listing):
    """The pilots (by project) and closed hosts `blocked` leaves out.

    Same readers as the designation (`review_designation_activation._notify`,
    `alert_briefs.claims_closed_hosts`); an unreadable source excludes nobody.
    """
    from . import alert_briefs, review_designation_activation as activation
    pilots = {project: activation._notify(cfg, db, project, listing)
              for project in sorted(set(projects))}
    try:
        closed = alert_briefs.claims_closed_hosts(db)
    except Exception:  # noqa: BLE001 - no proof a host is closed
        closed = set()
    return pilots, closed


def alerts(items, milestones, events, listing, *, families, now, pilots=None,
           closed_hosts=()):
    """One alert per family/episode, across projects and frozen lots."""
    from . import debit, exploitation
    grouped = {}
    ms, ev = {}, {}
    for row in milestones:
        ms.setdefault(int(row["work_item_id"]), []).append(row)
    for row in events:
        event = dict(row)
        event.setdefault("at_ts", event.get("created_ts"))
        ev.setdefault(int(row["work_item_id"]), []).append(event)
    agent_families = {str(row["name"]).casefold(): rd.live_family(row, families=families)
                      for row in listing}
    for item in items:
        ident = int(item["id"])
        if item.get("state") and item["state"] not in lifecycle.TO_VALIDATE:
            continue
        designation = debit.designations(ms.get(ident, []), ev.get(ident, []),
                                          agent_families=agent_families)
        if not designation.get("freeze"):
            continue
        project = item.get("app") or ""
        for pause in blocked(designation, listing, project=project,
                             families=families, now=now,
                             pilot=(pilots or {}).get(project) or "",
                             closed_hosts=closed_hosts):
            family = pause["family"]
            alert = grouped.setdefault(family, dict(
                schema=exploitation.ALERT_SCHEMA, type="review_family_paused",
                agent=None, since=None, value=None, threshold=None, family=family,
                resume_ts=pause["resume_ts"], pauses=[], held_lots=[]))
            for row in pause["pauses"]:
                if row not in alert["pauses"]:
                    alert["pauses"].append(row)
            frozen = float(designation["freeze"].get("at_ts") or now)
            alert["held_lots"].append(dict(lot=ident, issue_ref=item.get("issue_ref"),
                                      project=item.get("app"), age_s=max(0, now-frozen)))
    for alert in grouped.values():
        # The episode belongs to the family, not to today's held projects.
        # Removing a project's last lot must not create another notification.
        # #6809 : the deadline stays out of the key — a pace pause recomputes
        # it at every pass (01:30, 01:33, 01:36), which re-briefed the same
        # pause every 30 s. The account and its window name the episode.
        alert["episode"] = sorted({
            (_account(row) or "", _window(row))
            for row in listing if rd.live_family(row, families=families) == alert["family"]
            and str(row.get("status_text") or "").lower().startswith(budget.PAUSE_PREFIX)
            and not rd.is_short_window_pause(row["status_text"])}, key=repr)
        deadlines = [p["resume_ts"] for p in alert["pauses"] if p["resume_ts"] is not None]
        alert["resume_ts"] = min(deadlines) if deadlines else None
        alert["held_lots"].sort(key=lambda row: row["lot"])
        alert["lots"] = [row["lot"] for row in alert["held_lots"]]
        alert["detail"] = description(alert)
    return list(grouped.values())


def description(alert):
    import datetime
    deadline = alert.get("resume_ts")
    resume = (datetime.datetime.fromtimestamp(deadline, datetime.timezone.utc).isoformat()
              if deadline is not None else "unknown (check account)")
    accounts_ = ", ".join(sorted({p.get("account") or "unknown" for p in alert["pauses"]}))
    lots = ", ".join("#%s (%s, %.1f h)" % (row["lot"], row.get("issue_ref") or "no issue",
                                          row["age_s"] / 3600) for row in alert["held_lots"])
    return ("Review family %s paused on budget; account(s): %s; earliest resume: %s. "
            "Held lots: %s. Expected action: inspect ameesh accounts list and ameesh budget; "
            "use an authorized fallback account, obtain a cap decision, or request an owner "
            "decision. Preserve the required independent review families."
            % (alert["family"], accounts_, resume, lots))
