# SPDX-License-Identifier: AGPL-3.0-only
"""Échecs de tests remontés en temps réel au courrier de l'agent du lot (L161c).

    ameesh ci status <lot> [--limit N] [--json]
    ameesh ci run <lot> [--avis N] [--exception incident|unblock --reason …]
                        [--full --reason …] [--sha S] [--force] [--dry-run] [--json]
    ameesh ci cancel <lot> [--all] [--dry-run] [--json]

`ci status` lit les exécutions suivies ; `ci run` est le déclencheur du gel
(#1343, canon §3 : la relecture passe avant la CI) — il vérifie le gel, les
verdicts requis, l'idempotence du SHA et dispatche `tests.yml` en `mode=lot`
sur la tête gelée ; `ci cancel` annule les runs périmés d'un lot.

Étude L161 (docs/design/etudes/l161-tests-par-impact.md, section 3), voie A :
pendant les tests, chaque part de la CI publie un check run « tests (flux) »
(`scripts/ci-flow.py publish`) dont la sortie porte, en JSON lignes, chaque
test en échec au schéma `ameesh-test-event/1` (L161b). `ameesh notify`, sur
l'hôte que désigne `github.host` (comme les issues des lots, L126), fait à
chaque passage un relevé (`Watcher.tick`) :

1. les exécutions en cours ou en attente du workflow des tests (`WORKFLOWS`) de chaque dépôt
   de `github.projects` (une requête par dépôt) ; chacune est rattachée à un
   lot : par sa branche (`work_items.branch`, L118), sinon par la ligne
   `ameesh-work: <id>` de son commit de tête (une fusion dans main) ;
2. une nouvelle tête sur la branche d'un lot remplace l'exécution précédente
   (`superseded`), en cours ou déjà annulée : les messages de CI de l'ancienne tête que l'agent n'a
   pas encore reçus sont retirés sans être montrés ; un message déjà remis
   reste, car l'agent a pu agir ;
3. pour chaque exécution suivie, les check runs « tests (flux) » de sa tête :
   chaque échec nouveau est enregistré (`ci_test_failures`, 0055) ;
4. une exécution qui n'est plus en cours est conclue : verdict.

Courrier, de `ameesh` à l'assigné du lot, rangé dans le fil du lot, selon
L125 :

* premier échec de la tête : **urgent**, un seul par exécution (la CI seule ne
  peut pas déclencher `turn_churn`) ; passif (`ci_passif`) tant qu'aucune
  exécution conclue de la base n'a été relevée, car l'échec est peut-être
  hérité ;
* échecs suivants : un événement ordinaire par relevé (regroupé par la
  fenêtre de 90 s du courrier) ;
* verdict (vert, ou rouge avec la liste) : un événement ordinaire ; rien pour
  une exécution annulée ou remplacée ;
* **rouge hérité** : un test qui échouait déjà à la dernière exécution conclue
  de la base (la cible de la PR, sinon main ; pour main, l'exécution
  précédente de main) n'est jamais imputé à l'auteur : pas d'urgent, une ligne
  dans le verdict.

Une exécution rouge sur main lève aussi l'alerte `ci_main_red`
(`exploitation`, poussée par `ameesh notify` au responsable humain).

Le réessai d'un échec (étude : « premier échec confirmé ») n'est pas encore
émis par le flux : un échec compte dès sa première occurrence.
"""
from __future__ import annotations

import json
import time

from . import storage

#: workflows dont les exécutions sont suivies (nom du fichier)
WORKFLOWS = ("tests.yml",)
#: états GitHub d'une exécution pas encore conclue que le relevé lit : une
#: exécution en attente d'un runner est vue dès la poussée, avant que
#: l'exécution qu'elle remplace soit annulée
LIVE_STATUSES = ("in_progress", "queued")
#: préfixe du nom des check runs publiés par `scripts/ci-flow.py publish`
CHECK_PREFIX = "tests (flux)"
SCHEMA = "ameesh-test-event/1"
#: branches de base : une exécution n'y est jamais remplacée, et ses échecs
#: servent de référence au rouge hérité
BASE_BRANCHES = ("main",)
ECHECS = ("echec", "erreur")
#: conclusion GitHub → état de `ci_runs`
_CONCLUSIONS = {"success": "success", "neutral": "success", "skipped": "success",
                "cancelled": "cancelled", "stale": "cancelled"}
#: lignes d'extrait au plus dans un message
EXCERPT_LINES = 40
#: échecs détaillés au plus dans un message (les autres : leur nom)
DETAILED = 3
STATUS_SCHEMA = "ameesh-ci-status/1"


class CiError(RuntimeError):
    """Lot inconnu, réponse de GitHub illisible : message actionnable."""


# --------------------------------------------------------------------------
# lecture des check runs
# --------------------------------------------------------------------------

def events_of(check_runs, run_id: int) -> list[dict]:
    """Les événements `test` en échec, pour l'exécution `run_id`, portés par
    la sortie des check runs « tests (flux) » d'une tête."""
    out, seen = [], set()
    for check in check_runs or ():
        if not str(check.get("name") or "").startswith(CHECK_PREFIX):
            continue
        text = ((check.get("output") or {}).get("text")) or ""
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict) or ev.get("v") != 1 or ev.get("type") != "test":
                continue
            if ev.get("statut") not in ECHECS or not ev.get("test"):
                continue
            if ev.get("execution") is not None and str(ev["execution"]) != str(run_id):
                continue
            key = (str(ev.get("part") or ""), ev["test"])
            if key not in seen:
                seen.add(key)
                out.append(ev)
    return out


def lot_of_run(run: dict, lots: list[dict]) -> int | None:
    """Le lot d'une exécution : par sa branche (lots ouverts), sinon par la
    ligne `ameesh-work:` de son commit de tête."""
    from .plan_git import WORK_TRAILER_RE
    branch = (run.get("head_branch") or "").casefold()
    if branch and branch not in BASE_BRANCHES:
        for lot in lots:
            if (lot.get("branch") or "").casefold() == branch:
                return int(lot["id"])
    message = ((run.get("head_commit") or {}).get("message")) or ""
    match = WORK_TRAILER_RE.search(message)
    return int(match.group(1)) if match else None


def base_of(run: dict) -> str:
    """La branche de référence du rouge hérité : la cible de la PR, sinon main."""
    for pr in run.get("pull_requests") or ():
        ref = ((pr.get("base") or {}).get("ref")) or ""
        if ref:
            return ref
    return BASE_BRANCHES[0]


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------

def _clean(text: str) -> str:
    from .fil import clean_text
    return clean_text(text or "").strip()


def _excerpt(text: str) -> str:
    lines = _clean(text).splitlines()
    if len(lines) > EXCERPT_LINES:
        lines = lines[:EXCERPT_LINES] + ["… (%d more line(s))" % (len(lines) - EXCERPT_LINES)]
    return "\n".join(lines)


def _short(sha: str) -> str:
    return (sha or "")[:10]


def _head(run: dict, lot: dict | None) -> str:
    where = "lot #%d \"%s\"" % (lot["id"], lot.get("title") or "") if lot else "no lot"
    return "CI of %s, head %s on %s (%s)" % (where, _short(run["head_sha"]), run["branch"],
                                          run.get("url") or "run %s" % run["run_id"])


def _failure_lines(failures: list[dict], *, detail: bool) -> list[str]:
    out = []
    for i, f in enumerate(failures):
        out.append("• %s%s (%s, %s)" % (f["test"], " — inherited" if f.get("inherited") else "",
                                        f.get("part") or "?", f.get("status") or "echec"))
        if f.get("replay"):
            out.append("  replay: %s" % f["replay"])
        if detail and i < DETAILED and f.get("excerpt"):
            out.append("")
            out.extend("    " + line for line in _excerpt(f["excerpt"]).splitlines())
            out.append("")
    if detail and len(failures) > DETAILED:
        out.append("(excerpts of the first %d only)" % DETAILED)
    return out


def failure_text(run: dict, lot: dict | None, failures: list[dict], *, first: bool,
                 detail: bool = True, unknown_base: bool = False) -> str:
    """Le message d'un ou plusieurs échecs nouveaux d'une exécution."""
    head = _head(run, lot)
    what = ("first failure" if len(failures) == 1 else "first %d failures" % len(failures)) \
        if first else ("next failure" if len(failures) == 1
                       else "next %d failures" % len(failures))
    lines = ["%s: %s." % (head, what), ""]
    if unknown_base:
        lines += ["No finished run of %s has been seen yet: these failures may be "
                  "inherited, hence this message without a wake-up." % (run.get("base") or "main"),
                  ""]
    lines += _failure_lines(failures, detail=detail)
    lines += ["", "The suite goes on, verdict at the end. A new push replaces this "
                  "run: the messages you have not read yet will be withdrawn. No need "
                  "to wait for the CI in your turn (gh pr checks --watch): ameesh tells you."]
    return "\n".join(lines)


def verdict_text(run: dict, lot: dict | None, status: str, failures: list[dict]) -> str:
    """Le message de fin d'une exécution."""
    head = _head(run, lot)
    own = [f for f in failures if not f.get("inherited")]
    inherited = [f for f in failures if f.get("inherited")]
    if status == "success" and not failures:
        return "%s: green." % head
    lines = []
    if status == "success":
        lines.append("%s: green, despite failures seen along the way "
                     "(retried or tolerated)." % head)
    elif not own and inherited:
        lines.append("%s: red, but only failures inherited from %s (already red "
                     "before your push): nothing is charged to you."
                     % (head, run.get("base") or "main"))
    elif not failures:
        lines.append("%s: red, with no failed test seen in the stream (part interrupted, "
                     "timeout or another job): see the run." % head)
    else:
        lines.append("%s: red, %d failure(s)%s." % (
            head, len(own), ", and %d inherited from the base" % len(inherited) if inherited else ""))
    lines.append("")
    lines += _failure_lines(own + inherited, detail=False)
    return "\n".join(lines).rstrip()


def _send(db, to: str, text: str, short: str, *, lot: int | None, payload: dict,
          urgent: bool) -> int:
    """Envoie ; un texte refusé comme illisible (extrait binaire ou encodé)
    part sans ses extraits."""
    from . import mail, work
    from .fil import UnreadableBody
    body = dict(payload)
    if urgent:
        body["urgent"] = True
    try:
        return mail.send(db, work.SYSTEM_SENDER, to, text, kind="event",
                         work_item_id=str(lot) if lot else None, payload=body)
    except UnreadableBody:
        return mail.send(db, work.SYSTEM_SENDER, to, short, kind="event",
                         work_item_id=str(lot) if lot else None, payload=body)


def recipient_of(lot: dict | None) -> str | None:
    from . import work
    to = (lot or {}).get("assignee") or ""
    if not to or to.startswith(work.HUMAN_PREFIX):
        return None
    return to


# --------------------------------------------------------------------------
# relevé
# --------------------------------------------------------------------------

def gh_api(gh=None):
    """La fonction `chemin -> JSON` de l'API GitHub, par `gh api`."""
    from . import plan_github

    def api(path: str):
        nonlocal gh
        gh = gh or plan_github.Gh()
        return plan_github._json(gh, ["api", path])
    return api


class Watcher:
    """Le relevé de la CI, mené par `ameesh notify` à chaque passage, sur
    l'hôte désigné (`github.host`). Une erreur (GitHub, base) d'un dépôt est
    journalisée une fois par passage et n'arrête ni les autres dépôts, ni
    les alertes."""

    def __init__(self, cfg, *, api=None, log=None, clock=None, dry_run: bool = False):
        self.cfg = cfg
        self.api = api
        self.log = log or (lambda _text: None)
        self.clock = clock or time.time
        self.dry_run = bool(dry_run)

    def repos(self) -> list[str]:
        from . import plan_github
        try:
            settings = plan_github.settings_of(self.cfg)
        except plan_github.SettingsError:
            return []  # déjà journalisé par la projection des issues
        if not settings.projects or settings.host != getattr(self.cfg, "host", None):
            return []
        return sorted(set(settings.projects.values()))

    def tick(self, db) -> list[dict]:
        """Un relevé de chaque dépôt ; rend les envois et retraits."""
        repos = self.repos()
        if not repos or self.dry_run:
            return []
        if self.api is None:
            self.api = gh_api()
        out: list[dict] = []
        for repo in repos:
            try:
                out += self.watch(db, repo)
            except Exception as exc:  # noqa: BLE001 - jamais fatal au passage
                self.log("CI %s: check failed (%s)" % (repo, " ".join(str(exc).split())[:200]))
        return out

    # -- un dépôt --------------------------------------------------------------
    def watch(self, db, repo: str) -> list[dict]:
        st = storage.of(db)
        live = {}
        for status in LIVE_STATUSES:
            data = self.api("repos/%s/actions/runs?status=%s&per_page=50" % (repo, status)) or {}
            for r in data.get("workflow_runs") or ():
                if str(r.get("path") or "").rsplit("/", 1)[-1] in WORKFLOWS:
                    live.setdefault(int(r["id"]), r)
        lots = [r for r in st.work.open_for_sweep(500) if r.get("branch")]
        out: list[dict] = []
        for run_id in sorted(live):
            out += self._follow(db, repo, live[run_id], lots)
        for row in st.ci.running(repo):
            out += self._failures(db, repo, row)
            if int(row["run_id"]) not in live:
                out += self._conclude(db, repo, row)
        return out

    def _follow(self, db, repo: str, run: dict, lots: list[dict]) -> list[dict]:
        """Une exécution en cours : enregistrée si nouvelle ; une exécution
        connue sans lot rattaché l'est dès que la branche du lot est posée
        (#1321), tant que sa tête est encore celle de la branche ; elle
        remplace les exécutions d'une autre tête sur la même branche (hors
        base)."""
        st = storage.of(db)
        run_id = int(run["id"])
        known = st.ci.run(repo, run_id)
        if known is not None:
            if known.get("work_item_id") is None:
                # #1321 : la branche du lot peut être posée après la première
                # vue du run ; sans ce rattachement, `_conclude` lit
                # `recipient_of(None)` et le verdict part à personne.
                # `open_run` est idempotent (coalesce côté stockage) : il ne
                # remplit que le lien encore vide.
                lot = lot_of_run(run, lots)
                row = None
                if lot is not None:
                    row = next((l for l in lots if int(l["id"]) == lot), None)
                head = run.get("head_sha") or ""
                # une tête plus récente connue de la branche (`branch_head`,
                # posé par le relevé des branches) rend ce run périmé : le
                # rattacher enverrait un verdict d'une tête remplacée — et
                # l'exécution de remplacement peut être `pending`, invisible
                # au veilleur (#1161). Le silence vaut mieux qu'un faux vert.
                known_head = (row or {}).get("branch_head") or ""
                if lot is not None and (not known_head or head == known_head):
                    st.ci.open_run(repo, run_id, head_sha=head,
                                   branch=run.get("head_branch") or "",
                                   event=run.get("event") or "", base=base_of(run),
                                   work_item_id=lot, url=run.get("html_url"))
                    self.log("CI %s run %s: lot #%s linked (branch set later)"
                             % (repo, run_id, lot))
                elif lot is not None:
                    self.log("CI %s run %s: obsolete head %s (branch head %s), not linked"
                             % (repo, run_id, _short(head), _short(known_head)))
            return []
        st.ci.open_run(repo, run_id, head_sha=run.get("head_sha") or "",
                       branch=run.get("head_branch") or "", event=run.get("event") or "",
                       base=base_of(run), work_item_id=lot_of_run(run, lots),
                       url=run.get("html_url"))
        branch = run.get("head_branch") or ""
        if not branch or branch in BASE_BRANCHES:
            return []
        out = []
        old = st.ci.supersede(repo, branch, run.get("head_sha") or "", run_id)
        if old:
            ids = st.ci.message_ids(repo, [r["run_id"] for r in old])
            withdrawn = st.mailbox.withdraw(ids, {
                "raison": "obsolete", "remplace_par": run.get("head_sha"),
                "execution": run_id, "ts": float(self.clock())}) if ids else []
            for r in old:
                out.append({"action": "superseded", "repo": repo, "run_id": r["run_id"],
                            "by": run.get("head_sha"), "withdrawn": withdrawn})
        return out

    def _failures(self, db, repo: str, row: dict) -> list[dict]:
        """Les échecs nouveaux d'une exécution suivie, et leur message."""
        from . import work
        st = storage.of(db)
        checks = self.api("repos/%s/commits/%s/check-runs?per_page=100"
                          % (repo, row["head_sha"])) or {}
        events = events_of(checks.get("check_runs"), int(row["run_id"]))
        if not events:
            return []
        reference = st.ci.base_failures(repo, row.get("base") or BASE_BRANCHES[0])
        if reference and int(reference["run_id"]) == int(row["run_id"]):
            reference = {}
        # aucune exécution conclue de la base relevée (juste après le
        # déploiement, base peu active) : un échec peut être hérité sans
        # qu'on le sache, il n'est pas imputé par un urgent
        unknown_base = not reference
        before = set(reference.get("tests") or ())
        new = []
        for ev in events:
            failure = {"part": str(ev.get("part") or ""), "test": ev["test"],
                       "module": ev.get("module") or "", "status": ev.get("statut"),
                       "inherited": ev["test"] in before,
                       "excerpt": ev.get("extrait") or "", "replay": ev.get("rejouer") or "",
                       "duration_s": ev.get("duree_s"), "failed_at": ev.get("ts")}
            if st.ci.add_failure(repo, int(row["run_id"]), **failure):
                new.append(failure)
        own = [f for f in new if not f["inherited"]]
        if not own:
            return []
        lot = work.get(db, int(row["work_item_id"])) if row.get("work_item_id") else None
        to = recipient_of(lot)
        if not to:
            return [{"action": "failures", "repo": repo, "run_id": row["run_id"],
                     "tests": [f["test"] for f in own], "to": None}]
        # premier message de l'exécution (un passif sans base compte aussi)
        first = not row.get("urgent_message_id") and not any(
            f.get("message_id") for f in st.ci.failures(repo, int(row["run_id"])))
        urgent = first and not unknown_base
        text = failure_text(row, lot, own, first=first, unknown_base=unknown_base)
        short = failure_text(row, lot, own, first=first, unknown_base=unknown_base,
                             detail=False)
        payload = {"ci": {"repo": repo, "run_id": row["run_id"], "head_sha": row["head_sha"],
                          "branch": row["branch"], "kind": "failure",
                          "tests": [f["test"] for f in own]}}
        if unknown_base:
            payload["ci_passif"] = True
        message_id = _send(db, to, text, short, lot=lot["id"], payload=payload, urgent=urgent)
        if urgent:
            st.ci.set_message(repo, int(row["run_id"]), "urgent", message_id)
            row["urgent_message_id"] = message_id
        st.ci.set_failure_message(repo, int(row["run_id"]),
                                  [(f["part"], f["test"]) for f in own], message_id)
        return [{"action": "failures", "repo": repo, "run_id": row["run_id"],
                 "tests": [f["test"] for f in own], "to": to, "urgent": urgent,
                 "message_id": message_id}]

    def _conclude(self, db, repo: str, row: dict) -> list[dict]:
        """Une exécution qui n'est plus en cours : son verdict."""
        from . import work
        st = storage.of(db)
        run = self.api("repos/%s/actions/runs/%s" % (repo, row["run_id"])) or {}
        if (run.get("status") or "") != "completed":
            return []  # en attente d'un runner, ou relancée : au prochain relevé
        status = _CONCLUSIONS.get(run.get("conclusion") or "", "failure")
        if not st.ci.conclude(repo, int(row["run_id"]), status):
            return []
        if status == "cancelled":
            return [{"action": "verdict", "repo": repo, "run_id": row["run_id"],
                     "status": status, "to": None}]
        failures = st.ci.failures(repo, int(row["run_id"]))
        lot = work.get(db, int(row["work_item_id"])) if row.get("work_item_id") else None
        to = recipient_of(lot)
        if not to:
            return [{"action": "verdict", "repo": repo, "run_id": row["run_id"],
                     "status": status, "to": None}]
        text = verdict_text(row, lot, status, failures)
        payload = {"ci": {"repo": repo, "run_id": row["run_id"], "head_sha": row["head_sha"],
                          "branch": row["branch"], "kind": "verdict", "status": status,
                          "failures": len([f for f in failures if not f.get("inherited")]),
                          "inherited": len([f for f in failures if f.get("inherited")])}}
        message_id = _send(db, to, text, text, lot=lot["id"], payload=payload, urgent=False)
        st.ci.set_message(repo, int(row["run_id"]), "verdict", message_id)
        return [{"action": "verdict", "repo": repo, "run_id": row["run_id"], "status": status,
                 "to": to, "message_id": message_id}]


def describe(record: dict) -> str:
    """Une ligne de journal de `ameesh notify` pour un envoi ou un retrait."""
    where = "CI %s run %s" % (record.get("repo"), record.get("run_id"))
    if record["action"] == "superseded":
        return "%s superseded by %s; %d unread message(s) withdrawn" % (
            where, _short(record.get("by") or ""), len(record.get("withdrawn") or ()))
    to = "→ %s%s" % (record["to"], " (urgent)" if record.get("urgent") else "") \
        if record.get("to") else "(no recipient)"
    if record["action"] == "failures":
        return "%s: %d failure(s) %s" % (where, len(record.get("tests") or ()), to)
    return "%s: verdict %s %s" % (where, record.get("status"), to)


# --------------------------------------------------------------------------
# alerte ci_main_red
# --------------------------------------------------------------------------

def main_red(db) -> list[dict]:
    """Les dernières exécutions conclues des branches de base qui sont rouges :
    `{repo, branch, run_id, head_sha, url, work_item_id, concluded_ts, tests}`."""
    st = storage.of(db)
    out = []
    for row in st.ci.latest_concluded(BASE_BRANCHES):
        if row.get("status") != "failure":
            continue
        tests = [f["test"] for f in st.ci.failures(row["repo"], int(row["run_id"]))]
        out.append(dict(row, tests=tests))
    return out


# --------------------------------------------------------------------------
# ameesh ci status
# --------------------------------------------------------------------------

def status_of(db, item_id: int, limit: int = 5) -> dict:
    from . import work
    item = work.get(db, int(item_id))
    if item is None:
        raise CiError("lot #%s unknown" % item_id)
    st = storage.of(db)
    runs = []
    for row in st.ci.for_lot(int(item_id), limit):
        failures = st.ci.failures(row["repo"], int(row["run_id"]))
        runs.append(dict(row, failures=[{k: f.get(k) for k in (
            "part", "test", "status", "inherited", "replay", "failed_ts")} for f in failures]))
    return {"schema": STATUS_SCHEMA, "work_item": int(item_id), "title": item.get("title"),
            "branch": item.get("branch"), "runs": runs}


_STATUS_FR = {"running": "running", "success": "green", "failure": "red",
              "cancelled": "cancelled", "superseded": "superseded"}


def render_status(report: dict) -> list[str]:
    from .notify import local_time
    lines = ["lot #%d \"%s\" (branch %s)" % (report["work_item"], report.get("title") or "",
                                          report.get("branch") or "—")]
    if not report["runs"]:
        lines.append("  no CI run seen (seen by `ameesh notify` on the host "
                     "named by github.host)")
    for run in report["runs"]:
        state = _STATUS_FR.get(run["status"], run["status"])
        if run["status"] == "superseded" and run.get("superseded_by"):
            state += " by %s" % _short(run["superseded_by"])
        own = [f for f in run["failures"] if not f.get("inherited")]
        inherited = len(run["failures"]) - len(own)
        lines.append("  %s  %s  %s  %s%s%s" % (
            local_time(run["first_seen_ts"]), _short(run["head_sha"]), state,
            "%d failure(s)" % len(own), ", %d inherited" % inherited if inherited else "",
            "  %s" % run["url"] if run.get("url") else ""))
        for f in own[:10]:
            lines.append("      %s (%s)%s" % (f["test"], f.get("part") or "?",
                                              "  → %s" % f["replay"] if f.get("replay") else ""))
        if len(own) > 10:
            lines.append("      … and %d more" % (len(own) - 10))
    return lines


# --------------------------------------------------------------------------
# ameesh ci run / ameesh ci cancel — la CI d'un lot part au gel (#1343)
# --------------------------------------------------------------------------
#
# canon §3 (manaty/home#66, owner 14/10 21:45) : sur ameesh, la relecture passe
# avant la CI. Une poussée sur une PR de lot ne lance plus rien ; la CI du lot
# (tests impactés + contrat) ne tourne qu'une fois, sur la tête gelée, quand
# les verdicts requis sont acquis. `ameesh ci run <lot>` porte ce déclencheur :
#
#   * le lot porte un gel (`ameesh work milestone <lot> frozen --sha S`) et sa
#     branche distante est encore sur S — une tête changée est refusée, jamais
#     un vert sur une autre tête ;
#   * `--avis N` verdicts `ok` sur S, d'acteurs distincts et hors de l'assigné
#     (0 pour un lot léger, 1 pour un lot sensible, 2 pour SQL/sécurité/prod) ;
#   * aucun run déjà vu sur S : vert ou en cours rend la main sans dispatcher
#     (idempotence du gel et d'un événement rejoué), un run rouge ou annulé
#     demande `--force` ;
#   * `gh workflow run tests.yml --ref <branche> -f mode=lot -f sha=S
#     -f base=<merge-base> -f lot=<n>` ; le job `plan` revérifie S face à HEAD
#     et échoue (check requis rouge, rien exécuté) si la branche a bougé.
#
# Avant de dispatcher, les runs non conclues de la branche qui ne portent pas S
# sont annulés (canon §3 : un run lancé sur un lot qui attend encore un verdict
# ou une correction est annulé) ; un échec d'annulation est dit, jamais fatal.
#
# Exceptions (canon §3) : un lot qui débloque la CI ou corrige un incident
# dispatche en parallèle de sa relecture, `--exception incident|unblock
# --reason …` ; le run porte la marque et le lot reçoit une note.
#
# `ameesh ci cancel <lot>` fait la même annulation seule (`--all` : tout, y
# compris le SHA gelé).

GEL_SCHEMA = "ameesh-ci-gel/1"
CANCEL_SCHEMA = "ameesh-ci-cancel/1"
#: exceptions du canon §3 : la CI part en parallèle de la relecture, marquée
EXCEPTIONS = ("incident", "unblock")
#: états GitHub d'un run qui n'a pas conclu (LIVE_STATUSES en est un sous-ensemble)
OPEN_STATUSES = ("queued", "in_progress", "requested", "waiting", "pending")
#: nombre d'avis que `--avis` accepte (léger, sensible, SQL/sécurité/prod)
MAX_AVIS = 2


class GelError(CiError):
    """Déclencheur du gel : refus actionnable, jamais un run silencieux."""


def _q(value: str) -> str:
    """Une valeur de chemin ou de requête GitHub (une branche `lot/x` incluse)."""
    from urllib.parse import quote
    return quote(value or "", safe="")


def _sha_ok(sha: str) -> bool:
    import re
    return bool(re.fullmatch(r"[0-9a-f]{40}", sha or ""))


def repo_of(cfg, item: dict, *, repo: str = "") -> str:
    """`owner/repo` du lot : `--repo`, son `issue_ref`, sinon `github.projects`."""
    from . import plan_github
    if repo.strip():
        return repo.strip()
    parts = plan_github._ref_parts(item.get("issue_ref"))
    if parts and parts[0]:
        return parts[0]
    try:
        found = plan_github.settings_of(cfg).repo_for(item.get("app"))
    except plan_github.SettingsError as exc:
        raise GelError(str(exc)) from exc
    if found:
        return found
    raise GelError("lot #%s: no issue_ref and no github.projects entry for %r: "
                   "pass --repo owner/name" % (item.get("id"), item.get("app")))


def frozen_of(db, item_id: int) -> dict | None:
    """Le dernier jalon `frozen` du lot, ou None (le lot n'est pas gelé)."""
    from . import work
    return next((m for m in work.milestones(db, int(item_id))
                 if m["kind"] == "frozen"), None)


def independent_verdicts(db, item_id: int, sha: str, assignee: str) -> list[dict]:
    """Les verdicts `ok` posés sur `sha` par des acteurs distincts, hors assigné.

    Le canon §3 demande des avis *indépendants* : un verdict rendu par l'auteur
    du lot, ou deux verdicts du même acteur, ne comptent pas.
    """
    from . import work
    who = (assignee or "").strip().casefold()
    out, seen = [], set()
    for m in work.milestones(db, int(item_id)):
        if m["kind"] != "verdict" or (m.get("verdict") or "") != "ok":
            continue
        if (m.get("sha") or "").strip().casefold() != (sha or "").casefold():
            continue
        actor = (m.get("actor") or "").strip()
        key = actor.casefold()
        if not actor or key == who or key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def _head_of(api, repo: str, branch: str) -> str:
    data = api("repos/%s/branches/%s" % (repo, _q(branch))) or {}
    sha = ((data.get("commit") or {}).get("sha") or "").strip().lower()
    if not _sha_ok(sha):
        raise GelError("branch %s not found in %s" % (branch, repo))
    return sha


def _runs_of(api, repo: str, *, sha: str = "", branch: str = "") -> list[dict]:
    query = "per_page=50"
    if sha:
        query += "&head_sha=%s" % _q(sha)
    if branch:
        query += "&branch=%s" % _q(branch)
    data = api("repos/%s/actions/workflows/tests.yml/runs?%s" % (repo, query)) or {}
    return list(data.get("workflow_runs") or ())


def _merge_base_of(api, repo: str, target: str, sha: str) -> str:
    data = api("repos/%s/compare/%s...%s" % (repo, _q(target), _q(sha))) or {}
    return ((data.get("merge_base_commit") or {}).get("sha") or "").strip().lower()


def _carries_gel(api, repo: str, sha: str) -> bool:
    """Le workflow du SHA gelé porte-t-il le déclencheur #1343 ?

    Sans ce contrôle, un lot dont la branche précède la bascule serait refusé
    par GitHub (« Unexpected inputs ») sans dire quoi faire ; ici le refus est
    explicite : refusionner la branche sur main avant de dispatcher.
    """
    import base64
    data = api("repos/%s/contents/.github/workflows/tests.yml?ref=%s"
               % (repo, _q(sha))) or {}
    content = (data.get("content") or "").replace("\n", "")
    if not content:
        return False
    try:
        text = base64.b64decode(content).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return False
    return "#1343" in text


def _short_run(row: dict) -> dict:
    return {"id": row.get("id"), "status": row.get("status"),
            "conclusion": row.get("conclusion"), "event": row.get("event"),
            "url": row.get("html_url")}


def gel_plan(db, cfg, item_id: int, *, api, sha: str = "", avis: int = 1,
             exception: str = "", reason: str = "", full: bool = False,
             force: bool = False, repo: str = "") -> dict:
    """La décision de `ameesh ci run <lot>`, sans aucune écriture.

    Rend `{schema, action: dispatch|green|running|refuse, …}` : `dispatch` est
    le seul cas qui écrit (`cmd_ci_run` dispatche). Un refus est une `GelError`
    dont le message dit le geste à faire (geler, refusionner, relire…).
    """
    from . import work
    item = work.get(db, int(item_id))
    if item is None:
        raise GelError("lot #%s unknown" % item_id)
    if exception and exception not in EXCEPTIONS:
        raise GelError("--exception: %s (one of %s)" % (exception, ", ".join(EXCEPTIONS)))
    if exception and not reason.strip():
        raise GelError("--exception %s requires --reason: the exception is marked "
                       "(canon §3)" % exception)
    if full and not reason.strip():
        raise GelError("--full requires --reason: the full suite is an explicit "
                       "gesture (release, package, hand dispatch)")
    if avis < 0 or avis > MAX_AVIS:
        raise GelError("--avis: 0 (lot léger), 1 (sensible) or 2 (SQL/sécurité/prod)")
    name = repo_of(cfg, item, repo=repo)
    branch = (item.get("branch") or "").strip()
    if not branch:
        raise GelError("lot #%s has no branch: push the lot first" % item_id)
    target = (item.get("branch_target") or "main").strip() or "main"
    freeze = frozen_of(db, item_id)
    frozen = ((freeze or {}).get("sha") or "").strip().lower()
    asked = (sha or "").strip().lower()
    if asked and not _sha_ok(asked):
        raise GelError("--sha: the frozen commit (40 hex), not %r" % sha)
    if asked and frozen and asked != frozen:
        raise GelError("--sha %s is not the freeze of lot #%s (%s): refreeze first, "
                       "a correction needs a new review" % (asked[:12], item_id, frozen[:12]))
    frozen_sha = asked or frozen
    if not frozen_sha and (exception or full):
        # l'exception et le run complet n'attendent pas de gel : la tête de la
        # branche est le commit testé
        frozen_sha = _head_of(api, name, branch)
    if not frozen_sha:
        raise GelError("lot #%s is not frozen: ameesh work milestone %s frozen --sha <sha>"
                       % (item_id, item_id))
    if not _sha_ok(frozen_sha):
        raise GelError("lot #%s: the frozen milestone carries no usable sha (%r): "
                       "refreeze with --sha" % (item_id, frozen_sha))
    head = _head_of(api, name, branch)
    if head != frozen_sha:
        raise GelError("branch %s is on %s, the freeze is %s: the ref moved — refreeze "
                       "and re-review, nothing dispatched"
                       % (branch, head[:12], frozen_sha[:12]))
    reviews = independent_verdicts(db, item_id, frozen_sha, item.get("assignee") or "")
    if not (exception or full) and len(reviews) < avis:
        raise GelError("lot #%s: %d independent verdict(s) `ok` on %s, %d required "
                       "(ameesh work milestone %s verdict ok --sha %s --actor <relecteur>); "
                       "an incident or a CI unblock uses --exception incident|unblock "
                       "--reason …" % (item_id, len(reviews), frozen_sha[:12], avis,
                                       item_id, frozen_sha[:12]))
    runs = _runs_of(api, name, sha=frozen_sha)
    green = [r for r in runs if (r.get("conclusion") or "") == "success"]
    live = [r for r in runs if (r.get("status") or "") in OPEN_STATUSES]
    decision = {"schema": GEL_SCHEMA, "work_item": int(item_id), "repo": name,
                "branch": branch, "target": target, "sha": frozen_sha,
                "mode": "full" if full else "lot", "exception": exception,
                "reason": reason.strip(), "avis": avis,
                "verdicts": sorted((m.get("actor") or "") for m in reviews),
                "frozen": bool(freeze), "runs": len(runs)}
    if not force:
        if green:
            return {**decision, "action": "green", "why": "already green on the frozen "
                    "commit: nothing dispatched", "run": _short_run(green[-1])}
        if live:
            return {**decision, "action": "running", "why": "a run of the frozen commit "
                    "is already in flight: nothing dispatched", "run": _short_run(live[0])}
        if runs:
            raise GelError("lot #%s: %d run(s) already seen on %s, none green (%s): "
                           "--force to dispatch again (a correction needs a new freeze "
                           "and a new review)" % (item_id, len(runs), frozen_sha[:12],
                                                  ", ".join(sorted({(r.get("conclusion") or
                                                                     r.get("status") or "?")
                                                                    for r in runs}))))
    base = _merge_base_of(api, name, target, frozen_sha)
    if not _sha_ok(base):
        raise GelError("merge-base %s...%s not found in %s: the freeze is not on the "
                       "target branch" % (target, frozen_sha[:12], name))
    if not _carries_gel(api, name, frozen_sha):
        raise GelError("the workflow at %s does not carry the freeze trigger (#1343): "
                       "rebase the lot on main before dispatching" % frozen_sha[:12])
    return {**decision, "action": "dispatch", "base": base,
            "why": ("%s verdict(s) on %s" % (len(reviews), frozen_sha[:12])
                    if not (exception or full) else
                    ("exception %s: %s" % (exception, reason.strip()) if exception
                     else "full suite: %s" % reason.strip()))}


def cancel_plan(db, cfg, item_id: int, *, api, all_runs: bool = False,
                repo: str = "") -> dict:
    """Les runs d'un lot à annuler : non conclues, hors du SHA gelé.

    `all_runs` annule aussi le run du SHA gelé (geste explicite). Un lot non
    gelé n'a pas de SHA de référence : tous ses runs ouverts sont périmés.
    """
    from . import work
    item = work.get(db, int(item_id))
    if item is None:
        raise GelError("lot #%s unknown" % item_id)
    name = repo_of(cfg, item, repo=repo)
    branch = (item.get("branch") or "").strip()
    if not branch:
        raise GelError("lot #%s has no branch: nothing to cancel" % item_id)
    freeze = frozen_of(db, item_id)
    frozen = ((freeze or {}).get("sha") or "").strip().lower()
    rows = [r for r in _runs_of(api, name, branch=branch)
            if (r.get("status") or "") in OPEN_STATUSES]
    if frozen and not all_runs:
        rows = [r for r in rows if (r.get("head_sha") or "").strip().lower() != frozen]
    return {"schema": CANCEL_SCHEMA, "work_item": int(item_id), "repo": name,
            "branch": branch, "frozen": frozen, "all": bool(all_runs),
            "runs": [_short_run(r) for r in rows],
            "why": ("every open run of the branch" if all_runs or not frozen else
                    "open runs that do not carry the frozen commit %s" % frozen[:12])}


def _cancel_runs(gh, repo: str, runs: list[dict], *, say=print) -> int:
    """Annule les runs (POST …/cancel) ; rend le nombre d'échecs, jamais fatal."""
    failed = 0
    for row in runs:
        path = "repos/%s/actions/runs/%s/cancel" % (repo, row.get("id"))
        code, out, err = gh.run(["api", "-X", "POST", path])
        if code != 0:
            failed += 1
            say("cancel run %s: %s" % (row.get("id"),
                                       " ".join((err or out or "failed").split())[:200]))
        else:
            say("cancel run %s (%s) requested" % (row.get("id"),
                                                  (row.get("url") or "")[:80]))
    return failed


def _render_gel(decision: dict) -> list[str]:
    lines = ["lot #%d  %s  %s  %s" % (decision["work_item"], decision["repo"],
                                    decision["branch"], decision["sha"][:12])]
    lines.append("  %s: %s" % (decision["action"], decision.get("why") or ""))
    if decision.get("base"):
        lines.append("  base (merge-base with %s): %s" % (decision["target"],
                                                          decision["base"][:12]))
    if decision.get("exception"):
        lines.append("  exception %s (canon §3): %s" % (decision["exception"],
                                                        decision["reason"]))
    if decision.get("verdicts"):
        lines.append("  verdict(s): %s" % ", ".join(decision["verdicts"]))
    if decision.get("run"):
        run = decision["run"]
        lines.append("  run %s %s %s" % (run.get("id"), run.get("status"),
                                         run.get("url") or ""))
    return lines


def cmd_ci_run(cfg, args) -> int:
    """`ameesh ci run <lot>` : vérifie le gel et dispatche la CI une fois."""
    from . import plan_github, work
    from .db import open_db
    db = open_db(cfg)
    gh = plan_github.Gh()

    def api(path: str):
        return plan_github._json(gh, ["api", path])

    try:
        decision = gel_plan(db, cfg, args.id, api=api, sha=args.sha or "",
                            avis=args.avis, exception=args.exception or "",
                            reason=args.reason or "", full=args.full, force=args.force,
                            repo=args.repo or "")
    except (GelError, plan_github.GithubError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"schema": GEL_SCHEMA, "ok": False, "work_item": args.id,
                              "action": "refuse", "why": str(exc)}, ensure_ascii=False,
                             indent=1))
        else:
            print("ameesh ci run: %s" % exc)
        return 1
    if args.dry_run or decision["action"] != "dispatch":
        if args.json:
            print(json.dumps({**decision, "ok": True, "dispatched": False},
                             ensure_ascii=False, indent=1))
        else:
            print("\n".join(_render_gel(decision)))
        return 0
    # canon §3 : un run de la branche qui ne porte pas le SHA gelé est périmé.
    stale = cancel_plan(db, cfg, args.id, api=api, repo=args.repo or "")["runs"]
    if stale and not args.dry_run:
        _cancel_runs(gh, decision["repo"], stale,
                     say=lambda text: print("  %s" % text))
    argv = ["workflow", "run", "tests.yml", "--repo", decision["repo"],
            "--ref", decision["branch"],
            "-f", "mode=%s" % decision["mode"],
            "-f", "sha=%s" % decision["sha"],
            "-f", "base=%s" % decision["base"],
            "-f", "lot=%d" % decision["work_item"]]
    if decision["exception"]:
        argv += ["-f", "exception=%s" % decision["exception"]]
    code, out, err = gh.run(argv)
    if code != 0:
        # l'erreur du dispatch est dite, et le lot la garde
        why = "dispatch refused: %s" % " ".join((err or out or "failed").split())[:300]
        try:
            work.note(db, args.id, "ci: %s (sha %s, mode %s)"
                      % (why, decision["sha"][:12], decision["mode"]))
        except Exception:  # noqa: BLE001 - la note ne masque pas l'échec
            pass
        print("ameesh ci run: %s" % why)
        return 1
    runs = _runs_of(api, decision["repo"], sha=decision["sha"])
    run = next((r for r in runs if (r.get("event") or "") == "workflow_dispatch"), None)
    url = (run or {}).get("html_url") or ""
    note = ("ci: %s dispatched on %s (mode %s%s)%s"
            % ("full suite" if decision["mode"] == "full" else "impacted + contract",
               decision["sha"], decision["mode"],
               ", exception %s: %s" % (decision["exception"], decision["reason"])
               if decision["exception"] else "",
               " — %s" % url if url else ""))
    try:
        work.note(db, args.id, note)
    except Exception as exc:  # noqa: BLE001 - le dispatch est parti, on le dit
        print("ameesh ci run: dispatched, but the lot note failed (%s)" % exc)
    if args.json:
        print(json.dumps({**decision, "ok": True, "dispatched": True,
                          "run": _short_run(run or {})}, ensure_ascii=False, indent=1))
    else:
        print("\n".join(_render_gel(decision)))
        print("  dispatched: %s" % (url or "run queued, it will appear shortly"))
    return 0


def cmd_ci_cancel(cfg, args) -> int:
    """`ameesh ci cancel <lot>` : annule les runs périmés de la branche."""
    from . import plan_github
    from .db import open_db
    db = open_db(cfg)
    gh = plan_github.Gh()

    def api(path: str):
        return plan_github._json(gh, ["api", path])

    try:
        report = cancel_plan(db, cfg, args.id, api=api, all_runs=args.all,
                             repo=args.repo or "")
    except (GelError, plan_github.GithubError) as exc:
        print("ameesh ci cancel: %s" % exc)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print("lot #%d  %s  %s" % (report["work_item"], report["repo"], report["branch"]))
        print("  %s: %d run(s)" % (report["why"], len(report["runs"])))
    if not report["runs"]:
        return 0
    if args.dry_run:
        for row in report["runs"]:
            print("  would cancel run %s (%s)" % (row["id"], row["url"] or ""))
        return 0
    failed = _cancel_runs(gh, report["repo"], report["runs"])
    return 1 if failed else 0


def cmd_ci(cfg, args) -> int:
    from .db import open_db
    command = getattr(args, "ci_command", "status")
    if command == "run":
        return cmd_ci_run(cfg, args)
    if command == "cancel":
        return cmd_ci_cancel(cfg, args)
    db = open_db(cfg)
    try:
        report = status_of(db, args.id, limit=args.limit)
    except CiError as exc:
        print("ameesh ci: %s" % exc)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print("\n".join(render_status(report)))
    return 0
