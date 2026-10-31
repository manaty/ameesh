# SPDX-License-Identifier: AGPL-3.0-only
"""Le plan de travail et GitHub (lot L29) : fermeture sur fusion, projection.

ameesh reste la **source de vérité** (décision 0005) : le plan vient du canon
(fiches `WorkPackage`, table `work_packages`), l'état d'exécution des lots de
`work_items`. GitHub n'est qu'une **vue** ; ce qui y est modifié n'est jamais
réimporté comme état — au plus signalé comme **proposition**.

`ameesh work sync-github --repo R` (lecture seule de GitHub)
    lit les PR fusionnées (`gh pr list --state merged`) et ferme les lots
    qu'elles référencent : branche `lot/<id>` ou `lot/<id>-…` (identifiant
    de fiche ; la plus longue correspondance l'emporte ; L118b : sans fiche
    de ce nom, un `<id>` numérique est le lot de ce numéro), ou ligne
    `ameesh-lot: <id>` dans le corps. Une ligne `ameesh-work-partial: <n>`
    (L118b) retire le lot n : la PR n'en livre qu'une partie. Une ligne
    `ameesh-work: <n>` désigne un
    lot précis (`work_items`) : la PR ne ferme alors que ceux-là ; L126 : un
    mot-clé de fermeture de GitHub (« Closes #12 ») désigne de même le lot
    dont l'issue est #12 (son `issue_ref`). Seuls les
    lots créés AVANT la fusion sont fermés (un lot ouvert ensuite pour la
    même fiche n'est pas fermé par une ancienne PR). Idempotent (`already`) ;
    un lot fermé (abandonné, remplacé) n'est jamais rouvert (`refused`).
    #759 : une PR fusionnée ne ferme un lot que si aucune autre PR ouverte ni
    branche déclarée du lot (`branch`, autre que la sienne) n'y reste liée —
    sinon le lot reste ouvert et son journal porte `stage merged`. Une PR qui
    se déclare finale (`ameesh-work: <n> final`) ferme le lot malgré tout.

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

import contextlib
import fcntl
import functools
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import weakref
from dataclasses import dataclass, field
from typing import Iterable

from ... import work_lifecycle as lifecycle
from ... import plan as plan_mod
from ... import process_projection as projection_mod
from ... import process_workflow as workflow_mod
from ... import projects as projects_mod
from ... import role_run
from ... import storage
from ... import work as work_mod
from ...connectors import ConnectorError
from ...connectors.git_merge import resolve_gh
from ...plan_git import WORK_TRAILER_RE, final_lots, partial_lots, short_branch

_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}$")
_LOT_LINE_RE = re.compile(r"(?im)^[ \t>*-]*ameesh-lot[ \t]*:[ \t]*([A-Za-z0-9][A-Za-z0-9._-]*)[ \t]*$")
#: une ligne `ameesh-work: <n>` (même forme dans un commit de fusion : `plan_git`)
_WORK_LINE_RE = WORK_TRAILER_RE
#: L118b : une branche de PR `lot/<id>` ou `lot/<id>-…` (id numérique d'un lot)
LOT_HEAD_RE = re.compile(r"^lot/([1-9][0-9]{0,17})(?:-|$)")
_MARKER_RE = re.compile(r"<!-- ameesh:package=([A-Za-z0-9][A-Za-z0-9._-]*)(?: d=([0-9a-f.]*))? -->")

#: labels d'état d'une issue de lot
STATE_LABELS = (lifecycle.REQUESTED, lifecycle.IN_PROGRESS, *lifecycle.TO_VALIDATE,
                "merged", "promoted", "delivered", lifecycle.BLOCKED, lifecycle.WAITING_HUMAN)
BASE_LABEL = "ameesh"
EPIC_LABEL = "ameesh:epic"
LABEL_COLORS = {"merged": "0e8a16", "promoted": "0e8a16", lifecycle.REQUESTED: "c5def5", lifecycle.IN_PROGRESS: "1d76db",
                lifecycle.QA: "fbca04", lifecycle.VALIDATED: "0e8a16", lifecycle.DELIVERY_RECORDED: "0e8a16",
                lifecycle.BLOCKED: "b60205", lifecycle.WAITING_HUMAN: "d93f0b",
                "epic": "5319e7", "": "ededed",
                # L126 : étiquettes des issues de lot (état, type, priorité)
                lifecycle.PROMOTED: "0e8a16", lifecycle.CLOSED: "cccccc", "bug": "d73a4a",
                "evolution": "a2eeef", "improvement": "7057ff", "p1": "b60205",
                "p2": "fbca04", "p3": "c2e0c6"}
#: priorité d'attention quand une fiche a plusieurs lots ouverts
_ATTENTION = (lifecycle.WAITING_HUMAN, lifecycle.BLOCKED, *lifecycle.TO_VALIDATE,
              lifecycle.IN_PROGRESS, lifecycle.REQUESTED)


class GithubError(RuntimeError):
    """gh introuvable, refus ou réponse illisible : message actionnable."""


def check_repo(repo: str) -> str:
    if not _REPO_RE.fullmatch(repo or ""):
        raise GithubError("repository \"owner/repo\" expected, not %r" % (repo,))
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
            raise GithubError(re.sub(r"^git-merge ?: ", "", str(exc))) from exc
        self.timeout = float(timeout)

    def run(self, argv: list[str], stdin: str | None = None) -> tuple[int, str, str]:
        try:
            proc = subprocess.run([self.path, *argv], input=stdin, capture_output=True,
                                  text=True, timeout=self.timeout, env=self.env)
        except subprocess.TimeoutExpired:
            return 124, "", "timed out (%ss)" % self.timeout
        except OSError as exc:
            return 127, "", str(exc)
        return proc.returncode, proc.stdout, proc.stderr


def _call(gh, argv: list[str], stdin: str | None = None) -> str:
    code, out, err = gh.run(argv, stdin)
    if code != 0:
        raise GithubError("gh %s: exit code %d: %s" % (
            " ".join(argv[:3]), code, " ".join((err or out or "—").split())[:300]))
    return out


def _json(gh, argv: list[str], stdin: str | None = None):
    out = _call(gh, argv, stdin)
    try:
        return json.loads(out) if out.strip() else None
    except ValueError as exc:
        raise GithubError("gh %s: unreadable response" % " ".join(argv[:3])) from exc


def _lines(gh, argv: list[str]) -> list[dict]:
    """Sortie `--jq '.[] | @json'` : un objet JSON par ligne."""
    out = _call(gh, argv)
    rows = []
    for line in out.splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError as exc:
                raise GithubError("gh %s: unreadable line" % " ".join(argv[:3])) from exc
    return rows


#: première ligne d'une réponse `gh api -i`
_HTTP_STATUS_RE = re.compile(r"^HTTP/\S+\s+([1-9][0-9]{2})\b")


def _split_http(text: str) -> tuple[str, str]:
    """(en-têtes, corps) d'une sortie `gh api -i`, quel que soit le saut de ligne."""
    for sep in ("\r\n\r\n", "\n\n"):
        if sep in text:
            head, _, body = text.partition(sep)
            return head, body
    return text, ""


def _http_headers(head: str) -> dict:
    """Les en-têtes d'une sortie `gh api -i`, sans la ligne d'état."""
    headers: dict = {}
    for line in head.splitlines()[1:]:
        name, sep, value = line.partition(":")
        if sep and name.strip():
            headers[name.strip()] = value.strip()
    return headers


def gh_api_response(gh, path: str, *, etag: str | None = None) -> tuple[int, dict, str]:
    """`gh api -i` : (code HTTP, en-têtes, corps), `If-None-Match` pour revalider.

    Un 304 n'a pas de corps et `gh` sort en code 1 : les en-têtes restent
    lisibles, donc la revalidation conditionnelle marche aussi par `gh`
    (repli local, #848). Lève `GithubError` si la sortie ne porte pas de
    ligne d'état (gh absent, panne de transport)."""
    argv = ["api", str(path), "-i"]
    if etag:
        argv += ["-H", "If-None-Match: %s" % etag]
    _code, out, err = gh.run(argv)
    head, body = _split_http(out)
    lines = head.splitlines()
    match = _HTTP_STATUS_RE.match(lines[0]) if lines else None
    if match is None:
        raise GithubError("gh api %s: %s"
                          % (path, " ".join((err or out or "—").split())[:300]))
    return int(match.group(1)), _http_headers(head), body


# --------------------------------------------------------------------------
# lectures GitHub par jeton (L160f) : le client d'ameesh, sans sous-processus
# --------------------------------------------------------------------------

#: variables qui portent le jeton GitHub — les mêmes que `gh` (L160f : une
#: vue servie par `ameesh progress --serve` n'appelle jamais la CLI `gh`)
TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN")
#: base de l'API, surchargeable comme dans les flux CI (GitHub Enterprise)
API_BASE = "https://api.github.com"


def token_of(env=None) -> str:
    """Le jeton GitHub de l'hôte : `GH_TOKEN`, sinon `GITHUB_TOKEN`, sinon ''."""
    env = os.environ if env is None else env
    for name in TOKEN_VARS:
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return ""


# --------------------------------------------------------------------------
# cache partagé des lectures et quota GitHub (#848)
# --------------------------------------------------------------------------

#: sous-dossier de l'état de l'hôte qui porte le cache et le quota partagés
READS_DIR = "github"
#: durée de vie par défaut d'une lecture gardée (secondes) : une lecture
#: identique n'est pas repayée à chaque passage
READS_TTL = 60.0
#: durée de vie minimale d'une lecture gardée (secondes)
READS_MIN_TTL = 5.0
#: entrées de cache gardées au plus (les plus anciennes retirées)
READS_MAX = 2000
#: magasins avant un élagage du cache
READS_PRUNE_EVERY = 128
#: en-têtes de quota lus sur chaque réponse GitHub (lus sans dépendre de la casse)
QUOTA_HEADERS = {"limit": "X-RateLimit-Limit", "remaining": "X-RateLimit-Remaining",
                 "used": "X-RateLimit-Used", "reset": "X-RateLimit-Reset"}
#: ressource d'API par défaut quand la réponse ne dit pas `X-RateLimit-Resource` :
#: le bucket REST `core`. Le bucket GraphQL ne se confond plus avec lui (R1 #848).
DEFAULT_RESOURCE = "core"
#: attente imposée après un 403/429 de quota sans `X-RateLimit-Reset` (secondes),
#: et après une limite secondaire sans `Retry-After` (#1405)
RATE_LIMIT_WAIT = 60.0
#: marque d'une limite secondaire dans le corps d'un 403/429 (sans casse)
SECONDARY_LIMIT_MARK = "secondary rate limit"


def resource_of(path: str) -> str:
    """La ressource d'API qu'un chemin REST consomme (#1405, N2).

    GitHub la dit dans `X-RateLimit-Resource`, mais seulement une fois la
    requête partie : avant, le chemin seul doit dire quel bucket regarder,
    sinon une recherche épuisée repart vers GitHub jusqu'au 403."""
    head = str(path).lstrip("/").split("?", 1)[0].casefold()
    if head == "graphql":
        return "graphql"
    if head == "search/code" or head.startswith("search/code/"):
        return "code_search"
    if head == "search" or head.startswith("search/"):
        return "search"
    return DEFAULT_RESOURCE


class GithubRateLimited(GithubError):
    """Quota GitHub épuisé (#848) : ne rien redemander avant `reset` (epoch).

    Levée sans requête quand le quota observé est à zéro, ou après un 403/429
    de quota ; `reset` vient de `X-RateLimit-Reset` (ou `Retry-After`). Un
    appelant qui a une valeur en cache la sert : la panne ne perd pas de
    preuve, elle attend le reset."""

    def __init__(self, path: str, reset: float | None = None):
        when = time.strftime("%H:%M:%SZ", time.gmtime(reset)) if reset else "unknown"
        super().__init__("GitHub %s: rate limit exhausted (reset at %s)" % (path, when))
        self.reset = reset


@dataclass
class ReadEntry:
    """Une lecture gardée : valeur JSON, ETag de revalidation, instant de
    lecture, ressource d'API dite par la réponse (#1405)."""

    value: object
    etag: str | None
    read_ts: float
    resource: str | None = None


def _int_or_none(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def header_of(headers, name: str):
    """La valeur d'un en-tête, sans dépendre de sa casse (R1 #848, B1).

    `gh api -i` recopie la graphie de GitHub (`X-Ratelimit-Remaining`,
    `Etag`) ; urllib rend un `HTTPMessage`, insensible à la casse. Les deux
    doivent se lire pareil : sinon le repli `gh` ne voit ni le quota (donc
    ne s'arrête pas au reset) ni l'ETag (donc ne revalide pas en 304)."""
    if headers is None:
        return None
    items = getattr(headers, "items", None)
    if items is not None:
        wanted = str(name).lower()
        try:
            for key, value in items():
                if str(key).lower() == wanted:
                    return value
        except (AttributeError, TypeError, ValueError):
            pass
    getter = getattr(headers, "get", None)
    return getter(name) if getter is not None else None


@dataclass
class ReadMeta:
    """Ce qu'une lecture du transport a réellement servi (R1 #848, B2).

    `read_ts` : instant (epoch) de la lecture GitHub d'origine, pas celui du
    passage en cache ; `stale` : valeur antérieure servie faute de quota ;
    `source` : `network`, `revalidated` ou `cache`. `Api` et `GhApi`
    l'exposent dans `last_read` pour que `versions.Reader` ne dise jamais
    frais ce qui est périmé."""

    read_ts: float
    stale: bool = False
    source: str = "network"


def _atomic_write(path: str, data: dict) -> None:
    """Écrit `data` en JSON (0600), remplacement atomique dans son dossier."""
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def reads_scope(base: str | None, token: str | None = None) -> str:
    """La portée du cache partagé (#848, N4) : la base d'API et une empreinte
    non réversible du client (le jeton). Deux bases ou deux jetons ne se
    partagent plus les mêmes entrées, même sur un état d'hôte commun ; le
    jeton lui-même n'entre jamais dans une clé ni dans un fichier."""
    digest = hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:12] \
        if token else "anon"
    return "%s|%s" % (str(base or API_BASE).rstrip("/"), digest)


#: instances vivantes du processus : leurs compteurs en mémoire restent
#: visibles (`counters`) et partent une fois à la fin du passage (N3) —
#: jamais un verrou de fichier ni une réécriture par hit de cache. #1927 :
#: chaque instance porte un verrou en mémoire, pris par `count`, le retrait
#: (`_take_pending`) et `counters`, jamais pendant une E/S.
_LIVE_READS: "weakref.WeakSet" = weakref.WeakSet()


def _merge_counters(data: dict, delta: dict, clock) -> None:
    """Fusionne un `delta` de compteurs dans `data["counters"]`."""
    counters = data.setdefault("counters", {})
    if not isinstance(counters, dict):
        counters = data["counters"] = {}
    for name, n in delta.items():
        counters[name] = int(counters.get(name) or 0) + int(n)
    if delta:
        counters["updated"] = float(clock())


def _take_pending(pending: dict, lock) -> dict:
    """Retire les compteurs en attente sous verrou et les rend (#1927).

    Le retrait est un échange, jamais un vidage après coup : un incrément reçu
    pendant l'écriture se dépose dans le dictionnaire devenu vide et reste
    visible (`counters`), au lieu d'être purgé sans avoir été fusionné."""
    with lock:
        taken = dict(pending)
        pending.clear()
    return taken


def _restore_pending(pending: dict, taken: dict, lock) -> None:
    """Rend au dictionnaire les compteurs d'un `taken` dont l'écriture a
    échoué, en s'ajoutant aux incréments arrivés entre-temps (#1927)."""
    if not taken:
        return
    with lock:
        for name, n in taken.items():
            pending[name] = int(pending.get(name) or 0) + int(n)


def _write_counters(state_dir, root, lock_path, quota_path, pending, clock,
                    pending_lock) -> None:
    """Écrit une fois les compteurs en mémoire dans `quota.json` (N3).

    Un état d'hôte disparu (dossier temporaire nettoyé, poste effacé) n'est
    pas ressuscité pour y écrire des compteurs. Jamais bloquant : une panne
    (droits, disque) rend au dictionnaire les compteurs retirés, pour le
    prochain essai ; un incrément arrivé pendant l'écriture n'est jamais
    emporté par le retrait (#1927)."""
    taken = _take_pending(pending, pending_lock)
    if not taken:
        return
    if not os.path.isdir(state_dir):
        _restore_pending(pending, taken, pending_lock)
        return
    for directory in (root, os.path.join(root, "reads")):
        with contextlib.suppress(OSError):
            os.makedirs(directory, mode=0o700, exist_ok=True)
    if not os.path.isdir(root):
        _restore_pending(pending, taken, pending_lock)
        return
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        _restore_pending(pending, taken, pending_lock)
        return
    merged = False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = {}
        try:
            with open(quota_path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
        _merge_counters(data, taken, clock)
        _atomic_write(quota_path, data)
        merged = True
    except (OSError, ValueError):
        merged = False
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)
        # Dans `finally` : une panne d'un autre type (TypeError d'un
        # `quota.json` aux valeurs étrangères) se propage, mais rend aussi
        # les compteurs retirés.
        if not merged:
            _restore_pending(pending, taken, pending_lock)


def flush_reads(state_dir) -> int:
    """Écrit une fois les compteurs en attente des caches vivants de ce
    processus pour `state_dir` (#1813).

    Le passage du Watcher dure toute la vie du runner : sans cet appel, une
    suite de hits de cache restait invisible sur disque jusqu'à la requête
    réelle suivante (`note`) ou à la fin du processus. Rend le nombre de
    caches concernés ; jamais bloquant (une panne laisse `pending` intact)."""
    state_dir = str(state_dir or "").strip()
    if not state_dir:
        return 0
    root = os.path.join(state_dir, READS_DIR)
    flushed = 0
    for reads in list(_LIVE_READS):
        if reads.root != root:
            continue
        reads.flush()
        flushed += 1
    return flushed


class SharedReads:
    """Cache disque et quota GitHub partagés par les processus d'un hôte (#848).

    L'incident : le jeton GitHub partagé (core 5000/h) a été épuisé à 09:41Z
    par des lectures CI répétées ; chaque processus (notify, progress,
    versions) relisait GitHub sans partager ni revalider, et un 403 se
    répétait à chaque passage. Ici :

    * une lecture gardée (`ttl` secondes) est servie du disque, sans requête ;
    * une lecture périmée est revalidée par `If-None-Match` : un 304 ne
      consomme pas de quota (L161 §3) ;
    * le quota observé (`X-RateLimit-*`, `Retry-After`) vit sur disque : à
      zéro, les lectures servent le cache et les autres échouent aussitôt
      (`GithubRateLimited`), sans boucle agressive ;
    * des compteurs (requêtes, 304, cache servi) rendent l'économie mesurable
      (`ameesh ci status`).

    Le cache ne garde que des réponses JSON de l'API, jamais le jeton ;
    racine 0700, fichiers 0600, écriture atomique. Une panne du cache
    (disque plein, droits) ne fait jamais échouer une lecture : elle la
    laisse simplement repayée.

    N1 : une construction ou une lecture ne crée aucun dossier — le cache
    naît à sa première écriture, et un `state_dir` vide est refusé plutôt
    que de fabriquer un chemin relatif. N3 : les compteurs s'agrègent en
    mémoire et s'écrivent une fois par passage (`flush` : fin de passage du
    Watcher, #1813, ou fin de processus),
    jamais un verrou par hit ; le retrait avant écriture est un échange sous
    verrou, donc un incrément concurrent n'est jamais perdu (#1927).
    N4 : la clé d'une entrée porte la base d'API
    et une empreinte non réversible du jeton."""

    def __init__(self, state_dir, *, clock=time.time, ttl: float = READS_TTL,
                 max_entries: int = READS_MAX, scope: str = ""):
        state_dir = str(state_dir or "").strip()
        if not state_dir:
            raise ValueError("state_dir is required for the shared reads cache")
        self.state_dir = state_dir
        self.root = os.path.join(state_dir, READS_DIR)
        self.clock = clock
        self.ttl = float(ttl)
        self.max_entries = int(max_entries)
        self.scope = str(scope or "")
        self._reads_dir = os.path.join(self.root, "reads")
        self._quota_path = os.path.join(self.root, "quota.json")
        self._lock_path = os.path.join(self.root, ".lock")
        self._stores = 0
        self._pending: dict = {}
        # #1927 : `count` peut venir d'un autre fil que `flush`/`note` ; ce
        # verrou ne protège que le dictionnaire en mémoire, jamais une E/S.
        self._pending_lock = threading.Lock()
        _LIVE_READS.add(self)
        # N3 : les compteurs en attente partent une fois, à la fin du passage
        # (objet libéré ou processus terminé), sans ressusciter un état disparu.
        weakref.finalize(self, _write_counters, self.state_dir, self.root,
                         self._lock_path, self._quota_path, self._pending,
                         self.clock, self._pending_lock)

    def _ensure_dirs(self) -> None:
        """La racine du cache (0700), créée avant une écriture, jamais pour lire."""
        for directory in (self.root, self._reads_dir):
            with contextlib.suppress(OSError):
                os.makedirs(directory, mode=0o700, exist_ok=True)

    @contextlib.contextmanager
    def _locked(self):
        """Verrou de fichier partagé, pour les compteurs et le quota."""
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)

    # -- entrées du cache ---------------------------------------------------
    def _entry_path(self, path: str) -> str:
        """La clé d'une entrée : la portée (base, client) puis le chemin REST.

        N4 : deux bases d'API ou deux jetons ne partagent plus la même entrée
        de cache sur un état commun ; le jeton n'apparaît que par empreinte."""
        digest = hashlib.sha256(
            ("%s\n%s" % (self.scope, path)).encode("utf-8")).hexdigest()[:40]
        return os.path.join(self._reads_dir, digest + ".json")

    def cached(self, path: str) -> ReadEntry | None:
        """L'entrée gardée de ce chemin, même périmée, sinon None."""
        try:
            with open(self._entry_path(path), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("path") != path:
            return None
        try:
            read_ts = float(data.get("read_ts") or 0.0)
        except (TypeError, ValueError):
            return None                # entrée illisible : repayée, jamais une panne
        resource = data.get("resource")
        return ReadEntry(data.get("value"), data.get("etag"), read_ts,
                         str(resource) if resource else None)

    def fresh(self, entry: ReadEntry | None, *, now=None, ttl=None) -> bool:
        """Vrai si l'entrée est dans sa durée de vie (`ttl` par défaut)."""
        if entry is None:
            return False
        ttl = self.ttl if ttl is None else float(ttl)
        now = float(self.clock() if now is None else now)
        return ttl > 0 and now - entry.read_ts < ttl

    def store(self, path: str, value, *, etag: str | None = None,
              read_ts=None, resource: str | None = None) -> None:
        now = float(self.clock() if read_ts is None else read_ts)
        self._ensure_dirs()
        try:
            _atomic_write(self._entry_path(path),
                          {"path": path, "value": value, "etag": etag or None,
                           "read_ts": now, "resource": resource or None})
        except (OSError, ValueError, TypeError):
            return                 # le cache est une économie, jamais une panne
        self._stores += 1
        if self._stores % READS_PRUNE_EVERY == 0:
            self.prune()

    def touch(self, path: str, *, read_ts=None) -> None:
        """Rafraîchit l'instant d'une entrée revalidée (304, sans quota)."""
        entry = self.cached(path)
        if entry is not None:
            self.store(path, entry.value, etag=entry.etag, read_ts=read_ts,
                       resource=entry.resource)

    def prune(self) -> int:
        """Retire les entrées les plus anciennes au-delà de `max_entries`."""
        try:
            names = [os.path.join(self._reads_dir, name)
                     for name in os.listdir(self._reads_dir) if name.endswith(".json")]
        except OSError:
            return 0
        if len(names) <= self.max_entries:
            return 0

        def mtime(path: str) -> float:
            try:
                return os.stat(path).st_mtime
            except OSError:
                return 0.0

        names.sort(key=mtime)
        removed = 0
        for old in names[:len(names) - self.max_entries]:
            with contextlib.suppress(OSError):
                os.unlink(old)
                removed += 1
        return removed

    # -- quota et compteurs -------------------------------------------------
    def _load(self) -> dict:
        try:
            with open(self._quota_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def quota(self, resource: str = DEFAULT_RESOURCE) -> dict:
        """Le dernier quota observé pour cette ressource d'API (défaut `core`)."""
        quota = self.quotas().get(resource)
        return dict(quota) if isinstance(quota, dict) else {}

    def quotas(self) -> dict:
        """Tous les quotas observés, par ressource d'API (`core`, `graphql`…)."""
        quotas = self._load().get("quota")
        if not isinstance(quotas, dict):
            return {}
        return {name: dict(quota) for name, quota in quotas.items()
                if isinstance(quota, dict)}

    def counters(self) -> dict:
        """Les compteurs partagés : le disque, plus ceux encore en mémoire dans
        ce processus pour le même cache (`_LIVE_READS`)."""
        counters = self._load().get("counters")
        merged = dict(counters) if isinstance(counters, dict) else {}
        for reads in list(_LIVE_READS):
            if reads.root != self.root:
                continue
            with reads._pending_lock:
                pending = list(reads._pending.items())
            for name, n in pending:
                merged[name] = int(merged.get(name) or 0) + int(n)
        return merged

    def count(self, name: str, n: int = 1) -> None:
        """Un compteur partagé (hits, stale, refused…) ; jamais bloquant.

        N3 : il s'agrège en mémoire et n'écrit rien ici — `flush()` (fin de
        passage du Watcher, #1813, ou fin du processus) fait une seule écriture
        par passage, au lieu d'un verrou de fichier et d'une réécriture de
        `quota.json` à chaque hit de cache.
        #1927 : le verrou en mémoire rend l'incrément sûr entre fils, et le
        retrait avant écriture (`_take_pending`) ne l'emporte jamais."""
        try:
            key = str(name)
            value = int(n)
        except (TypeError, ValueError):
            return
        with self._pending_lock:
            self._pending[key] = self._pending.get(key, 0) + value

    def flush(self) -> None:
        """Écrit une fois les compteurs agrégés depuis le dernier `flush` (N3).

        Sans compteur en attente, ne touche à rien. Une panne (droits, disque)
        laisse les compteurs en mémoire : le prochain passage réessaiera."""
        _write_counters(self.state_dir, self.root, self._lock_path,
                        self._quota_path, self._pending, self.clock,
                        self._pending_lock)

    def note(self, headers, *, status: int, now=None, resource: str | None = None,
             body: str | None = None) -> float | None:
        """Enregistre le quota lu dans les en-têtes, compte la requête.

        Le quota est rangé par ressource d'API (`X-RateLimit-Resource`, sinon
        `resource`, celle du chemin, sinon `core`) : le bucket REST core et le
        bucket GraphQL ne se confondent plus (R1 #848 ; le pilote l'a demandé
        à 10:40Z). Une limite secondaire (403/429 dont `body` la cite) bloque
        aussi, même quota restant : `Retry-After`, sinon le reset si le quota
        est à zéro, sinon `RATE_LIMIT_WAIT` (#1405, N3). Rend l'instant
        (epoch) jusqu'auquel cette ressource est épuisée, sinon None."""
        now = float(self.clock() if now is None else now)
        values = {key: _int_or_none(header_of(headers, header))
                  for key, header in QUOTA_HEADERS.items()}
        retry = _int_or_none(header_of(headers, "Retry-After"))
        resource = str(header_of(headers, "X-RateLimit-Resource") or "").strip().lower() \
            or str(resource or "").strip().lower() or DEFAULT_RESOURCE
        secondary = status in (403, 429) and SECONDARY_LIMIT_MARK in str(body or "").lower()
        self._ensure_dirs()
        taken = _take_pending(self._pending, self._pending_lock)
        written = False
        try:
            with self._locked():
                data = self._load()
                _merge_counters(data, taken, self.clock)
                quotas = data.setdefault("quota", {})
                if not isinstance(quotas, dict):
                    quotas = data["quota"] = {}
                quota = quotas.setdefault(resource, {})
                if not isinstance(quota, dict):
                    quota = quotas[resource] = {}
                counters = data.setdefault("counters", {})
                counters["requests"] = int(counters.get("requests") or 0) + 1
                if status == 304:
                    counters["not_modified"] = int(counters.get("not_modified") or 0) + 1
                for key, value in values.items():
                    if value is not None:
                        quota[key] = value
                blocked = float(quota.get("blocked_until") or 0.0)
                remaining, reset = values.get("remaining"), values.get("reset")
                exhausted = status in (403, 429) and (remaining == 0 or retry is not None)
                if remaining == 0 and (reset or 0) > now:
                    exhausted = True
                if retry:
                    blocked = max(blocked, now + float(retry))
                elif exhausted:
                    blocked = max(blocked, float(reset or (now + RATE_LIMIT_WAIT)))
                elif secondary:
                    blocked = max(blocked, now + RATE_LIMIT_WAIT)
                quota["blocked_until"] = blocked if blocked > now else 0.0
                quota["checked"] = now
                _atomic_write(self._quota_path, data)
                written = True
                return quota["blocked_until"] or None
        except (OSError, ValueError):
            return None
        finally:
            # #1927 : le retrait n'est confirmé qu'une fois l'écriture faite ;
            # les compteurs retirés reviennent tant qu'elle n'a pas eu lieu,
            # quelle que soit la panne.
            if not written:
                _restore_pending(self._pending, taken, self._pending_lock)

    def blocked(self, now=None, resource: str = DEFAULT_RESOURCE) -> float | None:
        """L'instant (epoch) jusqu'auquel cette ressource est épuisée, sinon None."""
        now = float(self.clock() if now is None else now)
        until = float(self.quota(resource).get("blocked_until") or 0.0)
        return until if until > now else None


def _resource_header(headers) -> str | None:
    """La ressource d'API dite par une réponse (`X-RateLimit-Resource`), sinon None."""
    return str(header_of(headers, "X-RateLimit-Resource") or "").strip().lower() or None


def _error_body(exc) -> str:
    """Le corps d'un refus HTTP (borné), pour y lire une limite secondaire."""
    try:
        data = exc.read(4096)
    except Exception:  # noqa: BLE001 — un corps illisible ne dit rien
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return str(data or "")


class Api:
    """Les lectures GitHub par HTTPS, avec le jeton de l'hôte (lecture seule).

    `get(chemin, ttl=…) -> JSON` ; toute panne (réseau, refus, réponse
    illisible) lève `GithubError`, jamais autre chose. Avec un cache partagé
    (`cache_dir` ou `reads`) : les lectures gardées sont servies du disque,
    les autres revalidées par `If-None-Match`, et le quota épuisé fait
    répondre le cache ou lever `GithubRateLimited` sans requête. `last_read`
    dit ce que le dernier appel a servi (date d'origine, `stale`)."""

    def __init__(self, token=None, *, env=None, base=None, timeout=60.0,
                 cache_dir=None, reads: "SharedReads | None" = None,
                 ttl: float = READS_TTL, clock=time.time, opener=None):
        env = os.environ if env is None else env
        self.token = str(token if token is not None else token_of(env)).strip()
        self.base = str(base or env.get("GITHUB_API_URL") or API_BASE).rstrip("/")
        self.timeout = float(timeout)
        self.ttl = float(ttl)
        self.last_read: ReadMeta | None = None
        self.clock = clock
        self.reads = reads if reads is not None else (
            SharedReads(cache_dir, clock=clock, ttl=ttl,
                        scope=reads_scope(self.base, self.token)) if cache_dir else None)
        self._open = opener or urllib.request.urlopen

    def _request_headers(self, path: str, entry: ReadEntry | None) -> dict:
        headers = {"Authorization": "Bearer %s" % self.token,
                   "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "ameesh"}
        if entry is not None and entry.etag:
            headers["If-None-Match"] = entry.etag
        return headers

    def get(self, path: str, *, ttl=None):
        ttl = self.ttl if ttl is None else float(ttl)
        now = float(self.clock())
        self.last_read = None
        entry = self.reads.cached(path) if self.reads is not None else None
        resource = (entry.resource if entry is not None else None) or resource_of(path)
        if self.reads is not None and self.reads.fresh(entry, now=now, ttl=ttl):
            self.reads.count("hits")
            self.last_read = ReadMeta(entry.read_ts, False, "cache")
            return entry.value
        if self.reads is not None:
            until = self.reads.blocked(now, resource)
            if until:
                if entry is not None:
                    self.reads.count("stale")
                    self.last_read = ReadMeta(entry.read_ts, True, "cache")
                    return entry.value
                self.reads.count("refused")
                raise GithubRateLimited(path, until)
        url = "%s/%s" % (self.base, str(path).lstrip("/"))
        request = urllib.request.Request(url, method="GET",
                                         headers=self._request_headers(path, entry))
        try:
            with self._open(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
                headers = getattr(response, "headers", None)
        except urllib.error.HTTPError as exc:
            headers = getattr(exc, "headers", None)
            if exc.code == 304 and entry is not None:
                if self.reads is not None:
                    self.reads.touch(path, read_ts=now)
                    self.reads.note(headers, status=304, now=now, resource=resource)
                self.last_read = ReadMeta(now, False, "revalidated")
                return entry.value
            until = self.reads.note(headers, status=exc.code, now=now, resource=resource,
                                    body=_error_body(exc)) \
                if self.reads is not None else None
            if until:
                if entry is not None:
                    self.reads.count("stale")
                    self.last_read = ReadMeta(entry.read_ts, True, "cache")
                    return entry.value
                self.reads.count("refused")
                raise GithubRateLimited(path, until) from exc
            raise GithubError("GitHub %s: HTTP %d" % (path, exc.code)) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise GithubError("GitHub %s: %s"
                              % (path, " ".join(str(exc).split())[:200])) from exc
        try:
            value = json.loads(body) if body.strip() else None
        except ValueError as exc:
            raise GithubError("GitHub %s: unreadable response" % (path,)) from exc
        if self.reads is not None:
            etag = header_of(headers, "ETag")
            self.reads.note(headers, status=200, now=now, resource=resource)
            self.reads.store(path, value, etag=etag, read_ts=now,
                             resource=_resource_header(headers))
        self.last_read = ReadMeta(now, False, "network")
        return value


class GhApi:
    """`gh api` derrière l'interface d'`Api` : le repli LOCAL, quand l'hôte
    n'a pas de jeton (un poste de travail, jamais le serveur). Le cache
    partagé (#848) sert aussi ce repli, revalidation comprise : `gh api -i`
    rend les en-têtes, donc l'ETag et les en-têtes de quota se lisent comme
    avec le jeton (nos hôtes d'agents n'ont pas de `GH_TOKEN` : `gh` y lit
    sa session dans `~/.config/gh/hosts.yml`)."""

    def __init__(self, gh, *, reads: "SharedReads | None" = None,
                 ttl: float = READS_TTL, clock=time.time):
        self.gh = gh
        self.reads = reads
        self.ttl = float(ttl)
        self.clock = clock
        self.last_read: ReadMeta | None = None

    def get(self, path: str, *, ttl=None):
        ttl = self.ttl if ttl is None else float(ttl)
        now = float(self.clock())
        self.last_read = None
        if self.reads is None:
            value = _json(self.gh, ["api", path])
            self.last_read = ReadMeta(now, False, "network")
            return value
        entry = self.reads.cached(path)
        resource = (entry.resource if entry is not None else None) or resource_of(path)
        if self.reads.fresh(entry, now=now, ttl=ttl):
            self.reads.count("hits")
            self.last_read = ReadMeta(entry.read_ts, False, "cache")
            return entry.value
        until = self.reads.blocked(now, resource)
        if until:
            if entry is not None:
                self.reads.count("stale")
                self.last_read = ReadMeta(entry.read_ts, True, "cache")
                return entry.value
            self.reads.count("refused")
            raise GithubRateLimited(path, until)
        status, headers, body = gh_api_response(
            self.gh, path, etag=entry.etag if entry is not None else None)
        if status == 304 and entry is not None:
            self.reads.touch(path, read_ts=now)
            self.reads.note(headers, status=304, now=now, resource=resource)
            self.last_read = ReadMeta(now, False, "revalidated")
            return entry.value
        until = self.reads.note(headers, status=status, now=now, resource=resource,
                                body=body)
        if until and status in (403, 429):
            if entry is not None:
                self.reads.count("stale")
                self.last_read = ReadMeta(entry.read_ts, True, "cache")
                return entry.value
            self.reads.count("refused")
            raise GithubRateLimited(path, until)
        if status >= 400:
            raise GithubError("GitHub %s: HTTP %d" % (path, status))
        try:
            value = json.loads(body) if body.strip() else None
        except ValueError as exc:
            raise GithubError("gh api %s: unreadable response" % path) from exc
        self.reads.store(path, value, etag=header_of(headers, "ETag"), read_ts=now,
                         resource=_resource_header(headers))
        self.last_read = ReadMeta(now, False, "network")
        return value


def client(*, env=None, gh: str | None = None, timeout: float = 60.0,
           allow_gh: bool = True, cache_dir=None, ttl: float = READS_TTL,
           clock=time.time):
    """Le client de lecture GitHub : le jeton s'il est là, sinon `gh`.

    `allow_gh=False` (le serveur) : sans jeton, rend None — `gh` n'est pas
    appelé et l'appelant dit le manque. `cache_dir` (l'état de l'hôte) active
    le cache et le quota partagés (#848) ; sans lui, rien n'est gardé."""
    env = os.environ if env is None else env
    token = token_of(env)
    reads = SharedReads(cache_dir, clock=clock, ttl=ttl,
                        scope=reads_scope(env.get("GITHUB_API_URL"), token)) \
        if cache_dir else None
    if token:
        return Api(env=env, timeout=timeout, reads=reads, ttl=ttl, clock=clock)
    if not allow_gh:
        return None
    return GhApi(Gh(gh, env=env, timeout=timeout), reads=reads, ttl=ttl, clock=clock)


def client_name(env=None) -> str:
    """Ce que `client` utiliserait : `api` (jeton) ou `gh` (repli local)."""
    return "api" if token_of(env) else "gh"


def quota_stats(state_dir) -> dict:
    """Le quota et les compteurs partagés de l'hôte (#848), pour les vues.

    Rend `{"quota": {ressource: {...}}, "counters": {...}}`, vide sans cache
    encore écrit : `core` et `graphql` ne se confondent pas. Un `state_dir`
    vide est refusé (N1) : la vue ne fabrique pas de chemin relatif, et une
    lecture ne crée aucun dossier."""
    if not str(state_dir or "").strip():
        return {"quota": {}, "counters": {}}
    reads = SharedReads(state_dir)
    return {"quota": reads.quotas(), "counters": reads.counters()}


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


def _targets_of_pr(db, st, pr: dict, repo: str, idents: Iterable[str],
                   by_issue: dict | None = None):
    """Les lots qu'une PR désigne : lignes `ameesh-work:`, `Closes #n` (via
    `issue_ref`), fiches (`packages_of_pr`), et branche `lot/<id>-…` d'un
    dépôt même sans fiche. Rend `(targets, by_issue)` — `by_issue` n'est
    construit qu'à la demande (une fois par relevé).

    #2317 (N7) : un lot explicitement nommé (`ameesh-work:`) l'emporte sur la
    fiche — la rétention d'une PR ouverte est la SYMÉTRIE de sa fermeture :
    elle retient exactement les lots qu'elle fermerait si on la fusionnait
    maintenant. Sans ligne explicite, une PR de fiche (`lot/<fiche>…` ou
    `ameesh-lot:`) désigne tous les lots de la fiche. Unir les deux faisait
    qu'une PR de fiche oubliée, portant `ameesh-work: X`, retenait Y et Z de
    la fiche alors que leurs propres PR étaient fusionnées."""
    explicit = work_items_of_pr(pr)
    closes = issues_of_pr(pr, repo)
    if closes:
        # L126 : « Closes #n » désigne le lot dont l'issue est n (issue_ref)
        if by_issue is None:
            by_issue = _lots_by_issue_ref(st)
        explicit = sorted(set(explicit) | {lot for issue in closes
                                           for lot in by_issue.get(issue.casefold(), ())})
    packages = packages_of_pr(pr, idents)
    if not explicit and not packages:
        # L118b : `lot/<id>-…` sans fiche de ce nom désigne le lot <id>
        # #312 r1 : seulement une branche du dépôt même (pas d'un fork),
        # et la branche enregistrée du lot s'il en porte une
        head = LOT_HEAD_RE.match(pr.get("headRefName") or "")
        if head and pr.get("isCrossRepository") is False:
            item = work_mod.get(db, int(head.group(1)))
            # #2102 : une branche détachée du lot ne le désigne plus
            if item is None or (not item.get("branch") and not work_mod.is_detached(
                    st, int(head.group(1)), pr["headRefName"])) \
                    or short_branch(item.get("branch")) == pr["headRefName"]:
                explicit = [int(head.group(1))]
    if explicit:
        return [(i, None) for i in explicit], by_issue
    return ([(int(item["id"]), pid) for pid in packages
             for item in st.work.by_package(pid)], by_issue)


#: #1693 (N3) : profondeur de journal relue avant de reposer un constat
#: `stage merged` — une note intercalée ne doit pas faire réécrire le même
#: constat, sans relire tout le journal d'un lot à chaque relevé.
_STAGE_DEDUP_EVENTS = 50

#: #1693 (N1) : une fusion est annulée par une correction `merged → build`
#: du journal (vue `WorkItems.corrections`, qui lit la transition, pas la note).


def _stage_reason(item: dict, pr: dict, open_refs: list[str] | None,
                  ref: str = "", *, annulled_ts: float | None = None,
                  merged_ts: float | None = None) -> str | None:
    """#759 : pourquoi une PR fusionnée NE ferme PAS ce lot (None : elle ferme).

    Une PR qui se déclare finale pour le lot (`ameesh-work: <id> final`) ferme
    toujours. Sinon le lot reste ouvert si une PR ouverte le désigne encore,
    ou si sa branche déclarée n'est pas celle de la PR fusionnée (autre étape,
    gel en attente) : son journal porte alors `stage merged`.

    #1693 (N1) : une PR fusionnée avant la DERNIÈRE correction `merged → build`
    du lot (`annulled_ts`, vue cx2) ne le referme pas, même déclarée finale :
    sa fusion a été annulée, et le lot attend un transport fusionné APRÈS la
    correction. Le critère d'instant couvre les corrections répétées, qu'un
    simple `pr_ref` laisserait passer (il ne porte que la dernière PR fermante,
    pas l'historique).

    #2317 (N3) : l'égalité `pr_ref == ref` n'est qu'un REPLI quand la date de
    fusion manque (`merged_ts is None`). Avec `mergedAt`, l'instant décide : un
    repli joué en plus affichait « rouvert depuis » sur une PR pourtant
    postérieure à la correction, et la fermait à tort."""
    if annulled_ts is not None and merged_ts is not None and merged_ts <= annulled_ts:
        return ("stage merged: PR %s — fusion antérieure à la dernière correction "
                "merged → build, transport suivant attendu" % ref)
    if ref and merged_ts is None and (item.get("pr_ref") or "") == ref:
        return ("stage merged: PR %s a déjà fermé ce lot (rouvert depuis), "
                "transport suivant attendu" % ref)
    if int(item["id"]) in final_lots(pr.get("body")):
        return None
    reasons = []
    if open_refs:
        reasons.append("PR ouverte %s" % ", ".join(sorted(set(open_refs))))
    branch = item.get("branch") or ""
    if branch and short_branch(branch) != (pr.get("headRefName") or ""):
        reasons.append("branche déclarée %s" % short_branch(branch))
    if not reasons:
        return None
    return "stage merged: %s — lot non fermé" % " ; ".join(reasons)


def _linked_reason(db, item: dict, pr: dict, ref: str) -> str | None:
    """#5168 : pourquoi une PR désignée par sa SEULE branche `lot/<id>-…`
    (L118b) n'est pas la livraison du lot (None : elle l'est).

    Elle l'est si sa branche est la branche de livraison enregistrée du lot,
    ou si la tête fusionnée porte la tête gelée du lot (jalon `frozen`). Sinon
    c'est une fusion liée — une PR préalable d'un autre dépôt (cas #3392 :
    manaty/home#223, `lot/3392-canon-canary`, lot sans branche ni gel) : elle
    se trace, sans transition. Un lot sans gel ni branche n'est donc jamais
    clos par cette voie, et une correction à la main n'est pas défaite au
    relevé suivant."""
    head = pr.get("headRefName") or ""
    if item.get("branch") and short_branch(item["branch"]) == head:
        return None
    frozen = work_mod.frozen_head(db, int(item["id"]))
    commit = pr.get("mergeCommit") or {}
    tips = {str(pr.get("headRefOid") or "").strip().lower(),
            str((commit.get("oid") if isinstance(commit, dict) else "") or "").strip().lower()}
    if frozen and frozen in tips:
        return None
    return ("linked merge: PR %s (branch %s%s) merged without the lot's freeze %s nor its "
            "delivery branch — trace only, not a delivery"
            % (ref, head, (", head %s" % str(pr.get("headRefOid"))[:12])
               if pr.get("headRefOid") else "",
               frozen[:12] if frozen else "(none)"))


def open_prs_of_repo(gh, repo: str) -> list[dict]:
    """#759 : TOUTES les PR ouvertes du dépôt (REST paginé, sans plafond).

    La décision de fermer un lot exige de connaître exhaustivement les PR
    ouvertes qui le désignent encore : un `gh pr list --limit` tronqué
    laisserait une PR liée invisible et le lot se fermerait à tort. Les
    champs rendus sont ceux de `gh pr list --json` attendus par
    `_targets_of_pr` (le `--limit` du relevé des fusions ne s'y applique
    pas)."""
    rows = _lines(gh, ["api", "--paginate",
                       "repos/%s/pulls?state=open&per_page=100" % repo,
                       "--jq", ".[] | @json"])
    out: list[dict] = []
    for row in rows:
        head = row.get("head") or {}
        head_repo = (head.get("repo") or {}).get("full_name")
        out.append({"number": row.get("number"),
                    "headRefName": head.get("ref") or "",
                    "isCrossRepository": head_repo != repo,
                    "body": row.get("body") or "",
                    "url": row.get("html_url") or ""})
    return out


def sync_github(db, gh, repo: str, *, limit: int = 200, dry_run: bool = False) -> dict:
    """Ferme les lots dont la PR est fusionnée. Rend le compte rendu.

    #759 : une PR fusionnée ne ferme un lot que si aucune autre PR ouverte ni
    branche déclarée du lot n'y reste liée, sauf si elle se déclare finale
    pour ce lot (`ameesh-work: <id> final`) ; sinon le lot reste ouvert et son
    journal porte `stage merged`. #1693 (N1) : la PR qui a déjà fermé le lot
    (rouvert par correction `merged → build`) ne le referme pas.

    #1693 (N2)/#2317 (N7) : une PR OUVERTE retient exactement les lots
    # qu'elle fermerait si on la fusionnait maintenant — la symétrie de
    # `_targets_of_pr` : ligne `ameesh-work:` d'abord, sinon tous les lots de
    # la fiche (`lot/<fiche>-…`, `ameesh-lot:`). Une ligne explicite ne fait
    # donc pas retenir le reste de la fiche : une PR de fiche oubliée ne
    # bloque plus les lots dont la propre PR est déjà fusionnée.

    #5168 : une PR désignée par sa seule branche `lot/<id>-…` ne livre le lot
    que si c'est sa branche enregistrée ou si sa tête porte le gel du lot ;
    sinon elle est tracée comme fusion liée (`linked`), sans transition.
    """
    check_repo(repo)
    prs = _json(gh, ["pr", "list", "--repo", repo, "--state", "merged", "--limit", str(int(limit)),
                     "--json", "number,headRefName,headRefOid,isCrossRepository,body,state,mergeCommit,"
                     "mergedBy,mergedAt,url"])
    if not isinstance(prs, list):
        raise GithubError("gh pr list: list expected")
    open_prs = open_prs_of_repo(gh, repo)
    st = storage.of(db)
    idents = [p["id"] for p in st.packages.all() if p.get("kind") in work_mod.LINKABLE_KINDS]
    # #2317 (N4) : la lecture des corrections est bornée aux lots qu'une PR
    # fusionnée peut fermer. Collecter ces lots d'abord évite un LIKE sur tout
    # `work_item_events` à chaque relevé (l'ancien `events_noted(…, 0.0)`).
    targets_by_pr: dict[int, list] = {}
    by_branch: set[int] = set()
    candidates: set[int] = set()
    by_issue: dict | None = None
    for pr in prs:
        if (pr.get("state") or "MERGED") != "MERGED":
            continue
        targets, by_issue = _targets_of_pr(db, st, pr, repo, idents, by_issue)
        targets_by_pr[int(pr.get("number") or 0)] = targets
        # #5168 : désignée par sa seule branche `lot/<id>-…` (L118b), la PR
        # doit encore prouver qu'elle livre le lot (`_linked_reason`)
        if targets and not work_items_of_pr(pr) and not packages_of_pr(pr, idents) \
                and not any(lot for issue in issues_of_pr(pr, repo)
                            for lot in (by_issue or {}).get(issue.casefold(), ())):
            by_branch.add(int(pr.get("number") or 0))
        candidates.update(int(item_id) for item_id, _pid in targets)
    # #1693 (N1, cx2) : une PR fusionnée AVANT la dernière correction
    # `merged → build` d'un lot ne peut plus le fermer — sa fusion a été
    # annulée, quel que soit le nombre de corrections et de transports depuis
    # (le seul `pr_ref` ne porte que la dernière PR, pas l'historique). La
    # correction se lit à la TRANSITION du journal (#2317/N2), pas à sa note.
    annulled: dict[int, float] = st.work.corrections(candidates)
    results: list[dict] = []
    # #759 : les lots encore désignés par une PR ouverte ne se ferment pas
    open_lots: dict[int, list[str]] = {}
    for pr in sorted(open_prs, key=lambda p: int(p.get("number") or 0)):
        ref = "%s#%s" % (repo, pr.get("number"))
        targets, by_issue = _targets_of_pr(db, st, pr, repo, idents, by_issue)
        # L118b : une PR ouverte qui ne livre qu'une partie du lot le retient
        # aussi — `ameesh-work-partial:` n'est pas une ligne `ameesh-work:`,
        # elle n'apparaît donc pas dans `targets` (#759, incident #147).
        for item_id in sorted({i for i, _pid in targets} | partial_lots(pr.get("body"))):
            open_lots.setdefault(item_id, []).append(ref)
    for pr in sorted(prs, key=lambda p: int(p.get("number") or 0)):
        if (pr.get("state") or "MERGED") != "MERGED":
            continue
        ref = "%s#%s" % (repo, pr.get("number"))
        commit = pr.get("mergeCommit") or {}
        sha = commit.get("oid") if isinstance(commit, dict) else ""
        login = ((pr.get("mergedBy") or {}).get("login") if isinstance(pr.get("mergedBy"), dict)
                 else None)
        actor = "github:%s" % login if login else "github"
        merged_ts = _iso_ts(pr.get("mergedAt"))
        targets = targets_by_pr.get(int(pr.get("number") or 0), [])
        # L118b : une PR qui ne livre qu'une partie d'un lot ne le ferme pas
        partial = partial_lots(pr.get("body"))
        targets = [(i, pid) for i, pid in targets if i not in partial]
        for item_id, pid in targets:
            item = work_mod.get(db, item_id)
            entry = {"pr": ref, "work_item": item_id, "package": pid or (item or {}).get(
                "package_id"), "sha": sha or None, "merged_by": login}
            if item is None:
                entry.update(result="unknown", detail="work item #%d not found" % item_id)
            elif merged_ts is not None and item.get("created_ts") \
                    and float(item["created_ts"]) > merged_ts:
                entry.update(result="later", detail="work item created after the merge: not closed")
            else:
                # #759 : un lot encore lié (PR ouverte, autre branche déclarée)
                # n'est pas fermé par cette PR ; un lot déjà fermé suit son sort.
                # #1693 (N1) : pas non plus par une fusion antérieure à la
                # dernière correction merged → build (voir `_stage_reason`).
                live = (item["state"] not in work_mod.MERGED_STATES
                        and item["state"] != lifecycle.CLOSED)
                linked = (_linked_reason(db, item, pr, ref)
                          if live and int(pr.get("number") or 0) in by_branch else None)
                text = linked or (_stage_reason(item, pr, open_lots.get(item_id), ref,
                                                annulled_ts=annulled.get(item_id),
                                                merged_ts=merged_ts)
                                  if live else None)
                if text:
                    if not dry_run:
                        # #1693 (N3) : un relevé répété ne répète pas le même
                        # constat, même si une autre note s'est intercalée depuis
                        # (relance, correction) — la dédup lit le journal récent,
                        # pas seulement sa dernière ligne.
                        recent = work_mod.events(db, item_id, _STAGE_DEDUP_EVENTS)
                        if not any((e.get("note") or "") == text for e in recent):
                            work_mod.note(db, item_id, text, actor=actor)
                    entry.update(result="linked" if linked else "stage-merged",
                                 detail=text + (" (dry run)" if dry_run else ""))
                elif dry_run:
                    state = item["state"]
                    entry.update(result=("already" if state in work_mod.MERGED_STATES else
                                         "refused" if state == lifecycle.CLOSED else "would-merge"),
                                 detail="%s (dry run)" % state)
                else:
                    done = work_mod.deliver(
                        db, item_id, connector=work_mod.merge_connector(),
                        evidence={"seen": "sync-github", "sha": sha or "", "pr": ref},
                        actor=actor)
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


def _managed_label(name: str, managed=None) -> bool:
    """L'étiquette est à ameesh : `ameesh`, `ameesh:*`, ou un libellé de board
    admis du projet (#2130/D)."""
    return bool(name) and (name == BASE_LABEL or name.startswith(BASE_LABEL + ":")
                           or name in (managed or ()))


def _own_labels(labels: Iterable, managed=None) -> list[str]:
    names = [(lab.get("name") if isinstance(lab, dict) else str(lab)) for lab in labels or []]
    return sorted(n for n in names if _managed_label(n, managed))


def _fields(title: str, body: str, labels: Iterable, state: str, marker_re=None,
            managed=None) -> dict:
    return {"title": (title or "").strip(), "body": _clean(body, marker_re),
            "labels": _own_labels(labels, managed), "state": (state or "").lower()}


_FIELD_NAMES = ("title", "body", "labels", "state")
_FIELD_FR = {"title": "title", "body": "body", "labels": "ameesh labels", "state": "state"}


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


def card_link(canon_ref: str | None, base: str | None) -> str:
    """Lien vers la fiche : `base/<chemin>` (`{commit}` remplacé), sinon la réf."""
    ref = canon_ref or ""
    member_path, _, commit = ref.rpartition("@")
    path = member_path.split(":", 1)[1] if ":" in member_path else member_path
    if base and path:
        url = base.replace("{commit}", commit).rstrip("/") + "/" + path
        return "[%s](%s) (`%s`)" % (path, url, ref)
    return "`%s`" % ref if ref else "—"


#: L148 (stage 2c): old spelling kept for the callers not yet renamed.
fiche_link = card_link


def lot_state(items: list[dict]) -> tuple[str | None, str]:
    """(label d'état, état de l'issue) d'une fiche `lot` d'après ses lots."""
    status = plan_mod.package_status(items)
    if status == "pending":
        return lifecycle.REQUESTED, "open"
    if status == "merged":
        return "merged", "closed"
    if status == "abandoned":
        return None, "closed"
    states = {(lifecycle.DELIVERY_RECORDED if i["state"] in lifecycle.DELIVERED else i["state"])
              for i in items if i.get("state") != lifecycle.CLOSED}
    for state in _ATTENTION:
        if state in states:
            return state, "open"
    return lifecycle.REQUESTED, "open"


_HEADER = ("_View projected by ameesh: the source is the canon (package below) and "
           "the state of ameesh. A change made here is not imported; it is "
           "reported as a proposal: carry it to the canon through a PR._")


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
            "- Package: %s" % card_link(p.get("canon_ref"), canon_url),
            "- Epic: %s" % ("`%s`" % epic if epic else "—"),
            "- Owner: %s" % (p.get("responsible") or "—"),
            "- ameesh state: %s%s" % (
                label or "closed (abandoned)",
                "; work items %s" % ", ".join("#%s %s" % (i["id"], i["state"])
                                              for i in lots)
                if lots else "; no open work item"),
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
            "- Package: %s" % card_link(p.get("canon_ref"), canon_url),
            "- Milestone: %s" % ("`%s`" % e["milestone"] if e["milestone"] else "—"),
            "- Owner: %s" % (p.get("responsible") or "—"),
            "- Progress: %d/%d work items merged%s" % (
                e["lots_merged"], e["lots_total"] - e["lots_abandoned"],
                " (%d abandoned)" % e["lots_abandoned"] if e["lots_abandoned"] else ""),
        ])
        out.append(Desired(
            ident=pid, kind="epic", title="[%s] %s" % (pid, p.get("title") or pid), body=body,
            labels=sorted([BASE_LABEL, EPIC_LABEL]),
            state="closed" if done else "open",
            state_reason="completed" if done else None,
            tasks=[(lot["id"], lot["status"] == "merged") for lot in e["lots"]]))
    return out


def _with_tasks(view: Desired, numbers: dict) -> str:
    lines = [view.body, "", "Work items:"]
    for lot, merged in view.tasks:
        ref = "#%s" % numbers[lot] if lot in numbers else "`%s` (issue to create)" % lot
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
                          "description": description or "ameesh projection (L29)"}))

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
        # #2845: every projection comment must be ignored by the incoming
        # watch, including closure and duplicate comments. Accounts can be
        # shared with humans, so author identity cannot serve as a filter.
        if "<!-- ameesh:" not in body:
            body += "\n\n<!-- ameesh:projection:v1 -->"
        return _json(self.gh, ["api", "-X", "POST",
                               "repos/%s/issues/%d/comments" % (self.repo, number),
                               "--input", "-"], json.dumps({"body": body})) or {}

    def comments(self, number: int) -> list[dict]:
        return _lines(self.gh, ["api", "--paginate",
                               "repos/%s/issues/%d/comments?per_page=100" % (self.repo, number),
                               "--jq", ".[] | @json"])

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
                                      "detail": "package missing from the plan: issue left as is"})

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
                    "detail": "edited in GitHub (%s): proposal to carry to the canon; "
                              "the view is rewritten from ameesh"
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
        report["notes"] += ["sub-issues API not probed (dry run without a projected epic): "
                           "sub-issues or task list decided at the first real projection"]
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
SETTINGS_KEYS = ("projects", "host", "interval", "ci_ttl", "exclude_terms", "process")
#: #2130/D : clés admises d'un mapping Process (data only, compilé au chargement)
PROCESS_KEYS = ("ref", "version", "document", "labels")
#: forme longue d'un projet : `repo`, les workflows suivis par le veilleur de
#: CI (#1110, `ci.Watcher`) et la ou les branches de base du dépôt (#1483)
PROJECT_KEYS = ("repo", "workflows", "base")
#: suffixes admis pour un fichier de workflow (`.github/workflows/…`)
WORKFLOW_SUFFIXES = (".yml", ".yaml")
DEFAULT_INTERVAL = 300.0
MIN_INTERVAL = 60.0
#: durée de vie (secondes) des lectures CI dans le cache partagé (#848) :
#: 60 s = un passage de `ameesh notify` sur deux est servi du cache, les
#: autres revalident (un 304 ne consomme pas de quota)
DEFAULT_CI_TTL = READS_TTL
MIN_CI_TTL = READS_MIN_TTL
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

LOT_TERMINAL = lifecycle.COMPLETED
LOT_TYPES = ("bug", "evolution", "improvement")
PRIORITY_LABELS = {1: "p1", 2: "p2", 3: "p3"}
_TYPE_FR = {"bug": "bug", "evolution": "evolution", "improvement": "improvement"}
_PRIORITY_FR = {1: "high priority", 2: "normal priority", 3: "low priority"}
_STATE_FR = {lifecycle.REQUESTED: "to do", lifecycle.IN_PROGRESS: "in progress",
             lifecycle.QA: "in review", lifecycle.VALIDATED: "approved",
             "merged": "merged", "promoted": "delivered", "delivered": "delivered", lifecycle.BLOCKED: "blocked",
             lifecycle.WAITING_HUMAN: "waiting for a human", lifecycle.CLOSED: "closed"}
_LOT_FIELD_FR = {"title": "title", "body": "body", "labels": "ameesh labels",
                 "state": "state"}


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
    #: secondes de garde d'une lecture CI partagée (#848)
    ci_ttl: float = DEFAULT_CI_TTL
    #: termes que l'organisation ne publie jamais (noms de clients…), sans casse
    exclude_terms: tuple = ()
    #: {dépôt: (fichiers de workflow, …)} — les workflows que le veilleur de CI
    #: suit dans ce dépôt (#1110) ; sans entrée, il découvre ceux du dépôt
    workflows: dict = field(default_factory=dict)
    #: {dépôt: (branches de base, …)} — les branches sur lesquelles une
    #: exécution n'est jamais remplacée et dont le rouge sert de référence
    #: (#1483 : Nexlink a `develop` pour base) ; sans entrée, `ci.BASE_BRANCHES`
    bases: dict = field(default_factory=dict)
    #: #2130/D : {projet: (ProcessMapping, …)} — les révisions Process admises et
    #: leurs libellés de board ; un lot pinné n'est projeté que par un mapping
    #: admis de son couple exact (ref, version). Vide : aucune greffe.
    process: dict = field(default_factory=dict)

    def process_for(self, project: str | None) -> tuple:
        """Les mappings admis d'un projet (nom sans casse), ou () — opt-in."""
        name = self.project_name(project)
        return tuple(self.process.get(name, ())) if name else ()

    def project_name(self, project: str | None) -> str | None:
        """Le nom configuré d'un projet (sans casse), ou None."""
        key = (project or "").strip().casefold()
        if not key:
            return None
        return next((name for name in self.projects if name.casefold() == key), None)

    def repo_for(self, project: str | None) -> str | None:
        name = self.project_name(project)
        return self.projects[name] if name else None

    def workflows_for(self, repo: str | None) -> tuple | None:
        """Les workflows déclarés pour un dépôt, ou None (découverte)."""
        key = (repo or "").strip().casefold()
        if not key:
            return None
        return next((tuple(files) for name, files in self.workflows.items()
                     if name.casefold() == key), None)

    def bases_for(self, repo: str | None) -> tuple | None:
        """Les branches de base déclarées d'un dépôt, ou None (défaut du
        veilleur : `ci.BASE_BRANCHES`)."""
        key = (repo or "").strip().casefold()
        if not key:
            return None
        return next((tuple(branches) for name, branches in self.bases.items()
                     if name.casefold() == key), None)

    def all_bases(self) -> tuple:
        """Toutes les branches de base déclarées, sans doublon, dans l'ordre
        de déclaration : la lecture des alertes (`ci.main_red`) couvre ainsi
        les bases de tous les dépôts (le défaut `main` reste à la charge de
        l'appelant)."""
        out: list[str] = []
        for branches in self.bases.values():
            out.extend(branches)
        return tuple(dict.fromkeys(out))


def _board_labels(project: str, spec: dict, artifact) -> dict:
    """#2130/D : un libellé de board admis par étape, exactement."""
    where = "`github.process.%s.labels`" % project
    labels = spec.get("labels")
    if not isinstance(labels, dict) or not labels:
        raise SettingsError("%s: object {stage: board label} expected" % where)
    stages = artifact.statuses()
    unknown = sorted(str(key) for key in labels if str(key) not in stages)
    if unknown:
        raise SettingsError("%s: unknown stage(s) %s (artifact stages: %s)"
                            % (where, ", ".join(unknown), ", ".join(stages)))
    out = {}
    for stage in stages:
        value = labels.get(stage)
        if not isinstance(value, str) or not value.strip():
            raise SettingsError("%s.%s: non-empty board label expected" % (where, stage))
        out[stage] = value.strip()
    return out


def _process_mapping(project: str, spec) -> projection_mod.ProcessMapping:
    """#2130/D : compile un mapping admis (données seules, schéma fermé)."""
    where = "`github.process.%s`" % project
    if not isinstance(spec, dict):
        raise SettingsError("%s: object expected" % where)
    extra = sorted(set(map(str, spec)) - set(PROCESS_KEYS))
    if extra:
        raise SettingsError("%s: unknown key(s) %s (allowed: %s)"
                            % (where, ", ".join(extra), ", ".join(PROCESS_KEYS)))
    ref = spec.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        raise SettingsError("%s.ref: Process revision reference expected" % where)
    version = spec.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise SettingsError("%s.version: integer >= 0 expected" % where)
    document = spec.get("document")
    if not isinstance(document, dict):
        raise SettingsError("%s.document: Process document (data only) expected" % where)
    try:
        artifact = workflow_mod.compile_workflow(document, ref=ref.strip(), version=version)
    except workflow_mod.WorkflowRefusal as exc:
        raise SettingsError("%s.document: %s" % (where, exc)) from exc
    return projection_mod.ProcessMapping(
        project=project, ref=artifact.workflow_ref, version=artifact.workflow_version,
        spec=artifact, labels=_board_labels(project, spec, artifact))


def _process_mappings(project: str, value) -> tuple:
    """Un mapping, ou une liste : un transfert admet deux révisions à la fois."""
    items = value if isinstance(value, list) else [value]
    if not items:
        raise SettingsError("`github.process.%s`: at least one mapping expected" % project)
    out: list = []
    for spec in items:
        mapping = _process_mapping(project, spec)
        if any(m.ref == mapping.ref and m.version == mapping.version for m in out):
            raise SettingsError(
                "`github.process.%s`: revision %s@%d admitted twice "
                "(projection-ambiguous)" % (project, mapping.ref, mapping.version))
        out.append(mapping)
    return tuple(out)


def parse_settings(raw) -> Settings:
    """Valide la clé `github` (SettingsError : message actionnable)."""
    if raw is None or raw == {}:
        return Settings()
    if not isinstance(raw, dict):
        raise SettingsError("`github`: JSON object expected")
    unknown = sorted(set(map(str, raw)) - set(SETTINGS_KEYS))
    if unknown:
        raise SettingsError("`github`: unknown key(s) %s (allowed: %s)"
                            % (", ".join(unknown), ", ".join(SETTINGS_KEYS)))
    declared = raw.get("projects")
    declared = {} if declared is None else declared
    if not isinstance(declared, dict):
        raise SettingsError("`github.projects`: object {\"project\": \"owner/repo\"} expected")
    projects: dict = {}
    workflows: dict = {}
    bases: dict = {}
    for name, value in declared.items():
        name = str(name).strip()
        where = "`github.projects.%s`" % name
        if not name:
            raise SettingsError("`github.projects`: empty project name")
        if any(other.casefold() == name.casefold() for other in projects):
            raise SettingsError("%s: project declared twice (case-insensitive)" % where)
        files = None
        declared_bases = None
        if isinstance(value, dict):
            extra = sorted(set(map(str, value)) - set(PROJECT_KEYS))
            if extra:
                raise SettingsError("%s: unknown key(s) %s (allowed: %s)"
                                    % (where, ", ".join(extra), ", ".join(PROJECT_KEYS)))
            files = value.get("workflows")
            declared_bases = value.get("base")
            value = value.get("repo")
        if not isinstance(value, str) or not _REPO_RE.fullmatch(value.strip()):
            raise SettingsError("%s: repository \"owner/repo\" expected, not %r" % (where, value))
        projects[name] = value.strip()
        if files is not None:
            where_files = "%s.workflows" % where
            if not isinstance(files, list) or not files:
                raise SettingsError("%s: non-empty list of workflow file names expected"
                                    % where_files)
            names = []
            for item in files:
                if not isinstance(item, str) or not item.strip():
                    raise SettingsError("%s: workflow file name expected, not %r"
                                        % (where_files, item))
                filename = item.strip().rsplit("/", 1)[-1]
                if not filename.endswith(WORKFLOW_SUFFIXES):
                    raise SettingsError("%s: %r is not a workflow file name (…%s)"
                                        % (where_files, item, "/".join(WORKFLOW_SUFFIXES)))
                if filename not in names:
                    names.append(filename)
            key = projects[name]
            workflows[key] = tuple(dict.fromkeys(workflows.get(key, ()) + tuple(names)))
        if declared_bases is not None:
            where = "%s.base" % where
            items = [declared_bases] if isinstance(declared_bases, str) else declared_bases
            if not isinstance(items, list) or not items:
                raise SettingsError("%s: branch name or non-empty list expected "
                                    "(\"develop\" or [\"main\", \"develop\"])" % where)
            names = []
            for item in items:
                if not isinstance(item, str) or not item.strip() or item.strip() != item \
                        or any(ch.isspace() for ch in item):
                    raise SettingsError("%s: branch name expected, not %r" % (where, item))
                if item not in names:
                    names.append(item)
            key = projects[name]
            bases[key] = tuple(dict.fromkeys(bases.get(key, ()) + tuple(names)))
    host = raw.get("host")
    if host is not None and not isinstance(host, str):
        raise SettingsError("`github.host`: host name expected")
    interval = raw.get("interval", DEFAULT_INTERVAL)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) \
            or interval < MIN_INTERVAL:
        raise SettingsError("`github.interval`: a number of seconds, at least %d"
                            % MIN_INTERVAL)
    ci_ttl = raw.get("ci_ttl", DEFAULT_CI_TTL)
    if isinstance(ci_ttl, bool) or not isinstance(ci_ttl, (int, float)) \
            or ci_ttl < MIN_CI_TTL:
        raise SettingsError("`github.ci_ttl`: a number of seconds, at least %d"
                            % MIN_CI_TTL)
    terms = raw.get("exclude_terms")
    terms = [] if terms is None else terms
    if not isinstance(terms, list) or not all(isinstance(t, str) and t.strip() for t in terms):
        raise SettingsError("`github.exclude_terms`: list of non-empty terms expected")
    declared_process = raw.get("process")
    if declared_process is None:
        declared_process = {}
    if not isinstance(declared_process, dict):
        raise SettingsError("`github.process`: object {project: mapping} expected")
    process: dict = {}
    for name, value in declared_process.items():
        key = str(name).strip()
        canonical = next((p for p in projects if p.casefold() == key.casefold()), None)
        if canonical is None:
            raise SettingsError("`github.process.%s`: project not declared in "
                                "`github.projects`" % name)
        if canonical in process:
            raise SettingsError("`github.process`: project %s declared twice (case-insensitive)"
                                % canonical)
        process[canonical] = _process_mappings(canonical, value)
    return Settings(projects=projects, host=(host or "").strip() or None,
                    interval=float(interval), ci_ttl=float(ci_ttl),
                    exclude_terms=tuple(dict.fromkeys(t.strip() for t in terms)),
                    workflows=workflows, bases=bases, process=process)


def settings_of(cfg) -> Settings:
    """Les réglages `github` de la configuration de l'hôte (vides sans la clé)."""
    return parse_settings(getattr(cfg, "github", None) or {})


# -- contrôle de ce qui est publié -------------------------------------------

#: motifs de secret : jamais publiés, quel que soit le dépôt
SECRET_PATTERNS = tuple((label, re.compile(rx)) for label, rx in (
    ("GitHub token", r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    ("API key", r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),
    ("AWS key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("Scaleway key", r"\bSCW[0-9A-Z]{17}\b"),
    ("Google key", r"\bAIza[0-9A-Za-z_-]{35}"),
    ("Slack token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("npm or PyPI token", r"\bnpm_[A-Za-z0-9]{36}\b|\bpypi-[A-Za-z0-9_-]{40,}"),
    ("webhook", r"(?i)hooks\.slack\.com/services/[A-Za-z0-9/_-]+"
                r"|discord(?:app)?\.com/api/webhooks/[A-Za-z0-9/_-]+"),
    ("private key", r"-----BEGIN [A-Z0-9 -]*PRIVATE KEY"),
    ("JWT token", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ("credentials in a URL", r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@"),
    ("authorization token", r"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]{20,}"
                             r"|\bauthorization[ \t]*:[ \t]*(?:basic|token)[ \t]+\S{8,}"),
    ("plaintext secret", r"(?i)(?<![a-z0-9])(?:password|passwd|pwd|mot[ _-]de[ _-]passe"
                        r"|secret|token|jeton|api[_-]?key|access[_-]?key|private[_-]?key"
                        r"|client[_-]?secret)[\"']?[ \t]*[:=][ \t]*[\"']?"
                        r"(?=[^\s\"']*[0-9])(?=[^\s\"']*[A-Za-z])[^\s\"']{12,}"),
))

#: ce qu'un dépôt PUBLIC ne reçoit jamais, en plus des secrets et des termes
#: exclus : adresses, chemins locaux, identifiants de compte, noms d'hôte
PUBLIC_PATTERNS = tuple((label, re.compile(rx)) for label, rx in (
    ("IP address", r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
                   r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?![\w.])"),
    ("IP address", r"(?i)(?<![\w:])(?:(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}"
                   r"|[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?"
                   r"|::[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)(?![\w:])"),
    ("email address", r"(?i)(?<![\w.%+-])[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*"
                             r"\.[a-z]{2,}\b"),
    ("local path", r"(?<![\w.~/-])~[\w.-]*/[\w.~/-]*"
                     r"|(?<![\w.:/-])/(?:home|Users|root|tmp|var|etc|srv|opt|mnt|media|run"
                     r"|usr|private|Volumes|data|proc|sys|dev|nix|snap)/[\w.~/-]*"
                     r"|(?<!\w)[A-Za-z]:\\\S+"),
    ("account identifier", r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}"
                              r"-[0-9a-f]{12}\b|\borg-[A-Za-z0-9]{20,}\b|\bacct_[A-Za-z0-9]{8,}"),
    ("host name", r"(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:local|lan|internal|intranet|intra"
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
                add("secret pattern (%s)" % label)
        if any(_term_re(term).search(text) for term in self.exclude_terms):
            add("term excluded by the organization")
        if not public:
            return found
        for label, rx in PUBLIC_PATTERNS:
            if rx.search(text):
                add(label)
        if any(_term_re(name).search(text) for name in self.host_names):
            add("host name")
        for match in _URL_HOST_RE.finditer(text):
            host = match.group(1).casefold().rstrip(".")
            if not any(host == ok or host.endswith("." + ok) for ok in self.url_hosts):
                add("host name")
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
    """Le projet d'un lot, comme `ameesh projects` (L62) : `app`, `workstream`,
    équipe de sa fiche du plan (à défaut, son équipe : file d'amélioration,
    L119), projet (équipe ou `workstream`) de son assigné."""
    agent = projects_mod.agent_project({"team": item.get("assignee_team"),
                                        "workstream": item.get("assignee_workstream"),
                                        "chantier": item.get("assignee_chantier")})
    lot = dict(item, package_team=item.get("package_team") or item.get("team"))
    return projects_mod.lot_project(lot, {item.get("assignee") or "": agent})


def needs_issue(item: dict, mapping=None) -> bool:
    """Un lot sans issue_ref : son issue reste à créer.

    #2130/D : pour un lot pinné, « ouvert » est l'état PROJETÉ (le cœur
    `merged` reste ouvert) ; sans mapping admis, aucune issue neuve — jamais la
    règle legacy sur l'état de la ligne.
    """
    if (item.get("issue_ref") or "").strip():
        return False
    if projection_mod.pinned(item):
        try:
            return projection_mod.project_issue(item, mapping).state == "open"
        except projection_mod.ProjectionRefusal:
            return False
    return item.get("state") not in LOT_TERMINAL


def _priority(item: dict) -> int | None:
    try:
        value = int(item.get("priority"))
    except (TypeError, ValueError):
        return None
    return value if value in PRIORITY_LABELS else None


def lot_labels(item: dict, *, stage_label: str | None = None) -> list[str]:
    """`ameesh`, l'état — ou le libellé d'étape admis d'un lot pinné (#2130/D) —
    `ameesh:<type>`, `ameesh:p<priorité>`."""
    labels = {BASE_LABEL}
    if stage_label:
        labels.add(stage_label)
    elif item.get("state"):
        labels.add("%s:%s" % (BASE_LABEL, lifecycle.phase(item)))
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
    if state == lifecycle.CLOSED:
        # #988 : livré hors cible = terminé, pas « non prévu »
        return "closed", ("completed" if item.get("close_reason") == "delivered"
                          else "not_planned")
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
    #: #2130/D : refus de projection d'un lot pinné (codes stables) ; non vide,
    #: l'issue n'est touchée par AUCUN champ (l'appelant saute le lot).
    projection_refused: list[str] = field(default_factory=list)


def _assignee_text(item: dict, public: bool, guard: Guard) -> str:
    who = (item.get("assignee") or "").strip()
    if not who:
        return "unassigned"
    if who.startswith(work_mod.HUMAN_PREFIX):
        return "assigned to a human" if public else "assigned to `%s`" % who
    if guard.problems(who, public=public):
        return "assigned to an agent"
    return "assigned to `%s`" % who


def _lot_head(item: dict, *, public: bool, guard: Guard, project: str,
              minimal: bool = False) -> list[str]:
    parts = ["**ameesh work item %d**" % int(item["id"]),
             _TYPE_FR.get(item.get("type"), item.get("type") or "work item")]
    priority = _priority(item)
    if priority:
        parts.append(_PRIORITY_FR[priority])
    if minimal:
        parts.append("assigned" if item.get("assignee") else "unassigned")
    else:
        parts.append(_assignee_text(item, public, guard))
    lines = [" · ".join(parts)]
    planning = _plan_line(item)
    if planning:
        lines.append(planning)
    if not public and not minimal:
        extra = ["project `%s`" % project] if project else []
        if (item.get("workstream") or "").strip():
            extra.append("workspace `%s`" % item["workstream"].strip())
        if item.get("package_id"):
            extra.append("plan package `%s`" % item["package_id"])
        if extra:
            text = " · ".join(extra)
            lines.append(text[0].upper() + text[1:])
    return lines


def _day_fr(day: str | None) -> str | None:
    """« 2026-10-12 » → « 2026-10-12 » (le jour ISO, lisible en anglais)."""
    text = str(day or "")[:10]
    parts = text.split("-")
    return "-".join(parts) if len(parts) == 3 else None


def _plan_line(item: dict) -> str | None:
    """L157 : la ligne de planification de l'en-tête — durée estimée, dates
    prévues (L96) et, une fois le lot livré, la durée réelle et l'écart.
    Aucun nom ni aucune source : des nombres et des dates seulement. None
    sans estimation ni date prévue (l'issue reste inchangée)."""
    from ... import estimates

    minutes = item.get("estimate_minutes")
    dates = [(label, _day_fr(item.get(key))) for label, key in (
        ("start", "planned_start"), ("end", "planned_end"),
        ("delivery", "planned_delivery"))]
    dates = [(label, day) for label, day in dates if day]
    if not minutes and not dates:
        return None
    parts = ["Estimated duration %s" % estimates.label(minutes) if minutes
             else "Duration not estimated"]
    if dates:
        parts.append("planned: " + ", ".join("%s %s" % pair for pair in dates))
    if item.get("merged_ts") is not None and item.get("state") in work_mod.MERGED_STATES:
        v = estimates.view(item, merged_ts=item.get("merged_ts"))
        if v.get("actual_minutes") is not None:
            text = "actual %s" % estimates.label(v["actual_minutes"])
            if v.get("gap_minutes") is not None:
                text += " (%s%s, %s)" % ("+" if v["gap_minutes"] >= 0 else "−",
                                        estimates.label(abs(v["gap_minutes"])),
                                        estimates.ratio_text(v["ratio"]))
            parts.append(text)
    return " · ".join(parts)


def _lot_footer(item: dict) -> str:
    n = int(item["id"])
    return ("---\n_Issue maintained by ameesh from work item %d: its title, state and "
            "`ameesh:…` labels follow the work item; a change made here is not "
            "imported into ameesh. A PR that delivers this work item carries "
            "`ameesh-work: %d` and `Closes #<number of this issue>`._" % (n, n))


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
                withheld.append({"field": "public summary", "reasons": reasons})
            else:
                middle.append(_quiet(summary))
    else:
        text = _clean(item.get("body"), _WORK_MARKER_RE)
        if text:
            reasons = guard.problems(text, public=False)
            if reasons:
                withheld.append({"field": "body", "reasons": reasons})
                middle.append("_The work item body is not published here (%s): see `ameesh "
                              "work show %d`._" % (", ".join(reasons), n))
            else:
                middle.append(_quiet(_cut(text, BODY_MAX)))

    def join(head: list[str], parts: list[str]) -> str:
        return "\n\n".join(["\n".join(head)] + parts + [_lot_footer(item)])

    body = join(_lot_head(item, public=public, guard=guard, project=project), middle)
    reasons = guard.problems(body, public=public)
    if not reasons:
        return body, withheld
    # un nom de l'en-tête (assigné, projet, chantier, fiche) ne passe pas
    withheld.append({"field": "header", "reasons": reasons})
    minimal = _lot_head(item, public=public, guard=guard, project=project, minimal=True)
    for parts in (middle, []):
        body = join(minimal, parts)
        if not guard.problems(body, public=public):
            return body, withheld
    return None, withheld


def _closing(item: dict, *, repo: str, public: bool, issue_of,
             core: str | None = None) -> str | None:
    """Le commentaire de fermeture : pourquoi le lot est terminé.

    `core` (#2130/D) : le cœur décidé par la projection d'un lot pinné, quand
    il diffère de l'état de la ligne (retenue, ou cœur projeté)."""
    n = int(item["id"])
    state = core or lifecycle.phase(item)
    if state == "merged":
        pr = _ref_parts(item.get("pr_ref"))
        where = ""
        if pr and pr[0].casefold() == repo.casefold():
            where = " by PR #%d" % pr[1]
        elif pr and not public:
            where = " by PR %s#%d" % pr
        return "Work item %d merged%s: issue closed by ameesh." % (n, where)
    if state == "promoted":
        return "Work item %d delivered: issue closed by ameesh." % n
    if state == lifecycle.CLOSED:
        if item.get("close_reason") == "delivered":
            return "Work item %d delivered outside the target: issue closed by ameesh." % n
        other = item.get("superseded_by")
        if item.get("close_reason") == "superseded" and other:
            number = issue_of(int(other))
            return ("Work item %d superseded by work item %d%s: issue closed by ameesh."
                    % (n, int(other), " (#%d)" % number if number else ""))
        if item.get("close_reason") == "done":
            return "Work item %d delivered: issue closed by ameesh." % n
        return "Work item %d abandoned: issue closed by ameesh." % n
    return None


def lot_view(item: dict, *, public: bool, guard: Guard, project: str, repo: str,
             issue_of=lambda _lot: None, mapping=None) -> LotView:
    """L'issue voulue pour le lot `item` dans `repo` (public ou privé).

    #2130/D : un lot pinné (`workflow_ref`) est projeté par le mapping admis de
    son couple exact (ref, version) — état, libellé de board et commentaire de
    fermeture viennent de l'artefact, jamais de l'état legacy. Sans mapping, ou
    sur un fait manquant/ambigu, `projection_refused` porte le code et aucun
    champ désiré n'est calculé : l'appelant laisse l'issue intacte.
    """
    title = _cut(" ".join(str(item.get("title") or "").split()), TITLE_MAX) \
        or "Work item %d" % int(item["id"])
    refused = guard.problems(title, public=public)
    body, withheld = _compose(item, public=public, guard=guard, project=project)
    if body is None:
        refused = refused + [r for part in withheld for r in part["reasons"]
                             if r not in refused]
        body = ""
    core = None
    if projection_mod.pinned(item):
        try:
            projected = projection_mod.project_issue(item, mapping)
        except projection_mod.ProjectionRefusal as exc:
            return LotView(item_id=int(item["id"]), title=title, body=body, labels=[],
                           state="", state_reason=None, closing=None,
                           refused=refused, withheld=withheld,
                           projection_refused=[exc.code])
        state, reason, core = projected.state, projected.state_reason, projected.core
        labels = lot_labels(item, stage_label=projected.label)
    else:
        state, reason = lot_issue_state(item)
        labels = lot_labels(item)
    closing = _closing(item, repo=repo, public=public, issue_of=issue_of, core=core)
    if closing and guard.problems(closing, public=public):
        closing = "Work item %d finished: issue closed by ameesh." % int(item["id"])
    return LotView(item_id=int(item["id"]), title=title, body=body, labels=labels,
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


def _foreign_labels(issue: dict, managed=None) -> list[str]:
    """Les étiquettes qui ne sont pas à ameesh : jamais touchées."""
    names = [(lab.get("name") if isinstance(lab, dict) else str(lab))
             for lab in issue.get("labels") or []]
    return sorted(n for n in names if n and not _managed_label(n, managed))


def _three_way(view: LotView, issue: dict, managed=None) -> tuple[dict, list, list, list]:
    """(champs voulus, à écrire, changés dans GitHub, gardés tels quels).

    Les empreintes du marqueur disent ce qu'ameesh a écrit la dernière fois.
    Un champ que le lot a changé depuis est écrit ; un champ qu'un humain a
    changé dans GitHub, et que le lot n'a pas changé, est gardé. Une issue
    sans empreintes (désignée par l'issue_ref, sans marqueur) est adoptée :
    seules les étiquettes `ameesh` y sont posées. `managed` (#2130/D) : les
    libellés de board admis du projet sont à nous, comme `ameesh:*`."""
    want = _fields(view.title, view.body, view.labels, view.state, _WORK_MARKER_RE,
                   managed=managed)
    have = _fields(issue.get("title"), issue.get("body"), issue.get("labels"),
                   issue.get("state"), _WORK_MARKER_RE, managed=managed)
    recorded = (issue.get("_recorded") or "").split(".") if issue.get("_recorded") else []
    if len(recorded) == len(_FIELD_NAMES):
        ours = [k for k, d in zip(_FIELD_NAMES, recorded) if _digest(want[k]) != d]
        human = [k for k, d in zip(_FIELD_NAMES, recorded) if _digest(have[k]) != d]
    else:
        # Issue désignée et adoptée : rien n'a encore été écrit, donc aucune
        # empreinte. Seules les étiquettes sont à nous — plus la fermeture :
        # un lot déjà clos ferme son issue dans ce passage même, sinon le
        # commentaire « issue closed by ameesh » contredirait une issue restée
        # ouverte et l'empreinte figerait cet écart en retouche humaine (#3086).
        ours, human = ["labels"], []
        if (want["state"] or "").lower() == "closed" \
                and (have["state"] or "").lower() != "closed":
            ours.append("state")
    write = [k for k in ours if want[k] != have[k]]
    kept = [k for k in human if k not in ours]
    return want, write, human, kept


def _label_description(name: str) -> str:
    key = name.split(":", 1)[1] if ":" in name else ""
    if key in _STATE_FR:
        return "ameesh work item: %s" % _STATE_FR[key]
    if key in _TYPE_FR:
        return "ameesh work item: %s" % _TYPE_FR[key]
    for priority, label in PRIORITY_LABELS.items():
        if key == label:
            return "ameesh work item: %s" % _PRIORITY_FR[priority]
    return "issue maintained by ameesh"


def project_lots(db, gh, project: str, repo: str, *, dry_run: bool = False,
                 settings: Settings | None = None, mappings: Iterable = (),
                 feed: list | None = None,
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
        raise GithubError("empty project: --app <project>")
    settings = settings or Settings()
    st = storage.of(db)
    feed = list(st.work.issue_feed(FEED_LIMIT) if feed is None else feed)
    gh_repo = _Repo(gh, repo, dry_run)
    visibility = repo_visibility(gh_repo, cache=cache, now=now)
    public = visibility != "private"
    report: dict = {"project": project, "repo": repo, "visibility": visibility,
                    "public": public, "dry_run": bool(dry_run), "issues": [],
                    "labels_created": [], "refused": [], "withheld": [], "human_edits": [],
                    "issue_refs": [], "duplicates": [], "orphans": [], "errors": [],
                    "projection_refused": [], "comments": []}
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
                "detail": "work item unknown to this mesh: issue left as is"})
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
    # #2130/D : l'ensemble géré est celui du PROJET — l'union des libellés de
    # toutes les révisions admises. Un transfert admet deux révisions : le
    # libellé de l'ancienne est à nous et doit céder la place au nouveau, pas
    # rester comme une étiquette étrangère. `mapping` reste celui du lot.
    managed_set = set()
    for admitted in mappings or ():
        managed_set.update(admitted.labels.values())
    managed = managed_set or None
    refused_lots: set = set()
    stage_closings: dict = {}
    for item, issue in mine:
        lot_id = int(item["id"])
        mapping = projection_mod.mapping_for(mappings, item)
        view = lot_view(item, public=public, guard=guard, project=project, repo=repo,
                        issue_of=issue_of, mapping=mapping)
        if view.projection_refused:
            # #2130/D : fait manquant/ambigu — l'issue n'est touchée par aucun
            # champ, et l'état legacy ne sert JAMAIS de repli. Le lot est aussi
            # exclu de la pose d'`issue_ref` en fin de passe (aucune écriture
            # native : un mapping corrigé reprend sans autre geste).
            refused_lots.add(lot_id)
            report["projection_refused"].append({
                "work_item": lot_id, "number": int(issue["number"]) if issue else None,
                "codes": list(view.projection_refused),
                "ref": str(item.get("workflow_ref") or ""),
                "version": item.get("workflow_version") or 0,
                "stage": item.get("workflow_stage")})
            report["issues"].append({"work_item": lot_id,
                                     "number": int(issue["number"]) if issue else None,
                                     "action": "refused-projection", "changes": [],
                                     "codes": list(view.projection_refused)})
            continue
        if item.get("state") == "closed" and view.closing:
            stage_closings[lot_id] = view.closing.replace(" delivered", " completed")
        if issue is None and view.state == "closed":
            continue                    # jamais d'issue neuve pour un lot terminé
        for part in view.withheld:
            report["withheld"].append(dict(part, work_item=lot_id))
        entry = {"work_item": lot_id, "number": int(issue["number"]) if issue else None,
                 "title": None if view.refused else view.title, "state": view.state,
                 "labels": view.labels, "action": "unchanged", "changes": []}
        report["issues"].append(entry)
        if view.refused:
            report["refused"].append({"work_item": lot_id, "field": "title",
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
            want = _fields(view.title, view.body, view.labels, view.state, _WORK_MARKER_RE,
                           managed=managed)
            entry["action"] = "create"
            plans.append({"entry": entry, "item": item, "labels": view.labels,
                          "payload": {"title": view.title,
                                      "body": _lot_marker(lot_id, want) + "\n" + view.body,
                                      "labels": view.labels}})
            continue
        if view.refused:
            view.title = (issue.get("title") or "").strip()   # titre gardé tel quel
        want, write, human, kept = _three_way(view, issue, managed=managed)
        if mapping is not None and view.state == "closed" and "state" not in write \
                and (issue.get("state") or "").lower() == "closed" \
                and (issue.get("state_reason") or "") != (view.state_reason or ""):
            # #2130/D : l'issue est déjà fermée, mais pour une autre raison
            # (fermeture legacy au mauvais motif) : la raison se corrige, sans
            # commentaire — état et raison restent cohérents.
            write = list(write) + ["state"]
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
            payload["labels"] = _foreign_labels(issue, managed) + view.labels
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
        entry["changes"] = [_LOT_FIELD_FR[k] for k in write] or ["digests"]
        plans.append({"entry": entry, "item": item, "payload": payload,
                      "labels": view.labels if "labels" in write else []})
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
        # désigne, sauf si elle désigne déjà une issue existante de ce dépôt.
        # #2130/D : un lot pinné refusé n'écrit rien, pas même son issue_ref
        # (le refus n'est pas une adoption, et l'état legacy ne décide pas).
        for item, issue in mine:
            if int(item["id"]) in refused_lots:
                continue
            ref = _ref_parts(item.get("issue_ref"))
            if issue is None or (ref and ref[0].casefold() == repo.casefold()
                                 and ref[1] in by_number):
                continue
            _remember_ref(db, st, gh_repo, report, item, int(issue["number"]), created=False)
    from ... import github_stage_comments
    github_stage_comments.project(db, st, gh_repo, report, mine, guard=guard,
                                  refused=refused_lots, project=project, closings=stage_closings)
    return _tally_lots(report)


def _execute(db, st, gh_repo: "_Repo", report: dict, plan: dict) -> None:
    """Écrit une issue planifiée ; une erreur de GitHub est notée, jamais levée."""
    entry = plan["entry"]
    try:
        if entry["action"] == "create":
            created = gh_repo.create(plan["payload"])
            if not created.get("number"):
                raise GithubError("created without a returned number")
            entry["number"] = int(created["number"])
            _remember_ref(db, st, gh_repo, report, plan["item"], entry["number"], created=True)
            return
        gh_repo.update(int(entry["number"]), plan["payload"])
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
                "detail": "existing issue_ref kept; issue %s is found by its "
                          "marker" % ref})
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
                "body": (_DUPLICATE_MARKER % lot_id) + "\n_Duplicate of #%d, which tracks "
                        "work item %d._" % (other[1], lot_id)})
            gh_repo.comment(int(number), "Duplicate of #%d, created at the same time for "
                                         "the same work item: closed by ameesh."
                                         % other[1])
        except GithubError as exc:
            report["errors"].append({"work_item": lot_id, "number": int(number),
                                     "action": "duplicate", "detail": _one_line(exc)})
        report["duplicates"].append({"work_item": lot_id, "number": int(number),
                                     "kept": other[1], "closed": True})
        item["issue_ref"] = fresh
        return
    report["issue_refs"].append({"work_item": lot_id, "issue_ref": fresh or None,
                                 "result": "changed",
                                 "detail": "issue_ref changed in the meantime: kept"})


def _tally_lots(report: dict) -> dict:
    actions = [row["action"] for row in report["issues"] if not row.get("error")]
    report["counts"] = {
        "created": actions.count("create"), "updated": actions.count("update"),
        "closed": actions.count("close"), "unchanged": actions.count("unchanged"),
        "refused": actions.count("refused"), "deferred": actions.count("deferred"),
        "projection_refused": actions.count("refused-projection"),
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
                       "GitHub issues: invalid configuration, projection stopped: %s" % exc)
            return []
        if not settings.projects:
            return []
        here = getattr(self.cfg, "host", None)
        if not role_run.host_authorized(self.cfg, settings.host):
            if not settings.host:
                self._once("no-host", "GitHub issues: no designated host (`github.host`): no "
                                      "automatic projection; `ameesh work project-github --app "
                                      "<project>` remains possible")
            else:
                self._once(("elsewhere", settings.host),
                           "GitHub issues: the projection is run by host %s, not %s"
                           % (settings.host, here))
            return []
        reports: list[dict] = []
        feed = None
        for name, repo in sorted(settings.projects.items(), key=lambda kv: kv[0].casefold()):
            if now < self.backoff.get(name, 0.0):
                continue
            mappings = settings.process_for(name)
            last = self.last.get(name)
            try:
                if last is not None and now - last < settings.interval:
                    if self.dry_run:
                        continue
                    if feed is None:
                        feed = storage.of(db).work.issue_feed(FEED_LIMIT)
                    if not any(needs_issue(i, projection_mod.mapping_for(mappings, i))
                               and (int(i["id"]), _digest(i.get("title") or "")) not in self.skip
                               and (lot_project(i) or "").casefold() == name.casefold()
                               for i in feed):
                        continue
                if feed is None:
                    feed = storage.of(db).work.issue_feed(FEED_LIMIT)
                if self.gh is None:
                    self.gh = Gh()
                report = project_lots(db, self.gh, name, repo, dry_run=self.dry_run,
                                      settings=settings, mappings=mappings, feed=feed,
                                      cache=self.cache,
                                      forge_hosts=getattr(self.cfg, "forge_hosts", None) or (),
                                      local_host=here, now=now)
            except Exception as exc:  # noqa: BLE001 - GitHub, gh, base : jamais fatal
                self.backoff[name] = now + settings.interval
                self._once(("error", name, _one_line(exc)),
                           "GitHub issues %s (%s): failed, retrying in %d s: %s"
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
            ("created", "to create" if dry else "created"),
            ("updated", "to update" if dry else "updated"),
            ("closed", "to close" if dry else "closed"),
            ("deferred", "deferred to the next pass"),
            ("projection_refused", "refused (Process projection)"),
            ("errors", "in error")) if counts.get(key)]
        if parts:
            line = "GitHub issues %s: %s%s" % (where, ", ".join(parts), " [dry run]" if dry else "")
            if dry:
                self._once(("dry", line), line)
            else:
                self.log(line)
        for row in report["refused"]:
            self.skip.add((row["work_item"], row["title_digest"]))
            self._once(("refused", row["work_item"], row["title_digest"], tuple(row["reasons"])),
                       "GitHub issues %s: work item %d, %s refused (%s): %s; fix the work item "
                       "title" % (where, row["work_item"], row["field"],
                                  ", ".join(row["reasons"]),
                                  "title of #%d kept as is" % row["number"] if row["number"]
                                  else "not published"))
        for row in report["projection_refused"]:
            self._once(("proj", report["repo"], row["work_item"], tuple(row["codes"])),
                       "GitHub issues %s: work item %d pinned on %s@%s refused (%s): %s "
                       "left unchanged; fix the admitted Process mapping"
                       % (where, row["work_item"], row["ref"] or "(none)", row["version"],
                          ", ".join(row["codes"]),
                          "#%d" % row["number"] if row["number"] else "no issue created"))
        for row in report["withheld"]:
            self._once(("withheld", row["work_item"], row["field"], tuple(row["reasons"])),
                       "GitHub issues %s: work item %d, %s not published (%s)"
                       % (where, row["work_item"], row["field"], ", ".join(row["reasons"])))
        for row in report["human_edits"]:
            if row["kept"]:
                self._once(("human", report["repo"], row["number"], tuple(row["kept"])),
                           "GitHub issues %s: #%d (work item %d) edited in GitHub (%s): "
                           "kept as is, not imported into ameesh"
                           % (where, row["number"], row["work_item"], ", ".join(row["kept"])))
        for row in report["errors"]:
            self._once(("write", report["repo"], row["work_item"], row["detail"]),
                       "GitHub issues %s: work item %d, %s failed: %s"
                       % (where, row["work_item"], row["action"], row["detail"]))
        for row in report["duplicates"]:
            self._once(("dup", report["repo"], row["number"]),
                       "GitHub issues %s: #%d duplicate of #%d (work item %d)%s"
                       % (where, row["number"], row["kept"], row["work_item"],
                          ": closed" if row.get("closed") else ": left as is"))
