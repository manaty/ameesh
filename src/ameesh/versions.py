# SPDX-License-Identifier: AGPL-3.0-only
"""Versions of the projects and their next releases (lot #497, L160f).

Read-only, for the "Versions" tab of `ameesh progress` (`--serve` and
`--html` share the model): per project and product, the last **published**
version and its date (GitHub tags and releases) and the **next** release —
its release lots (`Release x.y.z`, `REL-`, `QA-`…), its planned date (the
plan of the lots, otherwise the p80 of the L157 reference table), its state
and its content (the draft notes of the release PR when it has some, e.g.
`docs/versions/1.10.0.md`; otherwise the lots merged since the last tag,
grouped by theme). A release lot without a planned date, a product without
a next release, a tag without a published release: said as `missing`, never
hidden.

Nothing comes from the canon: products are the version-tag streams of the
repositories the projects already cite (`issue_ref`, or the `github.projects`
setting of the host), the next version is the smallest one greater than the
last tag that an open release lot, an open `release/*` PR or a draft release
announces.

All GitHub reads go through `Reader`, read-only and cached `CACHE_S` seconds
(shared by the server): releases, tags, one file, one commit date. The client
is ameesh's own — the `GH_TOKEN`/`GITHUB_TOKEN` token through
`plan_github.Api` (the same `github` settings); `gh` only serves as a LOCAL
fallback when no token is set, and never on the server. Merged lots and
their pull requests come from the database (`sync-github`), not from the
API. Every datum carries its source and the time it was read, so a stale
cache is visible. A read that fails (no token, no network, no right) is
reported in `missing`; the rest of the view keeps working. No write, ever.
"""
from __future__ import annotations

import base64
import binascii
import datetime as _dt
import re
import textwrap
import time
from collections import Counter
from urllib.parse import quote

from . import estimates as estimates_mod
from . import lot_schedule
from . import plan_github
from . import registry
from . import work as work_mod

#: version of this view, embedded in `ameesh-progress/1` (`versions` key)
SCHEMA = "ameesh-versions/1"
#: durée de cache d'une lecture GitHub (secondes) : un onglet ouvert ne
#: martèle pas l'API ; le cache est partagé par le serveur (`Reader`)
CACHE_S = 300.0
#: délai d'un appel `gh api` : une lecture lente ne bloque pas la page
READ_TIMEOUT = 20.0
#: taille de page des lectures GitHub (une page suffit pour ces dépôts)
PAGE = 100
#: lots lus pour retrouver les lots de release et les fusionnés récents
LIMIT = 1000
#: dossier des notes de version du dépôt
NOTES_DIR = "docs/versions"
#: ordre d'attention d'un train de release : le premier état présent est dit
STATE_ORDER = ("blocked", "waiting_human", "qa", "build", "intake")

#: `v1.9.1`, `desktop-v0.2.8`, `desktop-0.2.8`… : préfixe de produit optionnel
_TAG_RE = re.compile(r"^(?:(?P<prefix>[A-Za-z][A-Za-z0-9._-]*)-)?v?(?P<num>\d+\.\d+(?:\.\d+)?)$")
#: un lot de release : titre commençant par Release/REL/QA (`RELEASE-QA-HOLD`)
_RELEASE_LOT_RE = re.compile(r"(?i)^\s*(?:release|rel|qa)\b")
#: une version citée dans un titre (`Release 1.10.0: …`, `REL-0.2.11 : …`)
_TITLE_VERSION_RE = re.compile(r"(?<![\d.])v?(\d+\.\d+(?:\.\d+)?)(?![\d.])")
#: `owner/repo#n` d'un lot (`issue_ref`)
_ISSUE_REF_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100})#([1-9][0-9]{0,17})$")
#: `**Thème**` ou `**Thème** : texte` d'une note de version
_THEME_RE = re.compile(r"^\*\*(.+?)\*\*[ \t]*:?[ \t]*(.*)$")
#: puce `- …` d'une note de version
_BULLET_RE = re.compile(r"^[ \t]*[-*][ \t]+(.*)$")
#: indice de produit dans un titre : `serveur`, `server`, `prod`…
_HINT_SERVER_RE = re.compile(r"(?i)\b(?:serveur|server|prod|production)\b")


# --------------------------------------------------------------------------
# versions et tags : fonctions pures
# --------------------------------------------------------------------------

def version_key(version: str) -> tuple:
    """Clé de tri d'une version numérique (`1.10.0` > `1.9.9`)."""
    parts = [int(part) for part in str(version).split(".") if part.isdigit()]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def parse_tag(tag: str) -> dict | None:
    """`{tag, prefix, version, key}` d'un tag de version, sinon None.

    `v1.9.1` → préfixe vide ; `desktop-v0.2.8` → préfixe `desktop`."""
    match = _TAG_RE.match(tag or "")
    if not match:
        return None
    version = match.group("num")
    return {"tag": tag, "prefix": match.group("prefix") or "", "version": version,
            "key": version_key(version)}


def version_of(title: str | None) -> str | None:
    """La première version citée dans un titre, sinon None."""
    match = _TITLE_VERSION_RE.search(title or "")
    return match.group(1) if match else None


def is_release_lot(title: str | None) -> bool:
    """Le titre annonce une release (`Release x.y.z`, `REL-…`, `QA-…`)."""
    return bool(_RELEASE_LOT_RE.match(title or ""))


def release_hint(title: str | None, prefixes) -> str | None:
    """Le préfixe de produit que le titre désigne, sinon None.

    `desktop` explicite ; `serveur`/`server`/`prod` désignent le flux sans
    préfixe (le produit principal du dépôt)."""
    low = (title or "").casefold()
    for prefix in prefixes:
        if prefix and prefix.casefold() in low:
            return prefix
    if "" in prefixes and _HINT_SERVER_RE.search(title or ""):
        return ""
    return None


def tag_style(tag: str, prefix: str) -> str:
    """Le style d'un tag de version : `v` (`v1.9.1`) ou rien (`1.9.1`)."""
    rest = tag[len(prefix) + 1:] if prefix else tag
    return "v" if rest.startswith("v") else ""


def tag_for(prefix: str, version: str, styles=("v",)) -> str:
    """Le tag attendu d'une version, au style des tags du flux."""
    style = "v" if "v" in (styles or ()) else ""
    base = "%s%s" % (style, version)
    return "%s-%s" % (prefix, base) if prefix else base


def streams_of(versions: list[dict]) -> list[dict]:
    """Les produits d'un dépôt : un flux par préfixe de tag.

    `versions` : `{prefix, version, tag, key}` (un par tag ou release). Rend
    `{prefix, version, tag, key, styles}` par flux, du plus récent au plus
    ancien (`styles` sert à deviner le tag d'une prochaine version)."""
    streams: dict = {}
    for item in versions:
        prefix = item.get("prefix") or ""
        current = streams.get(prefix)
        if current is None or item["key"] > current["key"]:
            streams[prefix] = {"prefix": prefix, "version": item["version"],
                               "tag": item["tag"], "key": item["key"],
                               "styles": {tag_style(item["tag"], prefix)}}
        else:
            current["styles"].add(tag_style(item["tag"], prefix))
    return sorted(streams.values(), key=lambda s: s["key"], reverse=True)


def stream_of(version: str, streams: list[dict], hint: str | None = None) -> str | None:
    """Le flux auquel appartient une version : indice explicite, puis
    même `majeur.mineur`, puis même majeur, puis le flux le plus récent
    sous cette version. None si aucun flux ne peut la porter."""
    if not streams:
        return None
    prefixes = {s["prefix"] for s in streams}
    if hint is not None and hint in prefixes:
        return hint
    key = version_key(version)
    for depth in (2, 1):
        same = [s for s in streams if s["key"][:depth] == key[:depth]]
        if same:
            return max(same, key=lambda s: s["key"])["prefix"]
    lower = [s for s in streams if s["key"] < key]
    if lower:
        return max(lower, key=lambda s: s["key"])["prefix"]
    return None


def parse_notes(text: str) -> dict:
    """Les notes `docs/versions/x.y.z.md` : `{summary, themes}`.

    Le premier paragraphe (avant le premier `**Thème**`) est le résumé ; les
    puces d'un thème sont ses items ; une ligne `**X** : texte` est un thème
    d'un item (`Migrations`) ; une ligne de continuation complète l'item
    précédent."""
    summary_lines: list[str] = []
    themes: list[dict] = []
    current: dict | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        theme = _THEME_RE.match(line)
        if theme:
            name = theme.group(1).strip()
            rest = theme.group(2).strip()
            current = {"theme": name, "items": [rest] if rest else []}
            themes.append(current)
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            item = bullet.group(1).strip()
            if current is None:
                summary_lines.append(item)
            else:
                current["items"].append(item)
            continue
        if current is not None and current["items"]:
            current["items"][-1] = (current["items"][-1] + " " + line).strip()
        else:
            summary_lines.append(line)
    summary = " ".join(summary_lines).strip() or None
    return {"summary": summary,
            "themes": [{"theme": t["theme"], "items": t["items"]} for t in themes
                       if t["items"]]}


# --------------------------------------------------------------------------
# lecture GitHub (lecture seule, cache)
# --------------------------------------------------------------------------

class Reader:
    """Les lectures GitHub de la vue, avec cache `ttl` (partageable).

    `api` : la fonction `chemin -> JSON` ; None : le client d'ameesh
    (`plan_github.client`) — le jeton `GH_TOKEN`/`GITHUB_TOKEN`, sinon `gh`
    en repli local (`allow_gh=False` : jamais `gh`, le serveur). Chaque
    lecture réussie garde son instant (`read_ts`, et `stamp(chemin)` par
    chemin) : un cache servi n'est jamais dit frais."""

    def __init__(self, api=None, *, ttl: float = CACHE_S, clock=time.monotonic,
                 wall=time.time, timeout: float = READ_TIMEOUT,
                 allow_gh: bool = True):
        self._api = api
        self._ttl = float(ttl)
        self._clock = clock
        self._wall = wall
        self._timeout = float(timeout)
        self._allow_gh = bool(allow_gh)
        self._cache: dict = {}
        self._stamps: dict = {}
        self._read_ts: float | None = None
        self._source = "github" if api is not None else plan_github.client_name()

    @property
    def source(self) -> str:
        """Le transport des lectures : `api` (jeton) ou `gh` (repli local)."""
        return self._source

    def read_ts(self) -> float | None:
        """Instant (epoch) de la dernière lecture GitHub réussie."""
        return self._read_ts

    def stamp(self, path: str) -> float | None:
        """L'instant (epoch) où ce chemin a été lu de GitHub, sinon None."""
        return self._stamps.get(path)

    def _call(self, path: str):
        if self._api is None:
            client = plan_github.client(timeout=self._timeout, allow_gh=self._allow_gh)
            if client is None:
                raise plan_github.GithubError(
                    "no GitHub token (%s) and no local gh"
                    % ", ".join(plan_github.TOKEN_VARS))
            self._api = client.get
        return self._api(path)

    def get(self, path: str):
        """Le JSON d'un chemin, gardé `ttl` secondes ; lève GithubError."""
        now = self._clock()
        hit = self._cache.get(path)
        if hit is not None and now - hit[1] < self._ttl:
            return hit[0]
        value = self._call(path)
        stamp = self._wall()
        self._cache[path] = (value, now, stamp)
        self._stamps[path] = stamp
        self._read_ts = stamp if self._read_ts is None else max(self._read_ts, stamp)
        return value

    def _list(self, path: str) -> list[dict]:
        data = self.get(path)
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    def releases(self, repo: str) -> list[dict]:
        return self._list(releases_path(repo))

    def tags(self, repo: str) -> list[dict]:
        return self._list(tags_path(repo))

    def repo_info(self, repo: str) -> dict:
        data = self.get(repo_path(repo))
        return data if isinstance(data, dict) else {}

    def file(self, repo: str, path: str, ref: str) -> str | None:
        """Le contenu d'un fichier à une référence, sinon None (404 inclus)."""
        try:
            data = self.get(file_path(repo, path, ref))
        except plan_github.GithubError:
            return None
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            return None
        try:
            return base64.b64decode(data.get("content") or "").decode("utf-8", "replace")
        except (ValueError, binascii.Error):
            return None

    def commit_date(self, repo: str, sha: str) -> float | None:
        """La date (epoch) d'un commit, sinon None."""
        if not sha:
            return None
        try:
            data = self.get(commit_path(repo, sha))
        except plan_github.GithubError:
            return None
        commit = (data or {}).get("commit") or {}
        stamp = (commit.get("committer") or {}).get("date") \
            or (commit.get("author") or {}).get("date")
        return _iso_ts(stamp)


#: les chemins de l'API, nommés une fois : l'heure de lecture d'une donnée
#: (`Reader.stamp`) se retrouve par le chemin qui l'a lue
def releases_path(repo: str) -> str:
    return "repos/%s/releases?per_page=%d" % (repo, PAGE)


def tags_path(repo: str) -> str:
    return "repos/%s/tags?per_page=%d" % (repo, PAGE)


def repo_path(repo: str) -> str:
    return "repos/%s" % (repo,)


def file_path(repo: str, path: str, ref: str) -> str:
    return "repos/%s/contents/%s?ref=%s" % (repo, path, quote(ref, safe=""))


def commit_path(repo: str, sha: str) -> str:
    return "repos/%s/commits/%s" % (repo, sha)


def github_view(reader: Reader, now: float) -> dict:
    """L'état de la lecture GitHub : sa source, son instant, son âge.

    « données GitHub d'il y a 3 min » : un cache servi garde l'heure de sa
    lecture, jamais celle de l'instantané, donc un cache périmé se voit."""
    stamp = reader.read_ts()
    age = None if stamp is None else max(0.0, float(now) - float(stamp))
    return {"source": reader.source, "read_ts": stamp, "age_s": age,
            "stale": age is not None and age > CACHE_S}


def _iso_ts(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return _dt.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso_day(ts: float | None) -> str | None:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc).date().isoformat()


def _local_day(ts: float | None) -> str | None:
    if ts is None:
        return None
    return _dt.date.fromtimestamp(float(ts)).isoformat()


def _short(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:200]


# --------------------------------------------------------------------------
# collecte
# --------------------------------------------------------------------------

def empty(reason: str | None = None) -> dict:
    """Une vue sans produit, avec sa raison (jamais une erreur)."""
    out = {"schema": SCHEMA, "products": [], "missing": [], "read_ts": None,
           "github": None}
    if reason:
        out["missing"].append(reason)
    return out


def collect(store, db, cfg, *, project: str | None = None, reader: Reader | None = None,
            now: float | None = None, packages: dict | None = None,
            package_items: list | None = None, limit: int = LIMIT) -> dict:
    """La vue `versions` d'un instantané de progression.

    `store` : `storage.of(db)` ; `reader` : le lecteur GitHub (None : rien
    n'est lu, la vue le dit) ; `packages`/`package_items` : les fiches déjà
    lues par l'instantané, pour les thèmes de repli."""
    now = time.time() if now is None else float(now)
    rows = store.work.items(state=None, assignee=None, limit=limit)
    if packages is None:
        packages = {p["id"]: p for p in store.packages()}
    if package_items is None:
        package_items = store.package_items()
    settings = None
    if cfg is not None:
        try:
            settings = plan_github.settings_of(cfg)
        except plan_github.SettingsError:
            settings = None
    out = empty()
    if reader is None:
        out["missing"].append(
            "versions: not read (no GitHub reader; `ameesh progress --no-versions`)")
    for name in _projects(rows, project, settings):
        repo, notes = _repo_of(name, rows, settings)
        if not repo:
            out["missing"] += notes
            out["products"].append({"project": name, "repo": None, "product": name,
                                    "label": name, "prefix": "", "last": None,
                                    "last_tag": None, "next": None,
                                    "missing": list(notes)})
            continue
        if reader is None:
            out["products"].append({"project": name, "repo": repo, "product": name,
                                    "label": name, "prefix": "", "last": None,
                                    "last_tag": None, "next": None,
                                    "missing": ["versions: not read (no GitHub reader)"]})
            continue
        try:
            products = _products(store, db, name, repo, rows, reader, now, packages,
                                 package_items)
        except plan_github.GithubError as exc:
            products = [{"project": name, "repo": repo, "product": name, "label": name,
                         "prefix": "", "last": None, "last_tag": None, "next": None,
                         "missing": ["GitHub unreadable (%s): %s" % (repo, _short(exc))]}]
        out["products"] += products
    if reader is not None:
        out["read_ts"] = reader.read_ts()
        out["github"] = github_view(reader, now)
    return out


def _projects(rows: list[dict], project: str | None, settings) -> list[str]:
    names = list((settings.projects if settings else {}) or {})
    for row in rows:
        name = row.get("app") or row.get("workstream")
        if name:
            names.append(str(name))
    order: list[str] = []
    for name in names:
        if name and name not in order:
            order.append(name)
    if project:
        known = next((n for n in order if n.casefold() == str(project).casefold()), None)
        return [known or str(project)]
    return sorted(order, key=lambda n: (n.casefold(), n))


def _repo_of(name: str, rows: list[dict], settings) -> tuple[str | None, list[str]]:
    repo = settings.repo_for(name) if settings else None
    counts: Counter = Counter()
    for row in rows:
        if (row.get("app") or row.get("workstream")) != name:
            continue
        match = _ISSUE_REF_RE.match((row.get("issue_ref") or "").strip())
        if match:
            counts[match.group(1)] += 1
    if repo:
        return repo, []
    if not counts:
        return None, ["%s: no GitHub repository known (no `issue_ref`, no "
                      "`github.projects` entry)" % name]
    ranked = counts.most_common()
    notes = []
    if len(ranked) > 1:
        notes.append("%s: several repositories cited (%s); only %s is shown"
                     % (name, ", ".join(r for r, _ in ranked), ranked[0][0]))
    return ranked[0][0], notes


def _products(store, db, name: str, repo: str, rows: list[dict], reader: Reader,
              now: float, packages: dict, package_items: list) -> list[dict]:
    """Un produit par flux de tags du dépôt (ameesh ; nexlink desktop ;
    nexlink server/prod)."""
    releases = reader.releases(repo)
    tags = reader.tags(repo)
    known: dict = {}          # version -> {version, key, prefix, tag, release, sha}
    for tag in tags:
        item = parse_tag(str(tag.get("name") or ""))
        if item:
            item["sha"] = ((tag.get("commit") or {}).get("sha")) or None
            known.setdefault(item["version"], {}).update(item)
    for release in releases:
        item = parse_tag(str(release.get("tag_name") or ""))
        if not item:
            continue
        entry = known.setdefault(item["version"], {})
        entry.update({k: item[k] for k in ("version", "key", "prefix", "tag")})
        entry["release"] = release
    if not known:
        return [{"project": name, "repo": repo, "product": name, "label": name,
                 "prefix": "", "last": None, "last_tag": None, "next": None,
                 "missing": ["%s: no version tag or release known" % repo]}]
    streams = streams_of(list(known.values()))

    open_rows = [row for row in rows
                 if row.get("state") not in work_mod.MERGED_STATES
                 and row.get("state") != "closed"
                 and (row.get("app") or row.get("workstream")) == name]
    candidates: dict = {}
    versionless: list[dict] = []
    for row in open_rows:
        if not is_release_lot(row.get("title")):
            continue
        version = version_of(row.get("title"))
        if not version:
            versionless.append(row)
            continue
        entry = candidates.setdefault(version, _candidate(version))
        entry["lots"].append(row)
        hint = release_hint(row.get("title"), {s["prefix"] for s in streams})
        if hint is not None:
            entry["hint"] = hint
        # L160f (mesh-design) : la PR du lot est en base (`sync-github`),
        # l'API ne sert qu'aux tags, aux releases et au fichier de notes
        pull = _pull_of(row)
        if pull and entry.get("pr") is None:
            entry["pr"] = pull
    for entry in known.values():
        release = entry.get("release")
        if release and release.get("draft"):
            candidates.setdefault(entry["version"],
                                  _candidate(entry["version"]))["release"] = release
    if not streams:
        streams = [{"prefix": "", "version": None, "tag": None, "key": None,
                    "styles": {"v"}}]
    for version, entry in candidates.items():
        entry["prefix"] = stream_of(version, streams, entry.get("hint"))

    # la prochaine version de chaque flux : la plus petite au-dessus de son
    # dernier tag ; un lot de release sans version rejoint le flux que son
    # titre désigne, sinon le seul flux qui a une suite — un flux sans
    # version annoncée garde son lot et dit le manque (serveur prod)
    upcoming: dict = {}
    for stream in streams:
        versions = [v for v, entry in candidates.items()
                    if entry["prefix"] == stream["prefix"]
                    and (stream["key"] is None or version_key(v) > stream["key"])]
        upcoming[stream["prefix"]] = sorted(versions, key=version_key)[0] if versions else None
    with_next = [prefix for prefix, version in upcoming.items() if version]
    attached: dict = {}
    leftover = []
    for row in versionless:
        hint = release_hint(row.get("title"), {s["prefix"] for s in streams})
        if hint is not None:
            target = hint
        elif len(with_next) == 1:
            target = with_next[0]
        else:
            target = None
        if target is None:
            leftover.append(row)
        else:
            attached.setdefault(target, []).append(row)

    products = []
    for stream in sorted(streams, key=lambda s: (s["key"] is None, s["key"] or (0, 0, 0)),
                         reverse=True):
        prefix = stream["prefix"]
        label = _label(name, prefix, len(streams) > 1)
        stream_known = [v for v in known.values() if (v.get("prefix") or "") == prefix]
        stream_published = [v for v in stream_known
                            if v.get("release") and not v["release"].get("draft")]
        last = max(stream_published, key=lambda v: v["key"]) if stream_published else None
        newest = max(stream_known, key=lambda v: v["key"]) if stream_known else None
        last_view = _last_view(reader, repo, last, prefix)
        last_tag = _last_tag_view(reader, repo, newest, last, prefix)
        product = {"project": name, "repo": repo, "product": label, "label": label,
                   "prefix": prefix, "last": last_view, "last_tag": last_tag,
                   "next": None, "missing": []}
        version = upcoming.get(prefix)
        if version:
            entry = candidates[version]
            entry["lots"] = entry["lots"] + attached.get(prefix, [])
            anchor = _anchor(known, prefix)
            product["next"] = _next_view(store, db, name, repo, version, entry, reader,
                                         now, packages, package_items, anchor,
                                         stream["styles"], rows)
        elif attached.get(prefix):
            product["next"] = _unknown_next_view(store, db, attached[prefix], now)
        else:
            product["missing"].append(
                "%s: no next release announced (no open release lot, no draft "
                "release)" % label)
        if last_tag and last_tag["version"] != version:
            product["missing"].append(
                "%s: tag %s has no published release" % (label, last_tag["tag"]))
        products.append(product)
    extras = []
    unassigned = sorted((v for v, entry in candidates.items() if entry["prefix"] is None),
                        key=version_key)
    if unassigned:
        extras.append("%s: version(s) announced by a release lot or a draft release "
                      "but in no known version stream: %s" % (name, ", ".join(unassigned)))
    if leftover:
        extras.append("%s: release lot(s) without a version in the title: %s"
                      % (name, ", ".join("#%s" % row.get("id") for row in leftover)))
    if extras:
        if not products:
            products.append({"project": name, "repo": repo, "product": name, "label": name,
                             "prefix": "", "last": None, "last_tag": None, "next": None,
                             "missing": list(extras)})
        else:
            products[0].setdefault("missing", []).extend(extras)
    return products


def _anchor(known: dict, prefix: str) -> dict | None:
    """Le plus récent tag du flux (publié ou non), pour savoir depuis quand
    les lots sont fusionnés."""
    values = [v for v in known.values() if (v.get("prefix") or "") == prefix]
    return max(values, key=lambda v: v["key"]) if values else None


def _candidate(version: str) -> dict:
    return {"version": version, "key": version_key(version), "prefix": None,
            "hint": None, "lots": [], "pr": None, "release": None}


def _pr_parts(ref: str | None) -> tuple[str, str] | None:
    """`owner/repo#12` d'un `pr_ref` de lot, sinon None."""
    match = _ISSUE_REF_RE.match((ref or "").strip())
    return (match.group(1), match.group(2)) if match else None


def _pull_of(row: dict) -> dict | None:
    """La PR d'un lot, lue en base (`sync-github`) : jamais par l'API."""
    parts = _pr_parts(row.get("pr_ref"))
    if not parts:
        return None
    repo, number = parts
    return {"number": int(number), "title": row.get("title"), "head": None,
            "url": "https://github.com/%s/pull/%s" % (repo, number),
            "source": "ameesh work items (sync-github)"}


def _pull_url(ref: str | None) -> str | None:
    parts = _pr_parts(ref)
    return "https://github.com/%s/pull/%s" % parts if parts else None


def _label(project: str, prefix: str, multi: bool) -> str:
    if prefix:
        return "%s %s" % (project, prefix)
    return "%s server/prod" % project if multi else project


def _last_view(reader: Reader, repo: str, last, prefix: str) -> dict | None:
    if last is None or (last.get("prefix") or "") != prefix:
        return None
    release = last.get("release") or {}
    stamp = _iso_ts(release.get("published_at"))
    return {"version": last["version"], "tag": last["tag"], "published_ts": stamp,
            "date": _iso_day(stamp), "url": release.get("html_url"),
            "prerelease": bool(release.get("prerelease")),
            "source": "github", "read_ts": reader.stamp(releases_path(repo))}


def _last_tag_view(reader: Reader, repo: str, newest, last, prefix: str) -> dict | None:
    """Le tag le plus récent du flux quand il dépasse la dernière release
    publiée (tag sans release, brouillon) : un manque, jamais caché."""
    if newest is None or (newest.get("prefix") or "") != prefix:
        return None
    if last is not None and (last.get("prefix") or "") == prefix \
            and newest["key"] <= last["key"]:
        return None
    release = newest.get("release") or {}
    stamp = _iso_ts(release.get("published_at")) or _iso_ts(release.get("created_at"))
    if stamp is None:
        stamp = reader.commit_date(repo, newest.get("sha") or "")
    return {"version": newest["version"], "tag": newest["tag"], "date": _iso_day(stamp),
            "published_ts": stamp, "draft": bool(release.get("draft")),
            "url": release.get("html_url"), "source": "github",
            "read_ts": reader.stamp(releases_path(repo))
            or reader.stamp(tags_path(repo))}


def _next_view(store, db, name: str, repo: str, version: str, entry: dict,
               reader: Reader, now: float, packages: dict, package_items: list,
               anchor: dict | None, styles, rows: list[dict]) -> dict:
    train = entry["lots"]
    lots, date, source, label, state = _train(store, db, train, now)
    release = entry.get("release")
    pull = entry.get("pr")
    if state is None and release and release.get("draft"):
        state = "draft"
    if state is None and pull:
        state = "pull request"
    content = _content(reader, repo, version, pull, release, rows, name,
                       _anchor_ts(reader, repo, anchor), (anchor or {}).get("tag"),
                       packages, package_items, now)
    out = {"version": version, "tag": tag_for(entry.get("prefix") or "", version, styles),
           "state": state, "date": date, "date_source": source, "date_label": label,
           "lots": lots, "release": _release_view(reader, repo, release),
           "pr": _pull_view(pull, now),
           "notes": content["notes"], "summary": content["summary"],
           "themes": content["themes"], "merged_lots": content["merged_lots"],
           "merged_since": content["merged_since"], "missing": []}
    if date is None:
        out["missing"].append(
            "release %s has no planned date (no plan, no p80 estimate)" % version)
    return out


def _unknown_next_view(store, db, rows: list[dict], now: float) -> dict:
    """Une prochaine release annoncée par un lot sans version dans le titre :
    le lot, sa date et son état sont dits, le numéro manque (jamais deviné),
    et le contenu n'est pas inventé."""
    lots, date, source, label, state = _train(store, db, rows, now)
    out = {"version": None, "tag": None, "state": state, "date": date,
           "date_source": source, "date_label": label, "lots": lots,
           "release": None, "pr": None, "notes": None, "summary": None,
           "themes": [], "merged_lots": [], "merged_since": None,
           "missing": ["the release version is not announced (no version in the "
                       "release lot title)"]}
    if date is None:
        out["missing"].append(
            "release has no planned date (no plan, no p80 estimate)")
    return out


def _train(store, db, rows: list[dict], now: float) -> tuple:
    """Les lots du train : leurs dates (plan, sinon p80), leur état d'ensemble
    et la date prévue de la release (la plus tardive)."""
    plans = {int(p["id"]): p for p in store.roadmap.item_plans(
        [int(row["id"]) for row in rows])}
    ref = estimates_mod.current_reference(db)
    body = ref["body"] if ref else None
    served = _p80(rows, body, db, now)
    lots = [_lot_view(row, plans.get(int(row["id"])), served.get(int(row["id"])), now)
            for row in rows]
    lots.sort(key=lambda l: (l["date"] is None, l["date"] or "", l["id"]))
    date, source, label = _train_date(lots)
    state = next((s for s in STATE_ORDER if s in {l["state"] for l in lots}), None)
    return lots, date, source, label, state


def _anchor_ts(reader: Reader, repo: str, anchor: dict | None) -> float | None:
    """L'instant du dernier tag du flux : date de sa release, sinon de son
    commit, sinon None (le repli « fusionnés depuis » ne devine pas)."""
    if anchor is None:
        return None
    release = anchor.get("release") or {}
    stamp = _iso_ts(release.get("published_at")) or _iso_ts(release.get("created_at"))
    if stamp is None:
        stamp = reader.commit_date(repo, anchor.get("sha") or "")
    return stamp


def _release_view(reader: Reader, repo: str, release: dict | None) -> dict | None:
    if not release:
        return None
    return {"tag": release.get("tag_name"), "draft": bool(release.get("draft")),
            "created_ts": _iso_ts(release.get("created_at")),
            "published_ts": _iso_ts(release.get("published_at")),
            "url": release.get("html_url"), "source": "github",
            "read_ts": reader.stamp(releases_path(repo))}


def _pull_view(pull: dict | None, now: float) -> dict | None:
    if not pull:
        return None
    return {"number": pull.get("number"), "title": pull.get("title"),
            "head": ((pull.get("head") or {}).get("ref")), "url": pull.get("html_url"),
            "source": pull.get("source") or "ameesh work items (sync-github)",
            "read_ts": now}


def _lot_view(row: dict, plan: dict | None, p80: dict | None, now: float) -> dict:
    plan = plan or {}
    date = source = label = None
    for key, text in (("delivery", "delivery"), ("end", "end"), ("start", "start")):
        value = plan.get("planned_%s" % key)
        if value:
            date, source = str(value)[:10], "plan"
            label = "%s %s (#%s%s)" % (text, date, row.get("id"),
                                       ", %s" % plan["planned_source"]
                                       if plan.get("planned_source") else "")
            break
    if date is None and p80:
        date, source = p80["date"], "p80"
        label = "p80 %s (#%s)" % (estimates_mod.label(p80["minutes"]), row.get("id"))
    out = {"id": int(row["id"]), "title": row.get("title"), "state": row.get("state"),
           "assignee": row.get("assignee"), "date": date, "date_source": source,
           "date_label": label, "url": _issue_url(row.get("issue_ref")),
           "pr": row.get("pr_ref") or None, "pr_url": _pull_url(row.get("pr_ref")),
           "source": "ameesh work items", "read_ts": now}
    if plan:
        out["planned"] = {k: plan.get(k) for k in
                          ("planned_start", "planned_end", "planned_delivery",
                           "planned_source", "planned_by")}
    return out


def _train_date(lots: list[dict]) -> tuple[str | None, str | None, str | None]:
    """La date prévue du train : la plus tardive de ses lots (la release
    sort quand le dernier est livré)."""
    dated = [l for l in lots if l["date"]]
    if not dated:
        return None, None, None
    last = max(dated, key=lambda l: l["date"])
    return last["date"], last["date_source"], last["date_label"]


def _p80(rows: list[dict], body: dict | None, db, now: float) -> dict:
    """`{id: {date, minutes, basis}}` au p80 de la table L157, pour les lots
    sans date prévue ; sans table ni estimation, le lot est absent."""
    out: dict = {}
    for row in rows:
        agent = {}
        if row.get("assignee"):
            try:
                agent = registry.get(db, row["assignee"]) or {}
            except Exception:  # noqa: BLE001 - registre illisible : groupe plus large
                agent = {}
        duration = lot_schedule.duration(row, body, agent)
        if not duration:
            continue
        minutes = float(duration["p80_minutes"])
        started = row.get("started_ts")
        end = max(now, float(started) + minutes * 60.0) if started \
            else now + minutes * 60.0
        out[int(row["id"])] = {"date": _local_day(end), "minutes": minutes,
                               "basis": duration.get("basis")}
    return out


def _content(reader: Reader, repo: str, version: str, pull, release, rows: list[dict],
             name: str, anchor_ts: float | None, anchor_tag: str | None,
             packages: dict, package_items: list, now: float) -> dict:
    """Le contenu de la prochaine version : les notes brouillon du dépôt
    quand la branche de la PR de release en a, sinon les lots fusionnés
    depuis le dernier tag groupés par thème (leur epic, sinon un seul
    groupe dit)."""
    path = "%s/%s.md" % (NOTES_DIR, version)
    notes = _notes(reader, repo, path, version, release)
    if notes is not None:
        parsed = parse_notes(notes["text"])
        return {"notes": {"path": path, "source": notes["source"],
                          "origin": "github", "read_ts": notes["read_ts"]},
                "summary": parsed["summary"], "themes": parsed["themes"],
                "merged_lots": [], "merged_since": None}
    merged = _merged_since(rows, name, anchor_ts, packages, package_items, now)
    grouped: dict = {}
    for row in merged:
        package = packages.get(row.get("package_id")) or {}
        theme = package.get("title") or "Merged since the last tag"
        grouped.setdefault(theme, []).append(row)
    ordered = sorted(grouped.items())
    return {"notes": None, "summary": None,
            "themes": [{"theme": theme, "items": [row["title"] for row in items]}
                       for theme, items in ordered],
            "merged_lots": [row for _, items in ordered for row in items],
            "merged_since": anchor_tag if merged else None}


def _notes(reader: Reader, repo: str, path: str, version: str, release) -> dict | None:
    """Les notes de version : la branche de la PR de release (`release/vX`,
    `release/X`), sinon le tag, sinon la branche par défaut du dépôt (jamais
    devinées) ; la source dite est la référence réellement lue."""
    refs = []
    if version:
        refs += [("release/v%s" % version, "release/v%s" % version),
                 ("release/%s" % version, "release/%s" % version)]
    if release and release.get("tag_name"):
        refs.append(("tag %s" % release["tag_name"], release["tag_name"]))
    try:
        default = reader.repo_info(repo).get("default_branch") or "main"
    except plan_github.GithubError:
        default = "main"
    refs.append((default, default))
    for source, ref in refs:
        text = reader.file(repo, path, ref)
        if text:
            return {"text": text, "source": source,
                    "read_ts": reader.stamp(file_path(repo, path, ref))}
    return None


def _merged_since(rows: list[dict], name: str, anchor_ts: float | None,
                  packages: dict, package_items: list, now: float) -> list[dict]:
    """Les lots fusionnés depuis le dernier tag (horodatage de leur dernière
    modification : la fusion, en pratique) et leur PR, lus en base
    (`sync-github`), pour le repli sans notes."""
    if anchor_ts is None:
        return []
    by_item = {int(item["id"]): item.get("package_id") for item in package_items}
    out = []
    for row in rows:
        if row.get("state") not in work_mod.MERGED_STATES:
            continue
        if (row.get("app") or row.get("workstream")) != name:
            continue
        if float(row.get("updated_ts") or 0) <= anchor_ts:
            continue
        out.append({"id": int(row["id"]), "title": row.get("title"),
                    "package_id": by_item.get(int(row["id"])) or row.get("package_id"),
                    "pr": row.get("pr_ref") or None,
                    "pr_url": _pull_url(row.get("pr_ref")),
                    "source": "ameesh work items (sync-github)", "read_ts": now})
    out.sort(key=lambda item: int(item["id"]))
    return out


def _issue_url(issue_ref: str | None) -> str | None:
    match = _ISSUE_REF_RE.match((issue_ref or "").strip())
    if not match:
        return None
    return "https://github.com/%s/issues/%s" % (match.group(1), match.group(2))


# --------------------------------------------------------------------------
# rendu texte (la page a son propre rendu, sur le même modèle)
# --------------------------------------------------------------------------

def format_lines(view: dict, width: int = 80) -> list[str]:
    """La section texte de la vue : dernière publiée, prochaine release,
    lots, thèmes et manques de chaque produit, et la fraîcheur de la lecture
    GitHub (« données GitHub d'il y a 3 min »)."""
    out = ["VERSIONS"]
    gh = view.get("github") or {}
    if gh.get("read_ts"):
        out.append("  GitHub (%s): read %s%s"
                   % (gh.get("source") or "github", _ago(gh.get("age_s")),
                      " — stale cache" if gh.get("stale") else ""))
    products = view.get("products") or []
    if not products:
        out.append("  " + ("; ".join(view.get("missing") or []) or "none"))
        return _wrap(out, width)
    for product in products:
        out.append("  %s (%s)" % (product.get("label") or product.get("product") or "?",
                                  product.get("repo") or "no repository"))
        last = product.get("last")
        out.append("    published: %s" % (
            "%s — %s" % (last.get("tag"), last.get("date") or "date unknown")
            if last else "none"))
        tag = product.get("last_tag")
        if tag:
            out.append("    tag %s — %s%s" % (
                tag.get("tag"), tag.get("date") or "date unknown",
                " (draft release)" if tag.get("draft") else " (no published release)"))
        nxt = product.get("next")
        if not nxt:
            out.append("    next: none announced")
        else:
            out.append("    next %s: %s — %s%s" % (
                nxt.get("version"), nxt.get("state") or "?",
                nxt.get("date") or "no planned date",
                " (%s)" % nxt["date_label"] if nxt.get("date_label") else ""))
            for lot in (nxt.get("lots") or [])[:10]:
                out.append("      #%s %s — %s" % (
                    lot["id"], lot.get("state") or "?",
                    _clip(lot.get("title"), max(20, width - 16))))
            if nxt.get("notes"):
                out.append("      notes: %s (%s)" % (nxt["notes"]["path"],
                                                     nxt["notes"]["source"]))
            themes = nxt.get("themes") or []
            for theme in themes[:6]:
                out.append("      %s: %s" % (theme["theme"],
                                             _clip(" · ".join(theme["items"]),
                                                   max(20, width - 16))))
            if len(themes) > 6:
                out.append("      … %d more themes" % (len(themes) - 6))
        for miss in product.get("missing") or []:
            out.append("    missing: %s" % miss)
    for miss in view.get("missing") or []:
        out.append("  missing: %s" % miss)
    return _wrap(out, width)


def _clip(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:max(1, limit - 1)].rstrip() + "…"


def _ago(seconds) -> str:
    """Un âge lisible : `just now`, `3 min ago`, `1 h 05 min ago`."""
    if seconds is None:
        return "not read"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "just now" if seconds < 5 else "%d s ago" % seconds
    minutes = seconds // 60
    if minutes < 60:
        return "%d min ago" % minutes
    hours, minutes = divmod(minutes, 60)
    return "%d h %02d min ago" % (hours, minutes) if minutes else "%d h ago" % hours


def _wrap(lines: list[str], width: int) -> list[str]:
    out = []
    for line in lines:
        indent = line[:len(line) - len(line.lstrip())]
        out.extend(textwrap.wrap(line.strip(), max(24, int(width)), initial_indent=indent,
                                 subsequent_indent=indent + "  ") or [indent])
    return out
