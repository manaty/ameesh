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
* `creation_note` : le fragment à préfixer à la note de création et les
  informations rendues à la CLI (tête relue, voisins, avertissements),
  appelé avant `work.add` pour que `work._create` ne lance pas la recherche
  git (il est aussi appelé par `incident.py`, `assignments.py` et
  `review_lot.py`) ;
* `neighbors` : les lots du même projet, ouverts ou terminés depuis moins de
  sept jours, qui citent le même chemin, partagent au moins trois mots
  significatifs de titre, ou dont un commit `tête..cible` touche la ligne
  citée (`git log -L`) — ce dernier motif s'affiche « peut-être corrigé par
  #N (sha court) ».

Tout est en lecture seule : la création n'est jamais refusée (L130). Sans
clone local du dépôt, le motif git est dit « non vérifié ».
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

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
#: un en-tête de hunk de diff (`@@ -a,b +c,d @@`)
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
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


def project_target(cfg, app: str, slug: str = "") -> str:
    """La cible d'intégration déclarée du projet (`radar.projects.<p>.target`),
    ou "" (#4733).

    Un lot sans `branch_target` se lit sur la cible que le projet déclare
    (`develop` pour Nexlink), jamais sur la branche par défaut de la forge
    (`origin/HEAD`, souvent `main`/prod) : le diff `main...branche` porterait
    tout l'écart d'intégration, pas les fichiers du lot.

    Le projet se reconnaît à son nom, puis au dépôt déclaré pour ce nom
    (`github.projects`) : un projet radar peut porter un autre nom que l'app.
    """
    if not slug and app:
        slug = _project_slug(cfg, app)
    project = _radar_project(cfg, app or "", slug)
    if project is None:
        return ""
    return str(getattr(project, "target", "") or "").strip()


# --------------------------------------------------------------------------
# motifs
# --------------------------------------------------------------------------

def _clean_path(path: str) -> str:
    """Le chemin débarrassé des préfixes de diff, casse conservée (Git Linux)."""
    value = str(path or "").strip()
    while value.startswith("./"):
        value = value[2:]
    if value.startswith(("a/", "b/")) and "/" in value[2:]:
        value = value[2:]
    return value


def _looks_like_host_path(path: str) -> bool:
    """`example.com/x.py` : le premier segment est un hôte, pas un dossier."""
    parts = str(path or "").split("/")
    if len(parts) < 2 or "." not in parts[0]:
        return False
    return parts[0].rsplit(".", 1)[-1].lower() in _HOST_SUFFIXES


def cited_paths(text: str) -> list[dict]:
    """Les chemins cités par un texte : `{path, git_path, line, end, raw}`, sans doublon.

    `path` est la clé de comparaison textuelle (minuscules) ; `git_path` garde
    la casse réelle, celle du fichier dans le dépôt (`Makefile` n'est pas
    `makefile` sous Linux) ; `display` est la citation d'origine. La ligne sert
    au `git log -L`. Un même chemin cité deux fois n'est gardé qu'une fois
    (première citation, avec sa ligne).
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
            key = (clean.casefold(), int(line) if line else None)
            if key in found:
                continue
            found[key] = {"path": clean.casefold(), "git_path": clean,
                          "line": int(line) if line else None,
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
    (casse comprise) est essayé d'abord ; sinon la liste de l'arbre (une fois
    par cible) rend l'unique fichier de ce nom. Une casse fautive n'est
    corrigée qu'en dernier recours, si elle ne désigne qu'un seul fichier :
    `Makefile` et `makefile` sont deux noms distincts sous Linux.
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
    basename = path.rsplit("/", 1)[-1]
    exact = [candidate for candidate in trees[target]
             if candidate.rsplit("/", 1)[-1] == basename]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    folded = [candidate for candidate in trees[target]
              if candidate.rsplit("/", 1)[-1].casefold() == basename.casefold()]
    return folded[0] if len(folded) == 1 else None


def _commits_touching(repo: str, head: str, target: str, path: str,
                      line: int, end: int) -> list[dict]:
    """Les commits de `head..target` qui touchent `path:line` (`git log -L`).

    Le séparateur `%x1e` est placé **en tête** du format : le patch d'un
    commit suit son en-tête, et le chunk suivant commence par l'en-tête du
    commit suivant, jamais par le patch précédent (le défaut B1 de #2988 : le
    second commit touchant la même ligne était rejeté, et le lot qu'il citait
    disparaissait). Le `%x00` final ferme le message avant son patch.
    """
    from . import canon

    span = "%d,%d:%s" % (line, max(int(end or line), int(line)), path)
    try:
        proc = _git(repo, "log", "--format=%x1e%H%x00%s%x00%B%x00",
                    "%s..%s" % (head, target), "-L" + span)
    except canon.GitError:
        return []
    if proc.returncode != 0:
        return []
    out = proc.stdout.decode("utf-8", "replace")
    commits = []
    for chunk in out.split(_GIT_RECORD_SEP):
        chunk = chunk.lstrip("\n")
        if not chunk:
            continue
        fields = chunk.split("\x00", 3)
        if len(fields) < 3:
            continue
        sha, subject = fields[0].strip(), fields[1].strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            continue
        # `%x00` ferme le message avant le patch : fields[2] est exactement
        # `%B`, même si le message cite lui-même un diff.
        commits.append({"sha": sha, "subject": subject,
                        "message": fields[2], "path": path, "line": line})
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


def _hunks(text: str) -> list[tuple]:
    """Les hunks `@@ -a,b +c,d @@` d'un diff (`--unified=0`)."""
    hunks: list[tuple] = []
    for line in str(text or "").splitlines():
        match = _HUNK_RE.match(line)
        if not match:
            continue
        hunks.append((int(match.group(1)), int(match.group(2) or 1),
                      int(match.group(3)), int(match.group(4) or 1)))
    return hunks


def _diff_hunks(repo: str, head: str, target: str, path: str,
                cache: dict[str, list[tuple]]) -> list[tuple]:
    """Les hunks `--unified=0` de `head..target` pour ce chemin."""
    if path in cache:
        return cache[path]
    try:
        proc = _git(repo, "diff", "--unified=0", "--no-color", "--no-ext-diff",
                    head, target, "--", path)
    except Exception:  # noqa: BLE001 - dépôt illisible
        proc = None
    hunks = (_hunks(proc.stdout.decode("utf-8", "replace"))
             if proc is not None and proc.returncode == 0 else [])
    cache[path] = hunks
    return hunks


def _path_history(repo: str, head: str, target: str, path: str,
                  cache: dict[str, list[dict]]) -> list[dict]:
    """Les commits `head..target` qui touchent `path`, du plus ancien au plus récent.

    Chaque entrée porte ses hunks (`--unified=0`). Sert au suivi vers l'avant
    d'une ligne que la cible ne porte plus (suppression de la ligne ou du
    fichier) : `git log -L` ne peut pas partir d'une ligne absente de la cible.
    """
    if path in cache:
        return cache[path]
    try:
        proc = _git(repo, "log", "--reverse", "--format=%x1e%H%x00%s%x00%B%x00",
                    "--unified=0", "--no-renames", "--no-color", "--no-ext-diff",
                    "%s..%s" % (head, target), "--", path)
    except Exception:  # noqa: BLE001 - dépôt illisible
        proc = None
    history: list[dict] = []
    if proc is not None and proc.returncode == 0:
        for chunk in proc.stdout.decode("utf-8", "replace").split(_GIT_RECORD_SEP):
            chunk = chunk.lstrip("\n")
            if not chunk:
                continue
            fields = chunk.split("\x00", 3)
            if len(fields) < 4:
                continue
            sha, subject = fields[0].strip(), fields[1].strip()
            if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
                continue
            history.append({"sha": sha, "subject": subject,
                            "message": fields[2], "hunks": _hunks(fields[3]),
                            "path": path})
    cache[path] = history
    return history


def _forward_line_commits(history: list[dict], line: int) -> list[dict]:
    """Les commits qui touchent `line` (coordonnées de `head`), du récent au vieux.

    Les hunks sont appliqués dans l'ordre chronologique en suivant la position
    de la ligne : une insertion au-dessus la décale, une suppression la fait
    disparaître — mais le commit qui la supprime la touche et doit être gardé
    (B2, second tour #2988). Une ligne remplacée continue d'être suivie.
    """
    position = max(1, int(line))
    touched: list[dict] = []
    for commit in history:
        # Les hunks d'un commit sont en coordonnées d'avant ce commit : la
        # position n'est décalée qu'après le dernier hunk (comme `_map_line`).
        offset = 0
        moved = None
        for old_start, old_count, new_start, new_count in commit["hunks"]:
            if old_count == 0:
                if old_start < position:
                    offset += new_count
                continue
            old_end = old_start + old_count - 1
            if position < old_start:
                break
            if position <= old_end:
                touched.append(commit)
                if new_count == 0:
                    # ligne supprimée : plus rien à suivre ensuite
                    return list(reversed(touched))
                moved = new_start + min(position - old_start, new_count - 1)
                break
            offset += new_count - old_count
        position = moved if moved is not None else position + offset
    return list(reversed(touched))


def _map_line(hunks: list[tuple], line: int) -> Optional[int]:
    """La ligne correspondante dans `target` pour une ligne de `head`.

    Une insertion au-dessus décale la ligne ; une suppression la fait
    disparaître (None). C'est le suivi que réclame le constat : il cite les
    coordonnées de la tête relue, pas celles de la cible quelques commits plus
    tard (B2 de #2988 : l'insertion en tête faisait lire `f.txt:3` au lieu de
    `f.txt:2`).
    """
    offset = 0
    for old_start, old_count, new_start, new_count in hunks:
        if old_count == 0:
            if old_start < line:
                offset += new_count
            continue
        old_end = old_start + old_count - 1
        if line < old_start:
            break
        if line <= old_end:
            if new_count == 0:
                return None
            return new_start + min(line - old_start, new_count - 1)
        offset += new_count - old_count
    return line + offset


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
    diffs: dict[str, list[tuple]] = {}
    histories: dict[str, list[dict]] = {}

    def record(commits, display, cited_line):
        for commit in commits:
            for lot in _lots_of_commit(repo, commit, hash_refs):
                if lot not in candidates or lot in hits:
                    continue
                hits[lot] = {"sha": commit["sha"], "subject": commit["subject"],
                             "path": display, "line": cited_line,
                             "motif": "peut-être corrigé par #%d (%s)"
                                      % (lot, commit["sha"][:9])}

    def deleted(history, first, last, display, cited_line, key):
        """Suivi avant pour une ligne (ou un fichier) absent de la cible."""
        if key in seen_spans:
            return
        seen_spans.add(key)
        commits: list[dict] = []
        for current in range(int(first), int(last) + 1):
            commits.extend(_forward_line_commits(history, current))
        record(commits, display, cited_line)

    for ref in refs:
        if not ref.get("line"):
            continue
        # B3 : le chemin Git garde sa casse ; la clé textuelle `path` ne sert
        # qu'à rapprocher les constats.
        git_path = ref.get("git_path") or ref["path"]
        display = ref.get("display") or git_path
        cited_line = int(ref["line"])
        cited_end = int(ref.get("end") or cited_line)
        path = _resolve_git_path(repo, target, git_path, trees)
        if path:
            hunks = _diff_hunks(repo, head, target, path, diffs)
            line = _map_line(hunks, cited_line)
            if line is not None:
                end = _map_line(hunks, cited_end)
                if end is None or end < line:
                    end = line
                key = ("log", path, line, end)
                if key in seen_spans:
                    continue
                seen_spans.add(key)
                record(_commits_touching(repo, head, target, path, line, end),
                       display, cited_line)
                continue
            # B2 (second tour) : la ligne citée a été supprimée entre la tête
            # et la cible ; `-L` ne peut pas partir d'une ligne absente. Le
            # suivi avant garde le commit qui la supprime.
            deleted(_path_history(repo, head, target, path, histories),
                    cited_line, cited_end, display, cited_line,
                    ("history", path, cited_line, cited_end))
            continue
        # Le fichier n'existe plus dans la cible : le résoudre dans la tête,
        # puis suivre la ligne (ou la suppression du fichier) vers la cible.
        head_path = _resolve_git_path(repo, head, git_path, trees)
        if not head_path:
            continue
        deleted(_path_history(repo, head, target, head_path, histories),
                cited_line, cited_end, display, cited_line,
                ("history", head_path, cited_line, cited_end))
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
    state = lifecycle.phase(item)
    if state == "promoted":
        return "livré"
    if state == lifecycle.CLOSED:
        reason = str(item.get("close_reason") or "")
        if reason in ("delivered", "done"):
            return "livré"
        return lifecycle.CLOSED + (" (%s)" % reason if reason else "")
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
        "SELECT id, title, body, state, legacy_state, workflow_stage, close_reason, branch_target, branch_head,"
        "       issue_ref,"
        "       extract(epoch from closed_at)::float8 AS closed_ts,"
        "       extract(epoch from updated_at)::float8 AS updated_ts"
        "  FROM work_items"
        " WHERE app = %s"
        f"   AND (state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
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
        str(row.get("state") or "") in lifecycle.DELIVERED for row in rows)
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
        db, [int(row["id"]) for row in rows if row.get("state") in lifecycle.DELIVERED])

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


def creation_note(db, cfg, app: str, title: str = "", body: str = "",
                  source: str = "", issue_ref: str = "",
                  reviewed_head: Optional[str] = None) -> tuple[str, dict]:
    """Le fragment de note et les informations d'un lot créé (#2988).

    Appelé par la CLI **avant** `work.add` : la tête relue, la détection d'un
    lot né d'une revue et la recherche des voisins (dont le `git log` local)
    ne doivent pas tourner dans `work._create`, appelé aussi par
    `incident.py`, `assignments.py` et `review_lot.py`.

    Rend `(segment, infos)` : `segment` est préfixé à la note de création
    (`reviewed head: …; neighbors: …`) ; `infos` porte les valeurs lues par
    l'appelant (`reviewed_head`, `reviewed_from_review`, `neighbors`,
    `neighbors_git`, `neighbors_error`, `reviewed_warning`). Une tête non
    hexadécimale lève `IntakeNeighborsError` ; une recherche en échec
    n'empêche jamais la création (L130) et se dit dans `neighbors_error`.
    """
    head = normalize_head(reviewed_head)
    born = review_born(title=title or "", source=source or "",
                       issue_ref=issue_ref or "", reviewed_head=head)
    if head:
        try:
            clone = resolve_clone(cfg, app)
            resolved = resolve_head(clone, head)
        except Exception:  # noqa: BLE001 - un clone illisible ne refuse rien
            resolved = None
        if resolved:
            head = resolved
    try:
        found = neighbors(db, app or "", title=title or "", body=body or "",
                          reviewed_head=head, cfg=cfg)
    except Exception as exc:  # noqa: BLE001 - la recherche ne refuse jamais (L130)
        found = {"neighbors": [], "git": "", "error": str(exc)}
    segments = []
    if born:
        segments.append("reviewed head: %s" % (head or "absent"))
    if found.get("error"):
        segments.append("neighbors: recherche indisponible")
    line = note_line(found)
    if line:
        segments.append(line)
    infos = {
        "reviewed_head": head or ("absent" if born else None),
        "reviewed_from_review": born,
        "neighbors": found.get("neighbors") or [],
        "neighbors_git": found.get("git") or "",
        "neighbors_error": found.get("error") or "",
        "reviewed_warning": (
            "lot né d'une revue sans tête relue : --reviewed-head <sha>"
            if born and not head else ""),
    }
    return "; ".join(segments), infos
