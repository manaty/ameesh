# SPDX-License-Identifier: AGPL-3.0-only
"""#8195 : l'épique GitHub d'un sujet (tranche 4 de #8191, issue manaty/ameesh#6403).

Décision owner : une épique ameesh et une épique GitHub sont la sérialisation
l'une de l'autre, comme un ticket GitHub l'est d'un lot. Le sujet porte sa
référence `owner/repo#N` dans `work_subjects.github_ref` (0608).

- `work subject add <slug> --github owner/repo#N` adopte un ticket existant :
  il doit être lisible avec l'identité GitHub du mesh, sinon refus qui nomme
  l'accès manquant ;
- `work subject project <slug> --repo owner/repo` crée plus tard l'épique
  d'un sujet né sans `--github`.

Le corps du ticket reçoit UNE section délimitée par marqueurs
(`<!-- ameesh:subject=<slug> -->` … `<!-- ameesh:subject-end=<slug> -->`)
qui liste les lots du sujet et leur état. Elle est réécrite par la projection
(`plan_github.Projector`, hôte `github.host`) dès qu'un lot est ajouté ou
change d'état ; le texte hors marqueurs n'est jamais modifié. Le marqueur
`<!-- ameesh:` fait aussi ignorer le corps par la veille GitHub : seule la
fermeture du ticket y est lue (`github_watch.events_from`), et clôt le sujet.

Texte publié : titres contrôlés par `plan_github.Guard` (motifs de secret,
termes exclus ; et, sur un dépôt non privé, hôtes, adresses…), mentions
neutralisées. Un titre refusé est remplacé, jamais recopié dans un journal.
"""
from __future__ import annotations

import hashlib
import json
import re

from . import plan_github, storage
from . import work_lifecycle as lifecycle
from .work import WorkError

BEGIN = "<!-- ameesh:subject=%s -->"
END = "<!-- ameesh:subject-end=%s -->"
#: la marque cherchée par la veille GitHub pour reconnaître une épique de sujet
BEGIN_PREFIX = "<!-- ameesh:subject="
#: lots listés au plus dans une section (le corps d'un ticket est borné)
MAX_LOTS = 200
TITLE_MAX = 200
WITHHELD = "(title not published)"


def parse_ref(text: str) -> tuple[str, int]:
    """(`owner/repo`, N) de `owner/repo#N`, sinon refus."""
    match = plan_github._ISSUE_REF_RE.match((text or "").strip())
    if not match:
        raise WorkError("--github: owner/repo#N expected, not %r" % (text,))
    return match.group(1), int(match.group(2))


def ref_of(repo: str, number: int) -> str:
    return "%s#%d" % (repo, int(number))


def _access(ref: str, gesture: str, err: str) -> WorkError:
    return WorkError(
        "%s: cannot %s with the mesh's GitHub identity (%s): grant it access to the "
        "issues of %s (read and write), or check the reference"
        % (ref, gesture, plan_github._one_line(err or "no detail"), ref.split("#", 1)[0]))


def read_issue(gh, repo: str, number: int) -> dict:
    """Le ticket, lu avec l'identité du mesh ; refus qui nomme l'accès manquant."""
    ref = ref_of(repo, number)
    code, out, err = gh.run(["api", "repos/%s/issues/%d" % (repo, int(number))])
    if code != 0:
        raise _access(ref, "read the issue", err or out)
    try:
        issue = json.loads(out) if out.strip() else None
    except ValueError:
        issue = None
    if not isinstance(issue, dict):
        raise WorkError("%s: unreadable answer from GitHub" % ref)
    if issue.get("pull_request"):
        raise WorkError("%s is a pull request, not an issue" % ref)
    return issue


# -- la section ----------------------------------------------------------------

def _section_re(slug: str):
    return re.compile(re.escape(BEGIN % slug) + r".*?" + re.escape(END % slug), re.S)


def _done(item: dict) -> bool:
    return item.get("state") in lifecycle.COMPLETED


def _title(text: str, guard, public: bool) -> str:
    text = " ".join((text or "").split())
    if not text or guard.problems(text, public=public):
        return WITHHELD
    return plan_github._quiet(plan_github._cut(text, TITLE_MAX))


def render(subject: dict, lots: list[dict], *, guard, public: bool) -> str:
    """La section du sujet : ses lots, cochés une fois terminés."""
    slug = subject["slug"]
    lines = [BEGIN % slug,
             "### ameesh lots — %s" % _title(subject.get("title"), guard, public),
             "_Maintained by ameesh (subject `%s`): rewritten when a lot is added or "
             "changes state. Edit outside these markers._" % slug, ""]
    for item in lots[:MAX_LOTS]:
        state = item.get("legacy_state") or item.get("state") or "?"
        issue = (item.get("issue_ref") or "").strip()
        issue = " · %s" % issue if plan_github._ref_parts(issue) else ""
        lines.append("- [%s] ameesh #%d — %s (%s)%s" % (
            "x" if _done(item) else " ", int(item["id"]),
            _title(item.get("title"), guard, public), state, issue))
    if len(lots) > MAX_LOTS:
        lines.append("- … %d more lots (`ameesh work subject show %s`)"
                     % (len(lots) - MAX_LOTS, slug))
    if not lots:
        lines.append("_No lots yet._")
    lines.append(END % slug)
    return "\n".join(lines)


def splice(body: str | None, slug: str, section: str) -> str:
    """Le corps avec la section du sujet remplacée (ou ajoutée en fin) ; le
    texte hors marqueurs est rendu tel quel. Un marqueur orphelin (section
    abîmée à la main) est refusé plutôt que de deviner où elle finit."""
    body = body or ""
    pattern = _section_re(slug)
    if pattern.search(body):
        return pattern.sub(lambda _m: section, body, count=1)
    if BEGIN % slug in body or END % slug in body:
        raise WorkError("subject %s: the section markers of the issue are damaged (one "
                        "is missing); restore or delete both markers" % slug)
    if not body.strip():
        return section
    return body.rstrip("\n") + "\n\n" + section + "\n"


class Context:
    """Ce qu'il faut pour publier : le client `gh`, la garde et la visibilité
    (en cache) des dépôts."""

    def __init__(self, cfg, db, gh, *, cache: dict | None = None, local_host=None):
        self.gh = gh
        self.cache = {} if cache is None else cache
        try:
            settings = plan_github.settings_of(cfg) if cfg is not None else plan_github.Settings()
        except plan_github.SettingsError:
            settings = plan_github.Settings()
        hosts = {local_host or getattr(cfg, "host", None)}
        try:
            hosts |= {row.get("host") for row in storage.of(db).hosts.current(None)}
        except Exception:  # noqa: BLE001 - une garde plus stricte suit (dépôt public)
            pass
        self.guard = plan_github.make_guard(
            settings, hosts=[h for h in hosts if h],
            forge_hosts=getattr(cfg, "forge_hosts", None) or ())

    def public(self, repo: str) -> bool:
        """Tout dépôt qui n'est pas prouvé privé est traité comme public."""
        repo_ops = plan_github._Repo(self.gh, repo, False)
        return plan_github.repo_visibility(repo_ops, cache=self.cache) != "private"


def section_for(db, ctx: Context, subject: dict, repo: str) -> str:
    lots = storage.of(db).work.subject_items(subject_id=int(subject["id"]))
    return render(subject, lots, guard=ctx.guard, public=ctx.public(repo))


def write_section(db, ctx: Context, subject: dict, repo: str, number: int,
                  issue: dict | None = None, section: str | None = None) -> bool:
    """Réécrit la section dans le ticket ; True si le corps a changé."""
    issue = issue if issue is not None else read_issue(ctx.gh, repo, number)
    body = issue.get("body") or ""
    if section is None:
        section = section_for(db, ctx, subject, repo)
    new = splice(body, subject["slug"], section)
    if new == body:
        return False
    code, out, err = ctx.gh.run(["api", "-X", "PATCH", "repos/%s/issues/%d" % (repo, number),
                                 "--input", "-"], json.dumps({"body": new}))
    if code != 0:
        raise _access(ref_of(repo, number), "edit the issue body", err or out)
    return True


# -- adopter, créer --------------------------------------------------------------

def check_adoptable(db, gh, ref: str, *, subject: dict | None = None) -> tuple[str, int, dict]:
    """Avant toute écriture : la référence, le ticket lisible et ouvert, non
    adopté par un autre sujet, et le sujet sans autre épique."""
    repo, number = parse_ref(ref)
    ref = ref_of(repo, number)
    if subject is not None and subject.get("github_ref") \
            and subject["github_ref"].casefold() != ref.casefold():
        raise WorkError("subject %s already has its epic %s"
                        % (subject["slug"], subject["github_ref"]))
    others = [row for row in storage.of(db).work.subjects_by_github(ref)
              if subject is None or int(row["id"]) != int(subject["id"])]
    if others:
        raise WorkError("%s is already the epic of subject %s (%s)"
                        % (ref, others[0]["slug"], others[0]["project"]))
    issue = read_issue(gh, repo, number)
    if issue.get("state") == "closed":
        raise WorkError("%s is closed: reopen it or choose another issue" % ref)
    return repo, number, issue


def adopt(db, ctx: Context, subject: dict, ref: str, *, issue: dict | None = None) -> dict:
    """Adopte le ticket `ref` comme épique du sujet : section écrite, puis
    référence posée. Idempotent pour la même référence."""
    if subject["state"] != "open":
        raise WorkError("subject %s is closed" % subject["slug"])
    repo, number, read = check_adoptable(db, ctx.gh, ref, subject=subject)
    write_section(db, ctx, subject, repo, number, issue=issue or read)
    row = storage.of(db).work.subject_set_github(int(subject["id"]), ref_of(repo, number))
    if row is None:
        raise WorkError("%s was adopted by another subject meanwhile; the section of %s "
                        "stays in its body until removed by hand"
                        % (ref_of(repo, number), subject["slug"]))
    return row


def create_epic(db, ctx: Context, subject: dict, repo: str) -> dict:
    """Crée l'épique d'un sujet né sans `--github`, dans `repo`."""
    try:
        repo = plan_github.check_repo((repo or "").strip())
    except plan_github.GithubError as exc:
        raise WorkError("--repo: %s" % exc) from exc
    if subject["state"] != "open":
        raise WorkError("subject %s is closed" % subject["slug"])
    if subject.get("github_ref"):
        raise WorkError("subject %s already has its epic %s"
                        % (subject["slug"], subject["github_ref"]))
    if plan_github._Repo(ctx.gh, repo, False).info() is None:
        raise _access(repo, "read the repository", "")
    title = " ".join((subject.get("title") or "").split())
    if ctx.guard.problems(title, public=ctx.public(repo)):
        raise WorkError("subject %s: its title cannot be published in %s (%s); "
                        "create the issue by hand and adopt it with --github"
                        % (subject["slug"], repo,
                           ", ".join(ctx.guard.problems(title, public=ctx.public(repo)))))
    body = section_for(db, ctx, subject, repo)
    code, out, err = ctx.gh.run(["api", "-X", "POST", "repos/%s/issues" % repo, "--input", "-"],
                                json.dumps({"title": plan_github._quiet(title), "body": body}))
    if code != 0:
        raise _access(repo, "create an issue", err or out)
    try:
        number = int(json.loads(out)["number"])
    except (ValueError, KeyError, TypeError) as exc:
        raise WorkError("%s: unreadable answer from GitHub after creating the epic" % repo) from exc
    row = storage.of(db).work.subject_set_github(int(subject["id"]), ref_of(repo, number))
    if row is None:
        raise WorkError("%s created, but subject %s got another epic meanwhile; close "
                        "the new issue by hand" % (ref_of(repo, number), subject["slug"]))
    return row


# -- clôture et projection -------------------------------------------------------

def close_by_issue(db, ref: str, *, actor: str = "github") -> list[dict]:
    """Le ticket `ref` a été fermé côté GitHub : clôt ses sujets ouverts."""
    st = storage.of(db)
    closed = []
    for subject in st.work.subjects_by_github(ref):
        if subject["state"] == "open":
            closed.append(st.work.subject_close(int(subject["id"]), actor=actor) or subject)
    return closed


def sync(cfg, db, gh, *, written: dict, cache: dict | None = None, log=None) -> dict:
    """Un passage de la projection : réécrit la section des sujets ouverts
    dont la liste a changé depuis la dernière écriture (`written` : {id du
    sujet: empreinte}, gardé par l'appelant). Un ticket trouvé fermé clôt
    son sujet. Une erreur reste par sujet et n'arrête pas les autres."""
    log = log or (lambda _text: None)
    report = {"updated": 0, "closed": 0, "errors": []}
    subjects = [s for s in storage.of(db).work.subjects(open_only=True) if s.get("github_ref")]
    if not subjects:
        return report
    ctx = Context(cfg, db, gh, cache=cache)
    for subject in subjects:
        try:
            repo, number = parse_ref(subject["github_ref"])
            section = section_for(db, ctx, subject, repo)
            stamp = hashlib.sha256(section.encode("utf-8")).hexdigest()
            if written.get(int(subject["id"])) == stamp:
                continue
            issue = read_issue(gh, repo, number)
            if issue.get("state") == "closed":
                close_by_issue(db, subject["github_ref"],
                               actor="github:%s" % subject["github_ref"])
                report["closed"] += 1
                log("GitHub epic %s closed: subject %s (%s) closed"
                    % (subject["github_ref"], subject["slug"], subject["project"]))
                continue
            if write_section(db, ctx, subject, repo, number, issue=issue, section=section):
                report["updated"] += 1
            written[int(subject["id"])] = stamp
        except (WorkError, plan_github.GithubError) as exc:
            report["errors"].append({"subject": subject["slug"], "ref": subject["github_ref"],
                                     "detail": plan_github._one_line(exc)})
    return report
