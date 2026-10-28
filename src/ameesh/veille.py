# SPDX-License-Identifier: AGPL-3.0-only
"""#4293 (#3264 C) — adoption d'une version de harnais ou de modèle.

Le lot B (#4292) produit un **rapport de rejeu** ; ce module applique la règle
de l'owner (spec `docs/design/3264-veille-versions.md` §4) :

* conclusion « meilleure » ou « équivalente », aucune régression de qualité,
  **prix égal** : adoption automatique ;
* modèle plus cher, nouveau fournisseur ou changement de forfait : une
  **décision de l'owner** (`ameesh decide`) avec le rapport et le coût projeté
  sur 7 jours, sans aucun changement d'hôte ;
* régression : aucune adoption.

L'adoption automatique se fait en deux temps : un **canari** (un agent du
harnais, 24 h ou 10 tours, mesures de coût et de durée dans les bornes du
rejeu), puis la bascule **hôte par hôte**, entre deux tours et jamais dans les
20 minutes qui précèdent une coupure. Une régression en canari remet le
binôme/modèle précédent et clôt l'adoption ; le geste inverse est consigné dans
le lot.

État : la table `harness_adoptions` (migration 0505) porte la séquence ; chaque
transition est aussi notée au journal du lot (`veille-adoption:`), pour la
relecture. Les rapports sont figés avec leur empreinte : un second rapport sur
le même lot est refusé (une nouvelle évaluation est un nouveau lot).

Geste de bascule : `agent_registry.harness_bin` pour un harnais, `model` pour
un modèle. L'exécuteur relit la ligne de l'agent au début de chaque tour ; un
tour en cours ne change donc jamais de binaire sous lui. Le binaire nommé doit
exister sur l'hôte (l'exécuteur refuse un binaire absent : hôte non prêt) ;
l'installation de la nouvelle version reste le fait de l'hôte (L17), ce module
ne l'exécute pas.

  ameesh veille adopt <lot> --report FICHIER|- [--bin [HÔTE=]CHEMIN …]
                            [--agent NOM] [--requester NOM] [--dry-run]
  ameesh veille tick [--project P] [--now EPOCH] [--bin [HÔTE=]CHEMIN …]
  ameesh veille show <lot>
  ameesh veille rollback <lot> --reason TEXTE
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time

from . import config as config_mod
from . import db as db_mod
from . import decisions, registry, storage, work

#: schéma du rapport produit par le lot B
REPORT_SCHEMA = "ameesh-veille-report/1"
#: schéma de l'état d'une adoption
STATE_SCHEMA = "ameesh-veille-adoption/1"
#: préfixe des notes de journal (état lisible par un humain)
NOTE_PREFIX = "veille-adoption:"
#: préfixe des notes qui portent le rapport machine du lot B
REPORT_PREFIX = "veille-report:"
#: durées de la règle owner (spec §4) : 24 h ou 10 tours
CANARY_SECONDS = 24 * 3600
CANARY_TURNS = 10
#: marge avant une coupure de release (spec §4, canon §7) : 20 minutes
CUTOFF_MARGIN_S = 20 * 60
#: objets adoptables
OBJECTS = ("harness", "model")
#: conclusions du rapport de rejeu
CONCLUSIONS = ("better", "equivalent", "regression")
#: natures de prix
PRICE_KINDS = ("equal", "higher", "new_provider", "plan_change")
#: étapes d'une adoption
STAGES = ("canary", "rollout", "done", "refused", "waiting_owner",
          "blocked", "rolled_back")
#: étapes terminales
TERMINAL = ("done", "refused", "rolled_back")
#: identifiant de verrou consultatif (un seul tick/geste à la fois par base)
LOCK_KEY = 0x7665696C6C65  # « veille »
#: un identifiant court (harnais, agent, modèle)
_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,119}$")
#: une version de harnais ou un identifiant de modèle : pas d'espace ni de
#: saut de ligne (une valeur passée telle quelle à l'exécuteur)
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,119}$")


class VeilleError(RuntimeError):
    """Rapport incomplet, état incohérent, geste refusé : rien n'est changé."""


def _number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VeilleError("%s: a number is required (got %r)" % (name, value))
    return float(value)


def _text(value, name: str, *, pattern=_IDENT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VeilleError("%s: a non-empty string is required" % name)
    value = value.strip()
    if not pattern.match(value):
        raise VeilleError("%s: invalid value %r" % (name, value))
    return value


def _rate_table(value, name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise VeilleError("%s: an object of rates is required" % name)
    out = {}
    for key, rate in value.items():
        if key in ("provider", "plan"):
            out[key] = str(rate)
        elif isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate < 0:
            raise VeilleError("%s.%s: a non-negative number is required" % (name, key))
        else:
            out[key] = float(rate)
    return out


def _bindings(value) -> dict:
    """Bornes du canari : `{cost_usd: [min, max], duration_s: [min, max]}`."""
    if not isinstance(value, dict):
        raise VeilleError("bounds: an object is required")
    out = {}
    for key in ("cost_usd", "duration_s", "quality_note"):
        if key not in value:
            continue
        pair = value[key]
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in pair)):
            raise VeilleError("bounds.%s: [min, max] numbers are required" % key)
        low, high = float(pair[0]), float(pair[1])
        if low > high:
            raise VeilleError("bounds.%s: min > max" % key)
        out[key] = [low, high]
    if not (out.get("cost_usd") or out.get("duration_s")):
        raise VeilleError("bounds: cost_usd or duration_s is required to judge "
                          "the canary")
    return out


class Report:
    """Le rapport de rejeu du lot B, relu strictement.

    Les noms sont le contrat avec B (#4292) ; un champ inconnu est conservé
    dans `raw` (audit) mais ne change aucune décision.
    """

    def __init__(self, raw: dict):
        if not isinstance(raw, dict):
            raise VeilleError("report: a JSON object is required")
        self.raw = dict(raw)
        schema = raw.get("schema")
        if schema != REPORT_SCHEMA:
            raise VeilleError("report: schema %r required (got %r)"
                              % (REPORT_SCHEMA, schema))
        lot = raw.get("evaluation_lot")
        if isinstance(lot, bool) or not isinstance(lot, int) or lot <= 0:
            raise VeilleError("report.evaluation_lot: a work item id is required")
        self.evaluation_lot = int(lot)
        self.harness = _text(raw.get("harness"), "report.harness")
        self.object = str(raw.get("object") or "harness")
        if self.object not in OBJECTS:
            raise VeilleError("report.object: %r (harness | model)" % self.object)
        self.from_version = _text(raw.get("from_version"), "report.from_version",
                                  pattern=_VERSION)
        self.to_version = _text(raw.get("to_version"), "report.to_version",
                                pattern=_VERSION)
        if self.from_version == self.to_version:
            raise VeilleError("report: from_version and to_version are identical")
        self.model = _optional_text(raw.get("model"), "report.model")
        self.provider = _optional_text(raw.get("provider"), "report.provider")
        self.effort = _optional_text(raw.get("effort"), "report.effort")
        self.conclusion = str(raw.get("conclusion") or "")
        if self.conclusion not in CONCLUSIONS:
            raise VeilleError("report.conclusion: %r (%s)"
                              % (self.conclusion, " | ".join(CONCLUSIONS)))
        quality = raw.get("quality") or {}
        if not isinstance(quality, dict):
            raise VeilleError("report.quality: an object is required")
        regression = quality.get("regression", False)
        if not isinstance(regression, bool):
            raise VeilleError("report.quality.regression: a boolean is required")
        self.quality_regression = regression
        self.quality_note = None
        if quality.get("note") is not None:
            self.quality_note = _number(quality["note"], "report.quality.note")
            if not 0 <= self.quality_note <= 10:
                raise VeilleError("report.quality.note: 0 to 10")
        defects = quality.get("defects") or []
        if not isinstance(defects, list):
            raise VeilleError("report.quality.defects: a list is required")
        self.defects = [str(item) for item in defects]
        price = raw.get("price")
        if not isinstance(price, dict):
            raise VeilleError("report.price: an object is required")
        self.price_kind = str(price.get("kind") or "")
        if self.price_kind not in PRICE_KINDS:
            raise VeilleError("report.price.kind: %r (%s)"
                              % (self.price_kind, " | ".join(PRICE_KINDS)))
        self.price_from = _rate_table(price.get("from"), "report.price.from")
        self.price_to = _rate_table(price.get("to"), "report.price.to")
        projected = price.get("projected_7d_usd")
        self.projected_7d_usd = (None if projected is None
                                 else _number(projected, "report.price.projected_7d_usd"))
        self.bounds = _bindings(raw.get("bounds"))
        bench = raw.get("bench") or []
        if not isinstance(bench, list):
            raise VeilleError("report.bench: a list is required")
        self.bench = bench

    # -- dérivés ---------------------------------------------------------
    @property
    def digest(self) -> str:
        payload = json.dumps(self.raw, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def effective_price(self) -> str:
        """La nature de prix, recalculée quand les tarifs la contredisent.

        Un rapport qui dit « égal » alors que le tarif de sortie augmente est
        une augmentation : on ne l'adopte pas sans l'owner. Un fournisseur ou
        un forfait qui change est nommé comme tel.
        """
        frm, to = self.price_from, self.price_to
        if (frm.get("provider") and to.get("provider")
                and frm["provider"] != to["provider"]):
            return "new_provider"
        if frm.get("plan") and to.get("plan") and frm["plan"] != to["plan"]:
            return "plan_change"
        for key in ("input", "cached_input", "output", "cache_write"):
            if key in frm and key in to and to[key] > frm[key]:
                return "higher"
        if self.price_kind != "equal":
            return self.price_kind
        return "equal"


def _optional_text(value, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def parse_report(text: str) -> Report:
    """Un rapport depuis un JSON seul, une ligne `veille-report:` ou un bloc
    ```json d'une note de lot."""
    text = (text or "").strip()
    if not text:
        raise VeilleError("empty report")
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise VeilleError("report: invalid JSON (%s)" % exc) from exc
        return Report(data)
    marker = text.find(REPORT_PREFIX)
    if marker >= 0:
        after = text[marker + len(REPORT_PREFIX):].lstrip()
        end = after.find("\n```")
        candidate = after if end < 0 else after[:end]
        try:
            return Report(json.loads(candidate.strip()))
        except (ValueError, VeilleError):
            pass
    for match in re.finditer(r"```json\s*(\{.*?\})\s*```", text, re.S):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("schema") == REPORT_SCHEMA:
            return Report(data)
    raise VeilleError("report: no %s document found" % REPORT_SCHEMA)


def report_from_lot(db, lot: int) -> Report:
    """Le rapport le plus récent noté sur le lot (journal, du plus récent au
    plus ancien), sinon dans son corps. Aucun fichier local n'est supposé."""
    for event in work.events(db, int(lot), limit=None):
        text = str(event.get("note") or "")
        if REPORT_PREFIX in text or REPORT_SCHEMA in text:
            try:
                return parse_report(text)
            except VeilleError:
                continue
    item = work.get(db, int(lot)) or {}
    body = str(item.get("body") or "")
    if REPORT_PREFIX in body or REPORT_SCHEMA in body:
        return parse_report(body)
    raise VeilleError("lot #%s: no readable %s note (write it with "
                      "`ameesh work note %s -`, or pass --report FILE)"
                      % (lot, REPORT_SCHEMA, lot))


# --------------------------------------------------------------------------
# état
# --------------------------------------------------------------------------

def _state_defaults(report: Report, *, project: str, now: float) -> dict:
    return {
        "schema": STATE_SCHEMA,
        "lot": report.evaluation_lot,
        "project": project,
        "harness": report.harness,
        "object": report.object,
        "from_version": report.from_version,
        "to_version": report.to_version,
        "stage": "blocked",
        "report_digest": report.digest,
        "started_ts": round(float(now), 3),
        "target_agent": None,
        "canary_host": None,
        "canary_bin": None,
        "canary_before": None,
        "deadline_ts": None,
        "decision_id": None,
        "bins": {},
        "hosts": [],
        "next_host": 0,
        "applied": [],
        "metrics": None,
        "inverse": {"version": report.from_version},
        "reason": "",
        "blocked_from": None,
        "updated_ts": round(float(now), 3),
    }


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row_state(row: dict) -> dict:
    state = row.get("state")
    if isinstance(state, str):
        try:
            state = json.loads(state)
        except ValueError as exc:
            raise VeilleError("adoption #%s: unreadable state (%s)" % (row["lot_id"], exc))
    if not isinstance(state, dict):
        raise VeilleError("adoption #%s: state is not an object" % row["lot_id"])
    if state.get("schema") != STATE_SCHEMA:
        raise VeilleError("adoption #%s: unknown state schema %r"
                          % (row["lot_id"], state.get("schema")))
    if state.get("stage") not in STAGES:
        raise VeilleError("adoption #%s: unknown stage %r"
                          % (row["lot_id"], state.get("stage")))
    row["state"] = state
    if isinstance(row.get("report"), str):
        try:
            row["report"] = json.loads(row["report"])
        except ValueError:
            row["report"] = {}
    return row


def load(db, lot: int) -> dict | None:
    rows = db.query(
        """
        SELECT lot_id, project, harness, object, from_version, to_version,
               stage, report, report_digest, state,
               extract(epoch from updated_at)::float8 AS updated_ts
          FROM harness_adoptions WHERE lot_id = %s
        """, (int(lot),))
    return _row_state(rows[0]) if rows else None


def open_adoptions(db, *, project: str | None = None) -> list[dict]:
    rows = db.query(
        """
        SELECT lot_id, project, harness, object, from_version, to_version,
               stage, report, report_digest, state,
               extract(epoch from updated_at)::float8 AS updated_ts
          FROM harness_adoptions
         WHERE stage NOT IN ('done', 'refused', 'rolled_back')
           AND (%s::text IS NULL OR project = %s)
         ORDER BY lot_id
        """, (project, project))
    return [_row_state(row) for row in rows]


def save(db, row: dict, state: dict) -> None:
    state["updated_ts"] = round(time.time(), 3)
    db.execute(
        """
        INSERT INTO harness_adoptions
               (lot_id, project, harness, object, from_version, to_version,
                stage, report, report_digest, state)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb)
        ON CONFLICT (lot_id) DO UPDATE
           SET stage = EXCLUDED.stage,
               report = EXCLUDED.report,
               report_digest = EXCLUDED.report_digest,
               state = EXCLUDED.state,
               updated_at = clock_timestamp()
        """,
        (int(state["lot"]), state.get("project") or "", state["harness"],
         state["object"], state["from_version"], state["to_version"],
         state["stage"], _json(row.get("report") or {}), state["report_digest"],
         _json(state)))


def journal(db, lot: int, state: dict, text: str, *, actor: str = "ameesh") -> None:
    """Trace lisible de la transition ; l'état machine reste la table."""
    if not text:
        return
    try:
        work.note(db, int(lot), "%s %s %s" % (NOTE_PREFIX, state["stage"], text),
                  actor=actor or "ameesh")
    except Exception as exc:  # noqa: BLE001 — une note ne défait pas un geste fait
        print("veille: lot #%s: note not written (%s)" % (lot, exc), file=sys.stderr)


def _lock(db) -> bool:
    try:
        rows = db.query("SELECT pg_try_advisory_lock(%s) AS ok", (LOCK_KEY,))
        return bool(rows and rows[0].get("ok"))
    except Exception:  # noqa: BLE001 — sans verrou, on ne touche à rien
        return False


def _unlock(db) -> None:
    try:
        db.query("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    except Exception:  # noqa: BLE001
        pass


def _journal(row: dict, state: dict, text: str, actor: str) -> None:
    journal(row["_db"], row["lot_id"], state, text, actor=actor)


def _block(row: dict, state: dict, reason: str, *, from_stage: str,
           actor: str = "ameesh") -> bool:
    """Passe en `blocked`, sans réécrire un blocage identique (le tick tourne
    toutes les 5 minutes : une carte de binaires absente ne doit pas noyer le
    journal). Rend True si l'état a changé."""
    if (state.get("stage") == "blocked"
            and state.get("blocked_from") == from_stage
            and state.get("reason") == reason):
        return False
    state.update({"stage": "blocked", "blocked_from": from_stage, "reason": reason})
    save(row["_db"], row, state)
    _journal(row, state, "blocked (%s): %s" % (from_stage, reason), actor=actor)
    return True


# --------------------------------------------------------------------------
# décision (règle du §4)
# --------------------------------------------------------------------------

class Decision:
    """Ce que la règle fait du rapport : automatique, owner, refus, blocage."""

    def __init__(self, action: str, price: str, reason: str = ""):
        self.action = action
        self.price = price
        self.reason = reason


def decide(report: Report) -> Decision:
    """La règle owner, sans effet de bord."""
    price = report.effective_price()
    if report.conclusion == "regression" or report.quality_regression:
        why = "regression"
        if report.conclusion == "regression" and report.quality_regression:
            why = "conclusion and quality regression"
        elif report.conclusion == "regression":
            why = "replay conclusion is regression"
        else:
            why = "quality regression (note/defects)"
        return Decision("refused", price, why)
    if price != "equal":
        if report.projected_7d_usd is None:
            return Decision("blocked", price,
                            "price is %s but projected_7d_usd is missing: the owner "
                            "decision must carry the 7-day projection" % price)
        return Decision("owner", price, "price is %s" % price)
    return Decision("automatic", price, "")


# --------------------------------------------------------------------------
# gestes : lire/poser la valeur d'un agent
# --------------------------------------------------------------------------

def _value_key(object_name: str) -> str:
    return "harness_bin" if object_name == "harness" else "model"


def _agent_value(agent: dict, object_name: str):
    return agent.get(_value_key(object_name))


def _set_agent_value(db, name: str, object_name: str, value: str | None) -> None:
    if object_name == "harness":
        ok = storage.of(db).operations.set_settings(
            name, {"harness_bin": value or ""})
        if not ok:
            raise VeilleError("agent %s: harness_bin not written (unknown agent)" % name)
    else:
        rows = db.query(
            "UPDATE agent_registry SET model = nullif(%s, ''), "
            "updated_at = clock_timestamp() WHERE name = %s RETURNING name",
            (value or "", name))
        if not rows:
            raise VeilleError("agent %s: model not written (unknown agent)" % name)


def _agents(db, harness: str, *, host: str | None = None) -> list[dict]:
    where = ("harness = %s AND mode = 'execute' AND status <> 'stopped' "
             "AND retired_at IS NULL")
    params = [harness]
    if host is not None:
        where += " AND host = %s"
        params.append(host)
    return db.query(
        "SELECT name, host, status, model, harness_bin FROM agent_registry "
        "WHERE " + where + " ORDER BY host, name", tuple(params))


def _running(db, harness: str, host: str) -> list[str]:
    rows = db.query(
        "SELECT name FROM agent_registry WHERE harness = %s AND host = %s "
        "AND status = 'running' AND retired_at IS NULL ORDER BY name",
        (harness, host))
    return [row["name"] for row in rows]


def _apply_to_agents(db, agents: list[dict], object_name: str,
                     value: str | None) -> list[dict]:
    """Pose la valeur sur ces agents ; rend l'inverse exact (nom, ancienne)."""
    inverse = []
    for agent in agents:
        before = _agent_value(agent, object_name)
        _set_agent_value(db, agent["name"], object_name, value)
        inverse.append({"name": agent["name"], "before": before})
    return inverse


def _restore(db, entries: list[dict], object_name: str) -> None:
    """Restaure dans l'ordre inverse (une bascule de canari est incluse dans
    les entrées d'hôte ; le retour doit retrouver la valeur la plus ancienne)."""
    for entry in reversed(entries):
        for agent in reversed(entry.get("agents") or []):
            _set_agent_value(db, agent["name"], object_name, agent.get("before"))


# --------------------------------------------------------------------------
# rapport / bornes
# --------------------------------------------------------------------------

def measure(db, agent: str, since: float) -> dict:
    """Les tours terminés de l'agent depuis `since` : nombre, coût médian et
    durée médiane (secondes). Une mesure absente vaut None, jamais zéro."""
    rows = db.query(
        """
        SELECT count(*)::int AS turns,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY usd)::float8 AS cost_usd,
               percentile_cont(0.5) WITHIN GROUP (
                   ORDER BY extract(epoch FROM (recorded_at - started_at)))::float8
                   AS duration_s
          FROM turn_costs
         WHERE agent = %s AND recorded_at >= to_timestamp(%s)
           AND (void_reason IS NULL OR void_reason = '')
        """, (agent, float(since)))
    row = rows[0] if rows else {}
    return {"turns": int(row.get("turns") or 0),
            "cost_usd": row.get("cost_usd"),
            "duration_s": row.get("duration_s"),
            "since": round(float(since), 3)}


def canary_verdict(report: Report, metrics: dict) -> tuple[bool, list[str]]:
    """Les mesures du canari restent-elles dans les bornes du rejeu ?"""
    failures = []
    if int(metrics.get("turns") or 0) <= 0:
        return False, ["no measured turn"]
    for key in ("cost_usd", "duration_s"):
        bound = report.bounds.get(key)
        if not bound:
            continue
        value = metrics.get(key)
        if value is None:
            failures.append("no %s measure" % key)
        elif value < bound[0] or value > bound[1]:
            failures.append("%s %.6g outside [%.6g, %.6g]"
                            % (key, value, bound[0], bound[1]))
    return (not failures), failures


# --------------------------------------------------------------------------
# canari et bascule
# --------------------------------------------------------------------------

def _pick_canary(db, report: Report, *, bins: dict | None, agent: str | None) -> dict:
    if agent:
        row = registry.get(db, agent)
        if row is None:
            raise VeilleError("canary agent %r does not exist" % agent)
        if (row.get("harness") or "") != report.harness:
            raise VeilleError("canary agent %s is %s, not %s"
                              % (agent, row.get("harness"), report.harness))
        if (row.get("mode") or "execute") != "execute":
            raise VeilleError("canary agent %s is external: no turn to measure" % agent)
        if row.get("status") == "stopped":
            raise VeilleError("canary agent %s is stopped" % agent)
        if row.get("status") == "running":
            raise VeilleError("canary agent %s is in a turn: retry between turns"
                              % agent)
        return row
    candidates = _agents(db, report.harness)
    # #4293 : le canari se pose ENTRE deux tours. Un agent qui tourne n'est
    # pas candidat (son tour finirait sur l'ancienne version et sa mesure
    # serait comptée) ; les autres attendent le prochain tick.
    candidates = [row for row in candidates if row.get("status") != "running"]
    if not candidates:
        raise VeilleError("no idle agent of harness %s: canary waits for the end "
                          "of a turn" % report.harness)
    if report.object == "harness" and bins:
        candidates = [row for row in candidates if bins.get(row.get("host") or "")]
        if not candidates:
            raise VeilleError("no agent of %s on a host with a --bin path"
                              % report.harness)
    # un agent au repos d'abord, puis le nom : déterministe et stable
    candidates.sort(key=lambda row: (row.get("status") != "idle",
                                     row.get("host") or "", row.get("name") or ""))
    return candidates[0]


def _bins_from_state(state: dict) -> dict:
    value = state.get("bins")
    return dict(value) if isinstance(value, dict) else {}


def _merge_bins(state: dict, bins: dict | None) -> dict:
    merged = _bins_from_state(state)
    for host, path in (bins or {}).items():
        merged[str(host)] = str(path)
    state["bins"] = merged
    return merged


def _check_bin(path: str | None) -> str:
    if not path:
        raise VeilleError("a --bin path is required for a harness adoption")
    if "\x00" in path or "\n" in path or not os.path.isabs(path):
        raise VeilleError("invalid bin path %r: an absolute path is required" % path)
    return path


def start_canary(db, row: dict, state: dict, report: Report, *,
                 agent: str | None = None, now: float) -> dict:
    """Démarre le canari : un seul agent passe sur la nouvelle version."""
    target = _pick_canary(db, report, bins=_bins_from_state(state), agent=agent)
    host = target.get("host") or ""
    value: str | None = None
    bin_path = None
    if report.object == "harness":
        bin_path = _check_bin(_bins_from_state(state).get(host))
        value = bin_path
    else:
        value = report.to_version
    before = _agent_value(target, report.object)
    state.update({
        "stage": "canary",
        "project": row.get("project") or state.get("project") or "",
        "target_agent": target["name"],
        "canary_host": host,
        "canary_bin": bin_path,
        "canary_before": before,
        "started_ts": round(float(now), 3),
        "deadline_ts": round(float(now) + CANARY_SECONDS, 3),
        "reason": "",
        "blocked_from": None,
    })
    state["applied"] = [{"host": host, "at": round(float(now), 3),
                         "canary": True,
                         "agents": [{"name": target["name"], "before": before}]}]
    # la bascule et son état sont UNE écriture : une panne entre les deux ne
    # laisse pas un agent sur la nouvelle version sans canari qui le suit.
    with db.transaction() as tx:
        _set_agent_value(tx, target["name"], report.object, value)
        save(tx, row, state)
    _journal(row, state, "canary started: %s on %s (%s)"
             % (target["name"], host, value), actor="ameesh")
    return state


def _hosts(db, report: Report, state: dict) -> list[str]:
    hosts = [row["host"] for row in _agents(db, report.harness)
             if row.get("host")]
    ordered = sorted(set(hosts))
    canary_host = state.get("canary_host")
    if canary_host in ordered:
        ordered.remove(canary_host)
        ordered.insert(0, canary_host)
    return ordered


def _cutoff_seconds(cfg, project: str, now: float, rules=None) -> float:
    if rules is None:
        from . import release_cutoff
        rules, _source = release_cutoff.compiled_rules(cfg, project)
    return float(rules.next_cutoff(float(now)) - float(now))


# --------------------------------------------------------------------------
# entrées publiques
# --------------------------------------------------------------------------

def adopt(cfg, db, lot: int, report: Report, *, bins: dict | None = None,
          agent: str | None = None, requester: str | None = None,
          actor: str = "", dry_run: bool = False, now: float | None = None,
          rules=None) -> dict:
    """Fige un rapport sur un lot, sous le verrou des gestes de veille."""
    if not _lock(db):
        raise VeilleError("another veille gesture holds the lock")
    try:
        return _adopt_locked(cfg, db, lot, report, bins=bins, agent=agent,
                             requester=requester, actor=actor, dry_run=dry_run,
                             now=now, rules=rules)
    finally:
        _unlock(db)


def _adopt_locked(cfg, db, lot: int, report: Report, *, bins: dict | None = None,
                  agent: str | None = None, requester: str | None = None,
                  actor: str = "", dry_run: bool = False, now: float | None = None,
                  rules=None) -> dict:
    """Fige un rapport sur un lot et applique la règle du §4.

    Idempotent : rejouer le même rapport rend l'état courant sans second
    geste. Un rapport différent sur le même lot est refusé (une nouvelle
    évaluation est un nouveau lot)."""
    now = time.time() if now is None else float(now)
    item = work.get(db, int(lot))
    if item is None:
        raise VeilleError("work item #%s not found" % lot)
    if report.evaluation_lot != int(lot):
        raise VeilleError("report.evaluation_lot is #%s, not #%s"
                          % (report.evaluation_lot, lot))
    if item.get("state") in ("merged", "promoted", "closed"):
        raise VeilleError("work item #%s is %s: nothing to adopt"
                          % (lot, item.get("state")))
    existing = load(db, int(lot))
    if existing is not None:
        state = existing["state"]
        if state.get("report_digest") != report.digest:
            raise VeilleError("lot #%s already carries another replay report "
                              "(digest %s…); a new evaluation is a new lot"
                              % (lot, str(state.get("report_digest"))[:12]))
        return {"lot": int(lot), "result": "unchanged", "state": state,
                "report": existing.get("report")}
    project = item.get("app") or ""
    state = _state_defaults(report, project=project, now=now)
    decision = decide(report)
    row = {"lot_id": int(lot), "project": project, "report": report.raw,
           "_db": db}
    if dry_run:
        return {"lot": int(lot), "result": "dry-run", "action": decision.action,
                "price": decision.price, "reason": decision.reason,
                "state": state, "report": report.raw}
    if decision.action == "refused":
        state.update({"stage": "refused", "reason": decision.reason})
        state["inverse"] = {"version": report.from_version, "restore": []}
        save(db, row, state)
        _journal(row, state, "no adoption (%s); the lot is closed with its report"
                 % decision.reason, actor=actor)
        return {"lot": int(lot), "result": "refused", "state": state,
                "report": report.raw}
    if decision.action == "blocked":
        state.update({"stage": "blocked", "reason": decision.reason})
        save(db, row, state)
        _journal(row, state, "adoption blocked: %s" % decision.reason, actor=actor)
        return {"lot": int(lot), "result": "blocked", "state": state,
                "report": report.raw}
    state["bins"] = dict(bins or {})
    if decision.action == "owner":
        requester = requester or item.get("assignee") or actor
        if not requester or registry.get(db, requester) is None:
            raise VeilleError("an agent requester is required for the owner "
                              "decision (--requester); got %r" % requester)
        view = _ask_owner(cfg, db, item, report, decision, requester=requester)
        state.update({"stage": "waiting_owner", "decision_id": int(view["id"]),
                      "reason": decision.reason})
        save(db, row, state)
        _journal(row, state, "owner decision #%s requested (%s); no host changed"
                 % (view["id"], decision.reason), actor=actor)
        return {"lot": int(lot), "result": "waiting_owner", "decision": view,
                "state": state, "report": report.raw}
    # automatique : canari
    try:
        state = start_canary(db, row, state, report, agent=agent, now=now)
    except VeilleError as exc:
        _block(row, state, str(exc), from_stage="canary", actor=actor)
        return {"lot": int(lot), "result": "blocked", "state": state,
                "report": report.raw}
    return {"lot": int(lot), "result": "canary", "state": state,
            "report": report.raw}


def _ask_owner(cfg, db, item: dict, report: Report, decision: Decision, *,
               requester: str) -> dict:
    projected = report.projected_7d_usd
    question = ("Adopter %s %s -> %s ? Rejeu : %s, aucune régression de qualité. "
                "Prix : %s. Coût projeté sur 7 jours : %.2f USD. Rapport du lot #%s."
                % (report.harness, report.from_version, report.to_version,
                   report.conclusion, decision.price, projected or 0.0,
                   report.evaluation_lot))
    why = ("conclusion %s, %s ; prix %s" % (report.conclusion,
                                            "qualité sans régression"
                                            if not report.quality_regression
                                            else "régression de qualité",
                                            decision.price))
    view = decisions.ask(
        cfg, db, requester=requester, question=question, lot=str(item["id"]),
        options=[
            "adopt=adopter sur le mesh (canari puis hôtes), coût projeté %.2f USD"
            % (projected or 0.0),
            "refuse=garder %s" % report.from_version,
        ], recommend="adopt", why=why, urgent=False)
    return view


def advance(cfg, db, row: dict, *, now: float, bins: dict | None = None,
            actor: str = "ameesh", rules=None, dry_run: bool = False) -> dict:
    """Un pas de la séquence pour une adoption."""
    state = row["state"]
    report = Report(row["report"])
    _merge_bins(state, bins)
    stage = state.get("stage")
    if stage in TERMINAL:
        return {"lot": row["lot_id"], "result": stage}
    if stage == "blocked":
        origin = state.get("blocked_from")
        if origin == "waiting_owner_approved":
            return _start_from_owner(cfg, db, row, state, report, now=now,
                                     actor=actor, dry_run=dry_run)
        if origin == "canary":
            # le canari a pu être refusé faute d'agent au repos : on retente
            if dry_run:
                return {"lot": row["lot_id"], "result": "blocked",
                        "reason": state.get("reason")}
            try:
                state = start_canary(db, row, state, report, now=now)
            except VeilleError as exc:
                _block(row, state, str(exc), from_stage="canary", actor=actor)
                return {"lot": row["lot_id"], "result": "blocked",
                        "reason": str(exc)}
            return {"lot": row["lot_id"], "result": "canary", "state": state}
        if origin == "rollout":
            # la carte des binaires a pu arriver : on retente la bascule sans
            # réécrire l'étape (un blocage inchangé ne fait pas de note)
            return _advance_rollout(cfg, db, row, state, report, now=now,
                                    actor=actor, rules=rules, dry_run=dry_run)
        else:
            return {"lot": row["lot_id"], "result": "blocked",
                    "reason": state.get("reason")}
    if stage == "waiting_owner":
        return _advance_owner(cfg, db, row, state, report, now=now,
                              actor=actor, dry_run=dry_run)
    if stage == "canary":
        return _advance_canary(db, row, state, report, now=now, actor=actor,
                               dry_run=dry_run)
    if stage == "rollout":
        return _advance_rollout(cfg, db, row, state, report, now=now,
                                actor=actor, rules=rules, dry_run=dry_run)
    return {"lot": row["lot_id"], "result": stage}


def _advance_owner(cfg, db, row, state, report, *, now, actor, dry_run) -> dict:
    decision_id = state.get("decision_id")
    if not decision_id:
        return {"lot": row["lot_id"], "result": "blocked",
                "reason": "owner stage without a decision id"}
    view = decisions.get(db, int(decision_id))
    if view is None:
        return {"lot": row["lot_id"], "result": "waiting_owner",
                "reason": "decision #%s not found" % decision_id}
    if view.get("state") == "pending":
        return {"lot": row["lot_id"], "result": "waiting_owner"}
    if view.get("state") == "withdrawn":
        state.update({"stage": "refused", "reason": "owner decision withdrawn"})
        if not dry_run:
            save(db, row, state)
            _journal(row, state, "owner decision withdrawn: no adoption", actor=actor)
        return {"lot": row["lot_id"], "result": "refused",
                "reason": "owner decision withdrawn"}
    answer = view.get("answer") or {}
    option = answer.get("option")
    if view.get("state") != "answered" or option != "adopt":
        state.update({"stage": "refused",
                      "reason": "owner did not approve (%s)" % (option or "free text")})
        if not dry_run:
            save(db, row, state)
            _journal(row, state, "owner did not approve: no adoption", actor=actor)
        return {"lot": row["lot_id"], "result": "refused",
                "reason": state["reason"]}
    return _start_from_owner(cfg, db, row, state, report, now=now,
                             actor=actor, dry_run=dry_run)


def _start_from_owner(cfg, db, row, state, report, *, now, actor, dry_run) -> dict:
    if dry_run:
        return {"lot": row["lot_id"], "result": "would-start-canary"}
    state.update({"stage": "canary", "reason": "", "blocked_from": None})
    try:
        state = start_canary(db, row, state, report, now=now)
    except VeilleError as exc:
        _block(row, state, str(exc), from_stage="waiting_owner_approved",
               actor=actor)
        return {"lot": row["lot_id"], "result": "blocked", "reason": str(exc),
                "state": state}
    return {"lot": row["lot_id"], "result": "canary", "state": state}


def _advance_canary(db, row, state, report, *, now, actor, dry_run) -> dict:
    target = registry.get(db, state.get("target_agent") or "")
    if target is None or target.get("status") == "stopped":
        if not dry_run:
            state.update({"stage": "refused",
                          "reason": "canary agent disappeared or stopped"})
            with db.transaction() as tx:
                _restore(tx, state.get("applied") or [], report.object)
                save(tx, row, state)
            _journal(row, state, "canary refused: target disappeared; previous "
                     "version restored", actor=actor)
        return {"lot": row["lot_id"], "result": "refused",
                "reason": "canary agent disappeared or stopped"}
    expected = (state.get("canary_bin") if report.object == "harness"
                else report.to_version)
    current = _agent_value(target, report.object)
    if expected is not None and current != expected:
        if not dry_run:
            state.update({"stage": "refused",
                          "reason": "canary target no longer runs the evaluated "
                                    "version (%r)" % current})
            save(db, row, state)
            _journal(row, state, state["reason"], actor=actor)
        return {"lot": row["lot_id"], "result": "refused",
                "reason": state["reason"]}
    metrics = measure(db, state["target_agent"], float(state["started_ts"]))
    state["metrics"] = metrics
    due = (int(metrics["turns"]) >= CANARY_TURNS
           or float(now) >= float(state.get("deadline_ts") or 0))
    if not due:
        if not dry_run:
            save(db, row, state)
        return {"lot": row["lot_id"], "result": "canary", "metrics": metrics}
    passed, failures = canary_verdict(report, metrics)
    if failures:
        if dry_run:
            return {"lot": row["lot_id"], "result": "would-refuse",
                    "metrics": metrics, "failures": failures}
        state.update({
            "stage": "refused",
            "reason": "canary outside the replay bounds: " + "; ".join(failures),
            "inverse": {"version": report.from_version,
                        "restore": [{"agent": item["name"], "value": item["before"]}
                                    for entry in state.get("applied") or []
                                    for item in entry.get("agents") or []]},
        })
        with db.transaction() as tx:
            _restore(tx, state.get("applied") or [], report.object)
            save(tx, row, state)
        _journal(row, state, "canary refused, previous version restored (%s); "
                 "inverse: %s" % ("; ".join(failures), report.from_version),
                 actor=actor)
        return {"lot": row["lot_id"], "result": "refused",
                "reason": state["reason"], "state": state}
    hosts = _hosts(db, report, state)
    if not hosts:
        if not dry_run:
            _block(row, state, "no host runs harness %s" % report.harness,
                   from_stage="canary", actor=actor)
        return {"lot": row["lot_id"], "result": "blocked",
                "reason": state.get("reason")}
    state.update({"stage": "rollout", "hosts": hosts, "next_host": 0,
                  "reason": "", "blocked_from": None})
    if not dry_run:
        save(db, row, state)
        _journal(row, state, "canary passed (%d turn(s), cost %s, duration %s); "
                 "rollout order: %s"
                 % (metrics["turns"], metrics.get("cost_usd"),
                    metrics.get("duration_s"), ", ".join(hosts)), actor=actor)
    return {"lot": row["lot_id"], "result": "rollout", "state": state,
            "metrics": metrics}


def _advance_rollout(cfg, db, row, state, report, *, now, actor, rules,
                     dry_run) -> dict:
    hosts = list(state.get("hosts") or [])
    # un hôte apparu pendant la bascule (agent déplacé) rejoint la file : il
    # ne doit pas rester sur l'ancienne version sans être vu.
    current = _hosts(db, report, state)
    for host in current:
        if host not in hosts:
            hosts.append(host)
    state["hosts"] = hosts
    index = int(state.get("next_host") or 0)
    if index >= len(hosts):
        state.update({"stage": "done", "reason": ""})
        if not dry_run:
            save(db, row, state)
            _journal(row, state, "adopted on every host: %s -> %s"
                     % (report.from_version, report.to_version), actor=actor)
        return {"lot": row["lot_id"], "result": "done", "state": state}
    host = hosts[index]
    path = None
    if report.object == "harness":
        path = _bins_from_state(state).get(host)
        if not path:
            if not dry_run:
                _block(row, state, "no --bin path for host %s" % host,
                       from_stage="rollout", actor=actor)
            return {"lot": row["lot_id"], "result": "blocked", "host": host,
                    "reason": state.get("reason")}
        _check_bin(path)
    try:
        remaining = _cutoff_seconds(cfg, state.get("project") or "", now, rules)
    except Exception as exc:  # noqa: BLE001 — pas de calendrier, pas de bascule
        if not dry_run:
            _block(row, state, "cutoff calendar unreadable: %s" % exc,
                   from_stage="rollout", actor=actor)
        return {"lot": row["lot_id"], "result": "blocked", "host": host,
                "reason": state.get("reason")}
    if remaining < CUTOFF_MARGIN_S:
        if not dry_run:
            save(db, row, state)
        return {"lot": row["lot_id"], "result": "waiting_cutoff", "host": host,
                "seconds_to_cutoff": round(remaining, 3)}
    running = _running(db, report.harness, host)
    if running:
        if not dry_run:
            save(db, row, state)
        return {"lot": row["lot_id"], "result": "waiting_turn", "host": host,
                "agents": running}
    agents = _agents(db, report.harness, host=host)
    if not agents:
        state["next_host"] = index + 1
        if not dry_run:
            save(db, row, state)
        return {"lot": row["lot_id"], "result": "rollout",
                "host": host, "skipped": "no agent"}
    value = path if report.object == "harness" else report.to_version
    if dry_run:
        return {"lot": row["lot_id"], "result": "would-switch", "host": host,
                "agents": [a["name"] for a in agents], "value": value,
                "seconds_to_cutoff": round(remaining, 3)}
    with db.transaction() as tx:
        inverse = _apply_to_agents(tx, agents, report.object, value)
        state.setdefault("applied", []).append(
            {"host": host, "at": round(float(now), 3), "agents": inverse})
        state["inverse"] = {
            "version": report.from_version,
            "restore": [{"agent": item["name"], "value": item["before"]}
                        for entry in state["applied"]
                        for item in entry.get("agents") or []],
        }
        state["next_host"] = index + 1
        save(tx, row, state)
    _journal(row, state, "host switched between turns: %s (%d agent(s), %s); "
             "next: %s" % (host, len(agents), value,
                           hosts[index + 1] if index + 1 < len(hosts) else "done"),
             actor=actor)
    return {"lot": row["lot_id"], "result": "rollout", "host": host,
            "agents": [a["name"] for a in agents], "state": state}


def tick(cfg, db, *, now: float | None = None, project: str | None = None,
         bins: dict | None = None, actor: str = "ameesh", rules=None,
         dry_run: bool = False) -> dict:
    """Avance toutes les adoptions ouvertes. Un seul passage à la fois."""
    now = time.time() if now is None else float(now)
    if not _lock(db):
        return {"schema": "ameesh-veille-tick/1", "result": "busy",
                "adoptions": []}
    try:
        discovered = discover(cfg, db, now=now, project=project, bins=bins,
                              actor=actor, dry_run=dry_run)
        results = []
        for row in open_adoptions(db, project=project):
            row["_db"] = db
            state = row["state"]
            hosts = sorted({agent.get("host") or ""
                            for agent in _agents(db, row["harness"])
                            if agent.get("host")}
                           | {host for host in (state.get("hosts") or [])
                              if host}
                           | ({state.get("canary_host")}
                              if state.get("canary_host") else set()))
            row_bins = _expand_bins(bins or {}, hosts)
            try:
                results.append(advance(cfg, db, row, now=now, bins=row_bins,
                                       actor=actor, rules=rules, dry_run=dry_run))
            except VeilleError as exc:
                results.append({"lot": row["lot_id"], "result": "error",
                                "reason": str(exc)})
            except Exception as exc:  # noqa: BLE001 — une adoption ne bloque pas les autres
                results.append({"lot": row["lot_id"], "result": "error",
                                "reason": "%s: %s" % (type(exc).__name__, exc)})
        return {"schema": "ameesh-veille-tick/1", "result": "ok",
                "adoptions": results, "discovered": discovered}
    finally:
        _unlock(db)


def discover(cfg, db, *, now: float, project: str | None = None,
             bins: dict | None = None, actor: str = "ameesh",
             dry_run: bool = False) -> list[dict]:
    """Démarre les adoptions dont le rapport est déjà noté.

    A (détection) ouvre un lot « Évaluer <harnais> <version> » ; B y écrit son
    rapport en note. Le tick le relit et applique la règle du §4 sans qu'un
    humain ait à lancer `adopt` — c'est le « sans geste humain » de la spec.
    Un lot sans rapport lisible est simplement ignoré (le suivant le
    reprendra).
    """
    rows = db.query(
        """
        SELECT w.id FROM work_items w
         WHERE w.state NOT IN ('merged', 'promoted', 'closed')
           AND w.title ILIKE %s
           AND (%s::text IS NULL OR w.app = %s)
           AND NOT EXISTS (SELECT 1 FROM harness_adoptions a
                            WHERE a.lot_id = w.id)
         ORDER BY w.id DESC LIMIT 20
        """, ("Évaluer %", project, project))
    out = []
    for row in rows:
        lot = int(row["id"])
        try:
            report = report_from_lot(db, lot)
            hosts = sorted({agent.get("host") or ""
                            for agent in _agents(db, report.harness)
                            if agent.get("host")})
            expanded = _expand_bins(bins or {}, hosts)
            result = adopt(cfg, db, lot, report, bins=expanded, actor=actor,
                           dry_run=dry_run, now=now)
            out.append({"lot": lot, "result": result.get("result")})
        except VeilleError as exc:
            out.append({"lot": lot, "result": "ignored", "reason": str(exc)})
        except Exception as exc:  # noqa: BLE001 — un lot ne bloque pas les autres
            out.append({"lot": lot, "result": "error",
                        "reason": "%s: %s" % (type(exc).__name__, exc)})
    return out


def rollback(cfg, db, lot: int, *, reason: str, actor: str = "",
             now: float | None = None) -> dict:
    """Remet la version précédente, consignée dans le lot ; idempotent."""
    now = time.time() if now is None else float(now)
    row = load(db, int(lot))
    if row is None:
        raise VeilleError("lot #%s: no adoption" % lot)
    row["_db"] = db
    state = row["state"]
    if not _lock(db):
        raise VeilleError("another veille gesture holds the lock")
    try:
        report = Report(row["report"])
        applied = state.get("applied") or []
        state.update({
            "stage": "rolled_back",
            "reason": reason,
            "inverse": {
                "version": report.from_version,
                "restore": [{"agent": item["name"], "value": item["before"]}
                            for entry in applied for item in entry.get("agents") or []],
            },
            "updated_ts": round(float(now), 3),
        })
        with db.transaction() as tx:
            _restore(tx, applied, report.object)
            save(tx, row, state)
        _journal(row, state, "rolled back: %s; inverse version: %s"
                 % (reason, report.from_version), actor=actor or "ameesh")
        return {"lot": int(lot), "result": "rolled_back", "state": state}
    finally:
        _unlock(db)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _parse_bins(values) -> dict:
    """`--bin /chemin` (tous les hôtes) ou `--bin HOTE=/chemin`."""
    out = {}
    for raw in values or []:
        value = str(raw or "").strip()
        if not value:
            continue
        if "=" in value:
            host, _, path = value.partition("=")
            host = host.strip()
            path = path.strip()
            if not host:
                raise VeilleError("--bin %r: host is empty" % raw)
        else:
            host, path = "*", value
        if not os.path.isabs(path):
            raise VeilleError("--bin %r: an absolute path is required" % raw)
        out[host] = path
    return out


def _expand_bins(bins: dict, hosts: list[str]) -> dict:
    """`--bin /x` s'applique à tous les hôtes connus ; `HOTE=` reste ciblé."""
    if not bins:
        return {}
    if "*" not in bins:
        return dict(bins)
    common = bins["*"]
    out = {host: common for host in hosts}
    out.update({host: path for host, path in bins.items() if host != "*"})
    return out


def _report_text(args) -> Report | None:
    if args.report == "-":
        return parse_report(sys.stdin.read())
    if args.report:
        with open(os.path.expanduser(args.report), "r", encoding="utf-8") as fh:
            return parse_report(fh.read())
    return None


def _print_result(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    lot = result.get("lot")
    outcome = result.get("result")
    if outcome == "ok":
        rows = result.get("adoptions") or []
        if not rows and not result.get("discovered"):
            print("veille: no open adoption")
        for row in rows:
            print("lot #%s: %s%s" % (row.get("lot"), row.get("result"),
                                     (" — " + row["reason"]) if row.get("reason") else ""))
        for row in result.get("discovered") or []:
            if row.get("result") == "ignored":
                continue
            print("lot #%s: discovered %s%s" % (
                row.get("lot"), row.get("result"),
                (" — " + row["reason"]) if row.get("reason") else ""))
        return
    print("lot #%s: %s%s" % (lot, outcome,
                             (" — " + result["reason"]) if result.get("reason") else ""))
    state = result.get("state")
    if state:
        print("  stage=%s %s %s -> %s" % (state.get("stage"), state.get("harness"),
                                          state.get("from_version"),
                                          state.get("to_version")))
        if state.get("canary_host"):
            print("  canary: %s on %s" % (state.get("target_agent"),
                                          state.get("canary_host")))
        if state.get("metrics"):
            print("  metrics: %s" % json.dumps(state["metrics"], ensure_ascii=False))


def run(cfg, args) -> int:
    db = db_mod.open_db(cfg)
    try:
        db_mod.require_schema(db)
        now = time.time() if getattr(args, "now", None) is None else float(args.now)
        if args.veille_command == "adopt":
            report = _report_text(args)
            if report is None:
                report = report_from_lot(db, int(args.id))
            bins = _parse_bins(args.bin)
            hosts = [row["host"] for row in _agents(db, report.harness) if row.get("host")]
            bins = _expand_bins(bins, sorted(set(hosts)))
            result = adopt(cfg, db, int(args.id), report, bins=bins,
                           agent=args.agent, requester=args.requester,
                           actor=args.actor or "ameesh", dry_run=args.dry_run,
                           now=now, rules=None)
        elif args.veille_command == "tick":
            bins = _parse_bins(args.bin)
            result = tick(cfg, db, now=now, project=args.project, bins=bins,
                          actor=args.actor or "ameesh", rules=None,
                          dry_run=args.dry_run)
        elif args.veille_command == "show":
            row = load(db, int(args.id))
            if row is None:
                print("lot #%s: no adoption" % args.id, file=sys.stderr)
                return 1
            result = {"lot": row["lot_id"], "result": row["stage"],
                      "state": row["state"], "report": row["report"]}
        elif args.veille_command == "rollback":
            result = rollback(cfg, db, int(args.id), reason=args.reason,
                              actor=args.actor or "ameesh", now=now)
        else:
            raise VeilleError("unknown veille command")
        _print_result(result, args.json)
        return 0
    except (VeilleError, decisions.DecisionError) as exc:
        print("veille: %s" % exc, file=sys.stderr)
        return 1
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ameesh veille", description=__doc__)
    sub = parser.add_subparsers(dest="veille_command", required=True)

    p_adopt = sub.add_parser("adopt", help="freeze a replay report and apply §4")
    p_adopt.add_argument("id", type=int, help="evaluation work item")
    p_adopt.add_argument("--report", default=None,
                         help="report file, or - for standard input (default: the "
                              "latest report note of the lot)")
    p_adopt.add_argument("--bin", action="append", default=[],
                         metavar="[HOST=]PATH",
                         help="new harness binary (all hosts, or one host)")
    p_adopt.add_argument("--agent", default=None, help="canary agent (default: automatic)")
    p_adopt.add_argument("--requester", default=None,
                         help="agent filing the owner decision (default: assignee)")
    p_adopt.add_argument("--actor", default=None)
    p_adopt.add_argument("--dry-run", action="store_true")
    p_adopt.add_argument("--now", type=float, default=None)
    p_adopt.add_argument("--json", action="store_true")
    p_adopt.set_defaults(func=run)

    p_tick = sub.add_parser("tick", help="advance open adoptions (canary, rollout)")
    p_tick.add_argument("--project", default=None)
    p_tick.add_argument("--bin", action="append", default=[], metavar="[HOST=]PATH")
    p_tick.add_argument("--actor", default=None)
    p_tick.add_argument("--now", type=float, default=None)
    p_tick.add_argument("--dry-run", action="store_true")
    p_tick.add_argument("--json", action="store_true")
    p_tick.set_defaults(func=run)

    p_show = sub.add_parser("show", help="show an adoption")
    p_show.add_argument("id", type=int)
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=run)

    p_roll = sub.add_parser("rollback", help="restore the previous version")
    p_roll.add_argument("id", type=int)
    p_roll.add_argument("--reason", required=True)
    p_roll.add_argument("--actor", default=None)
    p_roll.add_argument("--now", type=float, default=None)
    p_roll.add_argument("--json", action="store_true")
    p_roll.set_defaults(func=run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    cfg = config_mod.load()
    return run(cfg, args)
