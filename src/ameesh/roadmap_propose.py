# SPDX-License-Identifier: AGPL-3.0-only
"""Propositions de feuille de route tirées des décisions et des tâches (lot L96).

  ameesh plan propose [--from DOSSIER …] [--no-canon] [--no-tasks] [--record] [--json]

« Il faut qu'ameesh se serve de ce genre de décision [« on fera ça lundi »]
pour établir la roadmap » (propriétaire, 2026-10-10). Ce module LIT :

* les fiches `Decision` des canons configurés (et, avec `--from`, celles d'un
  dossier de Markdown, p. ex. les décisions d'un dépôt) :
  - une décision `proposed`/`draft` est une question ouverte qui attend un
    humain → **jalon de décision** ;
  - une phrase datée (« pour le 2026-10-12 », « on fera ça lundi ») →
    **engagement** à cette date (un jour de la semaine se compte depuis la
    date de la décision) ;
  - les lots cités dans les conséquences (« lots L97–L104 ») → un élément de
    feuille de route sans date, avec ses **dépendances** (« même vague que
    L95 », « après L73 », « dépend de L99 ») ;
* le corps des tâches ouvertes : une date qui n'est écrite que là (« prévu le
  lundi 2026-10-12 ») → proposition de date de livraison pour la tâche.

Ce ne sont que des PROPOSITIONS, rendues avec la commande qui les validerait.
`--record` les enregistre (statut `proposed`, dédoublonnées par une clé
stable) ; `ameesh plan accept <id> [--pour J]` les valide, `ameesh plan
cancel <id>` les rejette. Rien n'est créé ni daté sans ce contrôle.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shlex
import time

from . import canon as canon_mod
from . import roadmap as rm
from . import roadmap_dev as dev
from . import storage

#: statuts d'une décision qui attend encore un humain
OPEN_DECISION_STATUSES = ("proposed", "draft", "open", "pending")
#: longueur d'une phrase reprise dans une proposition
WHAT_CHARS = 140

_ISO_RE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
_WEEKDAY_RE = re.compile(r"\b(%s)\b" % "|".join(rm._WEEKDAYS), re.I)
#: marqueurs d'un engagement à venir dans une phrase
_FUTURE_RE = re.compile(
    r"\b(on fera|on le fera|ferons|fera|prévue?s?|pour le|pour|d'ici|livr\w*|déploi\w*|"
    r"mise en (?:production|service)|lancer|lancement|commencer|démarr\w*|échéance)\b", re.I)
_LOT_RE = re.compile(r"\bL(\d{1,3})(?:\s*(?:[–—-]|à)\s*L?(\d{1,3}))?\b")
_LOT_LINE_RE = re.compile(r"\blots?\b", re.I)
_WAVE_RE = re.compile(r"m[êe]me vague que\s+((?:L\d{1,3}[\s,;et]*)+)", re.I)
_AFTER_RE = re.compile(r"\b(?:après|dépend(?:ant)? de|d[ée]pend de|suit)\s+((?:L\d{1,3}[\s,;et]*)+)",
                       re.I)
_SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+|\n+")


def _key(*parts) -> str:
    return hashlib.sha256("\x1f".join(str(p or "") for p in parts).encode("utf-8")
                          ).hexdigest()[:24]


def _sentences(body: str) -> list[str]:
    out = []
    for raw in _SENTENCE_RE.split(body or ""):
        text = " ".join(raw.strip(" -*>#\t").split())
        if text:
            out.append(text)
    return out


def _lots(text: str) -> list[str]:
    found: list[str] = []
    for match in _LOT_RE.finditer(text):
        a = int(match.group(1))
        b = int(match.group(2)) if match.group(2) else a
        if b < a or b - a > 40:
            b = a
        for n in range(a, b + 1):
            name = "L%d" % n
            if name not in found:
                found.append(name)
    return found


def _ref_date(data: dict, default: _dt.date) -> _dt.date:
    for key in ("decision_date", "date"):
        value = data.get(key)
        if value:
            try:
                return _dt.date.fromisoformat(str(value).strip()[:10])
            except ValueError:
                pass
    return default


def _command(p: dict) -> str:
    if p.get("plan_task") and p.get("due"):
        return "ameesh work plan %d --livraison %s --source %s" % (
            p["lot"], p["due"], shlex.quote(p["source_label"]))
    parts = ["ameesh", "plan", "add", shlex.quote(p["what"])]
    if p.get("due"):
        parts += ["--pour", p["due"]]
    if p["kind"] == "decision":
        parts.append("--decision")
    if p.get("lot"):
        parts += ["--lot", str(p["lot"])]
    if p.get("project"):
        parts += ["--projet", shlex.quote(p["project"])]
    parts += ["--source-type", p["source_kind"], "--source", shlex.quote(p["source_ref"] or "")]
    for dep in p.get("depends_on") or ():
        parts += ["--depend-de", dep]
    return " ".join(parts)


def _proposal(kind: str, what: str, *, due, source_kind: str, source_ref: str,
              source_label: str, origin: str, project=None, lot=None, owner=None,
              depends_on=(), lots=(), plan_task: bool = False) -> dict:
    what = what if len(what) <= WHAT_CHARS else what[:WHAT_CHARS - 1] + "…"
    p = {"kind": kind, "what": what, "due": due, "project": project, "lot": lot,
         "owner": owner, "source_kind": source_kind, "source_ref": source_ref,
         "source_label": source_label, "depends_on": list(depends_on), "lots": list(lots),
         "plan_task": plan_task,
         "key": _key(origin, kind, what, due, lot)}
    p["command"] = _command(p)
    return p


def from_decision(data: dict, body: str, *, ref: str, origin: str, base: _dt.date) -> list[dict]:
    """Les propositions d'une fiche Decision (frontmatter `data`, corps `body`).

    `ref` : la référence citée comme source (canon_ref ou chemin) ; `origin` :
    une forme stable de la fiche (membre:chemin), pour la clé de
    dédoublonnage."""
    title = " ".join(str(data.get("title") or os.path.basename(origin)).split())
    stem = os.path.basename(origin)[:-3] if origin.endswith(".md") else os.path.basename(origin)
    label = "décision %s" % stem
    project = data.get("project") or data.get("team") or None
    when = _ref_date(data, base)
    out: list[dict] = []
    status = str(data.get("status") or "").strip().lower()
    if status in OPEN_DECISION_STATUSES:
        due = None
        for key in ("decide_by", "due"):
            if data.get(key):
                try:
                    due = _dt.date.fromisoformat(str(data[key]).strip()[:10]).isoformat()
                except ValueError:
                    pass
        out.append(_proposal("decision", "Décider : %s" % title, due=due,
                             source_kind="decision", source_ref=ref, source_label=label,
                             origin=origin, project=project))
    lots: list[str] = []
    depends: list[str] = []
    for sentence in _sentences(body):
        if _LOT_LINE_RE.search(sentence):
            for name in _lots(sentence):
                if name not in lots:
                    lots.append(name)
        for regex in (_WAVE_RE, _AFTER_RE):
            for match in regex.finditer(sentence):
                for name in _lots(match.group(1)):
                    if name not in depends:
                        depends.append(name)
        if not _FUTURE_RE.search(sentence):
            continue
        dues = [d for d in _ISO_RE.findall(sentence)]
        days = []
        for d in dues:
            try:
                day = _dt.date.fromisoformat(d)
            except ValueError:
                continue
            if day >= when:
                days.append(day)
        for match in _WEEKDAY_RE.finditer(sentence):
            if not dues:
                days.append(rm.next_weekday(when, rm._WEEKDAYS.index(match.group(1).lower())))
        for day in sorted(set(days)):
            out.append(_proposal("commitment", sentence, due=day.isoformat(),
                                 source_kind="decision", source_ref=ref, source_label=label,
                                 origin=origin, project=project))
    lots = [l for l in lots if l not in depends]
    if lots:
        out.append(_proposal(
            "commitment", "Lots %s (%s : %s)" % (", ".join(lots), label, title), due=None,
            source_kind="decision", source_ref=ref, source_label=label, origin=origin,
            project=project, depends_on=depends, lots=lots))
    return out


def from_task(item: dict, *, base: _dt.date, has_due: set) -> list[dict]:
    """Une date écrite seulement dans le corps d'une tâche ouverte → date de
    livraison proposée (sauf si la tâche ou un engagement la porte déjà)."""
    out: list[dict] = []
    created = rm._day_of(item.get("created_ts")) or base
    for sentence in _sentences(item.get("body") or ""):
        if not _FUTURE_RE.search(sentence):
            continue
        days = []
        for d in _ISO_RE.findall(sentence):
            try:
                days.append(_dt.date.fromisoformat(d))
            except ValueError:
                continue
        if not days:
            days = [rm.next_weekday(created, rm._WEEKDAYS.index(m.group(1).lower()))
                    for m in _WEEKDAY_RE.finditer(sentence)]
        for day in sorted(set(days)):
            if day < created or (int(item["id"]), day.isoformat()) in has_due \
                    or item.get("planned_delivery") == day.isoformat():
                continue
            out.append(_proposal(
                "commitment", "#%d %s" % (int(item["id"]), item.get("title") or ""),
                due=day.isoformat(), source_kind="task", source_ref="#%d" % int(item["id"]),
                source_label="corps de la tâche #%d" % int(item["id"]),
                origin="task:%d" % int(item["id"]), lot=int(item["id"]),
                project=item.get("app") or item.get("workstream") or None,
                owner=item.get("assignee") or None, plan_task=True))
    return out


def _read_dir(directory: str) -> list[tuple[dict, str, str]]:
    """Les fiches Decision d'un dossier : (frontmatter, corps, chemin)."""
    out = []
    for root, _dirs, files in os.walk(directory):
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read(canon_mod.DECISION_BODY_MAX)
                block = canon_mod.split_frontmatter(text)
                data = canon_mod.load_yaml(block) if block is not None else None
            except (OSError, UnicodeDecodeError, canon_mod.YamlError):
                continue
            if not isinstance(data, dict) or data.get("type") != canon_mod.DECISION_TYPE:
                continue
            lines = text.split("\n")
            body = text
            for idx in range(1, len(lines)):
                if lines[idx].rstrip("\r \t") in ("---", "..."):
                    body = "\n".join(lines[idx + 1:])
                    break
            out.append((data, body, path))
    return out


def propose(db, *, cfg=None, dirs=(), canon: bool = True, tasks: bool = True,
            now: float | None = None, canons=None) -> dict:
    """Toutes les propositions, dédoublonnées, avec celles déjà enregistrées."""
    now = time.time() if now is None else float(now)
    base = rm.today(now)
    proposals: list[dict] = []
    sources: list[str] = []
    if canon:
        loaded = canons if canons is not None else (
            canon_mod.load_configured(cfg) if cfg is not None else [])
        for c in loaded:
            sources.append("canon %s" % getattr(c, "id", "?"))
            for note in c.decisions:
                proposals += from_decision(
                    note.fiche.data, note.body, ref=note.fiche.ref,
                    origin="%s:%s" % (note.fiche.member, note.fiche.path), base=base)
    for directory in dirs:
        sources.append(directory)
        for data, body, path in _read_dir(directory):
            proposals += from_decision(data, body, ref=os.path.relpath(path, directory),
                                       origin=os.path.abspath(path), base=base)
    st = storage.of(db)
    existing = st.roadmap.commitments()
    keys = {c["proposal_key"]: c for c in existing if c.get("proposal_key")}
    if tasks:
        sources.append("corps des tâches ouvertes")
        has_due = {(c["work_item_id"], c["due_on"]) for c in existing
                   if c.get("work_item_id") and c["status"] != "cancelled"}
        done = dev.CORRESPONDANCES["etats_livres"] + dev.CORRESPONDANCES["etats_abandonnes"]
        for item in st.roadmap.items(since_ts=now, limit=rm.MAX_ITEMS, done_states=done):
            if dev.livree(item.get("state")) or dev.abandonnee(item.get("state")):
                continue
            proposals += from_task(item, base=base, has_due=has_due)
    seen: set = set()
    unique = []
    for p in proposals:
        if p["key"] in seen:
            continue
        seen.add(p["key"])
        known = keys.get(p["key"])
        p["recorded"] = {"id": known["id"], "status": known["status"]} if known else None
        unique.append(p)
    return {"schema": "ameesh-roadmap-proposals/1", "generated_ts": round(now, 3),
            "sources": sources, "proposals": unique}


def record(db, proposals: list[dict], *, actor: str = "") -> list[dict]:
    """Enregistre les propositions nouvelles (statut `proposed`)."""
    st = storage.of(db)
    out = []
    for p in proposals:
        if p.get("recorded"):
            continue
        row = st.roadmap.record_proposal({
            "what": p["what"], "due_on": p["due"], "kind": p["kind"], "owner": p["owner"],
            "project": p["project"], "work_item_id": p["lot"], "source_kind": p["source_kind"],
            "source_ref": p["source_ref"], "depends_on": p["depends_on"] + [
                "lot %s" % l for l in p["lots"]] if p["lots"] else p["depends_on"],
            "note": "proposition tirée de %s" % p["source_label"], "created_by": actor or "",
            "proposal_key": p["key"]})
        if row is not None:
            p["recorded"] = {"id": row["id"], "status": row["status"], "new": True}
            out.append(row)
    return out


def run(db, args, *, cfg=None, now: float | None = None) -> int:
    view = propose(db, cfg=cfg, dirs=args.from_dir, canon=not args.no_canon,
                   tasks=not args.no_tasks, now=now)
    created = record(db, view["proposals"]) if args.record else []
    if args.json:
        print(json.dumps(dict(view, recorded=[c["id"] for c in created]), ensure_ascii=False,
                         indent=2))
        return 0
    print("propositions de feuille de route (%s) : %d" % (
        ", ".join(view["sources"]) or "aucune source", len(view["proposals"])))
    for p in view["proposals"]:
        state = ""
        if p.get("recorded"):
            state = " [%s e%d%s]" % (
                "enregistrée" if p["recorded"].get("new") else "déjà enregistrée",
                p["recorded"]["id"], ", %s" % p["recorded"]["status"]
                if not p["recorded"].get("new") else "")
        print("- %s %s%s — %s%s" % (
            {"decision": "jalon de décision", "commitment": "engagement"}[p["kind"]],
            "pour %s" % p["due"] if p["due"] else "sans date", state, p["what"],
            " (dépend de %s)" % ", ".join(p["depends_on"]) if p["depends_on"] else ""))
        print("    source : %s" % (p["source_ref"] or p["source_label"]))
        print("    pour valider : %s" % (
            "ameesh plan accept %d%s" % (p["recorded"]["id"], "" if p["due"] else
                                         " --pour AAAA-MM-JJ")
            if p.get("recorded") else p["command"]))
    if not args.record and view["proposals"]:
        print("(rien n'est enregistré : --record pour garder ces propositions, à valider)")
    return 0
