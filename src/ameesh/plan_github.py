# SPDX-License-Identifier: AGPL-3.0-only
"""Le plan de travail et GitHub (lot L29) : fermeture sur fusion, projection.

ameesh reste la **source de vérité** (décision 0005) : le plan vient du canon
(fiches `WorkPackage`, table `work_packages`), l'état d'exécution des lots de
`work_items`. GitHub n'est qu'une **vue** ; ce qui y est modifié n'est jamais
réimporté comme état — au plus signalé comme **proposition**.

`ameesh work sync-github --repo R` (lecture seule de GitHub)
    lit les PR fusionnées (`gh pr list --state merged`) et ferme les lots
    qu'elles référencent : branche `lot/<id>` ou `lot/<id>-…` (identifiant
    de fiche ; la plus longue correspondance l'emporte), ou ligne
    `ameesh-lot: <id>` dans le corps. Une ligne `ameesh-work: <n>` désigne un
    lot précis (`work_items`) : la PR ne ferme alors que ceux-là. Seuls les
    lots créés AVANT la fusion sont fermés (un lot ouvert ensuite pour la
    même fiche n'est pas fermé par une ancienne PR). Idempotent (`already`) ;
    un lot fermé (abandonné, remplacé) n'est jamais rouvert (`refused`).

`ameesh work project-github --repo R [--dry-run]`
    une issue par fiche `lot` (titre, lien vers la fiche, labels `ameesh` et
    `ameesh:<état>` parmi intake | build | qa | merged | blocked |
    waiting_human), une issue par `epic` qui porte ses lots en sous-issues
    (API sub-issues de GitHub ; si elle est indisponible, liste de tâches
    dans le corps de l'epic). Chaque issue porte un marqueur caché
    `<!-- ameesh:package=<id> d=<empreintes> -->` : il retrouve l'issue
    parmi TOUTES celles du dépôt, quels que soient ses labels (idempotence,
    jamais de doublon) et garde l'empreinte de ce qu'ameesh a écrit (titre,
    corps, labels ameesh, état). Une issue dont un de ces champs ne
    correspond plus à son empreinte a été modifiée dans GitHub : c'est
    signalé comme **proposition** (à porter au canon par PR), puis la vue
    est réécrite. Les labels qui ne commencent pas par `ameesh` ne sont
    jamais touchés. `--dry-run` lit GitHub et dit ce qui serait fait, sans
    rien écrire.

`gh` est résolu comme pour le connecteur `git-merge` (`AMEESH_GH_BIN`,
`AMEESH_BIN_DIR/gh`, PATH) ; les tests utilisent un faux `gh`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Iterable

from . import plan as plan_mod
from . import storage
from . import work as work_mod
from .connectors import ConnectorError
from .connectors.git_merge import resolve_gh
from .plan_git import WORK_TRAILER_RE

_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}$")
_LOT_LINE_RE = re.compile(r"(?im)^[ \t>*-]*ameesh-lot[ \t]*:[ \t]*([A-Za-z0-9][A-Za-z0-9._-]*)[ \t]*$")
#: une ligne `ameesh-work: <n>` (même forme dans un commit de fusion : `plan_git`)
_WORK_LINE_RE = WORK_TRAILER_RE
_MARKER_RE = re.compile(r"<!-- ameesh:package=([A-Za-z0-9][A-Za-z0-9._-]*)(?: d=([0-9a-f.]*))? -->")

#: labels d'état d'une issue de lot
STATE_LABELS = ("intake", "build", "qa", "merged", "blocked", "waiting_human")
BASE_LABEL = "ameesh"
EPIC_LABEL = "ameesh:epic"
LABEL_COLORS = {"intake": "c5def5", "build": "1d76db", "qa": "fbca04", "merged": "0e8a16",
                "blocked": "b60205", "waiting_human": "d93f0b", "epic": "5319e7", "": "ededed"}
#: priorité d'attention quand une fiche a plusieurs lots ouverts
_ATTENTION = ("waiting_human", "blocked", "qa", "build", "intake")


class GithubError(RuntimeError):
    """gh introuvable, refus ou réponse illisible : message actionnable."""


def check_repo(repo: str) -> str:
    if not _REPO_RE.fullmatch(repo or ""):
        raise GithubError("dépôt « owner/repo » attendu, pas %r" % (repo,))
    return repo


# --------------------------------------------------------------------------
# gh
# --------------------------------------------------------------------------

class Gh:
    """Appels à `gh` (sous-processus). `run` rend (code, stdout, stderr)."""

    def __init__(self, gh: str | None = None, env: dict | None = None, timeout: float = 60.0):
        self.env = dict(os.environ if env is None else env)
        self.env.setdefault("GH_PROMPT_DISABLED", "1")
        self.env.setdefault("NO_COLOR", "1")
        try:
            self.path = resolve_gh(gh, self.env)
        except ConnectorError as exc:
            raise GithubError(str(exc).replace("git-merge : ", "")) from exc
        self.timeout = float(timeout)

    def run(self, argv: list[str], stdin: str | None = None) -> tuple[int, str, str]:
        try:
            proc = subprocess.run([self.path, *argv], input=stdin, capture_output=True,
                                  text=True, timeout=self.timeout, env=self.env)
        except subprocess.TimeoutExpired:
            return 124, "", "délai dépassé (%ss)" % self.timeout
        except OSError as exc:
            return 127, "", str(exc)
        return proc.returncode, proc.stdout, proc.stderr


def _call(gh, argv: list[str], stdin: str | None = None) -> str:
    code, out, err = gh.run(argv, stdin)
    if code != 0:
        raise GithubError("gh %s : code %d : %s" % (
            " ".join(argv[:3]), code, " ".join((err or out or "—").split())[:300]))
    return out


def _json(gh, argv: list[str], stdin: str | None = None):
    out = _call(gh, argv, stdin)
    try:
        return json.loads(out) if out.strip() else None
    except ValueError as exc:
        raise GithubError("gh %s : réponse illisible" % " ".join(argv[:3])) from exc


def _lines(gh, argv: list[str]) -> list[dict]:
    """Sortie `--jq '.[] | @json'` : un objet JSON par ligne."""
    out = _call(gh, argv)
    rows = []
    for line in out.splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError as exc:
                raise GithubError("gh %s : ligne illisible" % " ".join(argv[:3])) from exc
    return rows


# --------------------------------------------------------------------------
# PR fusionnées → lots fermés (lecture seule de GitHub)
# --------------------------------------------------------------------------

def packages_of_pr(pr: dict, idents: Iterable[str]) -> list[str]:
    """Les fiches qu'une PR référence : branche `lot/<id>[-…]`, ligne `ameesh-lot:`."""
    by_fold = {i.casefold(): i for i in idents}
    found: list[str] = []
    head = pr.get("headRefName") or ""
    if head.startswith("lot/"):
        rest = head[4:].casefold()
        best = None
        for fold, ident in by_fold.items():
            if rest == fold or rest.startswith(fold + "-"):
                if best is None or len(fold) > len(best.casefold()):
                    best = ident
        if best:
            found.append(best)
    for match in _LOT_LINE_RE.finditer(pr.get("body") or ""):
        ident = by_fold.get(match.group(1).casefold())
        if ident and ident not in found:
            found.append(ident)
    return found


def work_items_of_pr(pr: dict) -> list[int]:
    return sorted({int(m.group(1)) for m in _WORK_LINE_RE.finditer(pr.get("body") or "")})


def _iso_ts(text: str | None) -> float | None:
    if not text:
        return None
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def sync_github(db, gh, repo: str, *, limit: int = 200, dry_run: bool = False) -> dict:
    """Ferme les lots dont la PR est fusionnée. Rend le compte rendu."""
    check_repo(repo)
    prs = _json(gh, ["pr", "list", "--repo", repo, "--state", "merged", "--limit", str(int(limit)),
                     "--json", "number,headRefName,body,state,mergeCommit,mergedBy,mergedAt,url"])
    if not isinstance(prs, list):
        raise GithubError("gh pr list : liste attendue")
    st = storage.of(db)
    idents = [p["id"] for p in st.packages.all() if p.get("kind") in work_mod.LINKABLE_KINDS]
    results: list[dict] = []
    for pr in sorted(prs, key=lambda p: int(p.get("number") or 0)):
        if (pr.get("state") or "MERGED") != "MERGED":
            continue
        ref = "%s#%s" % (repo, pr.get("number"))
        commit = pr.get("mergeCommit") or {}
        sha = commit.get("oid") if isinstance(commit, dict) else ""
        login = ((pr.get("mergedBy") or {}).get("login") if isinstance(pr.get("mergedBy"), dict)
                 else None)
        merged_ts = _iso_ts(pr.get("mergedAt"))
        explicit = work_items_of_pr(pr)
        packages = packages_of_pr(pr, idents)
        if explicit:
            targets = [(i, None) for i in explicit]
        else:
            targets = [(int(item["id"]), pid) for pid in packages
                       for item in st.work.by_package(pid)]
        for item_id, pid in targets:
            item = work_mod.get(db, item_id)
            entry = {"pr": ref, "work_item": item_id, "package": pid or (item or {}).get(
                "package_id"), "sha": sha or None, "merged_by": login}
            if item is None:
                entry.update(result="unknown", detail="lot #%d introuvable" % item_id)
            elif merged_ts is not None and item.get("created_ts") \
                    and float(item["created_ts"]) > merged_ts:
                entry.update(result="later", detail="lot créé après la fusion : non fermé")
            elif dry_run:
                state = item["state"]
                entry.update(result=("already" if state in work_mod.MERGED_STATES else
                                     "refused" if state == "closed" else "would-merge"),
                             detail="%s (essai)" % state)
            else:
                done = work_mod.close_merged(
                    db, item_id, sha=sha or "", actor="github:%s" % login if login else "github",
                    source="sync-github", pr_ref=ref)
                entry.update(result=done["result"], detail=done["detail"])
            results.append(entry)
    return {"repo": repo, "dry_run": bool(dry_run), "pull_requests": len(prs),
            "results": results,
            "merged": sum(1 for r in results if r["result"] == "merged"),
            "refused": [r for r in results if r["result"] == "refused"]}


# --------------------------------------------------------------------------
# projection : une issue par lot, l'epic avec ses sous-issues
# --------------------------------------------------------------------------

def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True)
                          .encode("utf-8")).hexdigest()[:8]


def _clean(body: str | None) -> str:
    """Le corps sans marqueur, fins de ligne normalisées."""
    text = _MARKER_RE.sub("", (body or "").replace("\r\n", "\n")).strip()
    return "\n".join(line.rstrip() for line in text.split("\n"))


def _own_labels(labels: Iterable) -> list[str]:
    names = [(lab.get("name") if isinstance(lab, dict) else str(lab)) for lab in labels or []]
    return sorted(n for n in names if n and (n == BASE_LABEL or n.startswith(BASE_LABEL + ":")))


def _fields(title: str, body: str, labels: Iterable, state: str) -> dict:
    return {"title": (title or "").strip(), "body": _clean(body),
            "labels": _own_labels(labels), "state": (state or "").lower()}


_FIELD_NAMES = ("title", "body", "labels", "state")
_FIELD_FR = {"title": "titre", "body": "corps", "labels": "labels ameesh", "state": "état"}


def _stamp(fields: dict) -> str:
    """Les empreintes de ce qu'ameesh écrit : titre.corps.labels.état."""
    return ".".join(_digest(fields[k]) for k in _FIELD_NAMES)


def _marker(ident: str, fields: dict) -> str:
    return "<!-- ameesh:package=%s d=%s -->" % (ident, _stamp(fields))


def _edited(issue: dict, recorded: str | None) -> list[str]:
    """Les champs modifiés dans GitHub depuis la dernière projection."""
    if not recorded:
        return []
    parts = recorded.split(".")
    if len(parts) != len(_FIELD_NAMES):
        return []
    now = _fields(issue.get("title"), issue.get("body"), issue.get("labels"),
                  issue.get("state"))
    return [k for k, d in zip(_FIELD_NAMES, parts) if _digest(now[k]) != d]


def fiche_link(canon_ref: str | None, base: str | None) -> str:
    """Lien vers la fiche : `base/<chemin>` (`{commit}` remplacé), sinon la réf."""
    ref = canon_ref or ""
    member_path, _, commit = ref.rpartition("@")
    path = member_path.split(":", 1)[1] if ":" in member_path else member_path
    if base and path:
        url = base.replace("{commit}", commit).rstrip("/") + "/" + path
        return "[%s](%s) (`%s`)" % (path, url, ref)
    return "`%s`" % ref if ref else "—"


def lot_state(items: list[dict]) -> tuple[str | None, str]:
    """(label d'état, état de l'issue) d'une fiche `lot` d'après ses lots."""
    status = plan_mod.package_status(items)
    if status == "pending":
        return "intake", "open"
    if status == "merged":
        return "merged", "closed"
    if status == "abandoned":
        return None, "closed"
    states = {("merged" if i["state"] in work_mod.MERGED_STATES else i["state"])
              for i in items if i.get("state") != "closed"}
    for state in _ATTENTION:
        if state in states:
            return state, "open"
    return "intake", "open"


_HEADER = ("_Vue projetée par ameesh : la source est le canon (fiche ci-dessous) et "
           "l'état d'ameesh. Une modification faite ici n'est pas reprise ; elle est "
           "signalée comme proposition — portez-la au canon par une PR._")


@dataclass
class Desired:
    ident: str
    kind: str
    title: str
    body: str
    labels: list[str]
    state: str                       # open | closed
    state_reason: str | None = None  # completed | not_planned
    epic: str | None = None
    tasks: list[tuple[str, bool]] = field(default_factory=list)  # (lot, fusionné)


def desired_views(packages: list[dict], items: list[dict], *, canon_url: str | None) -> list[Desired]:
    by_id = {p["id"]: p for p in packages}
    by_package: dict = {}
    for item in items:
        by_package.setdefault(item.get("package_id"), []).append(item)
    summary = plan_mod.summarize(packages, items)
    epics = {e["id"]: e for e in summary["epics"]}
    out: list[Desired] = []
    for pid in sorted(by_id):
        p = by_id[pid]
        if p.get("kind") != "lot":
            continue
        lots = by_package.get(pid, [])
        label, state = lot_state(lots)
        epic = plan_mod.epic_of(pid, by_id)
        body = "\n".join([
            _HEADER, "",
            "- Fiche : %s" % fiche_link(p.get("canon_ref"), canon_url),
            "- Epic : %s" % ("`%s`" % epic if epic else "—"),
            "- Responsable : %s" % (p.get("responsible") or "—"),
            "- État ameesh : %s%s" % (
                label or "fermé (abandonné)",
                " — lots %s" % ", ".join("#%s %s" % (i["id"], i["state"]) for i in lots)
                if lots else " — aucun lot ouvert"),
        ])
        out.append(Desired(
            ident=pid, kind="lot", title="[%s] %s" % (pid, p.get("title") or pid), body=body,
            labels=sorted([BASE_LABEL] + (["%s:%s" % (BASE_LABEL, label)] if label else [])),
            state=state,
            state_reason=None if state == "open" else (
                "completed" if label == "merged" else "not_planned"),
            epic=epic))
    for pid, e in sorted(epics.items()):
        p = by_id[pid]
        done = e["lots_total"] and e["lots_merged"] == e["lots_total"] - e["lots_abandoned"]
        body = "\n".join([
            _HEADER, "",
            "- Fiche : %s" % fiche_link(p.get("canon_ref"), canon_url),
            "- Jalon : %s" % ("`%s`" % e["milestone"] if e["milestone"] else "—"),
            "- Responsable : %s" % (p.get("responsible") or "—"),
            "- Progression : %d/%d lots fusionnés%s" % (
                e["lots_merged"], e["lots_total"] - e["lots_abandoned"],
                " (%d abandonné(s))" % e["lots_abandoned"] if e["lots_abandoned"] else ""),
        ])
        out.append(Desired(
            ident=pid, kind="epic", title="[%s] %s" % (pid, p.get("title") or pid), body=body,
            labels=sorted([BASE_LABEL, EPIC_LABEL]),
            state="closed" if done else "open",
            state_reason="completed" if done else None,
            tasks=[(lot["id"], lot["status"] == "merged") for lot in e["lots"]]))
    return out


def _with_tasks(view: Desired, numbers: dict) -> str:
    lines = [view.body, "", "Lots :"]
    for lot, merged in view.tasks:
        ref = "#%s" % numbers[lot] if lot in numbers else "`%s` (issue à créer)" % lot
        lines.append("- [%s] %s" % ("x" if merged else " ", ref))
    return "\n".join(lines)


class _Repo:
    """Opérations REST (`gh api`) sur un dépôt ; écritures refusées en essai."""

    def __init__(self, gh, repo: str, dry_run: bool):
        self.gh, self.repo, self.dry_run = gh, repo, dry_run

    def labels(self) -> set[str]:
        return {row.get("name") for row in _lines(self.gh, [
            "api", "--paginate", "repos/%s/labels?per_page=100" % self.repo,
            "--jq", ".[] | @json"])}

    def create_label(self, name: str) -> None:
        key = name.split(":", 1)[1] if ":" in name else ""
        _json(self.gh, ["api", "-X", "POST", "repos/%s/labels" % self.repo, "--input", "-"],
              json.dumps({"name": name, "color": LABEL_COLORS.get(key, "ededed"),
                          "description": "projection ameesh (L29)"}))

    def issues(self) -> list[dict]:
        """TOUTES les issues du dépôt (sans filtre de label) : une issue se
        retrouve par son marqueur, même si son label `ameesh` a été retiré
        dans GitHub (sinon la projection suivante créerait un doublon)."""
        rows = _lines(self.gh, [
            "api", "--paginate", "repos/%s/issues?state=all&per_page=100" % self.repo,
            "--jq", ".[] | @json"])
        return [r for r in rows if not r.get("pull_request")]

    def create(self, payload: dict) -> dict:
        return _json(self.gh, ["api", "-X", "POST", "repos/%s/issues" % self.repo,
                               "--input", "-"], json.dumps(payload)) or {}

    def update(self, number: int, payload: dict) -> dict:
        return _json(self.gh, ["api", "-X", "PATCH", "repos/%s/issues/%d" % (self.repo, number),
                               "--input", "-"], json.dumps(payload)) or {}

    def sub_issues(self, number: int) -> list[dict] | None:
        """Les sous-issues, ou None si l'API sub-issues est indisponible."""
        code, out, _err = self.gh.run([
            "api", "--paginate", "repos/%s/issues/%d/sub_issues?per_page=100" % (self.repo, number),
            "--jq", ".[] | @json"])
        if code != 0:
            return None
        try:
            return [json.loads(line) for line in out.splitlines() if line.strip()]
        except ValueError:
            return None

    def add_sub_issue(self, number: int, sub_id: int) -> None:
        _json(self.gh, ["api", "-X", "POST",
                        "repos/%s/issues/%d/sub_issues" % (self.repo, number), "--input", "-"],
              json.dumps({"sub_issue_id": int(sub_id), "replace_parent": True}))

    def remove_sub_issue(self, number: int, sub_id: int) -> None:
        _json(self.gh, ["api", "-X", "DELETE",
                        "repos/%s/issues/%d/sub_issue" % (self.repo, number), "--input", "-"],
              json.dumps({"sub_issue_id": int(sub_id)}))


def _payload(view: Desired, body: str, issue: dict | None) -> dict:
    keep = [] if issue is None else sorted(
        n for n in ((lab.get("name") if isinstance(lab, dict) else str(lab))
                    for lab in issue.get("labels") or [])
        if n and not (n == BASE_LABEL or n.startswith(BASE_LABEL + ":")))
    fields = _fields(view.title, body, view.labels, view.state)
    payload = {"title": view.title, "body": _marker(view.ident, fields) + "\n" + body,
               "labels": keep + view.labels}
    if issue is not None or view.state != "open":
        payload["state"] = view.state
        if view.state_reason:
            payload["state_reason"] = view.state_reason
    return payload


def project_github(db, gh, repo: str, *, dry_run: bool = False,
                   canon_url: str | None = None) -> dict:
    """Projette le plan sur les issues de `repo`. Rend le compte rendu."""
    check_repo(repo)
    st = storage.of(db)
    packages = st.packages.all()
    items = st.progress.package_items()
    views = desired_views(packages, items, canon_url=canon_url)
    gh_repo = _Repo(gh, repo, dry_run)
    report: dict = {"repo": repo, "dry_run": bool(dry_run), "issues": [], "proposals": [],
                    "labels_created": [], "sub_issues": [], "sub_issues_api": None,
                    "orphans": [], "duplicates": [], "notes": []}
    present = {p["id"] for p in packages}

    # labels
    wanted = sorted({lab for v in views for lab in v.labels}
                    | {"%s:%s" % (BASE_LABEL, s) for s in STATE_LABELS})
    existing_labels = gh_repo.labels()
    for name in wanted:
        if name not in existing_labels:
            report["labels_created"].append(name)
            if not dry_run:
                gh_repo.create_label(name)

    # issues existantes, retrouvées par leur marqueur
    found: dict[str, dict] = {}
    for issue in sorted(gh_repo.issues(), key=lambda i: int(i.get("number") or 0)):
        match = _MARKER_RE.search(issue.get("body") or "")
        if not match:
            continue
        ident = match.group(1)
        if ident in found:
            report["duplicates"].append({"package": ident, "number": issue.get("number"),
                                         "kept": found[ident].get("number")})
            continue
        found[ident] = dict(issue, _recorded=match.group(2))
        if ident not in present:
            report["orphans"].append({"package": ident, "number": issue.get("number"),
                                      "detail": "fiche absente du plan : issue laissée telle quelle"})

    numbers: dict[str, int] = {}
    ids: dict[str, int] = {}

    def apply(view: Desired, body: str) -> None:
        issue = found.get(view.ident)
        entry = {"package": view.ident, "kind": view.kind, "number": None,
                 "action": "unchanged", "state": view.state,
                 "labels": view.labels}
        if issue is not None:
            entry["number"] = issue.get("number")
            edited = _edited(issue, issue.get("_recorded"))
            if edited:
                report["proposals"].append({
                    "package": view.ident, "number": issue.get("number"),
                    "fields": edited,
                    "detail": "modifié dans GitHub (%s) : proposition à porter au canon ; "
                              "la vue est réécrite depuis ameesh"
                              % ", ".join(_FIELD_FR[k] for k in edited)})
            want = _fields(view.title, body, view.labels, view.state)
            have = _fields(issue.get("title"), issue.get("body"), issue.get("labels"),
                           issue.get("state"))
            marker_ok = (issue.get("_recorded") or "") == _stamp(want)
            if want != have or not marker_ok:
                entry["action"] = "update"
                entry["changes"] = [_FIELD_FR[k] for k in _FIELD_NAMES if want[k] != have[k]]
                if not dry_run:
                    gh_repo.update(int(issue["number"]), _payload(view, body, issue))
            numbers[view.ident] = int(issue["number"])
            if issue.get("id") is not None:
                ids[view.ident] = int(issue["id"])
        else:
            entry["action"] = "create"
            if not dry_run:
                created = gh_repo.create(_payload(view, body, None))
                if view.state != "open" and created.get("number"):
                    # une issue se crée ouverte : la fermer ensuite
                    gh_repo.update(int(created["number"]), _payload(view, body, created))
                entry["number"] = created.get("number")
                if created.get("number"):
                    numbers[view.ident] = int(created["number"])
                if created.get("id") is not None:
                    ids[view.ident] = int(created["id"])
        report["issues"].append(entry)

    for view in views:
        if view.kind == "lot":
            apply(view, view.body)

    epic_views = [v for v in views if v.kind == "epic"]
    api: bool | None = None
    for view in epic_views:
        if view.ident in found:
            api = gh_repo.sub_issues(int(found[view.ident]["number"])) is not None
            break
    for view in epic_views:
        if api is None and view.ident not in found and not dry_run:
            # aucun epic encore projeté : le créer, puis sonder l'API sub-issues
            apply(view, view.body)
            number = numbers.get(view.ident)
            api = gh_repo.sub_issues(number) is not None if number else None
            if api is False and number:
                gh_repo.update(number, _payload(view, _with_tasks(view, numbers),
                                                {"labels": []}))
        else:
            apply(view, _with_tasks(view, numbers) if api is False else view.body)
        number = numbers.get(view.ident)
        if not api or number is None:
            continue
        current = gh_repo.sub_issues(number) or []
        current_ids = {int(sub["id"]) for sub in current if sub.get("id") is not None}
        want = {lot: ids[lot] for lot, _merged in view.tasks if lot in ids}
        for lot, sub_id in sorted(want.items()):
            if sub_id not in current_ids:
                report["sub_issues"].append({"epic": view.ident, "add": lot,
                                             "number": numbers.get(lot)})
                if not dry_run:
                    gh_repo.add_sub_issue(number, sub_id)
        for sub in current:
            match = _MARKER_RE.search(sub.get("body") or "")
            if match and match.group(1) not in want and sub.get("id") is not None:
                report["sub_issues"].append({"epic": view.ident, "remove": match.group(1),
                                             "number": sub.get("number")})
                if not dry_run:
                    gh_repo.remove_sub_issue(number, int(sub["id"]))
    if api is None and epic_views:
        report["notes"] += ["API sub-issues non sondée (essai sans epic projeté) : sous-issues "
                           "ou liste de tâches décidées à la première projection réelle"]
    report["sub_issues_api"] = api
    return report
