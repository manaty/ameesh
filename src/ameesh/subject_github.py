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
(`<!-- ameesh:subject=<id> -->` … `<!-- ameesh:subject-end=<id> -->`, id du sujet)
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

#: marqueurs par id du sujet, jamais par slug : un slug n'est pas contrôlé à
#: sa création et ne doit pas passer dans GitHub sans la garde (B impl-11)
BEGIN = "<!-- ameesh:subject=%d -->"
END = "<!-- ameesh:subject-end=%d -->"
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

def _section_re(subject_id: int):
    return re.compile(re.escape(BEGIN % subject_id) + r".*?" + re.escape(END % subject_id),
                      re.S)


def _done(item: dict) -> bool:
    return item.get("state") in lifecycle.COMPLETED


def _title(text: str, guard, public: bool) -> str:
    text = " ".join((text or "").split())
    if not text or guard.problems(text, public=public):
        return WITHHELD
    return plan_github._quiet(plan_github._cut(text, TITLE_MAX))


def _clean(text: str, guard, public: bool) -> bool:
    return not guard.problems(text, public=public)


def render(subject: dict, lots: list[dict], *, guard, public: bool) -> str:
    """La section du sujet : ses lots, cochés une fois terminés. Chaque champ
    libre (titres, slug, issue_ref) passe la garde ; la section entière la
    repasse avant de sortir (refus sans recopier le texte en cause)."""
    sid = int(subject["id"])
    slug = subject["slug"]
    name = "`%s`" % slug if _clean(slug, guard, public) else "#%d" % sid
    lines = [BEGIN % sid,
             "### ameesh lots — %s" % _title(subject.get("title"), guard, public),
             "_Maintained by ameesh (subject %s): rewritten when a lot is added or "
             "changes state. Edit outside these markers._" % name, ""]
    for item in lots[:MAX_LOTS]:
        state = item.get("legacy_state") or item.get("state") or "?"
        issue = (item.get("issue_ref") or "").strip()
        issue = (" · %s" % issue if plan_github._ref_parts(issue)
                 and _clean(issue, guard, public) else "")
        lines.append("- [%s] ameesh #%d — %s (%s)%s" % (
            "x" if _done(item) else " ", int(item["id"]),
            _title(item.get("title"), guard, public), state, issue))
    if len(lots) > MAX_LOTS:
        lines.append("- … %d more lots (`ameesh work subject show`)" % (len(lots) - MAX_LOTS))
    if not lots:
        lines.append("_No lots yet._")
    lines.append(END % sid)
    section = "\n".join(lines)
    problems = guard.problems(section, public=public)
    if problems:
        raise WorkError("subject #%d: section not published (%s)" % (sid, ", ".join(problems)))
    return section


def splice(body: str | None, subject_id: int, section: str) -> str:
    """Le corps avec la section du sujet remplacée (ou ajoutée en fin) ; le
    texte hors marqueurs est rendu tel quel. Un marqueur orphelin (section
    abîmée à la main) est refusé plutôt que de deviner où elle finit."""
    body = body or ""
    subject_id = int(subject_id)
    pattern = _section_re(subject_id)
    if pattern.search(body):
        return pattern.sub(lambda _m: section, body, count=1)
    if BEGIN % subject_id in body or END % subject_id in body:
        raise WorkError("subject #%d: the section markers of the issue are damaged (one "
                        "is missing); restore or delete both markers" % subject_id)
    if not body.strip():
        return section
    return body.rstrip("\n") + "\n\n" + section + "\n"


class Context:
    """Ce qu'il faut pour publier : le client `gh`, la garde et la visibilité
    (en cache) des dépôts."""

    def __init__(self, cfg, db, gh, *, cache: dict | None = None, local_host=None,
                 writes=None):
        """Échoue fermé : des réglages `github` invalides (`SettingsError`) ou
        une liste d'hôtes illisible lèvent ; jamais de publication avec une
        garde privée de ses termes exclus ou des noms d'hôtes du mesh.

        #8389 : `writes` est le crédit d'écritures partagé de la projection
        (`plan_github._Writes`). Sans lui (état d'hôte absent), les deux
        écritures de ce module reprennent leur `gh api` direct, comme avant ;
        le défaut le construit depuis la base, pour que la CLI et la passe
        automatique débitent le même budget."""
        self.gh = gh
        self.cache = {} if cache is None else cache
        self.writes = writes if writes is not None else plan_github._writes_for(db, gh)
        settings = plan_github.settings_of(cfg) if cfg is not None else plan_github.Settings()
        hosts = {local_host or getattr(cfg, "host", None)}
        hosts |= {row.get("host") for row in storage.of(db).hosts.current(None)}
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
    new = splice(body, subject["id"], section)
    if new == body:
        return False
    path = "repos/%s/issues/%d" % (repo, number)
    if ctx.writes is not None:
        try:
            ctx.writes.write("PATCH", path, {"body": new})
        except plan_github.ContentLimitReached:
            raise                       # sync arrête la passe ; la CLI l'explique
        except plan_github.GithubRateLimited:
            raise                       # limite mémorisée : arrête la passe
        except plan_github.GithubError as exc:
            raise _access(ref_of(repo, number), "edit the issue body",
                          plan_github._one_line(exc)) from exc
        return True
    code, out, err = ctx.gh.run(["api", "-X", "PATCH", path, "--input", "-"],
                                json.dumps({"body": new}))
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


def adopt(db, ctx: Context, subject: dict, ref: str) -> dict:
    """Adopte le ticket `ref` comme épique du sujet : section écrite, puis
    référence posée. Idempotent pour la même référence. La section est
    greffée sur la lecture faite ici, la plus récente : une édition humaine
    antérieure n'est pas écrasée."""
    if subject["state"] != "open":
        raise WorkError("subject %s is closed" % subject["slug"])
    repo, number, read = check_adoptable(db, ctx.gh, ref, subject=subject)
    try:
        write_section(db, ctx, subject, repo, number, issue=read)
    except plan_github.ContentLimitReached as exc:
        raise WorkError(
            "%s: GitHub content credit spent (%s); retry after the window"
            % (ref_of(repo, number), plan_github._one_line(exc))) from exc
    except plan_github.GithubRateLimited as exc:
        raise WorkError(
            "%s: GitHub rate limit (%s); retry after the window"
            % (ref_of(repo, number), plan_github._one_line(exc))) from exc
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
    payload = {"title": plan_github._quiet(title), "body": body}
    path = "repos/%s/issues" % repo
    if ctx.writes is not None:
        try:
            created = ctx.writes.write("POST", path, payload)
        except plan_github.ContentLimitReached as exc:
            raise WorkError(
                "%s: GitHub content credit spent (%s); retry after the window"
                % (repo, plan_github._one_line(exc))) from exc
        except plan_github.GithubRateLimited as exc:
            raise WorkError(
                "%s: GitHub rate limit (%s); retry after the window"
                % (repo, plan_github._one_line(exc))) from exc
        except plan_github.GithubError as exc:
            raise _access(repo, "create an issue", plan_github._one_line(exc)) from exc
        try:
            number = int(created["number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkError(
                "%s: unreadable answer from GitHub after creating the epic" % repo) from exc
    else:
        code, out, err = ctx.gh.run(["api", "-X", "POST", path, "--input", "-"],
                                    json.dumps(payload))
        if code != 0:
            raise _access(repo, "create an issue", err or out)
        try:
            number = int(json.loads(out)["number"])
        except (ValueError, KeyError, TypeError) as exc:
            raise WorkError(
                "%s: unreadable answer from GitHub after creating the epic" % repo) from exc
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
    report = {"updated": 0, "closed": 0, "errors": [], "deferred": 0}
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
        except plan_github.ContentLimitReached as exc:
            # #8389 : le crédit d'écritures est partagé et dépensé ; les
            # sections restantes repartent à la fenêtre suivante, sans
            # interroger GitHub une fois par sujet.
            report["deferred"] += 1
            log("GitHub subject epics: write credit spent, %s deferred: %s"
                % (subject["github_ref"], plan_github._one_line(exc)))
            break
        except plan_github.GithubRateLimited as exc:
            # #8357 : une limite mémorisée arrête la passe, pas seulement ce
            # sujet ; le suivant serait refusé sans requête mais compterait
            # une erreur de plus.
            report["errors"].append({"subject": subject["slug"], "ref": subject["github_ref"],
                                     "detail": plan_github._one_line(exc)})
            break
        except (WorkError, plan_github.GithubError) as exc:
            report["errors"].append({"subject": subject["slug"], "ref": subject["github_ref"],
                                     "detail": plan_github._one_line(exc)})
    return report
