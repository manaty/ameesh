# SPDX-License-Identifier: AGPL-3.0-only
"""#2988 — voisins d'un lot à sa création (intake), et tête relue.

Un lot né d'une revue (ultrareview, verdict, relecture) doit porter la tête
exacte que la revue a lue. Sans elle, le constat peut viser une ligne déjà
changée par un lot fusionné entre-temps : #2981 citait encore
`mobile-build.yml:121` dans sa version #2904, alors que #2915 avait remplacé
cette ligne sur `develop` quatre heures plus tôt.

Ce module fournit :

* `review_born` : le lot est-il né d'une revue ? (`--reviewed-head`, ou un
  mot de revue dans le titre ou la source ; les lots review-after-merge sont
  exclus de la détection automatique) ;
* `reviewed_head_of` : la tête relue écrite dans la note de création
  (`reviewed head: <sha>`, ou `absent`), relue par `work show` ;
* `neighbors` : les lots du même projet, ouverts ou terminés depuis moins de
  sept jours, qui citent le même chemin, partagent au moins trois mots
  significatifs de titre, ou dont un commit `tête..cible` touche la ligne
  citée (`git log -L`) — ce dernier motif s'affiche « peut-être corrigé par
  #N (sha court) ».

Tout est en lecture seule : la création n'est jamais refusée (L130). Sans
clone local du dépôt, le motif git est dit « non vérifié ».
"""
from __future__ import annotations

import os
import re
import unicodedata
from typing import Iterable, Optional

#: fenêtre par défaut des voisins terminés (jours)
SINCE_DAYS = 7
#: nombre maximal de voisins rendus
MAX_NEIGHBORS = 10
#: longueur admise d'une tête relue : une abréviation git (7) à un SHA-256
MIN_HEAD = 7
MAX_HEAD = 64
#: un SHA (hexadécimal), jamais vide
_HEAD_RE = re.compile(r"^[0-9a-fA-F]{%d,%d}$" % (MIN_HEAD, MAX_HEAD))
#: la note de création porte `reviewed head: <sha|absent>`
_REVIEWED_NOTE_RE = re.compile(
    r"(?:^|[;\n])\s*reviewed head:\s*(absent|[0-9a-fA-F]{%d,%d})(?![0-9a-fA-F])"
    % (MIN_HEAD, MAX_HEAD))
#: les mots d'une revue, dans un titre ou une source ; bornes de mot strictes
#: pour ne pas prendre « preview » ou « reviewed » pour une revue
REVIEW_WORD_RE = re.compile(
    r"(?i)(?<![\w-])(?:ultra[-\s]?reviews?|reviews?|revues?|relectures?|verdicts?)"
    r"(?![\w-])")
#: une ligne de `git log -L` : le hash puis les champs du format demandé
_GIT_RECORD_SEP = "\x1e"
#: extensions de fichier admises dans un constat (liste fermée : un constat
#: qui cite `example.com` ou `1.2.3` n'est pas un chemin)
_FILE_EXTENSIONS = frozenset("""
py pyi ts tsx js jsx mjs cjs json jsonc yml yaml toml ini cfg conf md mdx
rst txt sql sh bash zsh ps1 bat css scss sass less html htm xml svg png jpg
jpeg gif webp ico pdf lock gradle properties env service go rs java kt kts
swift rb php c h cc cpp cxx hpp cs ex exs erl hs lua pl r scala vue svelte
astro tf tfvars nix patch diff csv tsv proto graphql gql
""".split())
#: noms de fichier sans extension qu'un constat peut citer
_FILE_NAMES = frozenset((
    "Dockerfile", "Makefile", "LICENSE", "NOTICE", "README", "CHANGELOG",
    "Gemfile", "Rakefile", "Procfile", "Caddyfile", "Vagrantfile",
))
#: mots vides (4 lettres ou plus) : français, anglais, et le vocabulaire
#: générique d'une revue, qui ne doit pas rapprocher deux constats
_STOP_WORDS = frozenset("""
dans avec pour sans plus moins être etre cette leur leurs nous vous elles
ils sont fait faire tout tous toute toutes aussi mais donc alors entre vers
chez sous sur par pas que qui quoi dont comme lors apres après avant depuis
pendant encore deja déjà toujours jamais rien bien meme même autre autres
quel quelle quels quelles chaque selon est was were with from that this
these those have has been will would should could when where which what into
than then there their them they your about after before also only more most
some such just like over under between because while shall must does done
being other same review reviews revue revues relecture relectures ultrareview
verdict verdicts constat constats analyse issue ticket
""".split())

#: suffixe d'hôte : `example.com/x.py` est une URL, pas un chemin du dépôt
_HOST_SUFFIXES = frozenset((
    "com", "org", "net", "io", "dev", "fr", "ph", "co", "uk", "ai", "app",
    "info", "biz", "cloud", "site", "tech",
))
#: `chemin[:ligne]`, éventuellement `chemin:debut-fin`. Le préfixe interdit
#: une URL (`https://…`) et un fragment déjà consommé.
_PATH_RE = re.compile(
    r"(?<![\w./:-])((?:(?:[\w.-]+/)*[\w.-]+)\.([A-Za-z][A-Za-z0-9]{0,9}))"
    r"(?::(\d+)(?:-(\d+))?)?(?![\w/])")
#: un chemin sans extension mais dont le nom est connu (`Dockerfile`)
_NAME_RE = re.compile(
    r"(?<![\w./:-])((?:[\w.-]+/)*(?:%s))(?::(\d+)(?:-(\d+))?)?"
    r"(?![\w/]|\.\w)" % "|".join(sorted(_FILE_NAMES)))
#: un fichier caché connu (`/.gitignore`, `/.env`)
_DOTFILE_RE = re.compile(
    r"(?<![\w./:-])((?:[\w.-]+/)*\.(?:gitignore|gitattributes|dockerignore|env))"
    r"(?::(\d+)(?:-(\d+))?)?(?![\w-]|\.\w)")


class IntakeNeighborsError(ValueError):
    """Entrée invalide (tête relue non hexadécimale)."""


def normalize_head(value: Optional[str]) -> Optional[str]:
    """La tête relue, normalisée en minuscules, ou None si vide.

    Un SHA non hexadécimal (ou hors de 7..64 caractères) est refusé : le
    journal doit citer une révision, pas un mot.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if not _HEAD_RE.match(text):
        raise IntakeNeighborsError(
            "--reviewed-head: a hexadecimal commit sha (7 to 64 characters), not %r"
            % (value,))
    return text.lower()


def _review_after_merge(issue_ref: str = "", source: str = "") -> bool:
    """Le lot est-il un lot de relecture après fusion (`review_lot.review_ref`) ?"""
    try:
        from . import review_lot
    except Exception:  # noqa: BLE001 - module absent : aucune exclusion
        return False
    try:
        return bool(review_lot.review_ref({"issue_ref": issue_ref or "",
                                           "source": source or ""}))
    except Exception:  # noqa: BLE001 - référence illisible : pas d'exclusion
        return False


def review_born(*, title: str = "", source: str = "", issue_ref: str = "",
                reviewed_head: Optional[str] = None) -> bool:
    """Le lot est-il né d'une revue ?

    Oui s'il porte une tête relue explicite, ou si son titre ou sa source
    cite une revue (regex étroite). L'exclusion des lots review-after-merge
    ne vaut que pour la détection automatique : une tête relue passée
    explicitement reste enregistrée.
    """
    if str(reviewed_head or "").strip():
        return True
    if _review_after_merge(issue_ref, source):
        return False
    return bool(REVIEW_WORD_RE.search(" ".join([str(title or ""),
                                               str(source or "")])))


def reviewed_head_of(events: Iterable[dict]) -> Optional[str]:
    """La tête relue de la note de création (`<sha>` ou `absent`), sinon None.

    Le journal est lu du plus récent au plus ancien : la création est la
    ligne d'identifiant le plus petit. Une note postérieure qui parlerait de
    « reviewed head » ne doit jamais usurper la tête relue.
    """
    rows = list(events or ())
    if not rows:
        return None
    try:
        first = min(rows, key=lambda row: int(row.get("id") or 0))
    except (TypeError, ValueError):
        first = rows[-1]
    match = _REVIEWED_NOTE_RE.search(str(first.get("note") or ""))
    return match.group(1) if match else None


def format_reviewed_line(value: Optional[str]) -> Optional[str]:
    """La ligne `reviewed :` de `work show`, ou None."""
    if not value:
        return None
    if value == "absent":
        return "absent (lot né d'une revue sans tête relue)"
    return value


# --------------------------------------------------------------------------
# dépôt local et tête relue
# --------------------------------------------------------------------------

def _toplevel(path: str) -> str:
    from . import canon

    try:
        proc = canon._git(path, "rev-parse", "--show-toplevel")
    except canon.GitError:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", "replace").strip()


def _origin(path: str) -> str:
    from . import canon, migrations

    try:
        proc = canon._git(path, "remote", "get-url", "origin")
    except canon.GitError:
        return ""
    if proc.returncode != 0:
        return ""
    try:
        return migrations.repo_key(
            proc.stdout.decode("utf-8", "replace").strip()) or ""
    except Exception:  # noqa: BLE001 - une URL inhabituelle n'est pas un dépôt
        return ""


def _project_slug(cfg, app: str) -> str:
    """`owner/repo` déclaré pour ce projet (`github.projects`), sinon ''."""
    if cfg is None or not app:
        return ""
    try:
        from . import plan_github

        return plan_github.settings_of(cfg).repo_for(app) or ""
    except Exception:  # noqa: BLE001 - configuration absente ou invalide
        return ""


def _radar_project(cfg, app: str, slug: str = ""):
    """Le projet `radar.projects` de ce lot : par son nom, puis par son dépôt."""
    if cfg is None:
        return None
    try:
        from . import migrations, radar

        projects = list(radar.settings_of(cfg).projects.values())
    except Exception:  # noqa: BLE001 - configuration absente ou invalide
        return None
    for project in projects:
        if app and getattr(project, "name", "") == app:
            return project
    for project in projects:
        github = getattr(project, "github", "") or ""
        if not slug or not github:
            continue
        try:
            same = migrations.repo_key(github) == migrations.repo_key(slug)
        except Exception:  # noqa: BLE001 - URL inhabituelle
            continue
        if same:
            return project
    return None


def _declared_checkout(cfg, app: str, slug: str) -> str:
    """Le clone que l'hôte déclare pour ce projet (`radar.projects`), ou ''."""
    project = _radar_project(cfg, app, slug)
    if project is None:
        return ""
    repo = getattr(project, "repo", "") or ""
    return repo if repo and os.path.isdir(repo) else ""


def resolve_clone(cfg, app: str, repo: Optional[str] = None) -> Optional[str]:
    """Un clone local du dépôt du projet, ou None.

    Un `repo` explicite fait foi (vérifié comme dépôt git). Sinon le clone
    déclaré par l'hôte (`radar.projects.<p>.repo`, ou `github.projects` +
    ce clone) est préféré ; à défaut, le dossier courant, mais seulement si
    son `origin` n'est pas un autre dépôt que celui du projet.
    """
    if repo:
        top = _toplevel(repo)
        return top or None
    slug = _project_slug(cfg, app)
    project = _radar_project(cfg, app, slug)
    if project is not None and not slug and getattr(project, "github", ""):
        # le projet radar nomme le dépôt : le dossier courant ne doit pas
        # usurper un autre dépôt quand le clone déclaré est absent
        from . import migrations

        try:
            slug = migrations.repo_key(project.github) or ""
        except Exception:  # noqa: BLE001 - URL inhabituelle
            slug = ""
    declared = _declared_checkout(cfg, app, slug)
    if declared:
        return _toplevel(declared) or declared
    if slug:
        try:
            from . import migrations

            found = migrations.checkout_for(slug, cfg)
        except Exception:  # noqa: BLE001 - configuration illisible
            found = None
        if found:
            return _toplevel(found) or found
    current = _toplevel(os.getcwd())
    if not current:
        return None
    if slug:
        origin = _origin(current)
        if origin and origin != slug:
            return None
    return current


def resolve_head(repo: Optional[str], head: Optional[str]) -> Optional[str]:
    """Le SHA complet (40 caractères usuels) dans ce clone, sinon None."""
    if not repo or not head:
        return None
    from . import canon

    try:
        proc = canon._git(repo, "rev-parse", "--verify", "--quiet",
                          head + "^{commit}", env_extra=canon.NO_LAZY_FETCH)
    except canon.GitError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip().lower() or None


def default_target(repo: Optional[str]) -> str:
    """La cible du dépôt (`git config ameesh.target`, `origin/HEAD`, main)."""
    if not repo:
        return ""
    from . import plan_git

    try:
        return plan_git.default_target(repo) or ""
    except Exception:  # noqa: BLE001 - cible illisible : rien à comparer
        return ""


# --------------------------------------------------------------------------
# motifs
# --------------------------------------------------------------------------

def _clean_path(path: str) -> str:
    value = str(path or "").strip()
    while value.startswith("./"):
        value = value[2:]
    if value.startswith(("a/", "b/")) and "/" in value[2:]:
        value = value[2:]
    return value.casefold()


def _looks_like_host_path(path: str) -> bool:
    """`example.com/x.py` : le premier segment est un hôte, pas un dossier."""
    parts = str(path or "").split("/")
    if len(parts) < 2 or "." not in parts[0]:
        return False
    return parts[0].rsplit(".", 1)[-1].lower() in _HOST_SUFFIXES


def cited_paths(text: str) -> list[dict]:
    """Les chemins cités par un texte : `{path, line, end, raw}`, sans doublon.

    Le chemin est normalisé (préfixes `./`, `a/`, `b/` retirés, minuscules) ;
    la ligne sert au `git log -L`. Un même chemin cité deux fois n'est gardé
    qu'une fois (première citation, avec sa ligne).
    """
    found: dict[tuple, dict] = {}
    for regex, with_ext in ((_PATH_RE, True), (_NAME_RE, False), (_DOTFILE_RE, False)):
        for match in regex.finditer(str(text or "")):
            path = match.group(1)
            if with_ext:
                ext = match.group(2).lower()
                if ext not in _FILE_EXTENSIONS:
                    continue
            elif path.startswith("www."):
                continue
            if _looks_like_host_path(path):
                continue
            raw = match.group(0)
            line = match.group(3) if with_ext else match.group(2)
            end = match.group(4) if with_ext else match.group(3)
            clean = _clean_path(path)
            if not clean:
                continue
            key = (clean, int(line) if line else None)
            if key in found:
                continue
            found[key] = {"path": clean, "line": int(line) if line else None,
                          "end": int(end) if end else (int(line) if line else None),
                          "raw": raw, "display": path}
    return list(found.values())


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _path_match(mine: list[dict], theirs: list[dict]) -> Optional[dict]:
    """Le premier chemin du voisin qui désigne le même fichier que le constat."""
    for left in mine:
        for right in theirs:
            if left["path"] == right["path"]:
                return right
    for left in mine:
        for right in theirs:
            if _basename(left["path"]) == _basename(right["path"]):
                return right
    return None


def _fold(text: str) -> str:
    """Minuscules, accents repliés, ponctuation en espaces."""
    value = unicodedata.normalize("NFKD", str(text or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def significant_words(text: str) -> set[str]:
    """Les mots significatifs d'un titre (4 lettres ou plus, sans mots vides)."""
    return {word for word in _fold(text).split()
            if len(word) >= 4 and word.isalpha() and word not in _STOP_WORDS}


def _title_match(title: str, theirs: str) -> list[str]:
    common = significant_words(title) & significant_words(theirs)
    return sorted(common) if len(common) >= 3 else []


def _git(repo: str, *args: str):
    from . import canon

    return canon._git(repo, *args, env_extra=canon.NO_LAZY_FETCH)


def _resolve_git_path(repo: str, target: str, path: str,
                      trees: dict[str, list[str]]) -> Optional[str]:
    """Le chemin réel du fichier cité dans `target`, ou None.

    Un constat cite souvent le seul nom du fichier (`mobile-build.yml:121`)
    alors que le dépôt le porte sous `.github/workflows/`. Le chemin exact
    est essayé d'abord ; sinon la liste de l'arbre (une fois par cible) rend
    l'unique fichier de ce nom, ou rien si le nom est ambigu.
    """
    try:
        proc = _git(repo, "rev-parse", "--verify", "--quiet", "%s:%s" % (target, path))
    except Exception:  # noqa: BLE001 - dépôt illisible
        return None
    if proc.returncode == 0:
        return path
    if "/" in path:
        return None
    if target not in trees:
        try:
            listing = _git(repo, "ls-tree", "-r", "--name-only", target)
        except Exception:  # noqa: BLE001 - arbre illisible
            listing = None
        trees[target] = (listing.stdout.decode("utf-8", "replace").splitlines()
                         if listing is not None and listing.returncode == 0 else [])
    matches = [candidate for candidate in trees[target]
               if candidate.rsplit("/", 1)[-1] == path]
    return matches[0] if len(matches) == 1 else None


def _commits_touching(repo: str, head: str, target: str, path: str,
                      line: int, end: int) -> list[dict]:
    """Les commits de `head..target` qui touchent `path:line` (`git log -L`)."""
    from . import canon

    span = "%d,%d:%s" % (line, max(int(end or line), int(line)), path)
    try:
        proc = _git(repo, "log", "--format=%H%x00%s%x00%B%x1e",
                    "%s..%s" % (head, target), "-L" + span)
    except canon.GitError:
        return []
    if proc.returncode != 0:
        return []
    out = proc.stdout.decode("utf-8", "replace")
    commits = []
    for chunk in out.split(_GIT_RECORD_SEP):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        fields = chunk.split("\x00", 2)
        if len(fields) < 3:
            continue
        sha, subject = fields[0].strip(), fields[1].strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            continue
        message = fields[2]
        # `git log -L` ajoute le diff après le message : un trailer recopié
        # dans un ajout (`+ameesh-work: 999`) ne doit pas rattacher le commit.
        cut = message.find("\ndiff --git ")
        if cut != -1:
            message = message[:cut]
        commits.append({"sha": sha, "subject": subject,
                        "message": message, "path": path, "line": line})
    return commits


def _lots_of_commit(repo: str, commit: dict, hash_refs: Optional[bool] = None) -> dict[int, str]:
    """Les lots désignés par un commit : `cited_lots`, puis `lot/<id>-…`."""
    from . import plan_git

    if hash_refs is None:
        hash_refs = plan_git.lot_ref_hash(repo)
    message = commit.get("message") or ""
    try:
        lots = plan_git.cited_lots(message, hash_refs=hash_refs)
    except Exception:  # noqa: BLE001 - message illisible
        lots = {}
    try:
        branch = plan_git.merge_branch(message)
        lot = plan_git.branch_lot(branch) if branch else None
    except Exception:  # noqa: BLE001 - titre illisible
        lot = None
    if lot:
        lots.setdefault(lot, "lot-branch")
    return lots


def _git_hits(repo: str, head: str, target: str, refs: list[dict],
              candidates: set[int]) -> dict[int, dict]:
    """`{lot: {sha, subject, path, line, motif}}` des lignes déjà changées."""
    from . import plan_git

    try:
        hash_refs = plan_git.lot_ref_hash(repo)
    except Exception:  # noqa: BLE001 - configuration illisible
        hash_refs = False
    hits: dict[int, dict] = {}
    seen_spans: set[tuple] = set()
    trees: dict[str, list[str]] = {}
    for ref in refs:
        if not ref.get("line"):
            continue
        path = _resolve_git_path(repo, target, ref["path"], trees)
        if not path:
            continue
        key = (path, ref["line"], ref.get("end"))
        if key in seen_spans:
            continue
        seen_spans.add(key)
        for commit in _commits_touching(repo, head, target, path,
                                        int(ref["line"]),
                                        int(ref.get("end") or ref["line"])):
            for lot in _lots_of_commit(repo, commit, hash_refs):
                if lot not in candidates or lot in hits:
                    continue
                hits[lot] = {"sha": commit["sha"], "subject": commit["subject"],
                             "path": ref["path"], "line": ref["line"],
                             "motif": "peut-être corrigé par #%d (%s)"
                                      % (lot, commit["sha"][:9])}
    return hits


# --------------------------------------------------------------------------
# états affichés
# --------------------------------------------------------------------------

def _merged_milestone(db, item_ids: list[int]) -> dict[int, str]:
    """Le dernier sha de fusion de chaque lot (jalons `merged` non annulés)."""
    if not item_ids:
        return {}
    try:
        rows = db.query(
            "SELECT work_item_id, sha, at FROM work_item_milestones"
            " WHERE work_item_id IN (%s) AND kind = 'merged'"
            " AND NOT agent_mesh_merged_annulled(work_item_id, at)"
            " ORDER BY work_item_id, at, id" % ",".join(["%s"] * len(item_ids)),
            tuple(int(i) for i in item_ids))
    except Exception:  # noqa: BLE001 - base sans jalons : pas de preuve
        return {}
    out: dict[int, str] = {}
    for row in rows or ():
        sha = str(row.get("sha") or "").strip()
        if sha:
            out[int(row["work_item_id"])] = sha
    return out


def _main_ref(repo: Optional[str]) -> str:
    if not repo:
        return ""
    for ref in ("origin/main", "main", "refs/remotes/origin/main"):
        try:
            proc = _git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
        except Exception:  # noqa: BLE001 - dépôt illisible
            return ""
        if proc.returncode == 0:
            return ref
    return ""


def _in_main(repo: Optional[str], sha: str) -> Optional[bool]:
    """Le commit est-il ancêtre de main ? None sans clone ou sans sha."""
    ref = _main_ref(repo)
    if not ref or not sha:
        return None
    try:
        proc = _git(repo, "merge-base", "--is-ancestor", sha, ref)
    except Exception:  # noqa: BLE001 - dépôt illisible
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def _state_label(repo: Optional[str], item: dict, target: str,
                 merged_sha: str = "") -> str:
    state = str(item.get("state") or "")
    if state == "promoted":
        return "livré"
    if state == "closed":
        reason = str(item.get("close_reason") or "")
        if reason in ("delivered", "done"):
            return "livré"
        return "closed" + (" (%s)" % reason if reason else "")
    if state == "merged":
        destination = (item.get("branch_target") or target or "origin/main")
        label = "merged → %s" % destination
        inside = _in_main(repo, merged_sha or str(item.get("branch_head") or ""))
        if inside is True:
            label += " (dans main)"
        elif inside is False:
            label += " (hors main)"
        return label
    return state or "?"


# --------------------------------------------------------------------------
# recherche des voisins
# --------------------------------------------------------------------------

def _candidate_rows(db, app: str, since_days: int) -> list[dict]:
    if not app:
        return []
    return db.query(
        "SELECT id, title, body, state, close_reason, branch_target, branch_head,"
        "       issue_ref,"
        "       extract(epoch from closed_at)::float8 AS closed_ts,"
        "       extract(epoch from updated_at)::float8 AS updated_ts"
        "  FROM work_items"
        " WHERE app = %s"
        "   AND (state NOT IN ('merged', 'promoted', 'closed')"
        "        OR coalesce(closed_at,"
        "                    (SELECT max(m.at) FROM work_item_milestones m"
        "                      WHERE m.work_item_id = work_items.id"
        "                        AND m.kind = 'merged'),"
        "                    updated_at) >= now() - %s * interval '1 day')"
        " ORDER BY id",
        (app, int(since_days)))


def neighbors(db, app: str, title: str = "", body: str = "",
              reviewed_head: Optional[str] = None,
              since_days: int = SINCE_DAYS, *, cfg=None,
              repo: Optional[str] = None) -> dict:
    """Les voisins d'un lot à créer, et l'état du contrôle git.

    Rend `{neighbors, git, target, reviewed_head, app, since_days}` ;
    `neighbors` est une liste triée git, puis chemin, puis titre, bornée à
    `MAX_NEIGHBORS`. Aucune écriture. Les erreurs git ou de configuration
    sont absorbées (motif « non vérifié ») ; une lecture de base en échec
    remonte à l'appelant, qui, à la création, ne refuse jamais le lot (L130).
    """
    app = (app or "").strip()
    head = str(reviewed_head or "").strip().lower() or None
    result = {"app": app, "since_days": int(since_days),
              "reviewed_head": head, "git": "", "target": "", "neighbors": []}
    if not app:
        if head:
            result["git"] = "non vérifié"
        return result
    rows = _candidate_rows(db, app, int(since_days))
    needs_clone = bool(head) or any(
        str(row.get("state") or "") in ("merged", "promoted") for row in rows)
    clone = resolve_clone(cfg, app, repo) if needs_clone else None
    # La cible n'est utile qu'au `git log <tête>..<cible>` ; l'affichage de
    # l'état d'un voisin se contente de sa `branch_target`.
    target = default_target(clone) if (clone and head) else ""
    resolved = resolve_head(clone, head) if (clone and head) else None
    if resolved:
        head = resolved
    result["reviewed_head"] = head
    result["target"] = target
    if head:
        # Le contrôle git est dit même sans candidat : « git non vérifié » est
        # une information sur la revue, pas sur les voisins trouvés.
        result["git"] = ("vérifié" if (clone and target and resolved)
                         else "non vérifié")
    if not rows:
        return result

    mine_paths = cited_paths(" ".join([str(title or ""), str(body or "")]))
    by_id = {int(row["id"]): row for row in rows}
    hits: dict[int, dict] = {}
    if result["git"] == "vérifié":
        hits = _git_hits(clone, head, target, mine_paths, set(by_id))

    merged = _merged_milestone(
        db, [int(row["id"]) for row in rows if row.get("state") in ("merged", "promoted")])

    found: list[dict] = []
    for lot_id, row in by_id.items():
        theirs_paths = cited_paths(" ".join([str(row.get("title") or ""),
                                             str(row.get("body") or "")]))
        path = _path_match(mine_paths, theirs_paths)
        words = _title_match(title, str(row.get("title") or ""))
        hit = hits.get(lot_id)
        if not hit and not path and not words:
            continue
        motifs = []
        rank = 2
        if hit:
            motifs.append(hit["motif"])
            rank = 0
        if path:
            shown = path.get("display") or path["path"]
            if path.get("line"):
                shown += ":%s" % path["line"]
            motifs.append("même fichier %s" % shown)
            rank = min(rank, 1)
        if words:
            motifs.append("titre proche (%s)" % ", ".join(words))
        found.append({
            "id": lot_id,
            "title": row.get("title") or "",
            "state": row.get("state") or "",
            "state_label": _state_label(clone, row, target,
                                        merged.get(lot_id, "")),
            "motifs": motifs,
            "rank": rank,
            "git": bool(hit),
            "path": bool(path),
            "words": words,
        })
    found.sort(key=lambda row: (row["rank"], row["id"]))
    result["neighbors"] = found[:MAX_NEIGHBORS]
    return result


def format_neighbor(neighbor: dict) -> str:
    """Une ligne lisible : `#2915 merged → develop — même fichier …`."""
    return "#%d %s — %s" % (int(neighbor["id"]), neighbor.get("state_label") or
                            neighbor.get("state") or "?",
                            "; ".join(neighbor.get("motifs") or ()))


def note_line(result: dict) -> str:
    """Le fragment de la note de création : voisins et état git."""
    parts = []
    neighbors = result.get("neighbors") or []
    if neighbors:
        parts.append("neighbors: " + " | ".join(
            "#%d %s (%s)" % (int(row["id"]), row.get("state_label") or "?",
                             "; ".join(row.get("motifs") or ()))
            for row in neighbors))
    if result.get("git") == "non vérifié":
        parts.append("git non vérifié")
    return "; ".join(parts)
