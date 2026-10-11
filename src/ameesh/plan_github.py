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
    lot précis (`work_items`) : la PR ne ferme alors que ceux-là ; L126 : un
    mot-clé de fermeture de GitHub (« Closes #12 ») désigne de même le lot
    dont l'issue est #12 (son `issue_ref`). Seuls les
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

`ameesh work project-github --app P [--repo R] [--dry-run]` (L126)
    une issue par LOT (`work_items`) du projet P, fiche du plan ou non :
    créée une fois pour chaque lot ouvert, tenue à jour (titre, état,
    assigné, priorité), fermée avec un commentaire qui dit pourquoi quand le
    lot est fusionné, promu ou fermé ; `issue_ref` du lot renseigné. Le
    dépôt vient de la configuration de l'hôte (clé `github.projects`), la
    visibilité de l'API GitHub (en cache) : un dépôt PUBLIC ne reçoit que le
    titre et un résumé public court, jamais le corps interne du lot, et tout
    texte publié passe un contrôle (motifs de secret, termes exclus par
    l'organisation ; et, pour un dépôt public, adresses IP, noms d'hôte,
    chemins locaux, adresses électroniques, identifiants de compte).
    Marqueur `<!-- ameesh:work=<n> d=<empreintes> -->` : un champ qu'un
    humain a changé dans GitHub est gardé tant que le lot ne change pas ce
    champ (jamais réimporté). `ameesh notify` mène la projection sur l'hôte
    désigné (`github.host`) : un tour complet par `github.interval`, et les
    lots nouveaux dès le passage suivant leur création.

`gh` est résolu comme pour le connecteur `git-merge` (`AMEESH_GH_BIN`,
`AMEESH_BIN_DIR/gh`, PATH) ; les tests utilisent un faux `gh`.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Iterable

from . import plan as plan_mod
from . import projects as projects_mod
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
                "blocked": "b60205", "waiting_human": "d93f0b", "epic": "5319e7", "": "ededed",
                # L126 : étiquettes des issues de lot (état, type, priorité)
                "promoted": "0e8a16", "closed": "cccccc", "bug": "d73a4a",
                "evolution": "a2eeef", "improvement": "7057ff", "p1": "b60205",
                "p2": "fbca04", "p3": "c2e0c6"}
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


#: L126 : mots-clés de fermeture de GitHub (« Closes #12 », « fixes: o/r#3 »)
_CLOSES_RE = re.compile(
    r"(?i)(?<![\w-])(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)[ \t]*:?[ \t]+"
    r"(?:([A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}))?#([1-9][0-9]{0,17})\b")


def issues_of_pr(pr: dict, repo: str) -> list[str]:
    """Les issues qu'une PR ferme (`owner/repo#n`) : mots-clés de fermeture de
    son corps ; `#n` seul désigne une issue du dépôt de la PR."""
    refs = {"%s#%s" % (m.group(1) or repo, m.group(2))
            for m in _CLOSES_RE.finditer(pr.get("body") or "")}
    return sorted(refs, key=lambda r: (r.rpartition("#")[0].casefold(),
                                       int(r.rpartition("#")[2])))


def _lots_by_issue_ref(st) -> dict:
    """{issue_ref sans casse: [lots]} des lots qui portent une issue_ref."""
    out: dict = {}
    for item in st.work.issue_feed(FEED_LIMIT):
        ref = (item.get("issue_ref") or "").strip().casefold()
        if ref:
            out.setdefault(ref, []).append(int(item["id"]))
    return out


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
    by_issue: dict | None = None
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
        closes = issues_of_pr(pr, repo)
        if closes:
            # L126 : « Closes #n » désigne le lot dont l'issue est n (issue_ref)
            if by_issue is None:
                by_issue = _lots_by_issue_ref(st)
            explicit = sorted(set(explicit) | {lot for issue in closes
                                               for lot in by_issue.get(issue.casefold(), ())})
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


def _clean(body: str | None, marker_re=None) -> str:
    """Le corps sans marqueur, fins de ligne normalisées."""
    text = (marker_re or _MARKER_RE).sub("", (body or "").replace("\r\n", "\n")).strip()
    return "\n".join(line.rstrip() for line in text.split("\n"))


def _own_labels(labels: Iterable) -> list[str]:
    names = [(lab.get("name") if isinstance(lab, dict) else str(lab)) for lab in labels or []]
    return sorted(n for n in names if n and (n == BASE_LABEL or n.startswith(BASE_LABEL + ":")))


def _fields(title: str, body: str, labels: Iterable, state: str, marker_re=None) -> dict:
    return {"title": (title or "").strip(), "body": _clean(body, marker_re),
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

    def create_label(self, name: str, description: str | None = None) -> None:
        key = name.split(":", 1)[1] if ":" in name else ""
        _json(self.gh, ["api", "-X", "POST", "repos/%s/labels" % self.repo, "--input", "-"],
              json.dumps({"name": name, "color": LABEL_COLORS.get(key, "ededed"),
                          "description": description or "projection ameesh (L29)"}))

    def info(self) -> dict | None:
        """Le dépôt (`gh api repos/R`), ou None s'il est illisible (L126)."""
        code, out, _err = self.gh.run(["api", "repos/%s" % self.repo])
        if code != 0:
            return None
        try:
            data = json.loads(out) if out.strip() else None
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def comment(self, number: int, body: str) -> dict:
        return _json(self.gh, ["api", "-X", "POST",
                               "repos/%s/issues/%d/comments" % (self.repo, number),
                               "--input", "-"], json.dumps({"body": body})) or {}

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


# ==========================================================================
# L126 : une issue GitHub par lot, créée et tenue par ameesh
# ==========================================================================

#: lots lus au plus par une projection (ouverts, ou porteurs d'une issue_ref)
FEED_LIMIT = 5000
#: issues créées au plus par passage (limites secondaires de GitHub sur la
#: création de contenu) : le reste attend le passage suivant
MAX_CREATES = 40
#: bornes des textes publiés (GitHub : titre 256 caractères, corps 65 536)
TITLE_MAX = 256
BODY_MAX = 30000
SUMMARY_MAX = 300
#: clé `github` de la configuration de l'hôte : clés admises, cadence
SETTINGS_KEYS = ("projects", "host", "interval", "exclude_terms")
PROJECT_KEYS = ("repo",)
DEFAULT_INTERVAL = 300.0
MIN_INTERVAL = 60.0
#: cache de la visibilité d'un dépôt (secondes) ; un dépôt illisible vaut
#: « public » (un doute ne publie pas le corps) et n'est gardé qu'une minute
VISIBILITY_TTL = 3600.0
VISIBILITY_RETRY = 60.0
#: hôtes des URL admises dans un dépôt public (avec `forge_hosts`)
URL_HOSTS = ("github.com", "githubusercontent.com")

_WORK_MARKER_RE = re.compile(r"<!-- ameesh:work=([1-9][0-9]{0,17})(?: d=([0-9a-f.]*))? -->")
#: marqueur d'une issue fermée comme doublon : elle n'est plus retrouvée
_DUPLICATE_MARKER = "<!-- ameesh:work-duplicate=%d -->"
_ISSUE_REF_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100})#([1-9][0-9]{0,17})$")
#: ligne « Résumé public : … » du corps d'un lot : le seul texte libre du
#: corps qu'un dépôt public reçoit, après contrôle
_SUMMARY_RE = re.compile(r"(?im)^[ \t>*-]*(?:r[ée]sum[ée][ \t]+public|public[ \t]+summary)"
                         r"[ \t]*:[ \t]*(\S.*?)[ \t]*$")
#: mention GitHub (`@nom`) : neutralisée dans un texte publié, pour qu'une vue
#: d'ameesh n'envoie de notification à personne
_MENTION_RE = re.compile(r"(?<![\w`@])@(?=[A-Za-z0-9])")

LOT_TERMINAL = ("merged", "promoted", "closed")
LOT_TYPES = ("bug", "evolution", "improvement")
PRIORITY_LABELS = {1: "p1", 2: "p2", 3: "p3"}
_TYPE_FR = {"bug": "bug", "evolution": "évolution", "improvement": "amélioration"}
_PRIORITY_FR = {1: "priorité haute", 2: "priorité normale", 3: "priorité basse"}
_STATE_FR = {"intake": "à faire", "build": "en cours", "qa": "en relecture",
             "merged": "fusionné", "promoted": "livré", "blocked": "bloqué",
             "waiting_human": "attend un humain", "closed": "fermé"}
_LOT_FIELD_FR = {"title": "titre", "body": "corps", "labels": "étiquettes ameesh",
                 "state": "état"}


class SettingsError(GithubError):
    """Clé `github` de la configuration de l'hôte invalide."""


@dataclass(frozen=True)
class Settings:
    """La clé `github` de la configuration de l'hôte, validée (L126)."""

    #: {projet: "owner/repo"} — le projet d'un lot est celui de `ameesh projects`
    projects: dict = field(default_factory=dict)
    #: hôte qui mène la projection automatique (un seul, pour éviter les doublons)
    host: str | None = None
    #: secondes entre deux tours complets d'un projet
    interval: float = DEFAULT_INTERVAL
    #: termes que l'organisation ne publie jamais (noms de clients…), sans casse
    exclude_terms: tuple = ()

    def project_name(self, project: str | None) -> str | None:
        """Le nom configuré d'un projet (sans casse), ou None."""
        key = (project or "").strip().casefold()
        if not key:
            return None
        return next((name for name in self.projects if name.casefold() == key), None)

    def repo_for(self, project: str | None) -> str | None:
        name = self.project_name(project)
        return self.projects[name] if name else None


def parse_settings(raw) -> Settings:
    """Valide la clé `github` (SettingsError : message actionnable)."""
    if raw is None or raw == {}:
        return Settings()
    if not isinstance(raw, dict):
        raise SettingsError("`github` : objet JSON attendu")
    unknown = sorted(set(map(str, raw)) - set(SETTINGS_KEYS))
    if unknown:
        raise SettingsError("`github` : clé(s) inconnue(s) %s (admises : %s)"
                            % (", ".join(unknown), ", ".join(SETTINGS_KEYS)))
    declared = raw.get("projects")
    declared = {} if declared is None else declared
    if not isinstance(declared, dict):
        raise SettingsError("`github.projects` : objet {\"projet\": \"owner/repo\"} attendu")
    projects: dict = {}
    for name, value in declared.items():
        name = str(name).strip()
        where = "`github.projects.%s`" % name
        if not name:
            raise SettingsError("`github.projects` : nom de projet vide")
        if any(other.casefold() == name.casefold() for other in projects):
            raise SettingsError("%s : projet déclaré deux fois (sans casse)" % where)
        if isinstance(value, dict):
            extra = sorted(set(map(str, value)) - set(PROJECT_KEYS))
            if extra:
                raise SettingsError("%s : clé(s) inconnue(s) %s (admise : %s)"
                                    % (where, ", ".join(extra), ", ".join(PROJECT_KEYS)))
            value = value.get("repo")
        if not isinstance(value, str) or not _REPO_RE.fullmatch(value.strip()):
            raise SettingsError("%s : dépôt « owner/repo » attendu, pas %r" % (where, value))
        projects[name] = value.strip()
    host = raw.get("host")
    if host is not None and not isinstance(host, str):
        raise SettingsError("`github.host` : nom d'hôte attendu")
    interval = raw.get("interval", DEFAULT_INTERVAL)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) \
            or interval < MIN_INTERVAL:
        raise SettingsError("`github.interval` : un nombre de secondes, au moins %d"
                            % MIN_INTERVAL)
    terms = raw.get("exclude_terms")
    terms = [] if terms is None else terms
    if not isinstance(terms, list) or not all(isinstance(t, str) and t.strip() for t in terms):
        raise SettingsError("`github.exclude_terms` : liste de termes non vides attendue")
    return Settings(projects=projects, host=(host or "").strip() or None,
                    interval=float(interval),
                    exclude_terms=tuple(dict.fromkeys(t.strip() for t in terms)))


def settings_of(cfg) -> Settings:
    """Les réglages `github` de la configuration de l'hôte (vides sans la clé)."""
    return parse_settings(getattr(cfg, "github", None) or {})


# -- contrôle de ce qui est publié -------------------------------------------

#: motifs de secret : jamais publiés, quel que soit le dépôt
SECRET_PATTERNS = tuple((label, re.compile(rx)) for label, rx in (
    ("jeton GitHub", r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    ("clé d'API", r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),
    ("clé AWS", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("clé Scaleway", r"\bSCW[0-9A-Z]{17}\b"),
    ("clé Google", r"\bAIza[0-9A-Za-z_-]{35}"),
    ("jeton Slack", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("jeton npm ou PyPI", r"\bnpm_[A-Za-z0-9]{36}\b|\bpypi-[A-Za-z0-9_-]{40,}"),
    ("webhook", r"(?i)hooks\.slack\.com/services/[A-Za-z0-9/_-]+"
                r"|discord(?:app)?\.com/api/webhooks/[A-Za-z0-9/_-]+"),
    ("clé privée", r"-----BEGIN [A-Z0-9 -]*PRIVATE KEY"),
    ("jeton JWT", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ("identifiants dans une URL", r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@"),
    ("jeton d'autorisation", r"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]{20,}"
                             r"|\bauthorization[ \t]*:[ \t]*(?:basic|token)[ \t]+\S{8,}"),
    ("secret en clair", r"(?i)(?<![a-z0-9])(?:password|passwd|pwd|mot[ _-]de[ _-]passe"
                        r"|secret|token|jeton|api[_-]?key|access[_-]?key|private[_-]?key"
                        r"|client[_-]?secret)[\"']?[ \t]*[:=][ \t]*[\"']?"
                        r"(?=[^\s\"']*[0-9])(?=[^\s\"']*[A-Za-z])[^\s\"']{12,}"),
))

#: ce qu'un dépôt PUBLIC ne reçoit jamais, en plus des secrets et des termes
#: exclus : adresses, chemins locaux, identifiants de compte, noms d'hôte
PUBLIC_PATTERNS = tuple((label, re.compile(rx)) for label, rx in (
    ("adresse IP", r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
                   r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?![\w.])"),
    ("adresse IP", r"(?i)(?<![\w:])(?:(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}"
                   r"|[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?"
                   r"|::[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)(?![\w:])"),
    ("adresse électronique", r"(?i)(?<![\w.%+-])[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*"
                             r"\.[a-z]{2,}\b"),
    ("chemin local", r"(?<![\w.~/-])~[\w.-]*/[\w.~/-]*"
                     r"|(?<![\w.:/-])/(?:home|Users|root|tmp|var|etc|srv|opt|mnt|media|run"
                     r"|usr|private|Volumes|data|proc|sys|dev|nix|snap)/[\w.~/-]*"
                     r"|(?<!\w)[A-Za-z]:\\\S+"),
    ("identifiant de compte", r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}"
                              r"-[0-9a-f]{12}\b|\borg-[A-Za-z0-9]{20,}\b|\bacct_[A-Za-z0-9]{8,}"),
    ("nom d'hôte", r"(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:local|lan|internal|intranet|intra"
                   r"|corp|home|localdomain|private)\b"),
))
_URL_HOST_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://(?:[^\s/@]*@)?([^\s/:?#\[\]]+)")


@functools.lru_cache(maxsize=512)
def _term_re(term: str):
    """Un terme entier, sans casse : ni lettre ni chiffre accolé."""
    return re.compile(r"(?i)(?<![^\W_])%s(?![^\W_])" % re.escape(term.strip()))


@dataclass(frozen=True)
class Guard:
    """Le contrôle d'un texte avant sa publication dans GitHub (L126).

    `problems` rend les raisons d'un refus, par catégorie, jamais le texte
    trouvé : un secret ou un terme exclu ne se recopie dans aucun journal."""

    exclude_terms: tuple = ()
    host_names: tuple = ()
    url_hosts: tuple = URL_HOSTS

    def problems(self, text: str | None, *, public: bool) -> list[str]:
        text = text or ""
        found: list[str] = []

        def add(label: str) -> None:
            if label not in found:
                found.append(label)

        for label, rx in SECRET_PATTERNS:
            if rx.search(text):
                add("motif de secret (%s)" % label)
        if any(_term_re(term).search(text) for term in self.exclude_terms):
            add("terme exclu par l'organisation")
        if not public:
            return found
        for label, rx in PUBLIC_PATTERNS:
            if rx.search(text):
                add(label)
        if any(_term_re(name).search(text) for name in self.host_names):
            add("nom d'hôte")
        for match in _URL_HOST_RE.finditer(text):
            host = match.group(1).casefold().rstrip(".")
            if not any(host == ok or host.endswith("." + ok) for ok in self.url_hosts):
                add("nom d'hôte")
                break
        return found


def make_guard(settings: Settings, *, hosts: Iterable = (), forge_hosts: Iterable = ()) -> Guard:
    """Le contrôle de l'organisation : termes exclus de la configuration,
    noms des hôtes connus du mesh (3 caractères au moins), URL de la forge."""
    names = sorted({str(h).strip() for h in hosts if h and len(str(h).strip()) >= 3},
                   key=str.casefold)
    allowed = tuple(dict.fromkeys(str(h).strip().casefold()
                                  for h in (*URL_HOSTS, *forge_hosts) if h and str(h).strip()))
    return Guard(exclude_terms=tuple(settings.exclude_terms), host_names=tuple(names),
                 url_hosts=allowed)


def repo_visibility(gh_repo: "_Repo", *, cache: dict | None = None,
                    now: float | None = None) -> str:
    """`public`, `private`, `internal`, ou `unknown` (dépôt illisible), lue par
    l'API GitHub et gardée en cache (`VISIBILITY_TTL` ; un échec,
    `VISIBILITY_RETRY`). Seul `private` autorise le corps d'un lot."""
    now = time.time() if now is None else float(now)
    key = gh_repo.repo.casefold()
    if cache is not None and key in cache:
        value, until = cache[key]
        if now < until:
            return value
    info = gh_repo.info()
    value = "unknown"
    if info is not None:
        declared = str(info.get("visibility") or "").strip().lower()
        if declared in ("public", "private", "internal"):
            value = declared
        elif isinstance(info.get("private"), bool):
            value = "private" if info["private"] else "public"
    if cache is not None:
        cache[key] = (value, now + (VISIBILITY_RETRY if value == "unknown" else VISIBILITY_TTL))
    return value


# -- l'issue voulue d'un lot ---------------------------------------------------

def _ref_parts(ref: str | None) -> tuple[str, int] | None:
    """(`owner/repo`, numéro) d'une issue_ref GitHub, sinon None."""
    match = _ISSUE_REF_RE.match((ref or "").strip())
    return (match.group(1), int(match.group(2))) if match else None


def lot_project(item: dict) -> str | None:
    """Le projet d'un lot, comme `ameesh projects` (L62) : `app`, chantier,
    équipe de sa fiche du plan (à défaut, son équipe : file d'amélioration,
    L119), projet (équipe ou chantier) de son assigné."""
    agent = projects_mod.agent_project({"team": item.get("assignee_team"),
                                        "chantier": item.get("assignee_chantier")})
    lot = dict(item, package_team=item.get("package_team") or item.get("team"))
    return projects_mod.lot_project(lot, {item.get("assignee") or "": agent})


def needs_issue(item: dict) -> bool:
    """Un lot ouvert sans issue_ref : son issue reste à créer."""
    return item.get("state") not in LOT_TERMINAL and not (item.get("issue_ref") or "").strip()


def _priority(item: dict) -> int | None:
    try:
        value = int(item.get("priority"))
    except (TypeError, ValueError):
        return None
    return value if value in PRIORITY_LABELS else None


def lot_labels(item: dict) -> list[str]:
    """`ameesh`, `ameesh:<état>`, `ameesh:<type>`, `ameesh:p<priorité>`."""
    labels = {BASE_LABEL}
    if item.get("state"):
        labels.add("%s:%s" % (BASE_LABEL, item["state"]))
    if item.get("type") in LOT_TYPES:
        labels.add("%s:%s" % (BASE_LABEL, item["type"]))
    priority = _priority(item)
    if priority:
        labels.add("%s:%s" % (BASE_LABEL, PRIORITY_LABELS[priority]))
    return sorted(labels)


def lot_issue_state(item: dict) -> tuple[str, str | None]:
    """(état de l'issue, raison de fermeture) d'après l'état du lot."""
    state = item.get("state")
    if state in work_mod.MERGED_STATES:
        return "closed", "completed"
    if state == "closed":
        return "closed", "not_planned"
    return "open", None


def public_summary(body: str | None) -> str | None:
    """La ligne « Résumé public : … » du corps d'un lot, bornée, ou None."""
    match = _SUMMARY_RE.search(body or "")
    return _cut(" ".join(match.group(1).split()), SUMMARY_MAX) if match else None


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _quiet(text: str) -> str:
    """Le texte publié : mentions neutralisées, marqueurs d'ameesh désactivés."""
    text = text.replace("<!-- ameesh:", "<!-- (ameesh) ")
    return _MENTION_RE.sub("@⁠", text)


def _one_line(exc) -> str:
    return " ".join(str(exc).split())[:300]


@dataclass
class LotView:
    """L'issue voulue pour un lot (L126)."""

    item_id: int
    title: str
    body: str
    labels: list[str]
    state: str                      # open | closed
    state_reason: str | None        # completed | not_planned
    closing: str | None             # pourquoi l'issue est fermée (commentaire)
    refused: list[str] = field(default_factory=list)    # titre non publiable
    withheld: list[dict] = field(default_factory=list)  # parties retenues


def _assignee_text(item: dict, public: bool, guard: Guard) -> str:
    who = (item.get("assignee") or "").strip()
    if not who:
        return "non assigné"
    if who.startswith(work_mod.HUMAN_PREFIX):
        return "assigné à un humain" if public else "assigné à `%s`" % who
    if guard.problems(who, public=public):
        return "assigné à un agent"
    return "assigné à `%s`" % who


def _lot_head(item: dict, *, public: bool, guard: Guard, project: str,
              minimal: bool = False) -> list[str]:
    parts = ["**Lot ameesh n° %d**" % int(item["id"]),
             _TYPE_FR.get(item.get("type"), item.get("type") or "lot")]
    priority = _priority(item)
    if priority:
        parts.append(_PRIORITY_FR[priority])
    if minimal:
        parts.append("assigné" if item.get("assignee") else "non assigné")
    else:
        parts.append(_assignee_text(item, public, guard))
    lines = [" · ".join(parts)]
    planning = _plan_line(item)
    if planning:
        lines.append(planning)
    if not public and not minimal:
        extra = ["projet `%s`" % project] if project else []
        if (item.get("workstream") or "").strip():
            extra.append("chantier `%s`" % item["workstream"].strip())
        if item.get("package_id"):
            extra.append("fiche du plan `%s`" % item["package_id"])
        if extra:
            text = " · ".join(extra)
            lines.append(text[0].upper() + text[1:])
    return lines


def _day_fr(day: str | None) -> str | None:
    """« 2026-10-12 » → « 12/10/2026 »."""
    text = str(day or "")[:10]
    parts = text.split("-")
    return "%s/%s/%s" % (parts[2], parts[1], parts[0]) if len(parts) == 3 else None


def _plan_line(item: dict) -> str | None:
    """L157 : la ligne de planification de l'en-tête — durée estimée, dates
    prévues (L96) et, une fois le lot livré, la durée réelle et l'écart.
    Aucun nom ni aucune source : des nombres et des dates seulement. None
    sans estimation ni date prévue (l'issue reste inchangée)."""
    from . import estimates

    minutes = item.get("estimate_minutes")
    dates = [(label, _day_fr(item.get(key))) for label, key in (
        ("début", "planned_start"), ("fin", "planned_end"),
        ("livraison", "planned_delivery"))]
    dates = [(label, day) for label, day in dates if day]
    if not minutes and not dates:
        return None
    parts = ["Durée estimée %s" % estimates.label(minutes) if minutes
             else "Durée non estimée"]
    if dates:
        parts.append("prévu : " + ", ".join("%s %s" % pair for pair in dates))
    if item.get("merged_ts") is not None and item.get("state") in work_mod.MERGED_STATES:
        v = estimates.view(item, merged_ts=item.get("merged_ts"))
        if v.get("actual_minutes") is not None:
            text = "réel %s" % estimates.label(v["actual_minutes"])
            if v.get("gap_minutes") is not None:
                text += " (%s%s, %s)" % ("+" if v["gap_minutes"] >= 0 else "−",
                                        estimates.label(abs(v["gap_minutes"])),
                                        estimates.ratio_text(v["ratio"]))
            parts.append(text)
    return " · ".join(parts)


def _lot_footer(item: dict) -> str:
    n = int(item["id"])
    return ("---\n_Issue tenue par ameesh à partir du lot n° %d : son titre, son état et ses "
            "étiquettes `ameesh:…` suivent le lot ; une modification faite ici n'est pas "
            "reprise dans ameesh. Une PR qui livre ce lot porte `ameesh-work: %d` et "
            "`Closes #<numéro de cette issue>`._" % (n, n))


def _compose(item: dict, *, public: bool, guard: Guard,
             project: str) -> tuple[str | None, list[dict]]:
    """Le corps de l'issue et les parties retenues. Dépôt public : en-tête et
    « Résumé public » ; dépôt privé : en-tête et corps du lot. Le texte entier
    repasse le contrôle ; s'il échoue, l'en-tête se réduit au minimum."""
    withheld: list[dict] = []
    middle: list[str] = []
    n = int(item["id"])
    if public:
        summary = public_summary(item.get("body"))
        if summary:
            reasons = guard.problems(summary, public=True)
            if reasons:
                withheld.append({"field": "résumé public", "reasons": reasons})
            else:
                middle.append(_quiet(summary))
    else:
        text = _clean(item.get("body"), _WORK_MARKER_RE)
        if text:
            reasons = guard.problems(text, public=False)
            if reasons:
                withheld.append({"field": "corps", "reasons": reasons})
                middle.append("_Le corps du lot n'est pas publié ici (%s) : voir `ameesh work "
                              "show %d`._" % (", ".join(reasons), n))
            else:
                middle.append(_quiet(_cut(text, BODY_MAX)))

    def join(head: list[str], parts: list[str]) -> str:
        return "\n\n".join(["\n".join(head)] + parts + [_lot_footer(item)])

    body = join(_lot_head(item, public=public, guard=guard, project=project), middle)
    reasons = guard.problems(body, public=public)
    if not reasons:
        return body, withheld
    # un nom de l'en-tête (assigné, projet, chantier, fiche) ne passe pas
    withheld.append({"field": "en-tête", "reasons": reasons})
    minimal = _lot_head(item, public=public, guard=guard, project=project, minimal=True)
    for parts in (middle, []):
        body = join(minimal, parts)
        if not guard.problems(body, public=public):
            return body, withheld
    return None, withheld


def _closing(item: dict, *, repo: str, public: bool, issue_of) -> str | None:
    """Le commentaire de fermeture : pourquoi le lot est terminé."""
    n = int(item["id"])
    state = item.get("state")
    if state == "merged":
        pr = _ref_parts(item.get("pr_ref"))
        where = ""
        if pr and pr[0].casefold() == repo.casefold():
            where = " par la PR #%d" % pr[1]
        elif pr and not public:
            where = " par la PR %s#%d" % pr
        return "Lot n° %d fusionné%s : issue fermée par ameesh." % (n, where)
    if state == "promoted":
        return "Lot n° %d livré : issue fermée par ameesh." % n
    if state == "closed":
        other = item.get("superseded_by")
        if item.get("close_reason") == "superseded" and other:
            number = issue_of(int(other))
            return ("Lot n° %d remplacé par le lot n° %d%s : issue fermée par ameesh."
                    % (n, int(other), " (#%d)" % number if number else ""))
        return "Lot n° %d abandonné : issue fermée par ameesh." % n
    return None


def lot_view(item: dict, *, public: bool, guard: Guard, project: str, repo: str,
             issue_of=lambda _lot: None) -> LotView:
    """L'issue voulue pour le lot `item` dans `repo` (public ou privé)."""
    title = _cut(" ".join(str(item.get("title") or "").split()), TITLE_MAX) \
        or "Lot n° %d" % int(item["id"])
    refused = guard.problems(title, public=public)
    body, withheld = _compose(item, public=public, guard=guard, project=project)
    if body is None:
        refused = refused + [r for part in withheld for r in part["reasons"]
                             if r not in refused]
        body = ""
    state, reason = lot_issue_state(item)
    closing = _closing(item, repo=repo, public=public, issue_of=issue_of)
    if closing and guard.problems(closing, public=public):
        closing = "Lot n° %d terminé : issue fermée par ameesh." % int(item["id"])
    return LotView(item_id=int(item["id"]), title=title, body=body, labels=lot_labels(item),
                   state=state, state_reason=reason, closing=closing,
                   refused=refused, withheld=withheld)


def _lot_marker(item_id: int, fields: dict) -> str:
    return "<!-- ameesh:work=%d d=%s -->" % (int(item_id), _stamp(fields))


def _restamp(body: str | None, marker: str) -> str:
    """Le corps actuel de l'issue (gardé tel quel), marqueur remplacé ou ajouté."""
    body = (body or "").replace("\r\n", "\n")
    if _WORK_MARKER_RE.search(body):
        return _WORK_MARKER_RE.sub(lambda _m: marker, body, count=1)
    return marker + "\n" + body


def _foreign_labels(issue: dict) -> list[str]:
    """Les étiquettes qui ne sont pas à ameesh : jamais touchées."""
    names = [(lab.get("name") if isinstance(lab, dict) else str(lab))
             for lab in issue.get("labels") or []]
    return sorted(n for n in names if n and not (n == BASE_LABEL
                                                 or n.startswith(BASE_LABEL + ":")))


def _three_way(view: LotView, issue: dict) -> tuple[dict, list, list, list]:
    """(champs voulus, à écrire, changés dans GitHub, gardés tels quels).

    Les empreintes du marqueur disent ce qu'ameesh a écrit la dernière fois.
    Un champ que le lot a changé depuis est écrit ; un champ qu'un humain a
    changé dans GitHub, et que le lot n'a pas changé, est gardé. Une issue
    sans empreintes (désignée par l'issue_ref, sans marqueur) est adoptée :
    seules les étiquettes `ameesh` y sont posées."""
    want = _fields(view.title, view.body, view.labels, view.state, _WORK_MARKER_RE)
    have = _fields(issue.get("title"), issue.get("body"), issue.get("labels"),
                   issue.get("state"), _WORK_MARKER_RE)
    recorded = (issue.get("_recorded") or "").split(".") if issue.get("_recorded") else []
    if len(recorded) == len(_FIELD_NAMES):
        ours = [k for k, d in zip(_FIELD_NAMES, recorded) if _digest(want[k]) != d]
        human = [k for k, d in zip(_FIELD_NAMES, recorded) if _digest(have[k]) != d]
    else:
        ours, human = ["labels"], []
    write = [k for k in ours if want[k] != have[k]]
    kept = [k for k in human if k not in ours]
    return want, write, human, kept


def _label_description(name: str) -> str:
    key = name.split(":", 1)[1] if ":" in name else ""
    if key in _STATE_FR:
        return "lot ameesh : %s" % _STATE_FR[key]
    if key in _TYPE_FR:
        return "lot ameesh : %s" % _TYPE_FR[key]
    for priority, label in PRIORITY_LABELS.items():
        if key == label:
            return "lot ameesh : %s" % _PRIORITY_FR[priority]
    return "issue tenue par ameesh"


def project_lots(db, gh, project: str, repo: str, *, dry_run: bool = False,
                 settings: Settings | None = None, feed: list | None = None,
                 cache: dict | None = None, forge_hosts: Iterable = (),
                 local_host: str | None = None, max_creates: int = MAX_CREATES,
                 now: float | None = None) -> dict:
    """Une issue par lot du projet `project` dans `repo` (L126). Rend le compte rendu.

    Crée l'issue de chaque lot ouvert qui n'en a pas (jamais d'issue neuve
    pour un lot déjà terminé), écrit ce que le lot a changé (titre, état,
    étiquettes, corps : assigné, priorité), ferme l'issue d'un lot fusionné,
    promu ou fermé avec un commentaire qui dit pourquoi, et pose `issue_ref`.
    Un lot est du projet par son `issue_ref`, par le marqueur d'une issue du
    dépôt, sinon par son projet (`lot_project`). `dry_run` : lectures seules,
    rien n'est écrit, ni dans GitHub ni en base."""
    check_repo(repo)
    project = (project or "").strip()
    if not project:
        raise GithubError("projet vide : --app <projet>")
    settings = settings or Settings()
    st = storage.of(db)
    feed = list(st.work.issue_feed(FEED_LIMIT) if feed is None else feed)
    gh_repo = _Repo(gh, repo, dry_run)
    visibility = repo_visibility(gh_repo, cache=cache, now=now)
    public = visibility != "private"
    report: dict = {"project": project, "repo": repo, "visibility": visibility,
                    "public": public, "dry_run": bool(dry_run), "issues": [],
                    "labels_created": [], "refused": [], "withheld": [], "human_edits": [],
                    "issue_refs": [], "duplicates": [], "orphans": [], "errors": []}
    issues = gh_repo.issues()
    by_number = {int(i["number"]): i for i in issues if i.get("number") is not None}
    markers: dict = {}
    for issue in sorted(issues, key=lambda i: int(i.get("number") or 0)):
        match = _WORK_MARKER_RE.search(issue.get("body") or "")
        if match:
            markers.setdefault(int(match.group(1)), []).append(
                dict(issue, _recorded=match.group(2)))
    lots = {int(i["id"]): i for i in feed}
    for lot_id in sorted(set(markers) - set(lots)):
        # lot terminé dont l'issue_ref n'a pas été posée : retrouvé par son marqueur
        item = work_mod.get(db, lot_id)
        if item is None:
            report["orphans"].append({
                "work_item": lot_id, "numbers": [int(i["number"]) for i in markers[lot_id]],
                "detail": "lot inconnu de ce mesh : issue laissée telle quelle"})
            continue
        lots[lot_id] = item
    elsewhere = {r.casefold() for r in settings.projects.values()} - {repo.casefold()}
    mine: list = []
    for lot_id, item in sorted(lots.items()):
        ref = _ref_parts(item.get("issue_ref"))
        here = bool(ref) and ref[0].casefold() == repo.casefold()
        if ref and not here and ref[0].casefold() in elsewhere:
            continue                    # suivi dans le dépôt d'un autre projet
        candidates = markers.get(lot_id, [])
        if not (here or candidates
                or (lot_project(item) or "").casefold() == project.casefold()):
            continue
        issue = None
        if here:
            issue = next((c for c in candidates if int(c["number"]) == ref[1]), None)
            if issue is None and ref[1] in by_number \
                    and not _WORK_MARKER_RE.search(by_number[ref[1]].get("body") or ""):
                issue = dict(by_number[ref[1]], _recorded=None)   # désignée : adoptée
        if issue is None and candidates:
            issue = candidates[0]
        for dup in candidates:
            if issue is not None and int(dup["number"]) != int(issue["number"]):
                report["duplicates"].append({"work_item": lot_id, "number": int(dup["number"]),
                                             "kept": int(issue["number"])})
        mine.append((item, issue))
    numbers = {int(item["id"]): int(issue["number"]) for item, issue in mine if issue}

    def issue_of(lot_id: int) -> int | None:
        if lot_id in numbers:
            return numbers[lot_id]
        ref = _ref_parts((lots.get(lot_id) or {}).get("issue_ref"))
        return ref[1] if ref and ref[0].casefold() == repo.casefold() else None

    hosts: set = set()
    if public:
        hosts = {local_host} | {i.get("assignee_host") for i in feed} \
            | {row.get("host") for row in st.hosts.current(None)}
    guard = make_guard(settings, hosts=[h for h in hosts if h], forge_hosts=forge_hosts)

    plans: list[dict] = []
    creates = 0
    for item, issue in mine:
        lot_id = int(item["id"])
        if issue is None and item.get("state") in LOT_TERMINAL:
            continue                    # jamais d'issue neuve pour un lot terminé
        view = lot_view(item, public=public, guard=guard, project=project, repo=repo,
                        issue_of=issue_of)
        for part in view.withheld:
            report["withheld"].append(dict(part, work_item=lot_id))
        entry = {"work_item": lot_id, "number": int(issue["number"]) if issue else None,
                 "title": None if view.refused else view.title, "state": view.state,
                 "labels": view.labels, "action": "unchanged", "changes": []}
        report["issues"].append(entry)
        if view.refused:
            report["refused"].append({"work_item": lot_id, "field": "titre",
                                      "reasons": view.refused,
                                      "number": int(issue["number"]) if issue else None,
                                      "title_digest": _digest(item.get("title") or "")})
        if issue is None:
            if view.refused:
                entry["action"] = "refused"
                continue
            if creates >= max_creates:
                entry["action"] = "deferred"
                continue
            creates += 1
            want = _fields(view.title, view.body, view.labels, view.state, _WORK_MARKER_RE)
            entry["action"] = "create"
            plans.append({"entry": entry, "item": item, "labels": view.labels,
                          "payload": {"title": view.title,
                                      "body": _lot_marker(lot_id, want) + "\n" + view.body,
                                      "labels": view.labels}})
            continue
        if view.refused:
            view.title = (issue.get("title") or "").strip()   # titre gardé tel quel
        want, write, human, kept = _three_way(view, issue)
        if human:
            report["human_edits"].append({
                "work_item": lot_id, "number": int(issue["number"]),
                "fields": [_LOT_FIELD_FR[k] for k in human],
                "kept": [_LOT_FIELD_FR[k] for k in kept],
                "replaced": [_LOT_FIELD_FR[k] for k in human if k not in kept]})
        payload: dict = {}
        if "title" in write:
            payload["title"] = view.title
        if "labels" in write:
            payload["labels"] = _foreign_labels(issue) + view.labels
        if "state" in write:
            payload["state"] = view.state
            if view.state == "closed" and view.state_reason:
                payload["state_reason"] = view.state_reason
        marker = _lot_marker(lot_id, want)
        if "body" in write:
            payload["body"] = marker + "\n" + view.body
        elif issue.get("_recorded") != _stamp(want):
            payload["body"] = _restamp(issue.get("body"), marker)
        if not payload:
            continue
        closing = "state" in write and view.state == "closed" \
            and (issue.get("state") or "").lower() == "open"
        entry["action"] = "close" if closing else "update"
        entry["changes"] = [_LOT_FIELD_FR[k] for k in write] or ["empreintes"]
        plans.append({"entry": entry, "item": item, "payload": payload,
                      "labels": view.labels if "labels" in write else [],
                      "comment": view.closing if closing else None})
        if closing:
            entry["comment"] = view.closing
    needed = sorted({lab for plan in plans for lab in plan["labels"]})
    if needed:
        existing = gh_repo.labels()
        for name in needed:
            if name not in existing:
                report["labels_created"].append(name)
                if not dry_run:
                    gh_repo.create_label(name, _label_description(name))
    if not dry_run:
        for plan in plans:
            _execute(db, st, gh_repo, report, plan)
        # issue retrouvée par son marqueur (ou adoptée) : l'issue_ref du lot la
        # désigne, sauf si elle désigne déjà une issue existante de ce dépôt
        for item, issue in mine:
            ref = _ref_parts(item.get("issue_ref"))
            if issue is None or (ref and ref[0].casefold() == repo.casefold()
                                 and ref[1] in by_number):
                continue
            _remember_ref(db, st, gh_repo, report, item, int(issue["number"]), created=False)
    return _tally_lots(report)


def _execute(db, st, gh_repo: "_Repo", report: dict, plan: dict) -> None:
    """Écrit une issue planifiée ; une erreur de GitHub est notée, jamais levée."""
    entry = plan["entry"]
    try:
        if entry["action"] == "create":
            created = gh_repo.create(plan["payload"])
            if not created.get("number"):
                raise GithubError("création sans numéro rendu")
            entry["number"] = int(created["number"])
            _remember_ref(db, st, gh_repo, report, plan["item"], entry["number"], created=True)
            return
        gh_repo.update(int(entry["number"]), plan["payload"])
        if plan.get("comment"):
            gh_repo.comment(int(entry["number"]), plan["comment"])
    except GithubError as exc:
        entry["error"] = _one_line(exc)
        report["errors"].append({"work_item": entry["work_item"], "number": entry.get("number"),
                                 "action": entry["action"], "detail": _one_line(exc)})


def _remember_ref(db, st, gh_repo: "_Repo", report: dict, item: dict, number: int, *,
                  created: bool) -> None:
    """Pose `issue_ref` = `owner/repo#n` sur le lot (comparer-et-poser).

    Une issue_ref d'un autre outil est gardée (l'issue se retrouve par son
    marqueur). Si un autre projecteur a posé la sienne entre-temps, l'issue
    que nous venons de créer est un doublon : fermée, marqueur neutralisé."""
    lot_id = int(item["id"])
    repo = gh_repo.repo
    ref = "%s#%d" % (repo, int(number))
    current = (item.get("issue_ref") or "").strip() or None
    if current and current.casefold() == ref.casefold():
        return
    parts = _ref_parts(current)
    if current and not (parts and parts[0].casefold() == repo.casefold()):
        if created:
            report["issue_refs"].append({
                "work_item": lot_id, "issue_ref": current, "result": "kept",
                "detail": "issue_ref existante gardée ; l'issue %s se retrouve par son "
                          "marqueur" % ref})
        return
    try:
        done = st.work.set_issue_ref(lot_id, ref, current=current)
    except Exception as exc:  # noqa: BLE001 - l'issue existe ; son marqueur la retrouvera
        report["errors"].append({"work_item": lot_id, "number": int(number),
                                 "action": "issue_ref", "detail": _one_line(exc)})
        return
    if done:
        item["issue_ref"] = ref
        report["issue_refs"].append({"work_item": lot_id, "issue_ref": ref, "result": "set"})
        return
    fresh = (work_mod.get(db, lot_id) or {}).get("issue_ref") or ""
    other = _ref_parts(fresh)
    if created and other and other[0].casefold() == repo.casefold() and other[1] != number:
        try:
            gh_repo.update(int(number), {
                "state": "closed", "state_reason": "not_planned",
                "body": (_DUPLICATE_MARKER % lot_id) + "\n_Doublon de #%d, qui suit le lot "
                        "n° %d._" % (other[1], lot_id)})
            gh_repo.comment(int(number), "Doublon de #%d, créée en même temps pour le même "
                                         "lot : fermée par ameesh." % other[1])
        except GithubError as exc:
            report["errors"].append({"work_item": lot_id, "number": int(number),
                                     "action": "duplicate", "detail": _one_line(exc)})
        report["duplicates"].append({"work_item": lot_id, "number": int(number),
                                     "kept": other[1], "closed": True})
        item["issue_ref"] = fresh
        return
    report["issue_refs"].append({"work_item": lot_id, "issue_ref": fresh or None,
                                 "result": "changed",
                                 "detail": "issue_ref modifiée entre-temps : gardée"})


def _tally_lots(report: dict) -> dict:
    actions = [row["action"] for row in report["issues"] if not row.get("error")]
    report["counts"] = {
        "created": actions.count("create"), "updated": actions.count("update"),
        "closed": actions.count("close"), "unchanged": actions.count("unchanged"),
        "refused": actions.count("refused"), "deferred": actions.count("deferred"),
        "errors": len(report["errors"])}
    return report


class Projector:
    """La projection automatique des lots en issues (L126), menée par `ameesh
    notify` à chaque passage, seulement sur l'hôte désigné (`github.host`) :
    un seul hôte écrit, pas de doublon. Tour complet d'un projet toutes les
    `github.interval` secondes ; entre deux, un lot nouveau (ouvert, sans
    issue_ref) déclenche le tour de son projet au passage suivant sa
    création. Journal sobre : ce qui change, chaque refus, retenue ou erreur
    une seule fois. Une erreur (GitHub, `gh`, base) ne lève jamais : le
    projet attend un intervalle."""

    def __init__(self, cfg, *, dry_run: bool = False, log=None, gh=None, clock=None):
        self.cfg = cfg
        self.dry_run = bool(dry_run)
        self.log = log or (lambda _text: None)
        self.gh = gh
        self.clock = clock or time.time
        self.cache: dict = {}
        self.last: dict = {}
        self.backoff: dict = {}
        self.said: set = set()
        self.skip: set = set()

    def _once(self, key, text: str) -> None:
        if key not in self.said:
            self.said.add(key)
            self.log(text)

    def tick(self, db, now: float | None = None) -> list[dict]:
        now = float(self.clock() if now is None else now)
        try:
            settings = settings_of(self.cfg)
        except SettingsError as exc:
            self._once(("settings", str(exc)),
                       "issues GitHub : configuration invalide, projection coupée : %s" % exc)
            return []
        if not settings.projects:
            return []
        here = getattr(self.cfg, "host", None)
        if not settings.host:
            self._once("no-host", "issues GitHub : aucun hôte désigné (`github.host`) : pas de "
                                  "projection automatique ; `ameesh work project-github --app "
                                  "<projet>` reste possible")
            return []
        if settings.host != here:
            self._once(("elsewhere", settings.host),
                       "issues GitHub : la projection est menée par l'hôte %s, pas %s"
                       % (settings.host, here))
            return []
        reports: list[dict] = []
        feed = None
        for name, repo in sorted(settings.projects.items(), key=lambda kv: kv[0].casefold()):
            if now < self.backoff.get(name, 0.0):
                continue
            last = self.last.get(name)
            try:
                if last is not None and now - last < settings.interval:
                    if self.dry_run:
                        continue
                    if feed is None:
                        feed = storage.of(db).work.issue_feed(FEED_LIMIT)
                    if not any(needs_issue(i)
                               and (int(i["id"]), _digest(i.get("title") or "")) not in self.skip
                               and (lot_project(i) or "").casefold() == name.casefold()
                               for i in feed):
                        continue
                if feed is None:
                    feed = storage.of(db).work.issue_feed(FEED_LIMIT)
                if self.gh is None:
                    self.gh = Gh()
                report = project_lots(db, self.gh, name, repo, dry_run=self.dry_run,
                                      settings=settings, feed=feed, cache=self.cache,
                                      forge_hosts=getattr(self.cfg, "forge_hosts", None) or (),
                                      local_host=here, now=now)
            except Exception as exc:  # noqa: BLE001 - GitHub, gh, base : jamais fatal
                self.backoff[name] = now + settings.interval
                self._once(("error", name, _one_line(exc)),
                           "issues GitHub %s (%s) : échec, nouvel essai dans %d s : %s"
                           % (name, repo, int(settings.interval), _one_line(exc)))
                continue
            self.last[name] = now
            if report["errors"]:
                # une écriture refusée n'est pas retentée à chaque passage
                self.backoff[name] = now + settings.interval
            self._journal(report)
            reports.append(report)
        return reports

    def _journal(self, report: dict) -> None:
        where = "%s (%s)" % (report["repo"], report["project"])
        counts = report["counts"]
        dry = report["dry_run"]
        parts = ["%d %s" % (counts[key], label) for key, label in (
            ("created", "à créer" if dry else "créée(s)"),
            ("updated", "à mettre à jour" if dry else "mise(s) à jour"),
            ("closed", "à fermer" if dry else "fermée(s)"),
            ("deferred", "reportée(s) au passage suivant"),
            ("errors", "en erreur")) if counts.get(key)]
        if parts:
            line = "issues GitHub %s : %s%s" % (where, ", ".join(parts), " [essai]" if dry else "")
            if dry:
                self._once(("dry", line), line)
            else:
                self.log(line)
        for row in report["refused"]:
            self.skip.add((row["work_item"], row["title_digest"]))
            self._once(("refused", row["work_item"], row["title_digest"], tuple(row["reasons"])),
                       "issues GitHub %s : lot %d, %s refusé (%s) : %s ; corrigez le titre du "
                       "lot" % (where, row["work_item"], row["field"], ", ".join(row["reasons"]),
                                "titre de #%d gardé tel quel" % row["number"] if row["number"]
                                else "non publié"))
        for row in report["withheld"]:
            self._once(("withheld", row["work_item"], row["field"], tuple(row["reasons"])),
                       "issues GitHub %s : lot %d — %s non publié (%s)"
                       % (where, row["work_item"], row["field"], ", ".join(row["reasons"])))
        for row in report["human_edits"]:
            if row["kept"]:
                self._once(("human", report["repo"], row["number"], tuple(row["kept"])),
                           "issues GitHub %s : #%d (lot %d) modifiée dans GitHub (%s) : "
                           "gardée telle quelle, non reprise dans ameesh"
                           % (where, row["number"], row["work_item"], ", ".join(row["kept"])))
        for row in report["errors"]:
            self._once(("write", report["repo"], row["work_item"], row["detail"]),
                       "issues GitHub %s : lot %d, %s en échec : %s"
                       % (where, row["work_item"], row["action"], row["detail"]))
        for row in report["duplicates"]:
            self._once(("dup", report["repo"], row["number"]),
                       "issues GitHub %s : #%d doublon de #%d (lot %d)%s"
                       % (where, row["number"], row["kept"], row["work_item"],
                          " : fermée" if row.get("closed") else " : laissée telle quelle"))
