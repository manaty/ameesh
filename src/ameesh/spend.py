# SPDX-License-Identifier: AGPL-3.0-only
"""Dépenses pré-déléguées (#7865, manaty/ameesh#6096, spec §8.3).

Une approbation permanente peut porter une **dimension dépense** : plafonds
mensuel et journalier (unités mineures de la devise du grant), périmètre fermé
de fournisseurs et d'objets, horizon de la prochaine revue humaine. Une action
de dépense déclare, dans ses `args` signés, `spend = {recurrence, provider,
purpose}` : `recurrence` vaut `once` ou `daily` (aucun engagement mensuel ou
annuel n'est représentable), `provider` et `purpose` doivent être dans le
périmètre du grant.

Ce module porte les décisions qui ne sont pas du SQL : la forme et les limites
dures à la PROPOSITION (aucun nouveau moyen de paiement, aucun engagement
annuel, aucun fournisseur hors liste), l'escalade au humain responsable quand
un plafond refuse, et la vue du relevé (ressources récurrentes en cours,
projection, commande d'arrêt d'une ligne).

Le ledger (`spend_ledger`) est la seule source du déjà-dépensé et de la
projection ; `ameesh_spend_state` les calcule à l'heure réelle de la base. Ce
module ne réimplémente pas ce calcul : il lit `storage…grants.spend_state`.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone

from . import fil, storage, standing_policy

RECURRENCES = ("once", "daily")
#: acteur du balayage : un agent du mesh, jamais un `cli:` libre
SWEEP_ACTOR = "agent:ameesh-spend"
#: forme exacte d'une action de dépense : ni champ libre, ni montant annuel
ACTION_KEYS = frozenset({"recurrence", "provider", "purpose"})
_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class SpendError(ValueError):
    """Une dépense refusée, avec un code stable (`spend_*`)."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason

    def __str__(self) -> str:
        return "[%s] %s" % (self.code, self.reason)


def is_spend(action: dict) -> bool:
    """Une action porte-t-elle une description de dépense ?"""
    args = action.get("args")
    if isinstance(args, dict) and "spend" in args:
        return True
    return bool(action.get("recurrence") and action.get("recurrence") != "once")


def normalize(value, *, amount, currency, limits: standing_policy.Policy | None = None,
              connector: str = "", operation: str = "") -> dict:
    """Valide et normalise `spend = {recurrence, provider, purpose}`.

    `limits` (la politique du canon) est facultative : quand elle est fournie,
    le fournisseur et l'objet doivent appartenir à une règle de dépense qui
    couvre le connecteur et l'opération, et la dépense du jour doit tenir sous
    son plafond journalier. Ce contrôle est un PRÉ-FILTRE de proposition ; la
    décision d'autorisation reste celle du grant, sous verrou, au lancement.
    """
    if not isinstance(value, dict) or set(value) != ACTION_KEYS:
        raise SpendError("spend_scope",
                         "spend: {recurrence, provider, purpose} expected")
    recurrence = value.get("recurrence")
    if recurrence not in RECURRENCES:
        raise SpendError("spend_recurrence",
                         "spend.recurrence: once or daily expected, not %r "
                         "(no monthly or annual commitment)" % (recurrence,))
    provider = value.get("provider")
    purpose = value.get("purpose")
    for name, text in (("provider", provider), ("purpose", purpose)):
        if not isinstance(text, str) or not _IDENT_RE.fullmatch(text):
            raise SpendError("spend_scope",
                             "spend.%s: identifier of 1 to 128 characters expected"
                             % name)
    if type(amount) is not int or amount <= 0:
        raise SpendError("spend_amount",
                         "a spend action needs a positive amount (minor units)")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise SpendError("spend_currency", "a spend action needs an ISO 4217 currency")
    if limits is not None and limits.rules:
        rules = [rule for rule in limits.rules
                 if rule.connector == connector and operation in rule.operations
                 and rule.spend]
        if not rules:
            raise SpendError(
                "spend_policy",
                "the canon allows no spend for %s %s: no rule carries a monthly or "
                "daily spend cap" % (connector, operation))
        reasons = []
        codes = []
        for rule in rules:
            if provider not in rule.providers:
                codes.append("spend_provider")
                reasons.append("provider %r outside %s" % (provider, list(rule.providers)))
                continue
            if purpose not in rule.purposes:
                codes.append("spend_purpose")
                reasons.append("purpose %r outside %s" % (purpose, list(rule.purposes)))
                continue
            if amount > rule.max_per_day:
                codes.append("spend_daily_cap")
                reasons.append("amount %s > daily canon cap %s"
                               % (amount, rule.max_per_day))
                continue
            break
        else:
            code = codes[0] if codes else "spend_policy"
            raise SpendError(code,
                             "spend refused by the canon for %s %s: %s"
                             % (connector, operation, "; ".join(reasons)))
    return {"recurrence": recurrence, "provider": provider, "purpose": purpose}


def action_metadata(action: dict) -> dict | None:
    """La description de dépense d'une action, lue sur sa ligne (colonnes
    immuables) ou, à défaut, dans ses `args`."""
    if action.get("recurrence") in RECURRENCES and action.get("spend_provider"):
        return {"recurrence": action["recurrence"],
                "provider": action["spend_provider"],
                "purpose": action.get("spend_purpose") or ""}
    args = action.get("args")
    if isinstance(args, dict) and isinstance(args.get("spend"), dict):
        return dict(args["spend"])
    return None


def escalate(db, action: dict, code: str, reason: str) -> None:
    """Escalade un refus de plafond au humain responsable du grant.

    Écrit une entrée de fil pour l'approbateur du grant (et le lot de
    l'action s'il existe) : le propriétaire doit voir, dans son relevé, que la
    ressource demandée a été refusée et qu'il peut l'arrêter ou signer un
    nouveau grant. Ne lève jamais : le refus est déjà enregistré ; le fil est
    un canal d'information, pas l'autorisation.
    """
    cfg = getattr(db, "cfg", None)
    if cfg is None:
        return
    try:
        grant_id = action.get("auth_grant_id")
        approver = action.get("auth_approver") or ""
        if grant_id:
            grant = storage.of(db).grants.get(int(grant_id))
            if grant:
                approver = grant.get("approver") or approver
        text = ("Dépense refusée [%s] pour %s %s sur %s : %s. "
                "Arrêtez la ressource récurrente (ameesh action spend list) ou "
                "signez un nouveau grant ; aucun dépassement n'est autorisé."
                % (code, action.get("connector"), action.get("operation"),
                   action.get("target") or "—", reason))
        recipients = [approver] if approver else ["all"]
        fil.record(cfg, db, sender="ameesh:spend", recipients=recipients, text=text,
                   project=action.get("project") or "",
                   lot=action.get("work_item"),
                   meta={"action_id": action.get("action_id"), "event": "spend_refused",
                         "code": code, "grant_id": grant_id})
    except Exception as exc:  # noqa: BLE001 - l'escalade ne bloque jamais le refus
        fil.warn("spend refusal %s of %s not written to the thread (%s)"
                 % (code, action.get("action_id"), " ".join(str(exc).split())[:200]))


def utc_today() -> date:
    """La date UTC, celle du ledger (jamais le fuseau de la session)."""
    return datetime.now(timezone.utc).date()


def _review_deadline(row: dict) -> datetime:
    """La prochaine revue humaine d'une ligne (UTC)."""
    base = row.get("confirmed_ts") or row.get("started_ts")
    if base is None:
        base = datetime.now(timezone.utc).timestamp()
    return (datetime.fromtimestamp(float(base), timezone.utc)
            + timedelta(hours=int(row.get("review_horizon_hours") or 24)))


def _actor_ok(by: str) -> bool:
    return isinstance(by, str) and bool(re.fullmatch(r"(human|agent):[A-Za-z0-9._:/-]{1,64}",
                                                     by))


def ledger_rows(db, *, decided_by: str | None = None) -> list[dict]:
    """Les ressources récurrentes en cours d'un décideur, avec leur projection."""
    rows = storage.of(db).grants.spend_ongoing(decided_by=decided_by)
    states: dict[int, dict | None] = {}
    today = utc_today()
    for row in rows:
        grant_id = int(row["grant_id"])
        if grant_id not in states:
            states[grant_id] = storage.of(db).grants.spend_state(grant_id)
        state = states[grant_id] or {}
        projected = int(state.get("projected") or 0)
        month_cap = int(state.get("max_per_month") or 0)
        day_spent = int(state.get("day_spent") or 0)
        day_cap = int(state.get("max_per_day") or 0)
        deadline = _review_deadline(row)
        review_due = deadline.date() <= today
        stop_required = (review_due
                         or (month_cap > 0 and projected >= month_cap)
                         or (day_cap > 0 and day_spent > day_cap))
        row["daily_amount"] = int(row["amount"])
        row["day_spent"] = day_spent
        row["projected"] = projected
        row["month_spent"] = int(state.get("month_spent") or 0)
        row["max_per_month"] = month_cap
        row["max_per_day"] = day_cap
        row["review_deadline"] = deadline.date().isoformat()
        row["review_due"] = bool(review_due)
        row["stop_required"] = bool(stop_required)
        row["stop_command"] = "ameesh action spend stop %d" % int(row["id"])
    return rows


def recap(db, *, avatar: str) -> dict:
    """La section « dépenses » du relevé de l'owner pour un avatar.

    Liste les ressources récurrentes en cours (une ligne par ressource, avec
    la commande d'arrêt d'une ligne) et le total quotidien engagé. Une
    ressource dont la revue est dépassée, dont la projection atteint le
    plafond mensuel — ou dont le déjà-dépensé du jour dépasse le plafond
    journalier — porte `stop_required` : le relevé dit « ARRÊT REQUIS », le
    gate refuse toute nouvelle dépense sur ce grant.
    """
    name = avatar if avatar.startswith("agent:") else "agent:%s" % avatar
    rows = ledger_rows(db, decided_by=name)
    total_daily = sum(int(row["daily_amount"]) for row in rows)
    required = [row for row in rows if row["stop_required"]]
    return {"avatar": name, "resources": rows,
            "total_daily": total_daily, "stop_required": len(required),
            "review_due": sum(1 for row in rows if row.get("review_due"))}


def _connector(db, name: str):
    """Le connecteur d'une ligne, avec son dossier noop quand c'en est un."""
    from . import connectors
    options: dict = {}
    if name == "shell-noop":
        directory = os.environ.get("AMEESH_NOOP_DIR")
        cfg = getattr(db, "cfg", None)
        if not directory and cfg is not None:
            directory = os.path.join(cfg.state_dir, "noop")
        if directory:
            options["directory"] = directory
    return connectors.get(name, **options)


def _cap_reason(entry: dict, day: date) -> str:
    """Le motif d'arrêt d'une ressource retenue par le balayage."""
    if entry["review_due"]:
        return ("revue humaine du %s non confirmée : arrêt automatique"
                % entry["deadline"].isoformat())
    return ("plafond atteint : arrêt avant le jour suivant (projection mois %s, "
            "total %s ; aujourd'hui %s)"
            % (entry["projection_month"], entry["projection_total"], day.isoformat()))


def request_stop(db, ledger_id: int, *, by: str, reason: str | None = None,
                 connector=None, on: date | None = None) -> dict | None:
    """#7865 (B1) : arrête une ressource par une ACTION `operation = 'stop'`.

    L'action est proposée au connecteur de la ressource, liée à la ligne du
    ledger, puis exécutée (classe `reversible` : aucun reçu). La ligne ne
    passe `stopped` qu'à la CONFIRMATION du connecteur (déclencheur SQL) ;
    un échec certain libère la demande, une issue inconnue la laisse en
    place (l'arrêt a peut-être eu lieu). Aucun acteur libre : `by` doit être
    `agent:<id>` ou `human:<id>`.

    Rend `{ledger, action, result}` ; `action` peut rester `proposed` si le
    connecteur classe `stop` comme gardé (le CLI indique alors le reçu à
    obtenir), ou None si la ligne n'est pas une ressource quotidienne en
    cours / a déjà une demande vivante.
    """
    from . import actions as actions_mod
    if not _actor_ok(by):
        raise SpendError("spend_actor",
                         "agent:<id> or human:<id> expected, not %r" % (by,))
    if not isinstance(ledger_id, int) or ledger_id <= 0:
        raise SpendError("spend_scope", "spend stop: positive ledger id expected")
    grants = storage.of(db).grants
    row = grants.spend_get(ledger_id)
    if row is None or row.get("state") != "ongoing" or row.get("recurrence") != "daily":
        return None
    if row.get("stop_action_id"):
        action = actions_mod.get(db, row["stop_action_id"])
        if action is None:
            return None
        if action["state"] in ("proposed", "approved"):
            result = actions_mod.execute(db, action["action_id"],
                                         connector or _connector(db, action["connector"]),
                                         by=by)
            return {"ledger": grants.spend_get(ledger_id), "action": actions_mod.get(
                db, action["action_id"]), "result": result}
        return {"ledger": row, "action": action, "result": None,
                "pending": action["state"] in ("launched", "unknown")}
    original = actions_mod.get(db, row["action_id"])
    if original is None:
        raise SpendError("spend_scope", "spend stop: original action is missing")
    conn = connector or _connector(db, original["connector"])
    stop = actions_mod.propose(
        db, conn, project=original["project"], operation="stop",
        target=original["target"],
        args={"spend_stop": {"ledger_id": int(ledger_id),
                             "action_id": row["action_id"]}},
        proposed_by=by, work_item=original.get("work_item"))
    linked = grants.spend_request_stop(int(ledger_id), stop["action_id"],
                                       by=by, on=on, reason=reason)
    if linked is None:
        return None
    try:
        result = actions_mod.execute(db, stop["action_id"], conn, by=by)
    except actions_mod.ActionError as exc:
        if exc.code != actions_mod.RECEIPT_REQUIRED:
            raise
        return {"ledger": grants.spend_get(ledger_id),
                "action": actions_mod.get(db, stop["action_id"]),
                "result": None, "pending": True, "receipt_required": True}
    return {"ledger": grants.spend_get(ledger_id),
            "action": actions_mod.get(db, stop["action_id"]),
            "result": result}


def _proof_signed_at(db, view: dict) -> float | None:
    """L'heure SIGNÉE (`ts`) de l'approbation qui porte la preuve de `view`."""
    from . import decisions
    answer = view.get("answer") or {}
    proof = answer.get("proof") or {}
    if not proof:
        return None
    digest = decisions.answer_digest(
        view, answer.get("text") or "", answer.get("option"),
        answer.get("by") or "", answer.get("signer"))
    rows = storage.of(db).approvals.recent(
        action=decisions.APPROVAL_ACTION, artifact_hash=digest, limit=20)
    row = next((item for item in rows
                if int(item["id"]) == int(proof.get("approval_id") or 0)), None)
    if row is None:
        return None
    try:
        return float(row["created_ts"])
    except (TypeError, ValueError):
        return None


def confirm(db, ledger_id: int, *, decision: int) -> dict | None:
    """#7865 (B2/B4) : confirmation d'une revue par une décision SIGNÉE.

    Seule une décision de l'owner, répondue, courante et signée
    (`decisions.verify_proof(..., live=True)` : approbation encore vivante,
    consommée par cette décision, clé `owner` valide, signature Ed25519)
    confirme la revue. L'option signée doit être exactement
    `confirm-spend-<ledger>` : une décision d'un autre objet, une réponse non
    signée, remplacée, expirée ou utilisée pour une autre ressource est
    refusée. Une décision ne sert qu'une fois : la rejouer sur la même ligne
    rend la ligne courante SANS rafraîchir `confirmed_at`, même après une
    décision plus récente. `confirmed_at` est l'heure réelle de la base ;
    l'appelant ne fournit aucune date.
    """
    from . import decisions
    if not isinstance(ledger_id, int) or ledger_id <= 0:
        raise SpendError("spend_scope", "spend confirm: positive ledger id expected")
    if type(decision) is not int or decision <= 0:
        raise SpendError("spend_scope",
                         "spend confirm: positive decision id expected")
    grants = storage.of(db).grants
    used = grants.spend_confirmation(decision)
    row = grants.spend_get(ledger_id)
    if used is not None:
        if int(used["ledger_id"]) != ledger_id:
            raise SpendError(
                "spend_confirm",
                "decision #%d already confirmed another review" % decision)
        return row
    if row is None or row.get("state") != "ongoing" or row.get("recurrence") != "daily":
        return None
    view = decisions.get(db, decision)
    if view is None:
        raise SpendError("spend_confirm", "decision #%d not found" % decision)
    if view.get("state") != "answered":
        raise SpendError("spend_confirm",
                         "decision #%d is %s, not answered" % (decision, view["state"]))
    try:
        decisions.require_current(db, view)
    except decisions.DecisionError as exc:
        raise SpendError("spend_confirm", "decision #%d: %s" % (decision, exc)) from exc
    ok, detail = decisions.verify_proof(db, view, live=True)
    if not ok:
        raise SpendError(
            "spend_confirm",
            "decision #%d is not a live owner-signed answer: %s" % (decision, detail))
    answer = view.get("answer") or {}
    expected = "confirm-spend-%d" % ledger_id
    if answer.get("option") != expected:
        raise SpendError(
            "spend_confirm",
            "decision #%d does not confirm resource #%d (signed option %r, "
            "expected %r)" % (decision, ledger_id, answer.get("option"), expected))
    by = str(answer.get("by") or "")
    if not by.startswith("human:"):
        by = "human:%s" % by if by else ""
    if not _actor_ok(by):
        raise SpendError("spend_confirm",
                         "decision #%d has no readable human author" % decision)
    signed_at = _proof_signed_at(db, view)
    if signed_at is None:
        raise SpendError("spend_confirm",
                         "decision #%d has no signed timestamp" % decision)
    return grants.spend_confirm(ledger_id, by=by, decision=decision,
                                signed_at=signed_at)


def sweep(db, *, today: date | None = None, by: str = SWEEP_ACTOR,
          connector=None, dry_run: bool = False) -> dict:
    """#7865 (B2) : arrête les ressources qui franchiraient un plafond.

    Le calcul est par GRANT, pas ligne par ligne : la projection d'un grant
    est le déjà-dépensé plus le coût futur de CHAQUE ressource `daily` en
    cours jusqu'à sa prochaine revue. Une ressource dont la revue est dépassée
    est arrêtée ; si la projection dépasse le plafond mensuel ou total, le
    balayage arrête ensuite assez de ressources (coût futur le plus élevé
    d'abord, identifiant pour départager) pour que la projection des lignes
    conservées tienne sous les deux plafonds — jamais un dépassement.
    `request_stop` émet l'action d'arrêt AVANT le jour qui franchirait le
    plafond. Aucun geste humain n'est requis ; une confirmation signée
    (`confirm`) repousse la revue et le balayage reprend. `today` permet de
    simuler des jours (tests) ; en production c'est la date UTC.

    Rend `{today, stopped, planned, errors}` : `stopped` liste les lignes
    arrêtées (ou dont l'arrêt est confirmé), `planned` les arrêts qu'un
    `--dry-run` aurait émis, `errors` les refus certains (escaladés).
    """
    day = today or utc_today()
    if isinstance(day, str):
        day = date.fromisoformat(day)
    grants = storage.of(db).grants
    by_grant: dict[int, list[dict]] = {}
    for row in grants.spend_ongoing():
        by_grant.setdefault(int(row["grant_id"]), []).append(row)
    stopped: list[dict] = []
    planned: list[dict] = []
    errors: list[dict] = []
    for grant_id in sorted(by_grant):
        rows = by_grant[grant_id]
        total = grants.spend_totals_at(grant_id, day) or {}
        month_spent = int(total.get("month_spent") or 0)
        total_accrued = int(total.get("total_accrued") or 0)
        month_cap = int(rows[0].get("max_per_month") or 0)
        total_cap = int(rows[0].get("max_amount") or 0)
        entries = []
        for row in rows:
            deadline = _review_deadline(row).date()
            amount = int(row["amount"])
            entries.append({
                "row": row, "deadline": deadline, "amount": amount,
                "extra": amount * max(0, (deadline - day).days),
                "review_due": deadline <= day,
                "projection_month": 0, "projection_total": 0,
            })
        # revue dépassée : arrêt obligatoire, quel que soit le plafond
        chosen = [entry for entry in entries if entry["review_due"]]
        kept = [entry for entry in entries if not entry["review_due"]]
        projected_month = month_spent + sum(entry["extra"] for entry in kept)
        projected_total = total_accrued + sum(entry["extra"] for entry in kept)
        for entry in sorted(kept, key=lambda e: (-e["extra"], int(e["row"]["id"]))):
            if ((month_cap == 0 or projected_month <= month_cap)
                    and (total_cap == 0 or projected_total <= total_cap)):
                break
            entry["projection_month"] = projected_month
            entry["projection_total"] = projected_total
            chosen.append(entry)
            projected_month -= entry["extra"]
            projected_total -= entry["extra"]
        for entry in sorted(chosen, key=lambda e: int(e["row"]["id"])):
            row = entry["row"]
            reason = _cap_reason(entry, day)
            item = {"ledger_id": int(row["id"]), "action_id": row["action_id"],
                    "grant_id": grant_id, "reason": reason,
                    "connector": row.get("connector"), "target": row.get("target"),
                    "project": row.get("project"), "work_item": row.get("work_item"),
                    "month_spent": month_spent, "total_accrued": total_accrued,
                    "amount": entry["amount"], "future_cost": entry["extra"],
                    "review_due": entry["review_due"]}
            if dry_run:
                planned.append(item)
                continue
            conn = connector if (connector is not None
                                 and getattr(connector, "name", None) == row.get("connector")) else None
            try:
                outcome = request_stop(db, int(row["id"]), by=by, reason=reason,
                                       connector=conn, on=day)
            except Exception as exc:  # noqa: BLE001 - le balayage continue les autres lignes
                item["error"] = " ".join(str(exc).split())[:300]
                errors.append(item)
                continue
            if outcome is None:
                item["error"] = "stop not linked (already requested or line gone)"
                errors.append(item)
                continue
            result = outcome.get("result") or {}
            item.update({
                "stop_action_id": (outcome.get("action") or {}).get("action_id"),
                "stop_state": (outcome.get("action") or {}).get("state"),
                "ledger_state": (outcome.get("ledger") or {}).get("state"),
                "pending": bool(outcome.get("pending")) or result.get("state") == "unknown",
            })
            if outcome.get("pending") or result.get("state") in ("failed", "unknown"):
                if not item.get("error"):
                    item["error"] = "stop action %s: %s" % (
                        item.get("stop_action_id") or "?", result.get("state") or "not executed")
                errors.append(item)
            else:
                stopped.append(item)
    for item in errors:
        escalate(db, {"action_id": item.get("action_id"),
                      "connector": item.get("connector"),
                      "operation": "stop", "target": item.get("target"),
                      "project": item.get("project"),
                      "work_item": item.get("work_item"),
                      "auth_grant_id": item.get("grant_id")},
                 "spend_stop_failed", item.get("error") or "stop not confirmed")
    return {"today": day.isoformat(), "stopped": stopped,
            "planned": planned, "errors": errors}
