# SPDX-License-Identifier: AGPL-3.0-only
"""#3264 A / #4291 — veille quotidienne des versions de harnais et de modèles.

Une fois par jour, ameesh lit les versions **installées** (par hôte, dans
`harness_versions`), les versions **publiées** (registre npm — `dist-tags` et
`time` — ou releases GitHub), les **modèles servis** par le fournisseur et
l'identifiant réellement facturé (`turn_costs.model`), puis entretient **un**
lot d'évaluation par harnais.

Règles de la spec `docs/design/3264-veille-versions.md` §2 :

* seules les versions stables et rc sont surveillées ; alpha, beta et les
  autres pre-releases sont ignorées ;
* une version n'est prise en compte que si elle est **plus récente** que la
  version installée la plus récente du harnais (tous hôtes confondus) ; un
  écart entre hôtes est écrit dans le lot ;
* un seul lot d'évaluation est ouvert par harnais : une version plus récente
  remplace l'objet du lot ouvert, sans en créer un second ;
* une recommandation explicite du fournisseur (« version X or later ») donne
  la priorité 1 au lot ; sinon priorité 2 ;
* un objet déjà évalué (lot fusionné, promu ou clos) n'est pas rouvert.

Aucune source propre à un client n'est codée ici : le paquet npm, le dépôt,
le changelog et le fournisseur viennent de la configuration du mesh
(`veille.sources`, canon §7). Exemple neutre ::

    "veille": {"sources": [
        {"harness": "deepseek", "npm": "@org/dsh",
         "changelog": "https://example.invalid/dsh/changelog",
         "provider": "deepseek"}
    ]}

`settings` valide ce bloc ; `scan` lit, décide et réconcilie les lots ;
`ameesh veille scan` est le geste quotidien (minuterie
`ameesh-veille.timer`). La lecture réseau est injectable (`http_get`,
`github_api`, `listing`) : les tests ne touchent jamais le réseau.
"""
from __future__ import annotations

import contextlib
import dataclasses
import importlib
import json
import os
import re
import time
import urllib.parse
import urllib.request

from . import harness_versions, storage, work

#: schéma du rapport de `scan` (`ameesh veille scan --json`)
SCHEMA = "ameesh-veille/1"
#: préfixe de la `source` d'un lot d'évaluation : `veille:<harnais>`
SOURCE_PREFIX = "veille:"
#: estimation par défaut d'un lot d'évaluation (rejeu comparatif, §3)
DEFAULT_ESTIMATE = "6h"
#: fenêtre de lecture des modèles facturés, en jours
DEFAULT_MODEL_DAYS = 30.0
#: sources de modèles connues (découverte `ameesh.discovery`, L14)
MODEL_SOURCES = ("anthropic", "openai", "deepseek")
#: registre npm public (les tests injectent `http_get`)
NPM_REGISTRY = "https://registry.npmjs.org"
#: délai d'une lecture HTTP (npm, changelog)
HTTP_TIMEOUT_S = 10.0
#: taille maximale d'une réponse lue (un changelog, un document npm)
HTTP_MAX_BYTES = 8_000_000
#: lots terminaux : un objet qui y figure a déjà été évalué
COMPLETED = tuple(work.COMPLETED)

#: clés connues du bloc `veille` et d'une entrée de `veille.sources`
SETTINGS_KEYS = ("sources", "app", "estimate", "host", "model_days")
SOURCE_KEYS = ("harness", "npm", "releases", "changelog", "provider", "label")

#: identifiant de harnais (même forme que les descripteurs, L16)
_HARNESS_RE = re.compile(r"^[a-z][a-z0-9-]*$")
#: paquet npm, y compris une portée `@org/paquet`
_NPM_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$", re.I)
#: dépôt GitHub `owner/name`
_RELEASES_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
#: version semver, pré-release comprise, métadonnées de construction tolérées
_VERSION_RE = re.compile(
    r"^v?(\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.-]+)?)(?:\+[0-9A-Za-z.-]+)?$")
#: pré-release rc : `rc`, `rc1`, `rc-1`
_RC_RE = re.compile(r"^rc(?:[.-]?\d+)?$", re.I)
#: recommandation explicite « X or later » / « X ou ultérieure »
_OR_LATER_RE = re.compile(
    r"(?<![\w.])(?:version\s+|v)?(\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.-]+)?)"
    r"\s+(?:or\s+(?:later|newer|above)|ou\s+(?:ultérieure|supérieure|plus\s+récente))",
    re.I)
#: recommandation « recommends/requires … X »
_RECOMMEND_RE = re.compile(
    r"(?:recommend(?:s|ed|ation)?|requires?|exige|recommande|préconise|preconise)"
    r"[^.\n]{0,160}?(?:version\s+|v)?(\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.-]+)?)",
    re.I)
#: radicaux qui portent une recommandation de modèle
_RECOMMEND_WORDS = ("recommend", "require", "exig", "recommand", "préconis",
                    "preconis")
#: marque machine du lot : objet et version évalués
_MARKER_RE = re.compile(
    r"<!--\s*veille:\s*harness=(\S+)\s+object=(harness|model):(\S+)\s+"
    r"version=(\S+)\s*-->")


class VeilleError(ValueError):
    """Configuration ou lecture refusée : aucune conclusion n'est tirée."""


@dataclasses.dataclass(frozen=True)
class Source:
    """Une entrée de `veille.sources` : un harnais et ses sources de versions."""

    harness: str
    npm: str = ""
    releases: str = ""
    changelog: str = ""
    provider: str = ""
    label: str = ""

    @property
    def name(self) -> str:
        return self.label or self.harness

    @classmethod
    def from_config(cls, entry, where: str) -> "Source":
        if not isinstance(entry, dict):
            raise VeilleError("%s: an object is required" % where)
        unknown = set(entry) - set(SOURCE_KEYS)
        if unknown:
            raise VeilleError("%s: unknown key(s) %s"
                              % (where, ", ".join(sorted(unknown))))
        harness = str(entry.get("harness") or "").strip()
        if not harness or not _HARNESS_RE.match(harness):
            raise VeilleError("%s.harness: a lowercase harness id is required" % where)
        npm = str(entry.get("npm") or "").strip()
        releases = str(entry.get("releases") or "").strip()
        if npm and releases:
            raise VeilleError("%s: declare `npm` or `releases`, not both" % where)
        if not npm and not releases:
            raise VeilleError("%s: `npm` or `releases` is required" % where)
        if npm and not _NPM_RE.match(npm):
            raise VeilleError("%s.npm: a package name is required (no URL)" % where)
        if releases and not _RELEASES_RE.match(releases):
            raise VeilleError("%s.releases: `owner/repo` is required" % where)
        changelog = str(entry.get("changelog") or "").strip()
        if changelog:
            parts = urllib.parse.urlsplit(changelog)
            if parts.scheme not in ("http", "https") or not parts.netloc \
                    or any(ch.isspace() for ch in changelog):
                raise VeilleError("%s.changelog: an http(s) URL is required" % where)
        provider = str(entry.get("provider") or "").strip()
        if provider and provider not in MODEL_SOURCES:
            raise VeilleError("%s.provider: one of %s is required"
                              % (where, ", ".join(MODEL_SOURCES)))
        label = str(entry.get("label") or "").strip()
        return cls(harness=harness, npm=npm, releases=releases,
                   changelog=changelog, provider=provider, label=label)


@dataclasses.dataclass(frozen=True)
class Settings:
    """Le bloc `veille` validé de la configuration du mesh."""

    sources: tuple[Source, ...] = ()
    app: str = ""
    estimate: str = DEFAULT_ESTIMATE
    host: str = ""
    model_days: float = DEFAULT_MODEL_DAYS


def settings(cfg) -> Settings:
    """Valide et rend la clé `veille` de la configuration, ou un bloc vide.

    Un bloc absent ou vide = aucune veille. Les clés inconnues sont refusées
    (comme `packet_watch`) : une faute de frappe ne doit pas éteindre la
    surveillance en silence."""
    block = getattr(cfg, "veille", None)
    if block in (None, {}):
        return Settings()
    if not isinstance(block, dict):
        raise VeilleError("veille: an object is required")
    unknown = set(block) - set(SETTINGS_KEYS)
    if unknown:
        raise VeilleError("veille: unknown key(s) %s" % ", ".join(sorted(unknown)))
    declared = block.get("sources")
    if declared is None:
        declared = []
    if not isinstance(declared, list):
        raise VeilleError("veille.sources: a list is required")
    sources: list[Source] = []
    seen: set[str] = set()
    for index, entry in enumerate(declared):
        source = Source.from_config(entry, "veille.sources[%d]" % index)
        if source.harness in seen:
            raise VeilleError("veille.sources: harness %r declared twice"
                              % source.harness)
        seen.add(source.harness)
        sources.append(source)
    app = str(block.get("app") or "").strip()
    host = str(block.get("host") or "").strip()
    estimate = str(block.get("estimate") or DEFAULT_ESTIMATE).strip()
    try:
        from . import estimates
        estimates.parse(estimate)
    except (TypeError, ValueError, estimates.EstimateError) as exc:
        raise VeilleError("veille.estimate: a duration is required (%s)" % exc) from None
    try:
        model_days = float(block.get("model_days", DEFAULT_MODEL_DAYS))
    except (TypeError, ValueError) as exc:
        raise VeilleError("veille.model_days: a number is required") from exc
    if not (model_days > 0):
        raise VeilleError("veille.model_days: > 0 is required")
    return Settings(sources=tuple(sources), app=app, estimate=estimate, host=host,
                    model_days=model_days)


# --------------------------------------------------------------------------
# versions : ce qui est publié, ce qui est surveillé
# --------------------------------------------------------------------------

def clean_version(value) -> str | None:
    """La version semver contenue dans `value`, ou None (tag `v1.2.3` accepté)."""
    match = _VERSION_RE.match(str(value or "").strip())
    return match.group(1) if match else None


def is_rc(version: str | None) -> bool:
    """Vrai si `version` porte une pré-release `rc` (et non alpha/beta)."""
    cleaned = clean_version(version)
    if cleaned is None:
        return False
    _base, sep, pre = cleaned.partition("-")
    return bool(sep and _RC_RE.match(pre.split(".")[0]))


def watched(version: str | None) -> bool:
    """Vrai si la version est stable ou rc : seules celles-là sont surveillées."""
    cleaned = clean_version(version)
    if cleaned is None:
        return False
    _base, sep, pre = cleaned.partition("-")
    return not sep or bool(_RC_RE.match(pre.split(".")[0]))


def kind_of(version: str) -> str:
    return "rc" if is_rc(version) else "stable"


@dataclasses.dataclass(frozen=True)
class Release:
    """Une version publiée, stable ou rc."""

    version: str
    date: str = ""
    source: str = ""
    url: str = ""
    tag: str = ""

    @property
    def kind(self) -> str:
        return kind_of(self.version)

    def as_dict(self) -> dict:
        return {"version": self.version, "date": self.date, "kind": self.kind,
                "source": self.source, "url": self.url, "tag": self.tag}


def _sorted_releases(releases) -> list[Release]:
    return sorted(releases, key=lambda release: harness_versions.version_key(release.version))


def _date_text(value) -> str:
    """Une date ISO courte (AAAA-MM-JJ), ou le texte tel quel s'il est court."""
    text = str(value or "").strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    return text


def parse_npm(payload) -> tuple[list[Release], list[str]]:
    """Analyse le document d'un paquet npm : `(versions surveillées, ignorées)`.

    Les versions viennent de `time` (le registre les date) et des `dist-tags`
    (`latest`, `rc`…) ; `created`, `modified` et `unpublished` ne sont pas des
    versions. Une alpha/beta est mise dans `ignored`, jamais retenue."""
    if not isinstance(payload, dict):
        raise VeilleError("npm: a JSON object is required")
    times = payload.get("time") if isinstance(payload.get("time"), dict) else {}
    tags = payload.get("dist-tags") if isinstance(payload.get("dist-tags"), dict) else {}
    declared = payload.get("versions") if isinstance(payload.get("versions"), dict) else {}
    raw: list[tuple[str, str, str]] = []
    for name in times:
        if name not in ("created", "modified", "unpublished"):
            raw.append((name, str(times.get(name) or ""), ""))
    for name in declared:
        raw.append((name, str((times.get(name) if isinstance(times.get(name), str)
                               else "") or ""), ""))
    for tag, name in tags.items():
        if isinstance(name, str):
            raw.append((name, str(times.get(name) or ""), str(tag)))
    releases: dict[str, Release] = {}
    ignored: set[str] = set()
    for raw_version, date, tag in raw:
        version = clean_version(raw_version)
        if version is None:
            continue
        if not watched(version):
            ignored.add(version)
            continue
        package = str(payload.get("name") or "")
        url = ("https://www.npmjs.com/package/%s/v/%s" % (package, version)
               if package else "")
        current = releases.get(version)
        # une version vue par un tag garde le tag (utile au rapport) ; une
        # deuxième rencontre ne perd pas la date déjà lue
        releases[version] = Release(
            version=version, date=_date_text(current.date if current else date),
            source="npm", url=url, tag=current.tag if current and current.tag else tag)
    return (_sorted_releases(releases.values()),
            sorted(ignored, key=harness_versions.version_key))


def parse_releases(payload) -> tuple[list[Release], list[str]]:
    """Analyse une réponse `releases` GitHub : `(versions surveillées, ignorées)`."""
    if isinstance(payload, dict):
        data = payload.get("releases")
        if data is None:
            data = payload.get("data")
        payload = data
    if not isinstance(payload, list):
        raise VeilleError("releases: a JSON list is required")
    releases: dict[str, Release] = {}
    ignored: set[str] = set()
    for entry in payload:
        if not isinstance(entry, dict) or entry.get("draft"):
            continue
        version = clean_version(entry.get("tag_name") or entry.get("name"))
        if version is None:
            continue
        if not watched(version):
            ignored.add(version)
            continue
        releases[version] = Release(
            version=version,
            date=_date_text(entry.get("published_at") or entry.get("created_at")),
            source="releases",
            url=str(entry.get("html_url") or ""),
            tag=str(entry.get("tag_name") or ""))
    return (_sorted_releases(releases.values()),
            sorted(ignored, key=harness_versions.version_key))


def _http_get_text(url: str, headers: dict | None = None) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": "ameesh-veille", **(headers or {})})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:  # noqa: S310
        body = response.read(HTTP_MAX_BYTES + 1)
    if len(body) > HTTP_MAX_BYTES:
        raise VeilleError("response too large (over %d bytes)" % HTTP_MAX_BYTES)
    return body.decode("utf-8", "replace")


def _github_releases(repo: str, http_get=None):
    """Les releases d'un dépôt public, avec le jeton de l'hôte s'il existe.

    Sans jeton, la lecture est anonyme : envoyer un `Authorization` vide
    ferait refuser une release publique. Avec jeton, `plan_github.Api` garde
    sa lecture (quota, erreurs nommées) ; le jeton ne sort jamais du client.
    """
    path = "/repos/%s/releases?per_page=100" % repo
    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        url = "https://api.github.com%s" % path
        body = (http_get or _http_get_text)(
            url, {"Accept": "application/vnd.github+json"})
        return json.loads(body)
    from . import plan_github
    return plan_github.Api(token=token).get(path)


def published(source: Source, *, http_get=None, github_api=None) -> dict:
    """Les versions publiées de `source` : `{releases, ignored, source, error}`.

    Une panne (réseau, réponse illisible, quota) est rendue en `error` : la
    veille d'un harnais ne fait jamais tomber les autres, et l'absence de
    preuve n'ouvre aucun lot."""
    out = {"source": "npm" if source.npm else "releases", "releases": [],
           "ignored": [], "error": "", "url": ""}
    try:
        if source.npm:
            url = "%s/%s" % (NPM_REGISTRY,
                             urllib.parse.quote(source.npm, safe="@/"))
            out["url"] = url
            body = (http_get or _http_get_text)(
                url, {"Accept": "application/json"})
            releases, ignored = parse_npm(json.loads(body))
        else:
            path = "/repos/%s/releases?per_page=100" % source.releases
            out["url"] = path
            if github_api is not None:
                payload = github_api.get(path)
            else:
                payload = _github_releases(source.releases, http_get=http_get)
            releases, ignored = parse_releases(payload)
        out["releases"] = [release.as_dict() for release in releases]
        out["ignored"] = ignored
    except Exception as exc:  # noqa: BLE001 - une source ne juge pas les autres
        out["error"] = "%s: %s" % (type(exc).__name__,
                                   " ".join(str(exc).split())[:200])
    return out


# --------------------------------------------------------------------------
# recommandation du fournisseur
# --------------------------------------------------------------------------

def parse_recommendation(text: str, models=()) -> dict:
    """Relève une recommandation explicite dans un changelog.

    Rend `{version, model, text}` : `version` d'une formule « X or later » /
    « X ou ultérieure » (ou d'un verbe recommander/exiger), `model` d'un
    modèle servi cité près d'un de ces verbes. Aucune recommandation n'est
    inventée : les motifs sont explicites."""
    plain = re.sub(r"<[^>]+>", " ", str(text or ""))
    plain = " ".join(plain.split())
    version = None
    for pattern in (_OR_LATER_RE, _RECOMMEND_RE):
        for match in pattern.finditer(plain):
            candidate = clean_version(match.group(1))
            if candidate and watched(candidate):
                version = candidate
                break
        if version:
            break
    model = None
    lowered = plain.lower()
    for name in models or ():
        if not name:
            continue
        position = lowered.find(str(name).lower())
        if position < 0:
            continue
        window = lowered[max(0, position - 240):position + len(str(name)) + 240]
        if any(word in window for word in _RECOMMEND_WORDS):
            model = str(name)
            break
    return {"version": version, "model": model,
            "text": (plain[:400] if version or model else "")}


def recommendation(source: Source, *, http_get=None, models=()) -> dict:
    """La recommandation déclarée par le changelog, ou `{error: …}`."""
    out = {"version": None, "model": None, "text": "", "error": ""}
    if not source.changelog:
        return out
    try:
        text = (http_get or _http_get_text)(source.changelog)
        out.update(parse_recommendation(text, models=models))
    except Exception as exc:  # noqa: BLE001 - une lecture ne juge pas le lot
        out["error"] = "%s: %s" % (type(exc).__name__,
                                   " ".join(str(exc).split())[:200])
    return out


# --------------------------------------------------------------------------
# modèles servis et réellement facturés
# --------------------------------------------------------------------------

def discover_models(source: Source, *, listing=None) -> dict:
    """Les modèles servis par le fournisseur, si la source en déclare un.

    La découverte est celle de L14 (`ameesh.discovery`) : un endpoint de
    liste, jamais une complétion, donc aucun token. `complete` dit si la
    liste est entière ; une source incomplète ne fait pas conclure à un
    nouveau modèle."""
    out = {"source": source.provider, "served": [], "complete": False,
           "detail": "", "error": ""}
    if not source.provider:
        return out
    try:
        call = listing
        if call is None:
            module = importlib.import_module("ameesh.discovery.%s" % source.provider)
            call = module.listing
        result = call()
        out["served"] = sorted({str(model.model_id) for model in result.models
                                if getattr(model, "model_id", None)})
        out["complete"] = bool(getattr(result, "complete", False))
        out["detail"] = str(getattr(result, "detail", "") or "")
    except Exception as exc:  # noqa: BLE001 - pas de clé, pas de réseau : inconnu
        out["error"] = "%s: %s" % (type(exc).__name__,
                                   " ".join(str(exc).split())[:200])
    return out


def billed_models(db, harness: str, days: float, *, now: float | None = None) -> list[str]:
    """Les identifiants réellement facturés pour ce harnais (fenêtre `days`)."""
    now = time.time() if now is None else float(now)
    rows = storage.of(db).turn_costs.models_used(
        harness, now - float(days) * 86400.0)
    return [str(row["model"]) for row in rows if str(row.get("model") or "").strip()]


def installed_versions(db, harness: str, *, overview=None) -> dict:
    """Les versions installées de `harness`, par hôte, et l'écart éventuel.

    Les hôtes qui portent un agent de ce harnais mais n'ont aucun relevé sont
    nommés dans `missing` : un hôte muet n'est pas un hôte à jour."""
    rows = [row for row in storage.of(db).hosts.harness_versions()
            if row.get("harness") == harness]
    hosts = {str(row["host"]): {"version": row.get("version"), "path": row.get("path")}
             for row in rows}
    if overview is None:
        try:
            overview = storage.of(db).agents.overview()
        except Exception:  # noqa: BLE001 - un registre illisible ne juge pas
            overview = []
    expected = {str(row.get("host")) for row in overview
                if row.get("harness") == harness and row.get("host")}
    known = [host["version"] for host in hosts.values() if host["version"]]
    newest = max(known, key=harness_versions.version_key) if known else None
    return {"hosts": hosts, "newest": newest,
            "differs": len({harness_versions.version_key(v) for v in known}) > 1,
            "missing": sorted(expected - set(hosts))}


# --------------------------------------------------------------------------
# décision : quel objet évaluer, à quelle priorité
# --------------------------------------------------------------------------

def _newer(releases, reference: str | None) -> Release | None:
    """La plus récente version publiée strictement plus récente que `reference`."""
    if reference is None:
        return None
    best = None
    for release in releases:
        if harness_versions.version_key(release.version) > \
                harness_versions.version_key(reference):
            if best is None or harness_versions.version_key(release.version) > \
                    harness_versions.version_key(best.version):
                best = release
    return best


def _top_ignored(ignored, limit: int = 8) -> list[str]:
    ordered = sorted({str(item) for item in ignored or ()},
                     key=harness_versions.version_key)
    return ordered[-limit:]


def _body(source: Source, installed: dict, published_row: dict, rec: dict,
          models: dict, billed: list[str], obj: dict, candidate: Release | None,
          priority: int) -> str:
    """Le corps du lot : versions installées par hôte, publiée, dates, liens."""
    marker = ("<!-- veille: harness=%s object=%s:%s version=%s -->"
              % (source.harness, obj["kind"], obj["name"], obj["version"]))
    lines = [marker, "", "# %s" % _title(source, obj), "",
             "Détection automatique de la veille des versions (#3264 A). "
             "Aucune adoption n'est décidée ici : le rejeu comparatif et sa "
             "conclusion sont dans la spec #3264 (§3 et §4).", ""]
    lines.append("## Objet à évaluer")
    if obj["kind"] == "harness":
        lines.append("- Harnais : `%s`" % source.harness)
        if candidate is not None:
            lines.append("- Version publiée : `%s` (%s)" % (
                candidate.version, candidate.date or "date inconnue"))
            lines.append("- Type : %s" % candidate.kind)
            if candidate.url:
                lines.append("- Lien : %s" % candidate.url)
        lines.append("- Source : %s `%s`" % (
            published_row.get("source") or "?",
            source.npm or source.releases))
    else:
        lines.append("- Modèle : `%s`" % obj["name"])
        lines.append("- Servi par le fournisseur `%s`" % (source.provider or "?"))
    if source.changelog:
        lines.append("- Changelog : %s" % source.changelog)
    if published_row.get("error"):
        lines.append("- Source publiée : illisible (%s)"
                     % published_row["error"])
    if rec.get("version"):
        lines.append("- Recommandation du fournisseur : version `%s` ou "
                     "ultérieure%s" % (
                         rec["version"],
                         " — priorité 1" if priority == 1 else ""))
    elif rec.get("model"):
        lines.append("- Recommandation du fournisseur : modèle `%s`%s"
                     % (rec["model"], " — priorité 1" if priority == 1 else ""))
    if rec.get("error"):
        lines.append("- Recommandation : illisible (%s)" % rec["error"])
    lines.append("")

    lines.append("## Versions installées")
    if not installed["hosts"]:
        lines.append("- aucun relevé")
    for host in sorted(installed["hosts"]):
        row = installed["hosts"][host]
        lines.append("- %s : %s" % (host, row.get("version") or "illisible"))
    if installed["differs"]:
        detail = ", ".join("%s=%s" % (host, installed["hosts"][host].get("version") or "?")
                           for host in sorted(installed["hosts"]))
        lines.append("- **Écart entre hôtes : oui** (%s)" % detail)
    else:
        lines.append("- Écart entre hôtes : aucun")
    if installed["missing"]:
        lines.append("- Hôtes avec un agent `%s` mais sans relevé : %s"
                     % (source.harness, ", ".join(installed["missing"])))
    lines.append("")

    lines.append("## Modèles")
    if source.provider:
        served = models.get("served") or []
        lines.append("- Servis par `%s` : %s%s" % (
            source.provider,
            ", ".join("`%s`" % name for name in served) if served else "aucun",
            "" if models.get("complete") else " (liste incomplète)"))
        if models.get("error"):
            lines.append("- Lecture des modèles : %s" % models["error"])
    else:
        lines.append("- Aucun fournisseur de modèles déclaré pour ce harnais.")
    lines.append("- Réellement facturés (%d j) : %s" % (
        int(round(_model_days_hint(models))),
        ", ".join("`%s`" % name for name in billed) if billed else "aucun"))
    never = [name for name in (models.get("served") or []) if name not in set(billed)]
    if never:
        lines.append("- Servis et jamais facturés : %s"
                     % ", ".join("`%s`" % name for name in never))
    lines.append("")

    ignored = _top_ignored(published_row.get("ignored") or ())
    if ignored:
        lines.append("Versions alpha/beta ignorées : %s."
                     % ", ".join("`%s`" % version for version in ignored))
        lines.append("")

    lines.append("## Évaluation attendue")
    lines.append("Rejouer le banc comparatif de la spec #3264 §3 (3 lots livrés, "
                 "léger, normal, sensible), avec la version installée et la "
                 "version évaluée, même modèle, même effort, même plafond. "
                 "Le rapport va en note du lot ; aucune adoption sans "
                 "conclusion (§4). Budget : au plus 3 lots × 2 versions et "
                 "6 relectures (§3.3).")
    return "\n".join(lines) + "\n"


def _model_days_hint(models: dict) -> float:
    value = models.get("days")
    try:
        return float(value) if value is not None else DEFAULT_MODEL_DAYS
    except (TypeError, ValueError):
        return DEFAULT_MODEL_DAYS


def _title(source: Source, obj: dict) -> str:
    if obj["kind"] == "harness":
        return "Évaluer %s %s" % (source.name, obj["version"])
    return "Évaluer le modèle %s (%s)" % (obj["name"], source.name)


def evaluate(source: Source, installed: dict, published_row: dict, rec: dict,
             models: dict, billed: list[str]) -> dict:
    """Décide de l'objet du lot (ou de son absence) et de sa priorité."""
    releases = [Release(version=row["version"], date=row.get("date", ""),
                        source=row.get("source", ""), url=row.get("url", ""),
                        tag=row.get("tag", ""))
                for row in published_row.get("releases") or ()
                if watched(row.get("version"))]
    candidate = _newer(releases, installed.get("newest"))
    # une recommandation ne vaut que pour l'objet publié qu'on peut évaluer :
    # une version future (ou un « Node.js 20 or later ») ne donne pas la
    # priorité 1 à un lot sur une autre version
    rec_version = rec.get("version")
    if rec_version and (
            candidate is None
            or harness_versions.version_key(rec_version) >
            harness_versions.version_key(candidate.version)
            or (installed.get("newest") is not None
                and harness_versions.version_key(rec_version) <=
                harness_versions.version_key(installed["newest"]))):
        rec = dict(rec, version=None)
    billed_set = set(billed)
    served = list(models.get("served") or ())
    new_models = [name for name in served if name not in billed_set]
    rec_model = rec.get("model")
    rec_new_model = rec_model if rec_model in new_models else None
    rec_applies = bool(rec_new_model)
    if rec_version and installed.get("newest") is not None \
            and harness_versions.version_key(installed["newest"]) < \
            harness_versions.version_key(rec_version):
        if candidate is not None and harness_versions.version_key(candidate.version) >= \
                harness_versions.version_key(rec_version):
            rec_applies = True
    priority = 1 if rec_applies else 2
    obj = None
    reason = ""
    if candidate is not None:
        obj = {"kind": "harness", "name": source.harness,
               "version": candidate.version}
        reason = "version publiée %s > installée %s" % (
            candidate.version, installed.get("newest") or "?")
    elif rec_new_model is not None:
        obj = {"kind": "model", "name": rec_new_model, "version": rec_new_model}
        reason = "modèle recommandé et jamais facturé : %s" % rec_new_model
    elif new_models:
        chosen = new_models[-1]
        obj = {"kind": "model", "name": chosen, "version": chosen}
        reason = "modèle servi et jamais facturé : %s" % chosen
    else:
        if installed.get("newest") is None:
            reason = "aucune version installée connue : rien à comparer"
        elif published_row.get("error"):
            reason = "source publiée illisible : %s" % published_row["error"]
        else:
            reason = "à jour (installée %s)" % installed["newest"]
    title = _title(source, obj) if obj else ""
    body = (_body(source, installed, published_row, rec, models, billed, obj,
                  candidate, priority) if obj else "")
    return {"harness": source.harness, "title": title, "body": body,
            "object": obj, "priority": priority, "reason": reason,
            "candidate": candidate.as_dict() if candidate else None,
            "source": source}


# --------------------------------------------------------------------------
# réconciliation des lots
# --------------------------------------------------------------------------

def _marker(body: str) -> dict | None:
    match = _MARKER_RE.search(body or "")
    if not match:
        return None
    return {"harness": match.group(1), "kind": match.group(2),
            "name": match.group(3), "version": match.group(4)}


def _already_evaluated(db, source: Source, obj: dict) -> bool:
    """Un lot terminé porte-t-il déjà cet objet (ou une version plus récente) ?"""
    for row in work.with_source(db, SOURCE_PREFIX + source.harness, limit=100):
        if row.get("state") not in COMPLETED:
            continue
        marker = _marker(row.get("body") or "")
        if not marker or marker["kind"] != obj["kind"]:
            continue
        if marker["name"] != obj["name"]:
            continue
        if obj["kind"] == "model":
            return True
        if harness_versions.version_key(marker["version"]) >= \
                harness_versions.version_key(obj["version"]):
            return True
    return False


@contextlib.contextmanager
def _lock(db, key: str):
    """Verrou consultatif de la réconciliation d'un harnais, tenu par la
    connexion : deux hôtes ne créent pas deux lots pour le même événement."""
    db.query("SELECT pg_advisory_lock(hashtextextended(current_schema() || %s, 0))",
             (key,))
    try:
        yield
    finally:
        try:
            db.query("SELECT pg_advisory_unlock("
                     "hashtextextended(current_schema() || %s, 0))", (key,))
        except Exception:  # noqa: BLE001 - un déverrouillage ne juge pas
            pass


def _update_note(old: dict, new: dict) -> str:
    old_marker = _marker(old.get("body") or "")
    new_marker = _marker(new.get("body") or "")
    if old_marker and new_marker and old_marker["version"] != new_marker["version"]:
        return "veille: %s → %s" % (old_marker["version"], new_marker["version"])
    return "veille: mise à jour de la détection (%s)" % (
        new_marker["version"] if new_marker else "?")


def apply(db, cfg, config: Settings, decisions, *, dry_run: bool = False) -> list[dict]:
    """Crée, met à jour ou laisse en place le lot d'évaluation de chaque harnais."""
    app = config.app or str(getattr(cfg, "project", "") or "")
    actions: list[dict] = []
    for decision in decisions:
        source: Source = decision["source"]
        key = SOURCE_PREFIX + source.harness
        obj = decision.get("object")
        if obj is None:
            actions.append({"harness": source.harness, "action": "none",
                            "lot": None, "reason": decision.get("reason") or ""})
            continue
        if dry_run:
            open_lot = work.open_with_source(db, key)
            if open_lot is not None:
                actions.append({"harness": source.harness, "action": "would-update",
                                "lot": int(open_lot["id"]),
                                "reason": decision["reason"]})
            elif _already_evaluated(db, source, obj):
                actions.append({"harness": source.harness,
                                "action": "already-evaluated", "lot": None,
                                "reason": decision["reason"]})
            else:
                actions.append({"harness": source.harness, "action": "would-create",
                                "lot": None, "reason": decision["reason"]})
            continue
        try:
            with _lock(db, "veille:%s:%s" % (app, source.harness)):
                open_lot = work.open_with_source(db, key)
                if open_lot is not None:
                    current_priority = open_lot.get("priority")
                    if (open_lot.get("title") == decision["title"]
                            and open_lot.get("body") == decision["body"]
                            and (current_priority is not None
                                 and int(current_priority) <= int(decision["priority"]))):
                        actions.append({"harness": source.harness, "action": "unchanged",
                                        "lot": int(open_lot["id"]),
                                        "reason": decision["reason"]})
                        continue
                    updated = work.update_generated(
                        db, int(open_lot["id"]), title=decision["title"],
                        body=decision["body"], priority=int(decision["priority"]),
                        note=_update_note(open_lot, decision), actor=work.SYSTEM_SENDER)
                    if updated is None:
                        actions.append({"harness": source.harness, "action": "error",
                                        "lot": int(open_lot["id"]),
                                        "reason": "lot no longer open"})
                        continue
                    actions.append({"harness": source.harness, "action": "updated",
                                    "lot": int(open_lot["id"]),
                                    "reason": decision["reason"]})
                    continue
                if _already_evaluated(db, source, obj):
                    actions.append({"harness": source.harness,
                                    "action": "already-evaluated", "lot": None,
                                    "reason": decision["reason"]})
                    continue
                item = work.add(
                    db, title=decision["title"], type="evolution", app=app,
                    source=key, body=decision["body"],
                    priority=int(decision["priority"]), estimate=config.estimate,
                    estimate_source="veille des versions (#3264 A)",
                    actor=work.SYSTEM_SENDER)
                actions.append({"harness": source.harness, "action": "created",
                                "lot": int(item["id"]),
                                "reason": decision["reason"]})
        except Exception as exc:  # noqa: BLE001 - un harnais ne bloque pas les autres
            actions.append({"harness": source.harness, "action": "error", "lot": None,
                            "reason": "%s: %s" % (type(exc).__name__,
                                                  " ".join(str(exc).split())[:200])})
    return actions


# --------------------------------------------------------------------------
# le passage quotidien
# --------------------------------------------------------------------------

def probe_local(cfg, db, sources) -> list[dict]:
    """Lit la version de chaque harnais surveillé **sur cet hôte** et l'enregistre."""
    from . import adapters
    adapters.configure(cfg)
    found: list[dict] = []
    for source in sources:
        try:
            found.extend(harness_versions.probe_and_record(
                db, cfg.host, [source.harness]))
        except Exception as exc:  # noqa: BLE001 - une sonde ne juge pas les autres
            found.append({"harness": source.harness, "path": None, "version": None,
                          "raw": None,
                          "error": "%s: %s" % (type(exc).__name__,
                                               " ".join(str(exc).split())[:200])})
    return found


def scan(cfg, db, *, sources=None, dry_run: bool = False, probe: bool = True,
         http_get=None, github_api=None, listing=None, now: float | None = None) -> dict:
    """Le passage quotidien : lit, décide, puis réconcilie les lots.

    `sources` borne la lecture à ces identifiants de harnais (commande
    `--harness`) ; `probe=False` n'écrit rien dans `harness_versions` (tests,
    lecture seule) ; `dry_run` n'écrit aucun lot."""
    config = settings(cfg)
    report = {"schema": SCHEMA, "host": getattr(cfg, "host", ""),
              "enabled": bool(config.sources), "reconciled": False,
              "sources": [], "actions": [], "errors": []}
    if not config.sources:
        return report
    wanted = {str(name) for name in sources or ()}
    selected = [source for source in config.sources
                if not wanted or source.harness in wanted]
    unknown = sorted(wanted - {source.harness for source in config.sources})
    if unknown:
        report["errors"].append("harness not declared in veille.sources: %s"
                                % ", ".join(unknown))
    if probe:
        report["probed"] = probe_local(cfg, db, selected)
    if config.host and config.host != getattr(cfg, "host", ""):
        report["reconciled"] = False
        report["reason"] = "reconciliation is assigned to %s" % config.host
        return report
    report["reconciled"] = True
    try:
        overview = storage.of(db).agents.overview()
    except Exception:  # noqa: BLE001 - l'écart sera dit « inconnu »
        overview = []
    decisions = []
    for source in selected:
        entry = {"harness": source.harness, "published": {}, "installed": {},
                 "recommendation": {}, "models": {}, "billed": [],
                 "decision": {}, "error": ""}
        try:
            installed = installed_versions(db, source.harness, overview=overview)
            published_row = published(source, http_get=http_get,
                                      github_api=github_api)
            billed = billed_models(db, source.harness, config.model_days, now=now)
            models = discover_models(source, listing=listing)
            models["days"] = config.model_days
            rec = recommendation(source, http_get=http_get,
                                 models=models.get("served") or ())
            decision = evaluate(source, installed, published_row, rec, models,
                                billed)
            entry.update({"published": published_row, "installed": installed,
                          "recommendation": rec, "models": models,
                          "billed": billed,
                          "decision": {key: value for key, value in decision.items()
                                       if key not in ("body", "source")}})
            if published_row.get("error"):
                entry["error"] = published_row["error"]
                report["errors"].append("%s: %s" % (source.harness,
                                                    published_row["error"]))
            if rec.get("error"):
                report["errors"].append("%s: changelog: %s"
                                        % (source.harness, rec["error"]))
            decisions.append(decision)
        except Exception as exc:  # noqa: BLE001 - un harnais ne bloque pas les autres
            entry["error"] = "%s: %s" % (type(exc).__name__,
                                         " ".join(str(exc).split())[:200])
            report["errors"].append("%s: %s" % (source.harness, entry["error"]))
        report["sources"].append(entry)
    report["actions"] = apply(db, cfg, config, decisions, dry_run=dry_run)
    return report
