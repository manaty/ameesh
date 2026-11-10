# SPDX-License-Identifier: AGPL-3.0-only
"""#6669 — M3 de l'étude #6650 : la résolution explicite d'un constat de revue.

Un **constat** est un jalon `verdict` `blocked` : c'est la seule trace
structurée d'un B, et son identifiant de jalon le distingue (plusieurs
constats peuvent porter sur le même gel). Le `sha` du jalon est le **témoin**
de la tête réellement lue ; la résolution le recopie, et le déclencheur de la
base refuse un témoin qui n'est pas ce SHA exact.

Trois états, et trois seulement :

* `confirmed` : le constat était fondé — avec un correctif (`correction_sha`),
  une régression liée (`regression_lot`, le lot fautif ; le journal suffit et
  aucun lien `work_item_links` n'est écrit tant qu'un lecteur N-1 le lirait en
  erreur) ou une autre preuve (`evidence`, note obligatoire) ;
* `refuted` : le constat ne tient pas — retrait de la remarque par son auteur
  (`withdrawal`, `withdrawn_by`) ou autre preuve ;
* `undecided` : aucune preuve ne tranche (`insufficient`). C'est aussi l'état
  d'un B sans résolution explicite : un B suivi d'un OK sans correction reste
  non tranché, jamais confirmé ni infirmé par déduction.

La résolution est un **journal additif** (`review_finding_resolutions`) :
chaque geste ajoute une ligne, l'état effectif est la dernière ligne du
constat. Une correction de jugement s'écrit par une nouvelle ligne ; la base
refuse les UPDATE. Aucun compte d'avis, aucune famille, aucune politique et
aucune porte de fusion ne sont modifiés : M3 est une observation. M1
(`model_evaluation_report`) lit l'état effectif pour montrer confirmé /
infirmé / non tranché.

L'**adjudicateur** doit être indépendant des familles jugées : les auteurs
prouvés au gel (la marque de désignation), le relecteur du B (le reçu de
provenance exact du jalon) et le correcteur éventuel. Un humain (`human:<id>`)
est indépendant par autorité ; un agent dont la famille est inconnue ou
présente parmi les familles jugées est refusé, jamais supposé indépendant.

La **correction par le relecteur** est un cas particulier : le relecteur qui
pousse lui-même le correctif devient auteur de la tête (§2, #2888). On
enregistre `corrector` et, en observation seulement, la projection
`review_correction.check` (`review_room`) : elle ne refuse pas la résolution
et ne touche ni au quorum ni aux familles.

Lectures et écritures ne lisent jamais un corps de courrier, un prompt ni un
secret : les seules preuves sont les jalons, la marque de désignation, le reçu
de provenance et le registre des agents.
"""
from __future__ import annotations

import json
import re
import sys
import time
from typing import Any, Mapping, Sequence

from . import registry, review_correction, storage, work
from . import review_designation_pg as designation_pg

#: le schéma rendu par `ameesh work findings --json`
SCHEMA = "ameesh-review-finding/1"
#: la table du journal additif (migration 0564)
TABLE = "review_finding_resolutions"
#: préfixe de la note de journal écrite sur le lot, lisible et requêtable
NOTE_PREFIX = "review-resolution:"

STATE_CONFIRMED = "confirmed"
STATE_REFUTED = "refuted"
STATE_UNDECIDED = "undecided"
STATES = (STATE_CONFIRMED, STATE_REFUTED, STATE_UNDECIDED)

BASIS_CORRECTION = "correction"
BASIS_REGRESSION = "regression"
BASIS_WITHDRAWAL = "withdrawal"
BASIS_EVIDENCE = "evidence"
BASIS_INSUFFICIENT = "insufficient"
BASES = (BASIS_CORRECTION, BASIS_REGRESSION, BASIS_WITHDRAWAL,
         BASIS_EVIDENCE, BASIS_INSUFFICIENT)

#: un SHA de commit, complet ou abrégé de façon usuelle ; le gel écrit complet
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
#: borne de la note écrite sur le lot (le journal n'est pas un rapport)
NOTE_MAX = 600


class ReviewFindingError(work.WorkError):
    """Un constat, une résolution ou une preuve n'est pas exploitable."""


# -- petites lectures -------------------------------------------------------

def _iso(ts: Any) -> str | None:
    """Un instant epoch en ISO 8601 UTC, ou None s'il n'est pas lisible."""
    if ts is None:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
    except (TypeError, ValueError, OverflowError):
        return None


def _item(db, lot: int) -> dict:
    item = work.get(db, int(lot))
    if item is None:
        raise ReviewFindingError("lot #%s not found" % int(lot))
    return item


def _milestone(db, milestone_id: int) -> dict | None:
    rows = db.query(
        "SELECT m.id, m.work_item_id, m.kind, m.verdict, m.sha, m.actor,"
        "       m.note, extract(epoch FROM m.at)::float8 AS at_ts"
        "  FROM work_item_milestones m WHERE m.id = %s",
        (int(milestone_id),))
    return dict(rows[0]) if rows else None


def _reviewer_family(db, milestone_id: int) -> str:
    """La famille exacte du reçu `milestone:<id>` ; "" sans preuve."""
    rows = db.query(
        "SELECT harness FROM provenance_receipts WHERE subject = %s",
        ("milestone:%d" % int(milestone_id),))
    return str((rows[0] or {}).get("harness") or "").strip().lower() if rows else ""


def _agent_family(db, name: str) -> str:
    """La famille vivante du registre, jamais déduite du nom (L242b)."""
    text = str(name or "").strip()
    if not text:
        return ""
    try:
        row = registry.get(db, text)
    except Exception:  # noqa: BLE001 - registre illisible : famille inconnue
        return ""
    return registry.live_harness(row).strip().lower() if row else ""


def _freeze_id_for(db, lot: int, sha: str) -> int | None:
    """Le gel dont le SHA est exactement celui du verdict (texte, pas préfixe)."""
    text = str(sha or "").strip().lower()
    if not text:
        return None
    rows = db.query(
        "SELECT id FROM work_item_milestones"
        " WHERE work_item_id = %s AND kind = 'frozen'"
        "   AND lower(btrim(sha)) = %s"
        " ORDER BY at DESC, id DESC LIMIT 1",
        (int(lot), text))
    return int(rows[0]["id"]) if rows else None


def _designation(db, lot: int, sha: str) -> Mapping[str, Any] | None:
    """La dernière marque de désignation du gel lu, ou None.

    La marque porte les auteurs prouvés et leurs familles (`unknown_authors`
    compris) : c'est la preuve historique, jamais le registre courant. Une
    marque illisible ne fait pas deviner ; le doute rend l'indépendance non
    prouvée.
    """
    freeze_id = _freeze_id_for(db, lot, sha)
    if freeze_id is None:
        return None
    try:
        marks = storage.of(db).work.events_noted(
            designation_pg.mark_prefix(int(lot), freeze_id), 0.0, ids=[int(lot)])
    except Exception:  # noqa: BLE001 - journal illisible : pas de preuve
        return None
    for row in reversed(list(marks or ())):
        try:
            found = designation_pg.designation_of((row or {}).get("note"))
        except designation_pg.DesignationError:
            continue
        if not isinstance(found, Mapping):
            continue
        freeze = found.get("freeze") if isinstance(found.get("freeze"), Mapping) else {}
        if int(freeze.get("id") or 0) == int(freeze_id):
            return found
    return None


def _entry_families(entry: Mapping | None) -> tuple[set[str], bool]:
    """Les familles d'une entrée auteur de marque, et le doute éventuel."""
    entry = entry or {}
    families = {str(entry.get("family") or "").strip().lower()}
    for value in entry.get("families") or ():
        families.add(str(value or "").strip().lower())
    families.discard("")
    return families, not families


def judged_families(db, lot: int, milestone: Mapping, *,
                    corrector: str = "") -> tuple[set[str], list[str]]:
    """Les familles dont l'adjudicateur doit être indépendant, et les trous.

    La preuve est la marque de désignation du gel lu : ses `authors` portent
    les familles prouvées, `unknown_authors` les attributions manquantes. Sans
    marque, on retombe sur l'auteur au gel du constat (le `freeze_id` borné au
    SHA du verdict, la même référence historique que la porte) ; un gel absent
    ou un auteur sans famille prouvée reste un trou, jamais une famille devinée
    (le gel le plus récent d'une autre famille ne réécrit pas l'auteur de la
    tête relue, #6669 B2). Le relecteur vient du reçu exact du jalon.
    """
    judged: set[str] = set()
    unknown: list[str] = []
    actor = str(milestone.get("actor") or "").strip()
    reviewer = _reviewer_family(db, int(milestone["id"]))
    if reviewer:
        judged.add(reviewer)
    else:
        unknown.append("relecteur %s" % (actor or "?"))

    mark = _designation(db, lot, str(milestone.get("sha") or ""))
    if mark is not None:
        authors_seen = False
        for entry in mark.get("authors") or ():
            authors_seen = True
            families, blind = _entry_families(entry)
            judged |= families
            if blind:
                unknown.append("auteur %s" % ((entry or {}).get("name") or "?"))
        for value in mark.get("unknown_authors") or ():
            text = str(value or "").strip()
            if text:
                authors_seen = True
                unknown.append("auteur %s" % text)
        if not authors_seen:
            unknown.append("auteur ?")
    else:
        freeze_id = _freeze_id_for(db, int(lot),
                                   str(milestone.get("sha") or ""))
        if freeze_id is None:
            # le verdict ne nomme aucun gel du lot : l'auteur n'est pas prouvé
            unknown.append("auteur ?")
        else:
            try:
                from . import review_lot
                author = review_lot._author_at_freeze(
                    db, int(lot), _item(db, lot), freeze_id=int(freeze_id))
            except Exception:  # noqa: BLE001 - lecture historique impossible
                author = {}
            harness = str((author or {}).get("harness") or "").strip().lower()
            if harness:
                judged.add(harness)
            else:
                unknown.append("auteur %s" % ((author or {}).get("name") or "?"))

    name = str(corrector or "").strip()
    if name and name != actor:
        family = _agent_family(db, name)
        if family:
            judged.add(family)
        else:
            unknown.append("correcteur %s" % name)
    return judged, unknown


def adjudication(db, adjudicator: str, judged: Sequence[str],
                 unknown: Sequence[str]) -> tuple[str, bool]:
    """La famille de l'adjudicateur et son indépendance prouvée.

    Un humain est indépendant par autorité (aucune famille). Un agent doit
    avoir une famille connue, hors des familles jugées et hors des trous : le
    doute ne dit jamais « indépendant ». Lève `ReviewFindingError` sinon.
    """
    name = str(adjudicator or "").strip()
    if name.startswith(work.HUMAN_PREFIX) and len(name) > len(work.HUMAN_PREFIX):
        return "", True
    if not name or name == work.UNKNOWN_ACTOR:
        raise ReviewFindingError(
            "adjudication refused: an identified adjudicator is required "
            "(an agent of another family, or --adjudicator human:<id>)")
    unknown = [str(value) for value in unknown or () if str(value or "").strip()]
    if unknown:
        raise ReviewFindingError(
            "adjudication refused: families judged are not all proven (%s) — "
            "independence would be guessed" % ", ".join(unknown))
    family = _agent_family(db, name)
    if not family:
        raise ReviewFindingError(
            "adjudication refused: family of %s is unknown — independence is "
            "not proven" % name)
    trials = {str(value).strip().lower() for value in judged or ()
              if str(value or "").strip()}
    if family in trials:
        raise ReviewFindingError(
            "adjudication refused: %s (%s) belongs to a judged family (%s)"
            % (name, family, ", ".join(sorted(trials))))
    return family, True


# -- résolution -------------------------------------------------------------

def _normalise_state(state: str) -> str:
    text = str(state or "").strip().lower()
    if text not in STATES:
        raise ReviewFindingError(
            "state must be one of %s (got %r)" % (", ".join(STATES), state))
    return text


def _basis_for(state: str, basis: str | None, *, correction_sha: str,
               regression_lot: int | None, withdrawn_by: str, note: str) -> str:
    """La base explicite ; sans base, la preuve fournie la nomme."""
    text = str(basis or "").strip().lower()
    if not text:
        if state == STATE_CONFIRMED:
            if correction_sha:
                text = BASIS_CORRECTION
            elif regression_lot is not None:
                text = BASIS_REGRESSION
            else:
                text = BASIS_EVIDENCE
        elif state == STATE_REFUTED:
            text = BASIS_WITHDRAWAL if withdrawn_by else BASIS_EVIDENCE
        else:
            text = BASIS_INSUFFICIENT
    if text not in BASES:
        raise ReviewFindingError(
            "basis must be one of %s (got %r)" % (", ".join(BASES), basis))
    allowed = {
        STATE_CONFIRMED: (BASIS_CORRECTION, BASIS_REGRESSION, BASIS_EVIDENCE),
        STATE_REFUTED: (BASIS_WITHDRAWAL, BASIS_EVIDENCE),
        STATE_UNDECIDED: (BASIS_INSUFFICIENT,),
    }[state]
    if text not in allowed:
        raise ReviewFindingError(
            "%s cannot have basis %s (one of %s)"
            % (state, text, ", ".join(allowed)))
    if text == BASIS_CORRECTION and not correction_sha:
        raise ReviewFindingError("basis correction requires --correction SHA")
    if text == BASIS_REGRESSION and regression_lot is None:
        raise ReviewFindingError("basis regression requires --regression LOT")
    if text == BASIS_WITHDRAWAL and not withdrawn_by:
        raise ReviewFindingError(
            "basis withdrawal requires --withdrawn-by (who withdrew the remark)")
    if text == BASIS_EVIDENCE and not note:
        raise ReviewFindingError("basis evidence requires --note (the evidence)")
    return text


def _sha_arg(value: str, what: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        raise ReviewFindingError("%s requires a commit id" % what)
    if not _SHA.fullmatch(text):
        raise ReviewFindingError(
            "%s: %r is not a commit id (7 to 64 hex characters)"
            % (what, value))
    return text


def _room_for_reviewer_correction(cfg, db, lot: int, milestone: Mapping,
                                  corrector: str) -> dict | None:
    """La projection de `review_correction.check`, en observation seulement.

    Un échec de lecture n'annule pas la résolution : il nomme l'absence. La
    projection ne refuse rien et ne change aucun compte.
    """
    actor = str(milestone.get("actor") or "").strip()
    if not corrector or corrector != actor or cfg is None:
        return None
    try:
        room = review_correction.check(cfg, db, int(lot), corrector, fetch=False)
        return dict(room) if isinstance(room, Mapping) else None
    except Exception as exc:  # noqa: BLE001 - observation, jamais un refus
        return {"state": review_correction.STATE_UNAVAILABLE,
                "why": "%s: %s" % (type(exc).__name__, exc)}


def _note_text(milestone_id: int, state: str, basis: str, *,
               correction_sha: str, regression_lot: int | None,
               corrector: str, withdrawn_by: str, adjudicator: str,
               note: str) -> str:
    parts = ["%s%d: %s (%s)" % (NOTE_PREFIX, int(milestone_id), state, basis)]
    details = []
    if correction_sha:
        details.append("correctif %s" % correction_sha[:12])
    if regression_lot is not None:
        details.append("régression #%d" % int(regression_lot))
    if corrector:
        details.append("correcteur %s" % corrector)
    if withdrawn_by:
        details.append("retrait par %s" % withdrawn_by)
    details.append("adjudicateur %s" % adjudicator)
    if note:
        details.append(note.strip())
    text = parts[0] + " — " + " ; ".join(details)
    return text[:NOTE_MAX]


def resolve(db, lot: int, milestone_id: int, *, state: str, witness: str = "",
            basis: str | None = None, correction_sha: str = "",
            regression_lot: int | None = None, corrector: str = "",
            withdrawn_by: str = "", adjudicator: str = "", note: str = "",
            cfg=None) -> dict:
    """Écrit une résolution explicite d'un constat, puis rend sa lecture.

    Le geste est **additif** : une nouvelle ligne du journal, jamais une
    réécriture. Le témoin (`witness`) doit être le SHA lu par le verdict ; il
    est écrit tel quel, en minuscules. Un correctif (`correction_sha`) ou un
    lot de régression (`regression_lot`, le lot fautif) nomme la preuve.
    L'adjudicateur est refusé s'il n'est pas indépendant des familles jugées.
    Aucun quorum, aucune famille et aucune porte ne sont modifiés.
    """
    item = _item(db, int(lot))
    if not journal_present(db):
        raise ReviewFindingError(
            "the finding resolution journal is absent: migration 0564 is not "
            "applied on this schema")
    milestone = _milestone(db, int(milestone_id))
    if milestone is None:
        raise ReviewFindingError("finding milestone #%s not found" % milestone_id)
    if int(milestone.get("work_item_id") or 0) != int(lot):
        raise ReviewFindingError(
            "milestone #%s belongs to lot #%s, not #%s"
            % (milestone_id, milestone.get("work_item_id"), lot))
    if str(milestone.get("kind") or "") != "verdict" \
            or str(milestone.get("verdict") or "") != "blocked":
        raise ReviewFindingError(
            "milestone #%s is not a blocked verdict: only a review finding "
            "(B) has a resolution" % milestone_id)

    read_sha = str(milestone.get("sha") or "").strip().lower()
    if not read_sha:
        raise ReviewFindingError(
            "finding #%s carries no read SHA: the witness cannot be proven"
            % milestone_id)
    seen = str(witness or "").strip().lower() or read_sha
    if seen != read_sha:
        raise ReviewFindingError(
            "witness %s is not the read SHA %s of finding #%s (the verdict "
            "names the head it read)" % (seen, read_sha, milestone_id))

    state = _normalise_state(state)
    correction_sha = str(correction_sha or "").strip().lower()
    correction_sha = _sha_arg(correction_sha, "--correction") if correction_sha else ""
    if regression_lot is not None:
        regression_lot = int(regression_lot)
        if regression_lot == int(lot):
            raise ReviewFindingError(
                "regression lot #%s is the finding's own lot" % regression_lot)
        _item(db, regression_lot)
    corrector = str(corrector or "").strip()
    withdrawn_by = str(withdrawn_by or "").strip()
    note = str(note or "").strip()
    basis = _basis_for(state, basis, correction_sha=correction_sha,
                       regression_lot=regression_lot,
                       withdrawn_by=withdrawn_by, note=note)
    if state != STATE_CONFIRMED and (correction_sha or regression_lot is not None
                                     or corrector):
        raise ReviewFindingError(
            "%s does not take a correction, a regression or a corrector"
            % state)
    if state == STATE_CONFIRMED and basis != BASIS_CORRECTION and correction_sha:
        raise ReviewFindingError("--correction goes with basis correction")
    if state == STATE_CONFIRMED and basis != BASIS_REGRESSION \
            and regression_lot is not None:
        raise ReviewFindingError("--regression goes with basis regression")
    if corrector and basis != BASIS_CORRECTION:
        raise ReviewFindingError("--corrector goes with basis correction")
    if withdrawn_by and basis != BASIS_WITHDRAWAL:
        raise ReviewFindingError("--withdrawn-by goes with basis withdrawal")

    judged, unknown = judged_families(db, int(lot), milestone, corrector=corrector)
    adjudicator_family, independent = adjudication(
        db, adjudicator, judged, unknown)
    room = _room_for_reviewer_correction(cfg, db, int(lot), milestone, corrector)

    with db.transaction() as tx:
        row = tx.query("SELECT state FROM work_items WHERE id = %s"
                       " FOR NO KEY UPDATE", (int(lot),))
        if not row:
            raise ReviewFindingError("lot #%s not found" % int(lot))
        # #6669 B1 (ameesh-cx2, auditeur) : aucun lien `work_item_links` n'est
        # écrit ici. `add_link` prend le verrou de graphe puis les lignes, à
        # l'inverse du verrou de lot tenu ci-dessus : un interblocage possible.
        # Le genre `regressed_by` casserait aussi un lecteur N-1
        # (`_INVERSE[kind]`), donc aucune valeur n'est ajoutée à la contrainte
        # de 0501 ; `regression_lot` du journal porte la preuve.
        inserted = tx.query(
            "INSERT INTO " + TABLE + " ("
            " milestone_id, work_item_id, witness_sha, state, basis,"
            " correction_sha, regression_lot, corrector, withdrawn_by,"
            " adjudicator, adjudicator_family, judged_families, independent,"
            " review_room, note)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            "         %s::jsonb, %s) RETURNING id",
            (int(milestone_id), int(lot), read_sha, state, basis,
             correction_sha or None, regression_lot, corrector, withdrawn_by,
             adjudicator, adjudicator_family, sorted(judged), bool(independent),
             json.dumps(room, ensure_ascii=False) if room is not None else None,
             note))
        if not inserted:
            raise ReviewFindingError("resolution was not written")
        storage.of(tx).work.note(
            int(lot), str(row[0]["state"]),
            _note_text(int(milestone_id), state, basis,
                       correction_sha=correction_sha,
                       regression_lot=regression_lot, corrector=corrector,
                       withdrawn_by=withdrawn_by, adjudicator=adjudicator,
                       note=note),
            adjudicator)
    out = read(db, int(lot), milestone_id=int(milestone_id))
    out["written"] = int(inserted[0]["id"])
    return out


# -- lecture ----------------------------------------------------------------

def journal_present(db) -> bool:
    """La table du journal 0564 existe-t-elle sur ce schéma ?

    Un schéma N-1 (code déployé avant la migration) reste lisible : les
    constats sont alors tous non tranchés, comme le rapport M1. Une écriture,
    elle, refuse clairement au lieu d'échouer sur une table absente.
    """
    try:
        rows = db.query(
            "SELECT to_regclass(%s) IS NOT NULL AS present", (TABLE,))
    except Exception:  # noqa: BLE001 - lecture d'un schéma en retard
        return False
    return bool((rows[0] or {}).get("present")) if rows else False


def _latest_resolutions(db, lot: int) -> dict[int, dict]:
    rows = db.query(
        "SELECT *, extract(epoch FROM created_at)::float8 AS created_ts"
        "  FROM " + TABLE + " WHERE work_item_id = %s ORDER BY id",
        (int(lot),))
    latest: dict[int, dict] = {}
    for row in rows or ():
        latest[int(row["milestone_id"])] = dict(row)
    return latest


def _verdict_rows(db, lot: int) -> list[dict]:
    return [dict(row) for row in db.query(
        "SELECT m.id, m.sha, m.actor, m.verdict, m.note,"
        "       extract(epoch FROM m.at)::float8 AS at_ts"
        "  FROM work_item_milestones m"
        " WHERE m.work_item_id = %s AND m.kind = 'verdict'"
        " ORDER BY m.at, m.id", (int(lot),)) or ()]


def read(db, lot: int, *, milestone_id: int | None = None) -> dict:
    """Les constats du lot et leur état effectif, sans rien déduire d'un OK."""
    item = _item(db, int(lot))
    verdicts = _verdict_rows(db, int(lot))
    blocked = [row for row in verdicts
               if str(row.get("verdict") or "") == "blocked"]
    held = journal_present(db)
    latest = _latest_resolutions(db, int(lot)) if held else {}
    findings = []
    for index, row in enumerate(blocked):
        if milestone_id is not None and int(row["id"]) != int(milestone_id):
            continue
        # le prochain verdict du lot après ce constat, dans l'ordre du journal
        following = None
        for candidate in verdicts:
            if (float(candidate.get("at_ts") or 0), int(candidate["id"])) \
                    > (float(row.get("at_ts") or 0), int(row["id"])):
                following = candidate
                break
        resolution = latest.get(int(row["id"]))
        if resolution is None:
            state = STATE_UNDECIDED
        else:
            state = str(resolution.get("state") or STATE_UNDECIDED)
            if state in (STATE_CONFIRMED, STATE_REFUTED) \
                    and not bool(resolution.get("independent")):
                # la base l'interdit déjà ; un lecteur ne compte jamais une
                # résolution non indépendante comme tranchée
                state = STATE_UNDECIDED
        actor = str(row.get("actor") or "")
        corrector = str((resolution or {}).get("corrector") or "")
        findings.append({
            "milestone_id": int(row["id"]),
            "lot": int(lot),
            "reviewer": actor,
            "reviewer_family": _reviewer_family(db, int(row["id"])),
            "witness_sha": str(row.get("sha") or ""),
            "verdict": "blocked",
            "read_at": _iso(row.get("at_ts")),
            "note": str(row.get("note") or ""),
            "next_verdict": ({
                "milestone_id": int(following["id"]),
                "verdict": str(following.get("verdict") or ""),
                "sha": str(following.get("sha") or ""),
            } if following is not None else None),
            "state": state,
            "resolved": state != STATE_UNDECIDED,
            "explicit": resolution is not None,
            "basis": (resolution or {}).get("basis"),
            "correction_sha": (resolution or {}).get("correction_sha"),
            "regression_lot": ((int(resolution["regression_lot"])
                                if (resolution or {}).get("regression_lot")
                                is not None else None)),
            "corrector": corrector,
            "reviewer_correction": bool(corrector and corrector == actor),
            "withdrawn_by": str((resolution or {}).get("withdrawn_by") or ""),
            "adjudicator": (resolution or {}).get("adjudicator"),
            "adjudicator_family": str(
                (resolution or {}).get("adjudicator_family") or ""),
            "independent": (resolution or {}).get("independent"),
            "review_room": (resolution or {}).get("review_room"),
            "resolution_note": str((resolution or {}).get("note") or ""),
            "created_at": _iso((resolution or {}).get("created_ts")),
            "resolution_id": ((resolution or {}).get("id")),
        })
    counts = {"total": len(findings), STATE_CONFIRMED: 0,
              STATE_REFUTED: 0, STATE_UNDECIDED: 0, "resolved": 0}
    for finding in findings:
        counts[finding["state"]] += 1
        if finding["state"] != STATE_UNDECIDED:
            counts["resolved"] += 1
    out = {
        "schema": SCHEMA,
        "lot": int(lot),
        "title": str(item.get("title") or ""),
        "journal": "present" if held else "absent",
        "counts": counts,
        "findings": findings,
    }
    if milestone_id is not None:
        out["milestone_id"] = int(milestone_id)
        if not findings:
            raise ReviewFindingError(
                "no blocked verdict #%s on lot #%s" % (milestone_id, lot))
    return out


def format_text(value: Mapping[str, Any]) -> str:
    """Le rendu lisible de `read` : un constat par ligne, état explicite."""
    counts = value.get("counts") or {}
    lines = ["Constats de revue du lot #%s : %s (%s confirmé(s), %s infirmé(s), "
             "%s non tranché(s))"
             % (value.get("lot"), counts.get("total"),
                counts.get(STATE_CONFIRMED), counts.get(STATE_REFUTED),
                counts.get(STATE_UNDECIDED))]
    for finding in value.get("findings") or ():
        line = "  #%s  %-9s  B de %s  tête %s" % (
            finding.get("milestone_id"), finding.get("state"),
            finding.get("reviewer") or "?",
            (finding.get("witness_sha") or "")[:12] or "?")
        details = []
        if finding.get("basis"):
            details.append(str(finding["basis"]))
        if finding.get("correction_sha"):
            details.append("correctif %s" % str(finding["correction_sha"])[:12])
        if finding.get("regression_lot") is not None:
            details.append("régression #%s" % finding["regression_lot"])
        if finding.get("reviewer_correction"):
            details.append("corrigé par le relecteur")
        if finding.get("withdrawn_by"):
            details.append("retiré par %s" % finding["withdrawn_by"])
        if finding.get("adjudicator"):
            details.append("adjudiqué par %s (%s)"
                           % (finding["adjudicator"],
                              finding.get("adjudicator_family") or "humain"))
        if details:
            line += "  — " + " ; ".join(details)
        lines.append(line)
    if value.get("journal") == "absent":
        lines.append("Journal des résolutions absent (migration 0564 non "
                     "appliquée) : ces constats restent non tranchés.")
    lines.append("M3 : aucune résolution ne change le quorum, les familles ni "
                 "la porte de fusion.")
    return "\n".join(lines)


# -- CLI --------------------------------------------------------------------

def add_parsers(sub, func) -> None:
    parser = sub.add_parser(
        "findings",
        help="#6669: review findings (B) and their explicit resolutions "
             "(confirmed/refuted/undecided)")
    parser.add_argument("id", type=int, help="the reviewed lot")
    parser.add_argument("--resolve", type=int, default=None, metavar="MILESTONE",
                        help="write a resolution for this blocked verdict "
                             "(milestone id); without it, only read")
    parser.add_argument("--state", choices=STATES,
                        help="with --resolve: confirmed, refuted or undecided")
    parser.add_argument("--witness", default="",
                        help="the head actually read (default: the verdict's "
                             "SHA; it must match exactly)")
    parser.add_argument("--basis", choices=BASES,
                        help="the evidence kind (default: named by the "
                             "evidence given, insufficient for undecided)")
    parser.add_argument("--correction", default="",
                        help="the correction commit (basis correction)")
    parser.add_argument("--regression", type=int, default=None, metavar="LOT",
                        help="the regression lot (basis regression; the "
                             "journal carries it, no typed link is written)")
    parser.add_argument("--corrector", default="",
                        help="who pushed the correction (a reviewer's own "
                             "correction is recorded, never blocked)")
    parser.add_argument("--withdrawn-by", default="",
                        help="who withdrew the remark (basis withdrawal)")
    parser.add_argument("--note", default="",
                        help="the evidence, short and free of secrets")
    parser.add_argument("--adjudicator", default="",
                        help="the independent adjudicator (default: the bound "
                             "session; a human says human:<id>)")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=func)


def run(cfg, db, args) -> int:
    try:
        if args.resolve is None:
            if args.state or args.witness or args.basis or args.correction \
                    or args.regression is not None or args.corrector \
                    or args.withdrawn_by or args.note or args.adjudicator:
                raise ReviewFindingError(
                    "resolution options need --resolve MILESTONE")
            value = read(db, args.id)
        else:
            if not args.state:
                raise ReviewFindingError("--resolve needs --state")
            adjudicator = work.milestone_actor(
                cfg, db, args.adjudicator, kind="finding resolution",
                what="finding resolution")
            value = resolve(
                db, args.id, args.resolve, state=args.state,
                witness=args.witness, basis=args.basis,
                correction_sha=args.correction,
                regression_lot=args.regression, corrector=args.corrector,
                withdrawn_by=args.withdrawn_by, adjudicator=adjudicator,
                note=args.note, cfg=cfg)
        if args.json:
            print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(format_text(value))
        return 0
    except ReviewFindingError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except work.WorkError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
