# SPDX-License-Identifier: AGPL-3.0-only
"""Fusion d'un lot constatée par le CONTENU (lot L29), dans un clone git local.

    ameesh work sync-merges --git-dir D [--target T] [--repo owner/repo]
                            [--dry-run] [--json]

Sans `--target` (#1005), la cible est celle du lot (`branch_target`), sinon
celle du dépôt (`git config ameesh.target`), sinon sa branche par défaut
(`origin/HEAD`, puis main/master) ; quand rien n'est déclaré et qu'elle ne
porte pas le gel, les refs d'intégration reconnues (`develop`, `main`,
`master`, `release/…`) sont essayées et la fermeture nomme celle qui a livré.

Le nom d'un lot change (branche renommée entre gel et fusion), la fusion se
fait par rebase, squash ou cherry-pick sans commit de fusion reconnaissable :
le nom ne prouve rien. Seul le **dernier gel** d'un lot fait autorité (le
jalon `frozen` le plus récent de L10, avec son commit) : un gel antérieur
déjà fusionné ne prouve rien sur le travail courant. Pour chaque lot ouvert
qui a un tel gel, la fusion est constatée, dans cet ordre :

1. **ancêtre** : le commit du dernier gel est ancêtre de la branche cible —
   sauf si un revert postérieur en a annulé le contenu (#1825 : un paquet
   fusionné puis retiré par `git revert` laisse la tête ancêtre de la cible
   alors que le contenu n'y est plus). Détection au mieux : un revert hors
   de la fenêtre lue (MAX_REVERT_SCAN) ou sans `patch-id --verbatim` n'est
   pas vu, et l'ancêtre ferme alors comme avant #1825 ;
2. **patch-id exact** (rebase, cherry-pick) : chaque commit du gel absent
   de la cible (`cible..gel`, hors fusions) a son `git patch-id --verbatim`
   parmi ceux des commits de la cible depuis leur base commune ; ou
   (**squash**) le diff entier du gel (base..gel) a le patch-id d'un commit
   de la cible. Dans les deux cas, le commit porteur ne doit pas avoir été
   annulé depuis par un revert (#1825). `--verbatim` ne normalise AUCUN
   espace (`--stable` ignore les espaces, même dans une chaîne : `"a b"` et
   `"ab"` y sont égaux) ; seuls les numéros de ligne des en-têtes de bloc
   sont ignorés. Un git sans `--verbatim` (< 2.40) ne prouve que par
   l'ancêtre : faux négatif plutôt que faux positif ;
3. à défaut, si `--repo` est donné, **la PR liée est fusionnée**
   (`plan_github.sync_github`, lecture seule de GitHub).

La fermeture est conditionnée en base au gel examiné : si un nouveau gel est
déclaré entre l'examen et l'écriture, rien n'est fermé (`refrozen`).

#2443 : une unité dont la composition de paquet est enregistrée
(`work_packet_members`) est d'abord prouvée par son commit de composition,
dans la cible (`ameesh-work`, revert #1825 compris) : un jalon `frozen` n'est
pas nécessaire, ce qui couvre le cas #2366. La preuve est écartée si le gel
effectif courant de l'unité n'est plus celui de la composition (re-gel), et le
relevé retombe alors sur le gel déclaré.

Lecture seule du dépôt (`git merge-base`, `rev-list`, `log -p`, `patch-id`) ;
la cible est lue telle que le clone la connaît. Deux exceptions, bornées et
best-effort :

* (#1772) dans un clone partiel (remote *promisor*), le commit d'un gel absent
  localement est rapatrié par UN fetch explicite — le fetch paresseux des
  sondes de présence est coupé (`GIT_NO_LAZY_FETCH`, #1749) — puis la sonde
  conclut ; si le commit reste absent, la preuve d'absence est dite ;
* (#2302) le relevé des branches rafraîchit le clone de l'assigné avant de
  sonder (un fetch par dépôt et par remote, au plus une fois par relevé), et
  lit la cible publiée (`origin/<branche>`) plutôt qu'une branche locale
  périmée. Un clone que personne ne fetch ne fermait jamais les lots livrés
  par un paquet ; si le fetch échoue, le relevé continue sur les refs lues.

#2744 : une fermeture par contenu est retenue tant que le lot est `blocked`
ou qu'une note `hold:` n'est pas levée (le contenu fusionné est constaté, le
journal le dit, le détenteur est réveillé, le lot reste ouvert) ; le contrôle
et la fermeture partagent le verrou de ligne du lot avec les notes hold/lift
(`work.close_merged(content_guard=…)`), et une lecture du journal en erreur
retient aussi. #3219 : les retenues ordinaires de l'assigné (règle 1) ne
retiennent pas — le contenu prouvé les lève et la note de fusion garde la
trace ; celles d'un pilote ou d'un autre acteur retiennent, même recouvertes
par celle de l'assigné et après sa levée (les holds sont un ensemble, pas une
pile). `sync-branches` et les unités de paquet (`sync-packets`) portent la
même garde.

Un lot absorbé par un autre ou remplacé se clôt par
`ameesh work close <id> --superseded-by <id>` ; jamais de réouverture d'un
lot fermé.
"""
from __future__ import annotations

import os
import re
import tempfile

from ... import planned_waits
from ... import storage
from ... import work as work_mod
from ...canon import (FETCH_TIMEOUT, GIT_TIMEOUT, GitDeadline, GitError,
                    NO_LAZY_FETCH, _git, _norm_url, git_fetch)
from ...deadline import Deadline

#: cible historique du relevé de contenu (`--target` explicite) ; sans lui,
#: c'est la cible déclarée du lot ou du dépôt qui fait foi (#1005)
DEFAULT_TARGET = "origin/main"
#: commits de la cible examinés au plus depuis la base commune (borne de coût)
MAX_TARGET_COMMITS = 5000
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


class MergeProbeError(RuntimeError):
    """Dépôt ou cible illisible : message actionnable."""


class _NoVerbatim(Exception):
    """`git patch-id --verbatim` indisponible : aucune preuve par le contenu."""


#: le patch-id exact : aucun espace normalisé (voir le docstring du module)
PATCH_ID = ("patch-id", "--verbatim")

#: #1825 : commits lus au plus après un gel, à la recherche d'un revert (borne
#: de coût) ; plus ancien, un revert n'est pas vu et la preuve d'avant #1825
#: (ancêtre, patch-id) ferme le lot : un faux positif reste possible
MAX_REVERT_SCAN = 200
#: `git revert` écrit « This reverts commit <sha>. » — et, pour une fusion,
#: « This reverts commit <sha>, reversing changes made to <parent>. » (la
#: parente citée est la ligne principale, `-m 1` : l'apport du lot est annulé)
_REVERTED_COMMIT_RE = re.compile(r"This reverts commit ([0-9a-f]{7,64})[,.]")
#: abréviation minimale reconnue dans un message (« tête gelée <sha> »)
_SHORT_SHA = 7


def _patch_id_run(repo: str, data: bytes):
    proc = _git(repo, *PATCH_ID, stdin=data)
    if proc.returncode != 0:
        raise _NoVerbatim(proc.stderr.decode("utf-8", "replace").strip())
    return proc


def _bounded(deadline: Deadline | None) -> dict:
    """#3572 : l'argument `deadline` des aides, seulement quand il est donné
    (les appelants et doublures d'avant gardent leur signature)."""
    return {} if deadline is None else {"deadline": deadline}


def _out(repo: str, *args: str, deadline: Deadline | None = None) -> str | None:
    proc = _git(repo, *args, **_bounded(deadline))
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip()


def _commit(repo: str, rev: str, *, timeout: float | None = None,
            deadline: Deadline | None = None) -> str | None:
    # #1749 : sonde de présence — pas de fetch paresseux (l'échec laisse un
    # transport orphelin derrière lui dans un clone partiel).
    # #2665 : `timeout` laisse l'appelant ramener la sonde au temps qui reste ;
    # le défaut de `canon._git` (GIT_TIMEOUT) vaut quand il est absent.
    bound = {} if timeout is None else {"timeout": timeout}
    proc = _git(repo, "rev-parse", "--verify", "--quiet", rev + "^{commit}",
                env_extra=NO_LAZY_FETCH, **_bounded(deadline), **bound)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip()


#: #2851 : une sonde de présence de commit reste locale. Dans un clone partiel,
#: `rev-parse <absent>^{commit}` lance un fetch paresseux du promisor — mesuré
#: ~10 s vers GitHub, et `GIT_NO_LAZY_FETCH` ne l'empêche pas avec git 2.43.
#: La borne couvre une lecture d'objets locaux ; un dépassement rend None.
PROBE_TIMEOUT_S = 2.0


def local_commit(repo: str, rev: str, *,
                 timeout: float | None = PROBE_TIMEOUT_S,
                 deadline: Deadline | None = None) -> str | None:
    """`rev` résolu en commit complet **depuis les objets locaux** de `repo`.

    Sert aux sondes de présence qui balaient des clones d'autres projets
    (#2851 : le dépôt d'un gel, `lot_class.repo_for_freeze`) : elles ne
    doivent jamais réveiller le réseau. La résolution se fait dans un dépôt
    git vide, sans remote ni promisor, dont le magasin d'objets est celui de
    `repo` (`GIT_OBJECT_DIRECTORY`) : un commit absent — le cas d'un clone
    partiel qui n'a pas la branche — échoue en quelques millisecondes au lieu
    de lancer un fetch. Le commit présent se résout comme d'habitude, y
    compris à travers `objects/info/alternates`.

    Une ref non-SHA (`HEAD`, une branche) n'a pas de sens dans un magasin sans
    refs : on retombe sur `_commit`, borné. Lecture seule, jamais de fetch.

    #3572 : avec ``deadline``, chacune des commandes git de la sonde ne prend
    que le temps qui reste (pas le plafond de 60 s) ; épuisée, elle rend None.
    Sans elle, rien ne change pour les autres appelants."""
    rev = str(rev or "").strip()
    if not rev or rev.startswith("-"):
        return None
    if not commit_id(rev):
        try:
            return _commit(repo, rev, timeout=timeout, **_bounded(deadline))
        except GitDeadline:
            return None
    bound = {} if timeout is None else {"timeout": timeout}
    try:
        objects = _out(repo, "rev-parse", "--git-path", "objects",
                       **_bounded(deadline))
    except Exception:  # noqa: BLE001 - dépôt illisible : la piste est écartée
        return None
    if not objects:
        return None
    objects = os.path.realpath(os.path.join(repo, objects))
    try:
        with tempfile.TemporaryDirectory(prefix="ameesh-objets-") as gitdir:
            # sans GIT_DIR, `git init --bare` prend le dossier courant
            init = _git(gitdir, "init", "--quiet", "--bare", **_bounded(deadline))
            if init.returncode != 0:
                return None
            proc = _git(gitdir, "rev-parse", "--verify", "--quiet",
                        rev + "^{commit}", env_extra={
                            "GIT_DIR": gitdir,
                            "GIT_OBJECT_DIRECTORY": objects,
                            "GIT_ALTERNATE_OBJECT_DIRECTORIES": objects,
                            **NO_LAZY_FETCH}, **_bounded(deadline), **bound)
    except (GitError, GitDeadline, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip()


#: #3399 : borner le relevé d'accessibilité d'un gel (revwalk sur les refs).
REACHABLE_TIMEOUT_S = 30.0
#: #3399 B1 : nombre de pointes heads/tags non locales qu'on accepte de
#: rapatrier pour prouver l'absence ; au-delà, la preuve est impossible.
REMOTE_TIP_FETCH_BUDGET = 16
#: #3399 B1 (cx1) : les autres refs annoncées (`refs/archive/…`,
#: `refs/pull/N/head`, résolutions) gardent aussi des descendants d'un gel
#: après un force-push. Leur ascendance se prouve donc aussi, avec un budget
#: séparé : une forge en annonce des milliers, et au-delà de ce nombre la
#: preuve d'absence reste impossible (`None`, jamais un `False` déduit des
#: refs ignorées).
# #3904 : une seule négociation Git pour les milliers de pointes de PR.
# Le plafond reste explicite ; aucune ref au-delà du budget n'est ignorée.
REMOTE_EXTRA_TIP_FETCH_BUDGET = 4096


def _doubt_reason(exc: BaseException) -> str:
    """Le motif d'un doute de lecture, pour que le refus le dise (#3835).

    ``"timeout"`` est le seul motif qui n'est pas une preuve d'illisibilité :
    un appelant qui le reçoit ne doit pas le présenter comme un dépôt cassé,
    mais comme un relevé qui n'a pas conclu dans le délai."""
    text = str(exc)
    if "timed out" in text:
        return "timeout"
    return text


def ref_reachable(repo: str, rev: str, *,
                  timeout: float | None = REACHABLE_TIMEOUT_S,
                  why: list[str] | None = None) -> bool | None:
    """`rev` est-il contenu par une **ref** de `repo` (#3399) ?

    `True` : au moins une ref locale (`refs/heads`, `refs/remotes`,
    `refs/tags`...) contient le commit. `False` : l'objet est présent mais
    aucune ref ne le contient — un « pendant » laissé par un force-push, qui
    n'est plus accessible par une ref. `None` : dépôt ou SHA illisible (objet
    absent au sens de git, git en échec, délai dépassé) : l'appelant ne conclut
    pas (le gate refuse, la lecture garde le doute).

    #2989 N1 distinguait la présence de l'objet (`freeze_checks.commit_in`,
    un `rev-parse` qui lit un objet pendant) de son accessibilité. #3399 : un
    force-push retire le commit de toute ref sans l'élaguer ; le remplacer ne
    doit donc plus être refusé par la seule présence dans l'ODB. La lecture
    est locale et sans fetch paresseux ; un délai ou un dépôt illisible rend
    `None`, jamais `False`.

    #3835 : `for-each-ref --contains` coûte ~8 s sur un clone de 5 500 refs
    (mesuré sur un clone nexlink ; le commit-graph n'y change rien). `name-rev
    --name-only` élague par date de commit et rend `undefined` pour un ancêtre
    dont l'enfant est daté avant lui (faux négatif prouvé par ameesh-cx1 et
    l'auditeur). `--annotate-stdin` désactive cette coupure : il nomme le
    commit si une ref le contient, et rend le SHA tel quel sinon. On résout
    d'abord la révision en commit complet (une abréviation n'est pas
    substituée et se lirait « absente »), puis on lit l'annotation en ~0,3 s.
    Un SHA rendu inchangé est une absence, sauf si une ref porte exactement ce
    nom de SHA (cas pathologique) : on retombe alors, comme sur un échec de
    `name-rev`, sur `for-each-ref --contains`, la preuve de référence. `why`
    reçoit le motif d'un `None` (`"timeout"` ou le message git) : un délai se
    dit comme tel au lieu d'être confondu avec un dépôt illisible.

    Ne voit que les refs **locales** : les refs distantes non rapatriées
    (toute l'annonce : tags, branches hors refspec, `refs/archive/…`,
    `refs/pull/N/head`) sont couvertes par [`remote_reachable`] (#3399 B1)."""
    rev = str(rev or "").strip()
    if not repo or not rev or rev.startswith("-") or not os.path.isdir(repo):
        if why is not None:
            why.append("repository or commit unreadable")
        return None
    bound = {} if timeout is None else {"timeout": timeout}

    def doubt(exc: BaseException) -> None:
        if why is not None:
            why.append(_doubt_reason(exc))

    # `--annotate-stdin` ne substitue que des SHA complets : une abréviation
    # serait rendue telle quelle et lue à tort comme une absence.
    try:
        full = _commit(repo, rev, timeout=PROBE_TIMEOUT_S)
    except GitError as exc:
        doubt(exc)
        return None
    if not full:
        if why is not None:
            why.append("commit not found")
        return None

    # #3835 : chemin rapide. `name-rev` couvre toutes les refs (y compris
    # `refs/ameesh/…`, `refs/pull/…`), pas seulement heads/tags/remotes, et
    # `--annotate-stdin` désactive l'élagage par date.
    try:
        fast = _git(repo, "name-rev", "--annotate-stdin", "--name-only",
                    stdin=(full + "\n").encode(),
                    env_extra=NO_LAZY_FETCH, **bound)
    except GitError as exc:
        if _doubt_reason(exc) == "timeout":
            doubt(exc)
            return None
        # échec non temporel : la lecture de référence tranche
    except OSError as exc:
        doubt(exc)
        return None
    else:
        if fast.returncode == 0:
            name = fast.stdout.decode("utf-8", "replace").strip()
            if name and name != full:
                return True
            if name == full:
                # Un SHA rendu inchangé est une absence, sauf si une ref dont
                # le nom court est exactement ce SHA le contient : `name-rev`
                # rendrait alors le même texte. Ce seul cas va à la référence.
                clash = _out(repo, "for-each-ref", "--format=%(refname)",
                             "refs/heads/" + full, "refs/" + full)
                if clash is not None and not clash.strip():
                    return False
    # `name-rev` ambigu, vide ou en échec : la lecture de référence tranche.
    try:
        proc = _git(repo, "for-each-ref", "--contains=" + full,
                    "--format=%(refname)", env_extra=NO_LAZY_FETCH, **bound)
    except (GitError, OSError) as exc:
        doubt(exc)
        return None
    if proc.returncode != 0:
        if why is not None:
            why.append("`git for-each-ref --contains` failed (code %d)"
                       % proc.returncode)
        return None
    return bool(proc.stdout.strip())


#: #4255 : filtres de clone partiel qui gardent l'histoire des **commits**.
#: `object:type=<type>` n'omet pas le type demandé mais **tous les autres**
#: (git-rev-list(1)) : `object:type=blob`, `tree` ou `tag` omet donc des
#: commits et coupe l'ascendance. Un filtre absent, inconnu ou combiné
#: (`combine:`) reste un doute : la liste blanche est volontairement close.
_COMMIT_KEEPING_FILTER_RE = re.compile(
    r"^(?:blob:none|blob:limit=\d+[kmgKMG]?|tree:\d+|sparse:oid=\S+"
    r"|object:type=commit)$")


def _partial_clone_keeps_commits(repo: str) -> bool:
    """Les remotes *promisor* de `repo` gardent-ils toute l'histoire ?

    #4255 : le caractère partiel d'un clone ne dit rien de son histoire de
    commits ; c'est le **filtre** qui décide. `blob:none`, `blob:limit`,
    `tree:<profondeur>`, `sparse:oid` et `object:type=commit` n'omettent que
    des blobs, des arbres ou des tags : l'ascendance reste prouvable.
    `object:type=blob|tree|tag` omet tout ce qui n'est pas du type demandé,
    commits compris : le doute refuse. Un promisor sans filtre lisible, ou
    avec un filtre inconnu, rend False. Un dépôt sans promisor est complet.
    """
    proc = _git(repo, "config", "--local", "--get-regexp",
                r"^remote\..*\.promisor$")
    if proc.returncode == 1:        # aucun remote promisor : rien de filtré
        return True
    if proc.returncode != 0:
        return False
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        key = line.split(None, 1)[0]
        if not key.startswith("remote.") or not key.endswith(".promisor"):
            return False
        remote = key[len("remote."):-len(".promisor")]
        if not remote:
            return False
        # la valeur peut être implicite (`promisor` seul) : `--bool` tranche
        if _out(repo, "config", "--local", "--bool", "--get",
                "remote.%s.promisor" % remote) != "true":
            continue
        spec = _out(repo, "config", "--local", "--get",
                    "remote.%s.partialclonefilter" % remote)
        if not spec or not _COMMIT_KEEPING_FILTER_RE.match(spec):
            return False
    return True


def _history_complete(repo: str) -> bool:
    """Le dépôt porte-t-il une histoire de commits complète (non superficielle) ?

    Un clone **superficiel** coupe l'ascendance : il rend False. Un clone
    **partiel** (remote *promisor*) ne la coupe que si son filtre omet des
    commits : `blob:none`, `tree:<profondeur>`, `sparse:` ou
    `object:type=commit` gardent toute l'histoire des commits (#4242), tandis
    qu'`object:type=blob|tree|tag` l'omet et reste un doute (#4255). Un dépôt
    illisible rend False (le doute refuse)."""
    try:
        shallow = _out(repo, "rev-parse", "--is-shallow-repository")
        if shallow is None:
            return False
        if str(shallow).strip().lower() == "true":
            return False
        return _partial_clone_keeps_commits(repo)
    except Exception:  # noqa: BLE001 - dépôt illisible : preuve impossible
        return False


def _remote_tips(repo: str, remote: str, *,
                 timeout: float | None = FETCH_TIMEOUT
                 ) -> tuple[list[str], list[str]] | None:
    """Les pointes (commits, tags pelées) de **toutes** les refs annoncées.

    `ls-remote` lit l'annonce de la forge, **indépendamment du refspec de
    fetch local** : une ref non rapatriée (tag, branche hors refspec, mais
    aussi `refs/pull/N/head`, `refs/archive/…`) reste visible (#3399).
    Les tags annotés sont remplacés par leur cible pelée (`^{}`).

    Rend `(toutes, heads_tags)` : `toutes` sert à l'égalité exacte de pointe
    et à l'**ascendance** sur l'annonce entière (sans fetch pour l'égalité) ;
    `heads_tags` distingue les refs à l'ancien budget d'ascendance (16) des
    autres (`refs/pull`, `refs/archive`, résolutions), qui ont le leur (#3399
    B1 cx1) : une forge en annonce des milliers, et une preuve d'absence ne se
    déduit jamais d'une ref ignorée. `None` si l'annonce est illisible : la
    preuve d'absence est alors impossible."""
    try:
        proc = _git(repo, "ls-remote", "--", remote, timeout=timeout)
    except (GitError, OSError):
        return None
    if proc.returncode != 0:
        return None
    peeled: dict[str, str] = {}
    refs: list[tuple[str, str]] = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        fields = line.split("\t", 1)
        if len(fields) != 2:
            continue
        sha, ref = fields[0].strip().lower(), fields[1].strip()
        if not sha or not ref:
            continue
        if ref.endswith("^{}"):
            peeled[ref[:-3]] = sha
        else:
            refs.append((sha, ref))
    all_tips: list[str] = []
    all_seen: set[str] = set()
    heads_tags: list[str] = []
    heads_seen: set[str] = set()
    for sha, ref in refs:
        tip = peeled.get(ref, sha)
        if tip not in all_seen:
            all_seen.add(tip)
            all_tips.append(tip)
        if ref.startswith(("refs/heads/", "refs/tags/")) \
                and tip not in heads_seen:
            heads_seen.add(tip)
            heads_tags.append(tip)
    return all_tips, heads_tags


def remote_reachable(repo: str, rev: str, remote: str = "origin", *,
                     timeout: float | None = FETCH_TIMEOUT,
                     budget: int = REMOTE_TIP_FETCH_BUDGET,
                     extra_budget: int = REMOTE_EXTRA_TIP_FETCH_BUDGET
                     ) -> bool | None:
    """`rev` est-il contenu par une **ref distante** de `remote` ?

    #3399 : `refresh_clone` ne rapatrie que le refspec configuré et jamais
    les tags (`fetch --no-tags`) ; il ne prouve donc pas l'absence de toute
    ref distante. On lit l'annonce **entière** (`ls-remote`, indépendante du
    refspec), puis :

    - une pointe (ou un tag pelé) **égale** au commit prouve qu'il est
      accessible, même sans l'objet local — sur **toutes** les refs annoncées,
      `refs/pull/N/head` et `refs/archive/…` comprises ;
    - l'**ascendance** se prouve sur **toutes** les pointes annoncées : une
      pointe locale est jugée par `merge-base --is-ancestor` ; une pointe
      absente est rapatriée par SHA exact (`--no-tags`, sans écrire de ref),
      dans la limite de `budget` pour heads/tags et de `extra_budget` pour les
      autres refs (`refs/pull`, `refs/archive`, résolutions). Une ref hors
      heads/tags qui pointe un **descendant** du gel le rend donc accessible
      (#3399 B1 cx1) ;
    - un nombre de pointes à rapatrier au-delà de son budget, un `ls-remote`
      ou un fetch illisible, un dépôt superficiel ou un clone partiel dont le
      filtre peut omettre des commits : `None` (preuve impossible, l'appelant
      refuse). Aucune ref annoncée n'est ignorée sans que le doute soit rendu :
      l'absence n'est jamais déduite d'une ref non lue. Un clone partiel dont
      le filtre garde les commits (`blob:none`, `tree:<profondeur>`,
      `sparse:`, `object:type=commit`) n'est pas un doute (#4242, #4255).

    Rend True (accessible), False (aucune ref distante ne le contient, prouvé)
    ou None (vérification impossible)."""
    full = local_commit(repo, rev) or full_commit_id(rev)
    if not full:
        return None
    found = _remote_tips(repo, remote, timeout=timeout)
    if found is None:
        return None
    all_tips, heads_tags = found
    if full in all_tips:
        return True
    if not _history_complete(repo):
        return None                 # l'ascendance ne se prouve pas
    # Une pointe déjà pointée par une ref locale est couverte par
    # `ref_reachable` (False ici) : inutile de la rejouer, et une forge peut
    # annoncer des milliers de branches. Les autres pointes, **toutes refs
    # confondues**, sont jugées par leur ascendance.
    try:
        listing = _out(repo, "for-each-ref",
                       "--format=%(objectname) %(*objectname)")
    except Exception:  # noqa: BLE001 - refs illisibles : preuve impossible
        return None
    if listing is None:
        return None
    local: set[str] = set()
    for line in str(listing).splitlines():
        local.update(line.split())
    candidates = [tip for tip in all_tips if tip not in local]
    if not candidates:
        return False
    # Présence locale en un seul processus (`cat-file --batch-check`), puis
    # ascendance. Un objet absent est rapatrié par SHA exact, dans la limite
    # de son budget (heads/tags ou autres refs). #4242 : la sonde doit rester
    # locale — sans `NO_LAZY_FETCH`, un clone partiel réveille le promisor pour
    # chaque pointe absente (mesuré : 60 s de délai sur un clone `blob:none`,
    # un objet à la fois) ; l'absence se lit ici, le fetch borné ci-dessous ne
    # rapatrie que les pointes manquantes.
    try:
        proc = _git(repo, "cat-file", "--batch-check",
                    stdin=("\n".join(candidates) + "\n").encode(),
                    env_extra=NO_LAZY_FETCH)
    except (GitError, OSError):
        return None
    if proc.returncode != 0:
        return None
    kinds: dict[str, str] = {}
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        fields = line.split()
        if len(fields) >= 2:
            kinds[fields[0].strip().lower()] = fields[1].strip()
    heads_set = set(heads_tags)
    pending_heads: list[str] = []
    pending_extra: list[str] = []
    for tip in candidates:
        kind = kinds.get(tip)
        if kind is None:
            return None             # réponse incohérente : preuve impossible
        if kind == "missing":
            (pending_heads if tip in heads_set else pending_extra).append(tip)
            continue
        if kind == "tag":
            commit = local_commit(repo, tip)
            if commit is None:
                (pending_heads if tip in heads_set else pending_extra).append(tip)
                continue
        elif kind == "commit":
            commit = tip
        else:
            continue                # arbre/blob : ne contient aucun commit
        if commit == full:
            return True
        ancestor = _is_ancestor_checked(repo, full, commit)
        if ancestor is None:
            return None             # histoire illisible : doute, jamais absence
        if ancestor:
            return True
    if not pending_heads and not pending_extra:
        return False
    if len(pending_heads) > budget:
        return None                 # trop de pointes heads/tags inconnues
    if len(pending_extra) > extra_budget:
        return None                 # trop d'autres refs inconnues : doute
    pending = pending_heads + pending_extra
    # Un seul fetch : 3 000 PR ne doivent pas lancer 47 négociations réseau.
    # stdin évite la limite de taille d'argv ; les SHA exacts ne créent ni
    # ref locale ni FETCH_HEAD et toutes les ascendances restent vérifiées.
    try:
        proc = _git(repo, "fetch", "--quiet", "--no-tags", "--stdin",
                    "--no-write-fetch-head", "--", remote,
                    stdin=("\n".join(pending) + "\n").encode(),
                    timeout=timeout)
    except (GitError, OSError):
        return None
    if proc.returncode != 0:
        return None
    for tip in pending:
        commit = local_commit(repo, tip)
        if commit is None:
            return None             # pointe rapatriée illisible : doute
        if commit == full:
            return True
        ancestor = _is_ancestor_checked(repo, full, commit)
        if ancestor is None:
            return None             # histoire illisible après fetch : doute
        if ancestor:
            return True
    return False


def _patch_ids(repo: str, *log_args: str) -> dict[str, str]:
    """{patch-id: commit} des commits non-fusions de `git log -p <log_args>`."""
    log = _git(repo, "log", "-p", "--no-merges", "--no-color", "--format=commit %H",
               *log_args)
    if log.returncode != 0:
        raise MergeProbeError("git log %s: %s" % (" ".join(log_args),
                                                   log.stderr.decode("utf-8", "replace").strip()))
    if not log.stdout.strip():
        return {}
    return _parse_patch_ids(_patch_id_run(repo, log.stdout))


def _parse_patch_ids(proc) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2:
            out.setdefault(parts[0], parts[1])
    return out


def _diff_patch_id(repo: str, base: str, head: str) -> str | None:
    diff = _git(repo, "diff", "--no-color", base, head)
    if diff.returncode != 0 or not diff.stdout.strip():
        return None
    ids = _parse_patch_ids(_patch_id_run(
        repo, b"commit " + head.encode() + b"\n" + diff.stdout))
    return next(iter(ids), None)


def _promisor_remote(repo: str, target: str) -> str | None:
    """Le remote *promisor* dont dépend le clone (#1772), ou None.

    Un clone partiel marque son remote d'origine `remote.<nom>.promisor=true`
    (`extensions.partialClone`) : lui seul peut porter un objet absent
    localement. Sont essayés le remote nommé par `target` (« origin/main » →
    « origin »), celui d'`extensions.partialClone`, puis `origin`. Un dépôt
    complet n'a pas de promisor : aucun fetch n'est tenté, la cible reste lue
    telle que le clone la connaît."""
    candidates: list[str] = []
    remote, sep, _branch = target.partition("/")
    if sep and remote and not remote.startswith("refs"):
        candidates.append(remote)
    partial = _out(repo, "config", "--get", "extensions.partialClone")
    if partial and partial not in candidates:
        candidates.append(partial)
    if "origin" not in candidates:
        candidates.append("origin")
    for name in candidates:
        if _out(repo, "config", "--bool", "--get",
                "remote.%s.promisor" % name) == "true":
            return name
    return None


def _fetch_missing(repo: str, sha: str, target: str) -> str | None:
    """Rapatrie le seul commit `sha` absent localement, dans un clone partiel.

    #1772 : #1749 coupe le fetch paresseux des sondes de présence
    (`GIT_NO_LAZY_FETCH`) ; l'absence locale d'un gel n'est alors plus
    distinguable d'un gel inconnu du dépôt, alors qu'un clone partiel peut
    l'aller chercher chez son remote. Le fetch est explicite (jamais
    paresseux), borné (`FETCH_TIMEOUT`) et best-effort : toute erreur — objet
    non servi, réseau, remote absent — est avalée, la sonde conclut comme
    avant. Rend le remote employé, ou None si aucun fetch n'a été tenté."""
    remote = _promisor_remote(repo, target)
    if remote is None:
        return None
    try:
        _git(repo, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
             "--", remote, sha, timeout=FETCH_TIMEOUT)
    except GitError:
        pass                      # best-effort : l'absence se conclut plus bas
    return remote


def probe(repo: str, sha: str, target: str, *, lot: int | None = None,
          frozen_at: float | None = None, frozen_absent=()) -> dict:
    """Le commit `sha` est-il entré dans `target` ? Rend `{merged, how, ref, detail}`.

    `how` : ancestor | patch-id | squash | reverted ; `ref` : le commit de la
    cible qui porte le contenu (le gel lui-même s'il est ancêtre). `lot` :
    l'id du lot examiné, pour rattacher un revert à son porteur (#1825).

    `frozen_at` (#2786) : l'instant du jalon `frozen` examiné. Si le commit
    était déjà ancêtre de la cible **à cet instant**, il n'a jamais eu de
    commit propre : ce n'est pas une livraison (cas #2784). `frozen_absent`
    (la marque du gel) dispense de cette lecture d'instant pour la branche
    cible : le gel y était absent quand il a été posé, une avance rapide
    ultérieure est donc bien une livraison. Absent (une tête de branche, un
    avis), la preuve par l'ancêtre reste celle d'avant.

    #1825 : une preuve par le contenu est annulée par un revert postérieur
    (`reverted_content`) — une tête ancêtre de la cible dont la fusion a été
    annulée n'est pas livrée ; de même un patch-id encore présent dans
    l'historique quand le commit qui le porte a été annulé depuis.

    #1772 : un gel absent localement est rapatrié par un fetch explicite
    borné quand le clone est partiel ; sinon l'absence est conclue telle
    quelle."""
    target = _target_ref(repo, target)
    head = _commit(repo, target)
    if head is None:
        raise MergeProbeError("target %r not found in %s" % (target, repo))
    if not _SHA_RE.match(sha or ""):
        return {"merged": False, "how": None, "ref": None, "detail": "unreadable commit"}
    frozen = _commit(repo, sha)
    if frozen is None:
        # #1772 : dans un clone partiel, l'absence locale n'est pas une
        # preuve — un fetch explicite et borné du seul commit manquant.
        remote = _fetch_missing(repo, sha, target)
        frozen = _commit(repo, sha)
        if frozen is None:
            detail = "commit %s not in the repository" % sha[:12]
            if remote:
                detail += " (explicit fetch from %s brought nothing)" % remote
            return {"merged": False, "how": None, "ref": None, "detail": detail}
    if _git(repo, "merge-base", "--is-ancestor", frozen, head).returncode == 0:
        # #2786 (4)/(5) : un gel déjà ancêtre de la cible au moment où il a
        # été posé ne prouve aucune livraison — c'est le cas #2784 (un ancien
        # commit d'un autre lot, déjà dans main, gelé sans commit propre). La
        # marque `frozen_absent` (branche cible absente au gel) lève le doute.
        if frozen_at and _target_branch(target) not in set(frozen_absent) \
                and _was_ancestor_at(repo, frozen, target, frozen_at):
            return {"merged": False, "how": None, "ref": None,
                    "detail": "%s was already an ancestor of %s when frozen: no own "
                              "commit, not a delivery" % (frozen[:12], target)}
        stopped = reverted_content(repo, frozen, target, lot=lot)
        if stopped:
            return {"merged": False, "how": "reverted", "ref": None,
                    "detail": "%s is an ancestor of %s, but %s reverts its content"
                              % (frozen[:12], target, stopped["ref"][:12])}
        return {"merged": True, "how": "ancestor", "ref": frozen,
                "detail": "%s is an ancestor of %s" % (frozen[:12], target)}
    base = _out(repo, "merge-base", frozen, head)
    if not base:
        return {"merged": False, "how": None, "ref": None, "detail": "no common base"}
    try:
        return _by_patch_id(repo, frozen, head, base, target)
    except _NoVerbatim as exc:
        return {"merged": False, "how": None, "ref": None,
                "detail": "git patch-id --verbatim unavailable (git >= 2.40 required: %s): "
                          "only ancestry proves the merge" % (exc or "refused")}


def _by_patch_id(repo: str, frozen: str, head: str, base: str, target: str) -> dict:
    on_target = _patch_ids(repo, "--max-count=%d" % MAX_TARGET_COMMITS, "%s..%s" % (base, head))
    mine = _patch_ids(repo, "%s..%s" % (head, frozen))
    if mine and all(pid in on_target for pid in mine):
        carriers = {on_target[pid] for pid in mine}
        stopped = _effective_revert(repo, base, target,
                                    lambda sha, _message: sha in carriers)
        if stopped:
            return {"merged": False, "how": "reverted", "ref": None,
                    "detail": "%d frozen commit(s) found by patch-id in %s, but %s "
                              "reverts their content"
                              % (len(mine), target, stopped["ref"][:12])}
        # `git log` rend les commits du plus récent au plus ancien : le premier
        # patch-id est celui du dernier commit du gel
        last = on_target[next(iter(mine))]
        return {"merged": True, "how": "patch-id", "ref": last,
                "detail": "%d frozen commit(s) found by patch-id in %s"
                          % (len(mine), target)}
    squash = _diff_patch_id(repo, base, frozen)
    if squash and squash in on_target:
        carrier = on_target[squash]
        stopped = _effective_revert(repo, base, target,
                                    lambda sha, _message: sha == carrier)
        if stopped:
            return {"merged": False, "how": "reverted", "ref": None,
                    "detail": "frozen diff found in %s (squash), but %s reverts it"
                              % (target, stopped["ref"][:12])}
        return {"merged": True, "how": "squash", "ref": carrier,
                "detail": "frozen diff found as one commit of %s (squash)" % target}
    missing = sum(1 for pid in mine if pid not in on_target)
    return {"merged": False, "how": None, "ref": None,
            "detail": "%d/%d frozen commit(s) missing from %s" % (missing, len(mine), target)}


def branch_lot(branch: str | None) -> int | None:
    """L'id du lot d'une branche `lot/<id>` ou `lot/<id>-…`, sinon None."""
    match = re.match(r"^lot/(?P<lot>\d+)(?:-|$)", short_branch(branch))
    return int(match.group("lot")) if match else None


def _commit_message(repo: str, sha: str) -> str:
    return _out(repo, "log", "-1", "--format=%B", sha) or ""


def _commit_parents(repo: str, sha: str) -> list[str]:
    return (_out(repo, "rev-list", "--parents", "-n", "1", sha) or "").split()[1:]


def _carries_frozen(repo: str, commit: str, frozen: str, *, lot: int | None = None,
                    branch: str | None = None, hash_refs: bool | None = None,
                    message: str | None = None) -> bool:
    """#1825 : le commit `commit` a-t-il porté le gel `frozen` dans la cible ?

    Oui s'il EST le gel (poussée directe), s'il le cite par son abréviation
    (« tête gelée <sha> » des commits de composition), s'il désigne le lot
    (`cited_lots`, `merge_branch`) ou si c'est la fusion dont la première
    parente ne portait pas encore le gel (la fusion porteuse d'une PR)."""
    if commit == frozen:
        return True
    if message is None:
        message = _commit_message(repo, commit)
    if frozen[:_SHORT_SHA] in message:
        return True
    if hash_refs is None:
        hash_refs = lot_ref_hash(repo)
    if lot is not None and int(lot) in cited_lots(message, hash_refs=hash_refs):
        return True
    if branch and merge_branch(message) == short_branch(branch):
        return True
    parents = _commit_parents(repo, commit)
    return (len(parents) > 1 and _is_ancestor(repo, frozen, commit)
            and not _is_ancestor(repo, frozen, parents[0]))


def _effective_revert(repo: str, since: str, ref: str, carries) -> dict | None:
    """#1825 : le revert EFFECTIF le plus récent d'un porteur, dans `since..ref`.

    `carries(sha, message)` : ce commit a-t-il porté le contenu examiné ? Un
    revert n'est retenu que s'il cite le commit annulé (`This reverts commit
    <sha>`), si son diff annule exactement l'apport de ce commit (patch-id) et
    si aucun commit postérieur ne réapplique cet apport. Rend
    `{ref, undone, detail}`, ou None. La lecture est bornée à
    MAX_REVERT_SCAN commits (les plus récents). None ne prouve PAS l'absence
    de revert : au-delà de la borne, ou sans `patch-id --verbatim` (git <
    2.40, aucun revert n'est retenu), l'appelant retombe sur sa preuve
    d'avant #1825 (ancêtre, patch-id, citation) et peut fermer à tort un lot
    annulé — détection au mieux, pas une garantie."""
    out = _out(repo, "log", "--format=%H%x00%B%x1e",
               "--max-count=%d" % MAX_REVERT_SCAN, "%s..%s" % (since, ref))
    for record in (out or "").split("\x1e"):
        sha, _sep, message = record.strip().partition("\x00")
        if not sha or not (message or "").lstrip().startswith('Revert "'):
            continue
        match = _REVERTED_COMMIT_RE.search(message)
        if not match:
            continue
        undone = _commit(repo, match.group(1))
        if undone is None or not _is_ancestor(repo, undone, sha):
            continue
        if not carries(undone, _commit_message(repo, undone)):
            continue
        try:
            # l'apport du commit annulé : la première parente pour une fusion
            # (`git revert -m 1` ; `-m 2` annule l'autre côté, sans rapport)
            applied = _diff_patch_id(repo, undone + "^", undone)
            removed = _diff_patch_id(repo, sha, sha + "^")
            if not applied or applied != removed:
                continue
            if applied in _patch_ids(repo, "--max-count=%d" % MAX_TARGET_COMMITS,
                                     "%s..%s" % (sha, ref)):
                continue                     # réappliqué depuis : rien d'annulé
        except _NoVerbatim:
            return None
        return {"ref": sha, "undone": undone,
                "detail": "%s reverts %s: content removed from %s"
                          % (sha[:12], undone[:12], ref)}
    return None


def reverted_content(repo: str, frozen: str, ref: str, *, lot: int | None = None,
                     branch: str | None = None, hash_refs: bool | None = None) -> dict | None:
    """#1825 : le contenu du gel `frozen` a-t-il été annulé dans `ref` ?

    L'ascendance seule ne prouve plus la livraison : un paquet peut être
    fusionné puis annulé par un revert (`Revert "#<lot>: compose …"`), la tête
    gelée restant ancêtre de la cible. Rend le revert effectif, ou None."""
    return _effective_revert(
        repo, frozen, ref,
        lambda sha, message: _carries_frozen(repo, sha, frozen, lot=lot, branch=branch,
                                             hash_refs=hash_refs, message=message))


def _merge_reverted(repo: str, sha: str, ref: str) -> dict | None:
    """#1825 : ce commit de fusion a-t-il été annulé depuis (revert effectif) ?"""
    return _effective_revert(repo, sha, ref, lambda commit, _message: commit == sha)


def latest_freeze(rows: list[dict]) -> dict | None:
    """Le dernier gel d'un lot (jalon `frozen` le plus récent), ou None.

    Lui seul fait autorité : s'il n'a pas de commit, rien n'est prouvable."""
    freezes = [r for r in rows if r.get("kind") == "frozen"]
    if not freezes:
        return None
    return max(freezes, key=lambda r: (r.get("at_ts") or 0, int(r.get("id") or 0)))


#: un identifiant de commit exploitable : 4 à 64 hexadécimaux (git abrège
#: jusqu'à 4). La résolution en commit complet et unique reste au lecteur qui
#: a le dépôt (#1756).
_COMMIT_ID = re.compile(r"[0-9a-f]{4,64}")
#: #2786 : une tête gelée lue par le balayage est un commit **complet** (40 ou
#: 64 hexadécimaux) ; une abréviation ou une ref symbolique n'y est jamais
#: résolue (le `HEAD` du clone du balayage a passé un lot en `merged`, #2712).
_FULL_COMMIT_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def commit_id(value) -> str:
    """L'identifiant de commit d'un jalon, en minuscules (`''` sinon)."""
    text = str(value or "").strip().lower()
    return text if _COMMIT_ID.fullmatch(text) else ""


def full_commit_id(value) -> str:
    """Le commit complet (40 ou 64 hexadécimaux) d'un jalon, en minuscules, ou ''.

    #2786 : l'identifiant qu'un balayage ou une désignation accepte pour une
    tête gelée ; une abréviation reste lisible par `commit_id` (compatibilité)
    mais n'est jamais une preuve de livraison."""
    text = str(value or "").strip().lower()
    return text if _FULL_COMMIT_ID.fullmatch(text) else ""


#: #3648 : la forge d'un dépôt de travail sans `origin` (le `git init` de
#: `merge-pr`). Une clé de configuration, pas un remote : un remote `origin`
#: changerait les autres lectures du dépôt (rafraîchissement, rejeu, cibles).
FORGE_CONFIG_KEY = "ameesh.forge"


def forge_remote(repo: str) -> str | None:
    """Ce qu'il faut passer à `git fetch` pour demander un SHA à la forge.

    `origin` quand le dépôt en a un ; sinon l'URL de `ameesh.forge` (#3648 :
    le dépôt scratch de `merge-pr` la reçoit de son URL de lecture) ; sinon
    None — un dépôt sans forge n'a rien à rapatrier."""
    if _out(repo, "remote", "get-url", "origin") is not None:
        return "origin"
    return (_out(repo, "config", "--get", FORGE_CONFIG_KEY) or "").strip() or None


def same_commit(a, b) -> bool:
    """Deux identifiants qui nomment le même commit (préfixe admis, #1756)."""
    a, b = commit_id(a), commit_id(b)
    return bool(a and b) and (a == b or a.startswith(b) or b.startswith(a))


#: #2786 (5) : les branches d'intégration où le gel n'était PAS quand il a été
#: posé, écrites dans la note du jalon par `work.milestone` (une ligne
#: machine) : la preuve de livraison y reste valable, même après une avance
#: rapide. Les gels antérieurs n'ont pas la marque : repli sur l'instant.
_FREEZE_ABSENT_RE = re.compile(r"(?m)^ameesh-freeze-absent:\s*(.*)$")


def freeze_absent(note) -> frozenset[str]:
    """Les branches d'intégration absentes au gel, lues dans la note (#2786)."""
    match = _FREEZE_ABSENT_RE.search(str(note or ""))
    if not match:
        return frozenset()
    return frozenset(word for word in match.group(1).split() if word)


def _target_branch(target) -> str:
    """Le nom de branche d'une cible (`origin/develop`, `develop`, …), ou ''."""
    text = str(target or "").strip()
    for prefix in ("refs/remotes/origin/", "refs/heads/", "origin/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if not text or _SHA_RE.match(text):
        return ""
    return text


def rebase_identity(repo: str, reviewed: str, frozen: str, target: str) -> dict:
    """Le delta du lot est-il **mécaniquement constant** entre la tête relue
    `reviewed` (celle d'un avis) et la tête gelée `frozen` (gel republié après
    un rebase ou une correction de base) ?

    Un rebase mécanique ne change pas le contenu du lot : son delta net
    doit avoir le même `patch-id` de part et d'autre. Les commits individuels
    servent au diagnostic, jamais à ignorer le contenu d'une fusion. Les bases
    sont la base commune de chaque tête avec `target` (le rebase déplace la
    base, pas les commits du lot). Rend `{mechanical, detail}` ; `mechanical`
    est faux dès qu'un SHA ou un delta est illisible — jamais de report sur un
    doute (#1905, règle #87 appliquée à la main jusqu'ici)."""
    old, new = _commit(repo, reviewed), _commit(repo, frozen)
    if old is None or new is None:
        return {"mechanical": False, "detail": "commit illisible"}
    if old == new:
        return {"mechanical": True, "detail": "même commit"}
    tgt = _target_tip(repo, target) if target else None
    if tgt is None:
        return {"mechanical": False, "detail": "cible illisible"}
    bases = {}
    for name, head in (("old", old), ("new", new)):
        base = _out(repo, "merge-base", head, tgt)
        if not base:
            return {"mechanical": False, "detail": "pas de base commune"}
        bases[name] = base
    try:
        old_ids = _patch_ids(repo, "%s..%s" % (bases["old"], old))
        new_ids = _patch_ids(repo, "%s..%s" % (bases["new"], new))
    except (_NoVerbatim, MergeProbeError) as exc:
        return {"mechanical": False, "detail": "delta illisible: %s" % (exc or "refusé")}
    # Les commits seuls ne suffisent pas : --no-merges ignore une résolution
    # introduisant du contenu neuf, et un ensemble perd ordre/multiplicité.
    # Le delta net doit aussi être identique avant tout report d'avis.
    try:
        old_squash = _diff_patch_id(repo, bases["old"], old)
        new_squash = _diff_patch_id(repo, bases["new"], new)
    except (_NoVerbatim, MergeProbeError):
        old_squash = new_squash = None
    if old_squash and old_squash == new_squash:
        return {"mechanical": True, "detail": "diff écrasé de patch-id identique"}
    common = len(set(old_ids) & set(new_ids))
    return {"mechanical": False,
            "detail": "%d/%d commit(s) de patch-id communs" % (common, len(old_ids))}


def _freeze_mark(row: dict | None) -> dict | None:
    """Un jalon lu, réduit à ce que la provenance d'un gel effectif expose."""
    if row is None:
        return None
    return {"id": int(row.get("id") or 0), "kind": row.get("kind") or "",
            "sha": commit_id(row.get("sha")), "verdict": row.get("verdict") or "",
            "actor": row.get("actor") or "", "at_ts": row.get("at_ts")}


def freeze_candidate(rows: list[dict], repo: str | None = None,
                     target: str | None = None, *, waived: bool = False) -> dict:
    """Le gel effectif d'un lot (#1756) : le commit sur lequel un avis
    favorable existe, même sans jalon `frozen`, ou malgré un gel périmé.

    `latest_freeze` rend le dernier gel *déclaré* — la frise, la fermeture sur
    fusion et la classification s'y tiennent ; ici on rend un **candidat de
    lecture** pour les lecteurs qui décident si un lot est prêt (composeur du
    paquet horaire, §11bis). Aucune écriture, aucune date inventée : `origin`
    dit si le gel est `frozen` (déclaré) ou `verdict` (dérivé du dernier avis
    favorable), `declared` et `verdict` portent les jalons lus, `notes` les
    observations non bloquantes, `causes` les refus (le SHA est alors vide).

    Règles (celles de `progress._verdict_applies`, resserrées du « dernier
    avis par relecteur, blocage jamais masqué » de la porte des avis) :

    * gel et avis nommant le même commit (préfixe admis) : le gel déclaré ;
    * avis favorable postérieur à un gel qui nomme un autre commit : le SHA
      de l'avis, le gel déclaré étant périmé (rebase) — sauf si ce commit est
      nommé par un jalon antérieur au gel (ancienne tête) : l'avis est tardif
      et écarté, note `late-verdict-on-older-commit` (#1767) ;
    * gel postérieur à l'avis sur un autre commit : refus
      `freeze-after-verdict`, un nouvel avis est dû sur la tête gelée (#1283) ;
      si un avis favorable nomme déjà cette tête, retour au gel déclaré,
      note `returned-to-reviewed-head` (#2884), sous réserve des blocages
      applicables sur cette tête, lus par relecteur ci-dessous ;
      si `repo` est fourni et que le delta du lot est **mécaniquement
      constant** entre la tête relue et la tête gelée (`rebase_identity`,
      #1905), l'avis est porté sur la tête gelée, note `mechanical-rebase` ;
    * sans gel déclaré : le SHA de l'avis favorable ;
    * avis sans commit : il ne vaut que par l'instant, le gel déclaré (s'il
      existe) reste candidat, note `verdict-names-no-commit` ;
    * aucun avis : le gel déclaré reste candidat (note `no-verdict` — la
      classe légère n'exige pas d'avis) ; sans gel non plus, refus
      `no-verdict` ;
    * `waived` (#2681, dispense `-none` du pilote, #1979) : le gel déclaré
      est le candidat même quand le dernier avis porte sur une tête
      antérieure — la dispense couvre la tête gelée, pas une remise (#2496) ;
      note `verdict-not-required`. Un `blocked` sur la tête gelée refuse
      toujours ;
    * un `blocked` applicable n'est jamais masqué par le `ok` d'un autre
      relecteur : refus `blocked-verdict`.

    Le SHA rendu peut être abrégé : le lecteur qui a le dépôt le résout en un
    commit complet **unique** et le confronte à la tête **publiée** (jamais
    `branch_head`, une observation) ; un candidat qui n'est pas la tête
    publiée est une exclusion nommée, jamais une omission."""
    marks = list(rows or [])
    freeze = latest_freeze(marks)
    verdicts = [r for r in marks if r.get("kind") == "verdict"]

    def newest(items):
        if not items:
            return None
        return max(items, key=lambda r: (r.get("at_ts") or 0, int(r.get("id") or 0)))

    f_sha = commit_id((freeze or {}).get("sha"))
    f_at = float((freeze or {}).get("at_ts") or 0)
    # #1767 : un avis ok postérieur au gel sur un commit que la frise nommait
    # déjà avant ce gel (ancienne tête) est tardif : il ne déclasse pas le gel.
    # Un commit jamais nommé avant le gel reste lu comme un rebase relu.
    before = [commit_id(r.get("sha")) for r in marks
              if r.get("kind") in ("frozen", "verdict")
              and float(r.get("at_ts") or 0) < f_at] if f_sha else []

    def late(v) -> bool:
        sha = commit_id(v.get("sha"))
        return ((v.get("verdict") or "ok") == "ok" and bool(sha)
                and float(v.get("at_ts") or 0) > f_at
                and not same_commit(sha, f_sha)
                and any(same_commit(sha, b) for b in before))

    last = newest([v for v in verdicts if not late(v)])
    # #2884 : retour explicite à une source déjà relue après une tête
    # intermédiaire. Les avis acquis sur ce SHA restent valables ; le dernier
    # avis toutes têtes confondues ne suffit pas à choisir le candidat.
    returned = None
    if (f_sha and last is not None
            and f_at > float(last.get("at_ts") or 0)
            and commit_id(last.get("sha"))
            and not same_commit(f_sha, commit_id(last.get("sha")))):
        returned = newest([v for v in verdicts
                           if (v.get("verdict") or "ok") == "ok"
                           and same_commit(commit_id(v.get("sha")), f_sha)])
        if returned is not None:
            last = returned
    out = {"sha": "", "origin": "", "declared": _freeze_mark(freeze),
           "verdict": _freeze_mark(last), "causes": [], "notes": []}
    if returned is not None:
        out["notes"].append("returned-to-reviewed-head")
    if any(late(v) for v in verdicts):
        out["notes"].append("late-verdict-on-older-commit")
    if freeze is not None and not f_sha:
        out["notes"].append("declared-freeze-unusable")
    v_sha = commit_id((last or {}).get("sha"))
    v_at = float((last or {}).get("at_ts") or 0)

    carried_from = None
    if waived and f_sha:
        # La dispense du pilote couvre la tête déclarée (#2681) : un avis
        # antérieur ne la déclasse pas. La porte garde son blocage courant,
        # vérifié ci-dessous sur cette même tête.
        out.update(sha=f_sha, origin="frozen")
        out["notes"].append("verdict-not-required")
    else:
        if last is None:
            if f_sha:
                out.update(sha=f_sha, origin="frozen")
                out["notes"].append("no-verdict")
                return out
            out["causes"].append("no-verdict")
            return out
        if (last.get("verdict") or "ok") != "ok":
            out["causes"].append("blocked-verdict")
            return out
        if f_sha and v_sha:
            if same_commit(f_sha, v_sha):
                out.update(sha=max(f_sha, v_sha, key=len), origin="frozen")
            elif f_at > v_at:
                carried = (rebase_identity(repo, v_sha, f_sha, target or DEFAULT_TARGET)
                           if repo else {"mechanical": False})
                if not carried["mechanical"]:
                    out["causes"].append("freeze-after-verdict")
                    return out
                out.update(sha=f_sha, origin="frozen")
                out["notes"].append("mechanical-rebase: %s" % carried["detail"])
                carried_from = v_sha
            else:
                out.update(sha=v_sha, origin="verdict")
                out["notes"].append("declared-freeze-stale")
        elif v_sha:
            out.update(sha=v_sha, origin="verdict")
            if freeze is not None:
                out["notes"].append("declared-freeze-stale")
        elif f_sha and v_at >= f_at:
            out.update(sha=f_sha, origin="frozen")
            out["notes"].append("verdict-names-no-commit")
        else:
            out["causes"].append("verdict-names-no-commit")
            return out

    # Blocage applicable : le dernier avis de chaque relecteur sur le commit
    # candidat (mêmes règles que la porte des avis, #1374).
    by_actor: dict = {}
    for v in verdicts:
        cand = commit_id(v.get("sha"))
        applies = (same_commit(cand, out["sha"]) or
                   (carried_from is not None and same_commit(cand, carried_from))) if cand else (
            freeze is None or float(v.get("at_ts") or 0) >= f_at)
        if not applies:
            continue
        actor = (v.get("actor") or "").strip()
        # Un avis sur l'ancienne tête ne peut lever un blocage de la nouvelle,
        # même s'il est rendu plus tard par le même relecteur.
        scope = "carried" if cand and not same_commit(cand, out["sha"]) else "current"
        key = (actor, scope)
        seen = by_actor.get(key)
        if seen is None or (v.get("at_ts") or 0, int(v.get("id") or 0)) > \
                (seen.get("at_ts") or 0, int(seen.get("id") or 0)):
            by_actor[key] = v
    blocked = sorted({(v.get("actor") or "?") for v in by_actor.values()
                      if (v.get("verdict") or "ok") != "ok"})
    if blocked:
        out.update(sha="", origin="")
        out["causes"].append("blocked-verdict")
        out["notes"].append("blocked by %s" % ", ".join(blocked))
        return out
    return out


def _content_refs(row: dict, target: str | None, *, declared_repo: str | None,
                  default: str | None, candidates: list[str],
                  project: str = "") -> list[tuple[str, bool, bool]]:
    """#1005 : les refs où chercher le contenu gelé d'un lot, dans l'ordre, et
    `(ref, hors cible, ferme)`.

    Une cible explicite (`--target`) ou déclarée par le lot (`branch_target`)
    est seule à foi : elle ferme, jamais une autre ref ; à défaut, celle du
    projet du lot (`project`, #5110) de même. Sinon la cible
    déclarée du dépôt (`ameesh.target`) fait foi de même, et les refs
    d'intégration ne servent qu'à signaler le résidu. Sans aucune cible
    déclarée : la cible par défaut du clone (`origin/HEAD`, puis main/master),
    puis les refs d'intégration, qui ferment en se nommant. Sans cible par
    défaut, les seules refs d'intégration examinées ferment."""
    own = str(row.get("branch_target") or "").strip()
    if target is not None:
        return [(target, False, True)]
    if own:
        return [(own, False, True)]
    if project:
        return [(project, False, True)]
    if default is None:
        return [(ref, bool(index), True) for index, ref in enumerate(candidates)]
    to_try = [(default, False, True)]
    to_try += [(ref, True, declared_repo is None) for ref in candidates]
    return to_try


def _packet_delivery(repo: str, lot: int, members: list[dict], refs: list[tuple],
                     marks: list[dict], *, default: str | None) -> dict | None:
    """#2443 : l'unité a-t-elle été livrée par un paquet composé ?

    `members` sont les appartenances enregistrées (`work_packet_members`) ;
    seule une ref qui ferme est examinée. Le commit de composition (`unit_sha`)
    doit être entré dans cette ref (`probe`, revert #1825 compris). La preuve
    n'est retenue que si le gel effectif courant de l'unité est encore celui de
    la composition — le gel enregistré, ou la tête composée elle-même quand
    aucun gel n'existait alors ; un nouveau gel rend la preuve périmée et le
    lot retombe sur le relevé de son gel.

    Rend `{ref, proof, member, target, off_target}` ou None.
    """
    if not members or not refs:
        return None
    target_ref = refs[0][0]
    try:
        candidate = freeze_candidate(marks, repo=repo, target=target_ref)
    except (MergeProbeError, GitError):
        return None                       # gel courant illisible : jamais de preuve
    current = commit_id(candidate.get("sha"))
    # Sans gel effectif, seule l'absence de tout gel/avis (`no-verdict`) laisse
    # la composition conclure : un avis bloqué, un gel postérieur à l'avis ou
    # un avis sans commit déclarent une tête que la composition ne couvre pas.
    undecided = not current and bool(
        set(candidate.get("causes") or ()) - {"no-verdict"})
    for member in members:
        unit = commit_id(member.get("unit_sha"))
        if not unit:
            continue
        recorded = commit_id(member.get("frozen_sha"))
        source = commit_id(member.get("source_sha"))
        if current:
            if not (same_commit(current, recorded) or same_commit(current, source)):
                continue                 # un nouveau gel : la composition est périmée
            # Le gel capturé par record peut avoir avancé APRÈS la composition.
            # Son identité avec le gel courant ne prouve donc pas sa livraison :
            # la source réellement composée doit aussi en porter le contenu.
            if not same_commit(current, source):
                if not source:
                    continue
                try:
                    if not probe(repo, current, source, lot=lot)["merged"]:
                        continue
                except (MergeProbeError, GitError):
                    continue
        elif recorded:
            continue                     # le gel enregistré a disparu : ne pas conclure
        elif undecided:
            continue                     # une déclaration existe, sans gel lisible
        for ref, off, closes in refs:
            if not closes:
                continue
            try:
                proof = probe(repo, unit, ref, lot=lot)
            except (MergeProbeError, GitError):
                continue
            if not proof["merged"]:
                continue
            return {"ref": ref, "proof": proof, "member": member, "target": ref,
                    "off_target": default if off else None}
    return None


#: #2744 : états qui retiennent la fermeture par le contenu — le lot peut être
#: livré sans être clos (une passe, une décision ou un déblocage restent dus).
CONTENT_HELD_STATES = ("blocked",)


def content_retention(st, row: dict) -> dict | None:
    """#2744 : la retenue qui empêche de fermer un lot par son contenu.

    Un lot `blocked`, ou sous une note `hold: …` non levée (#1664), n'est pas
    fermé par le contenu : sa livraison est constatée, sa fermeture attend la
    levée. `planned_waits.explicit_holds` est la seule lecture des hold : une
    note `unhold:` ou un changement d'état lève la retenue. Une lecture du
    journal qui échoue rend une retenue `unreadable` : une erreur ne doit
    jamais autoriser une fermeture. Rend `{what, motif, by, since}`, ou None.

    #3219 : les retenues ordinaires de l'assigné (règle 1, non comptées par
    `planned_waits.pilot_holds`) ne retiennent plus la fermeture — son auteur
    les lèverait lui-même, mais le contenu prouvé est déjà la livraison ; la
    passe les lève et `work.close_merged` écrit la levée dans sa note de
    fusion (`lift: True`). Les retenues d'un pilote ou d'un autre acteur
    restent actives et retiennent : les holds sont un ensemble, pas une pile,
    donc un hold de pilote recouvert par celui de l'assigné garde son effet.
    Sa levée reste un geste autorisé (règle #1664)."""
    row = row or {}
    try:
        lot = int(row["id"])
        state = str(row.get("state") or "")
    except (KeyError, TypeError, ValueError):
        return None
    try:
        events = st.progress.lot_hold_events([lot], planned_waits.HOLD_NOTE_PATTERNS)
    except Exception as exc:  # noqa: BLE001 — l'erreur retient, elle n'ouvre pas
        return {"what": "unreadable",
                "motif": "journal des retenues illisible (%s: %s)"
                         % (type(exc).__name__, " ".join(str(exc).split())[:120]),
                "by": str(row.get("assignee") or ""), "since": None}
    db = getattr(st, "db", None)
    holds = planned_waits.active_holds(db, events,
                                       cfg=getattr(db, "cfg", None)).get(lot) or {}
    if state in CONTENT_HELD_STATES:
        transitions = [e for e in events if e.get("state") in CONTENT_HELD_STATES]
        last = transitions[-1] if transitions else {}
        # Une observation de fusion peut être la dernière transition, avec
        # un acteur « runner …@hôte » : elle n'est pas l'auteur du hold.
        hold = max(holds.values(), key=lambda h: float(h.get("since") or 0.0),
                   default={})
        by = str(hold.get("by") or row.get("assignee") or "")
        return {"what": "blocked", "motif": "état %s" % state, "by": by,
                "since": hold.get("since") if hold else last.get("created_ts")}
    if holds:
        assignee = str(row.get("assignee") or "")
        own, retained = [], []
        for hold in holds.values():
            if hold.get("by") == assignee and not hold.get("pilot"):
                own.append(hold)
            else:
                retained.append(hold)
        if own and not retained:
            last = max(own, key=lambda hold: float(hold.get("since") or 0.0))
            lift = "levée du hold de l'assigné %s" % assignee
            reason = _hold_reason(last)
            if reason:
                lift += " : %s" % reason
            return {"what": "hold", "lift": True, "motif": lift, "by": assignee,
                    "since": last.get("since")}
        chosen = max(retained or own,
                     key=lambda hold: float(hold.get("since") or 0.0))
        by = str(chosen.get("by") or "")
        motif = "hold" + (" de %s" % by if by else "")
        reason = _hold_reason(chosen)
        if reason:
            motif += " : %s" % reason
        return {"what": "hold", "motif": motif, "by": by,
                "since": chosen.get("since")}
    return None


def dry_run_retention(db, st, row: dict, *, sha: str, repo: str | None) -> dict | None:
    """La retenue qu'un relevé à blanc annonce : #2744 (blocked, hold), puis
    #3022 (porte de production hors de la branche de prod) — la même que
    `work.close_merged` appliquerait, sans verrou ni écriture. Le hold de
    l'assigné levé par le contenu (#3219, `lift: True`) n'exempte pas de la
    porte : sa retenue prime, sinon la levée est rendue."""
    retention = content_retention(st, row)
    if retention is None or retention.get("lift"):
        from ... import prod_gate
        gate = prod_gate.check(st, row, sha=sha, repo=repo,
                               cfg=getattr(db, "cfg", None))["retention"]
        if gate is not None:
            retention = gate
    return retention


def _hold_reason(hold: dict) -> str:
    """Le motif d'une retenue (`hold: <raison>`), sur une ligne."""
    reason = " ".join(str((hold or {}).get("note") or "").split())
    if reason[:5].lower() == "hold:":
        reason = reason[5:].strip()
    return reason[:160]


def _retention_text(retention: dict, entry: dict, target: str) -> str:
    """La ligne de journal d'une fermeture retenue (#2744)."""
    ref = str(entry.get("ref") or entry.get("sha") or "")
    return ("contenu fusionné, fermeture retenue : %s — %s (%s %s)"
            % (retention["motif"], target, entry.get("how") or "?",
               ref[:12] or "?"))


def mark_withheld(entry: dict, retention: dict, *, target: str,
                  dry_run: bool = False) -> str:
    """#2744 : l'entrée de compte rendu d'une fermeture retenue."""
    text = _retention_text(retention, entry, target)
    entry["retention"] = retention
    entry["result"] = "would-withhold" if dry_run else "withheld"
    entry["detail"] = "%s — %s" % (entry.get("detail") or "", text)
    return text


def mark_lifted(entry: dict, retention: dict) -> None:
    """#3219 : la retenue ordinaire de l'assigné que la fermeture lèverait.

    Un relevé à blanc (`dry_run`) ne doit pas annoncer `would-withhold` quand
    la passe réelle fermerait : il dit la levée dans le détail, sans retenir."""
    entry["retention"] = retention
    entry["detail"] = "%s — %s" % (
        entry.get("detail") or "",
        retention.get("motif") or "hold de l'assigné levé par le contenu")


def mark_retained(entry: dict, retention: dict, *, target: str,
                  dry_run: bool = False) -> None:
    """#2744 : compte rendu d'une retenue, ou d'une lecture de journal en
    erreur — cette dernière est une **erreur** explicite, jamais un simple
    hold : le lot reste ouvert et l'appelant la voit (`result: error`)."""
    if retention.get("what") == "unreadable":
        entry["retention"] = retention
        entry["result"] = "error"
        entry["detail"] = "%s — %s" % (entry.get("detail") or "",
                                       retention.get("motif") or "journal illisible")
        return
    mark_withheld(entry, retention, target=target, dry_run=dry_run)


def record_content_withheld(db, st, row: dict, entry: dict, retention: dict, *,
                            target: str, actor: str = "") -> None:
    """#2744 : journalise et réveille une fermeture retenue, une fois.

    Appelé après le refus atomique de `work.close_merged` : la note et le
    courrier ne sont pas dans la transaction de fermeture, mais la fermeture,
    elle, a déjà été refusée sous le verrou du lot."""
    text = _retention_text(retention, entry, target)
    lot = int(row["id"])
    if not _first_observation(st, lot, text):
        return
    try:
        work_mod.note(db, lot, text, actor=actor)
    except work_mod.WorkError:
        return                    # le lot a changé : rien à journaliser
    _wake_retention(db, row, retention, entry=entry, target=target)


def _first_observation(st, lot: int, text: str) -> bool:
    """#2744 : cette retenue n'est-elle pas déjà au journal ? (une note et un
    réveil par observation, pas un par balayage)."""
    try:
        return not any((e.get("note") or "").strip() == text
                       for e in st.work.events(lot, 40))
    except Exception:  # noqa: BLE001 — une lecture impossible ne bloque pas la note
        return True


def _wake_retention(db, row: dict, retention: dict, *, entry: dict, target: str) -> None:
    """#2744 : réveille le détenteur du hold, sinon le signaleur du blocage.

    Best-effort : la note est déjà écrite ; un courrier non délivré ne doit pas
    casser le relevé. Un humain n'est pas réveillé par la boîte aux lettres."""
    from ... import mail, registry
    from ...config import NAME_RE
    recipient = ""
    try:
        for candidate in (retention.get("by"), row.get("assignee")):
            candidate = str(candidate or "").strip()
            if candidate.startswith(work_mod.HUMAN_PREFIX):
                return  # un hold humain n'est pas délégué par courrier
            if candidate == work_mod.SYSTEM_SENDER or not NAME_RE.fullmatch(candidate):
                continue
            agent = registry.get(db, candidate)
            if agent and agent.get("status") != "stopped":
                recipient = candidate
                break
    except Exception:  # noqa: BLE001 — la note reste la preuve de la retenue
        return
    if not recipient:
        return
    ref = str(entry.get("ref") or entry.get("sha") or "")
    lift = ("lift the hold (a state change or an `unhold:` note) and the next "
            "content sweep will close it")
    if retention.get("what") == "production":
        # #3022 : c'est la présence sur la branche de prod qui ferme
        lift = ("it closes once the fix reaches the production branch (release or "
                "incident fix); a wrong mark is lifted by a `prod-gate lifted:` note")
    body = ("Lot #%d \"%s\": its frozen content is merged into %s (%s, %s), but the "
            "closure is withheld: %s. The lot stays %s; %s."
            % (int(row["id"]), row.get("title") or "", target,
               entry.get("how") or "?", ref[:12] or "?", retention["motif"],
               row.get("state") or "?", lift))
    try:
        mail.send(db, work_mod.SYSTEM_SENDER, recipient, body, kind="event",
                  work_item_id=str(int(row["id"])),
                  payload={"closure_withheld": {
                      "lot": int(row["id"]), "reason": retention["what"],
                      "target": target, "how": entry.get("how"),
                      "ref": ref}})
    except Exception:  # noqa: BLE001 — le journal porte déjà la retenue
        pass


def sync_merges(db, repo_dir: str, *, target: str | None = None, dry_run: bool = False,
                limit: int = 1000) -> dict:
    """Ferme les lots dont le contenu gelé est entré dans la cible.

    Sans `target` explicite (#1005), la cible est celle du lot
    (`branch_target`), sinon celle du dépôt (`git config ameesh.target`),
    sinon sa cible par défaut (`origin/HEAD`, puis main/master) ; quand rien
    n'est déclaré et que cette cible ne porte pas le gel, les refs
    d'intégration reconnues (`develop`, `main`, `master`, `release/…`) sont
    essayées à leur tour et la fermeture nomme celle qui a livré. Une cible
    explicite ou déclarée garde le sens d'origine : seule elle ferme, et son
    absence est une erreur (`target %r not found in …`).

    #2786 : le dernier gel examiné doit nommer un commit complet ; une ref
    symbolique (`HEAD`, une branche) ou une abréviation vaut doute (`doubt`),
    rien n'est résolu dans ce clone ni fermé."""
    st = storage.of(db)
    if target is not None:
        try:
            if _target_tip(repo_dir, target) is None:
                raise MergeProbeError("target %r not found in %s" % (target, repo_dir))
        except GitError as exc:
            raise MergeProbeError(str(exc)) from exc
        declared_repo, default, candidates = None, target, []
    else:
        try:
            declared_repo = declared_target(repo_dir)
            default = default_target(repo_dir)
            candidates = target_candidates(repo_dir, default)
        except GitError as exc:
            raise MergeProbeError(str(exc)) from exc
        if default is None and candidates:
            # aucun `origin/HEAD` ni main/master : la première ref
            # d'intégration tient lieu de cible par défaut
            default, candidates = candidates[0], candidates[1:]
    rows = [r for r in st.work.items(state=None, assignee=None, limit=limit)
            if r["state"] not in work_mod.MERGED_STATES and r["state"] != "closed"]
    declared: dict = {}
    for ms in st.progress.lot_milestones([int(r["id"]) for r in rows]):
        declared.setdefault(int(ms["work_item_id"]), []).append(ms)
    # #2443 : l'appartenance enregistrée à la composition, lue une fois pour
    # tous les lots examinés. Une base d'avant 0465 ne fait pas échouer le
    # relevé : le repli par le gel reste inchangé, l'absence est dite.
    packet_error = ""
    packets: dict[int, list[dict]] = {}
    try:
        from ...db import DbError
        members = st.work.packet_members(item_ids=[int(r["id"]) for r in rows])
    except DbError as exc:                     # table absente (schéma ancien)
        members, packet_error = [], str(exc)
    for member in members:
        packets.setdefault(int(member["work_item_id"]), []).append(member)
    projects: dict = {}                      # #5110 : app → cible déclarée du projet
    results: list[dict] = []
    for row in sorted(rows, key=lambda r: int(r["id"])):
        lot = int(row["id"])
        marks = declared.get(lot, [])
        freeze = latest_freeze(marks)
        sha = ((freeze or {}).get("sha") or "").strip()
        frozen_id = int(freeze["id"]) if freeze else None
        freeze_origin = "frozen" if sha else None
        unchecked = None
        app = (row.get("app") or "").strip()
        if app not in projects:
            # la ref publiée (`origin/develop`), comme le relevé des branches
            declared_project = _project_target(db, app)
            projects[app] = (published_target(repo_dir, declared_project)
                             if declared_project else "")
        project = projects[app]
        if not sha:
            # #2399 (cas #2366) : un avis `ok` sur la tête publiée vaut gel
            # effectif (`freeze.origin=verdict`, #1756) ; sans jalon `frozen`,
            # c'est le contenu relu qui entre dans la cible. Un avis sur une
            # autre tête ne prouve rien : la branche a avancé depuis.
            from ... import merge_queue
            candidate = freeze_candidate(
                marks, repo_dir, target or row.get("branch_target") or project or default,
                waived=merge_queue.is_waived(row))
            # #2444 : la tête confrontée est la tête publiée (origin d'abord) ;
            # une branche disparue ne vaut pas un contrôle de la tête.
            reviewed = commit_id(candidate.get("sha"))
            if candidate.get("origin") == "verdict" and reviewed:
                head = _published_tip(repo_dir, row["branch"]) if row.get("branch") else None
                if head is None:
                    unchecked = "verdict freeze not checked: branch head not found"
                elif same_commit(reviewed, head):
                    sha, freeze_origin = head, "verdict"
        entry = {"work_item": lot, "package": row.get("package_id"),
                 "state": row["state"], "result": "no-commit", "how": None, "ref": None,
                 "target": None, "off_target": None, "freeze_origin": freeze_origin,
                 "detail": unchecked or (
                     "last freeze has no commit (milestone frozen --sha)" if freeze
                     else "no freeze declared (milestone frozen --sha)")}
        if sha:
            # #2786 : le gel examiné doit nommer un commit complet. Un `HEAD`, une
            # branche ou une abréviation n'est jamais résolu ici : le balayage
            # d'un autre clone lirait un commit que personne n'a relu (#2712).
            resolved_sha = full_commit_id(sha)
            if not resolved_sha:
                entry.update(result="doubt", how="unreadable",
                             detail="last freeze %r is not a full commit id: doubt, "
                                    "nothing merged — freeze again on the full head "
                                    "(`git rev-parse <ref>`)" % sha[:24])
                results.append(entry)
                continue
            sha = resolved_sha
        # #2443 : ne pas sortir sur l'absence de gel déclaré avant d'avoir
        # examiné une composition de paquet — c'est la voie du cas #2366.
        refs = _content_refs(row, target, declared_repo=declared_repo, default=default,
                             candidates=candidates, project=project)
        if not refs:
            entry.update(result="no-target",
                         detail="no target: `git config %s <branch>` in %s, or --target"
                                % (TARGET_GIT_KEY, repo_dir))
            results.append(entry)
            continue
        composition = _packet_delivery(repo_dir, lot, packets.get(lot, []), refs,
                                       declared.get(lot, []), default=default)
        if composition is not None:
            member = composition["member"]
            entry.update(target=composition["target"],
                         off_target=composition["off_target"], how="composition",
                         ref=composition["proof"]["ref"], sha=member["unit_sha"],
                         packet=int(member["package_id"]), result="open",
                         detail=("%s is the composition unit of package #%s (%s) in %s"
                                 % ((member["unit_sha"] or "")[:12],
                                    member["package_id"],
                                    member.get("package_branch") or "?",
                                    composition["target"])),
                         composition={"package": int(member["package_id"]),
                                      "branch": member.get("package_branch") or "",
                                      "unit": member["unit_sha"]})
            if dry_run:
                # #2744 : la même retenue que la fermeture par le gel ;
                # #3219 : celle de l'assigné est levée par le contenu.
                retention = dry_run_retention(db, st, row, sha=entry["ref"] or "",
                                              repo=repo_dir)
                if retention is not None and not retention.get("lift"):
                    mark_retained(entry, retention, target=composition["target"],
                                  dry_run=True)
                    results.append(entry)
                    continue
                if retention is not None:
                    mark_lifted(entry, retention)
                entry["result"] = "would-merge"
            else:
                done = work_mod.deliver(
                    db, lot, connector=work_mod.merge_connector(),
                    evidence={"seen": "package composition #%s (%s)"
                              % (member["package_id"], member.get("package_branch") or "?"),
                              "sha": entry["ref"] or ""},
                    actor="composition:%s" % member["package_id"],
                    frozen_id=int(freeze["id"]) if freeze else None,
                    # #2444 : aucun gel déclaré examiné (gel d'origine verdict) ;
                    # un gel apparu entre-temps refuse la fermeture.
                    unfrozen=freeze is None,
                    repo=repo_dir,      # L539 : dépôt du relevé, indice de classe
                    content_guard=content_retention)   # #2744 : verrou commun avec le hold
                entry["result"] = done["result"]
                if done["result"] in ("withheld", "error"):
                    mark_retained(entry, done["retention"],
                                  target=composition["target"])
                    if done["result"] == "withheld":
                        record_content_withheld(
                            db, st, row, entry, done["retention"],
                            target=composition["target"],
                            actor="composition:%s" % member["package_id"])
                else:
                    entry["detail"] = "%s — %s" % (entry["detail"], done["detail"])
            results.append(entry)
            continue
        if not sha:
            if packets.get(lot):
                entry["detail"] += ("; recorded packet composition #%s not delivered yet"
                                    % ", #".join(str(m["package_id"])
                                                 for m in packets[lot]))
            results.append(entry)
            continue
        found_target = None              # la ref qui a livré le gel
        found_proof = None
        off_default = None               # la cible par défaut qu'elle n'est pas
        first_ref = first_proof = None   # la ref autoritaire, pour le compte rendu
        residue = None                   # une ref qui la porte sans pouvoir fermer
        reverted = None                  # un gel livré puis annulé (#1825)
        error = None
        for ref, off, closes in refs:
            try:
                proof = probe(
                    repo_dir, sha, ref, lot=lot,
                    frozen_at=(freeze or {}).get("at_ts")
                    if freeze_origin == "frozen" else None,
                    frozen_absent=freeze_absent((freeze or {}).get("note"))
                    if freeze_origin == "frozen" else ())
            except (MergeProbeError, GitError) as exc:
                if off:
                    residue = residue or "%s is unreadable (%s)" % (ref, exc)
                    continue
                error = str(exc)
                break
            if first_proof is None:
                first_ref, first_proof = ref, proof
            if not proof["merged"]:
                # #1825 : le gel a été livré sur cette ref puis annulé ; le
                # dire, même quand la cible par défaut ne le porte pas du tout
                if proof["how"] == "reverted" and reverted is None:
                    reverted = (ref, proof)
                continue
            if closes:
                found_target, found_proof, off_default = ref, proof, (default if off else None)
            else:
                residue = ("%s carries the frozen content (%s), but the declared target "
                           "%s does not: declare `git config %s %s` to close it explicitly"
                           % (ref, proof["how"], default, TARGET_GIT_KEY, ref))
            break
        if error is not None:
            entry.update(result="error", detail=error)
            results.append(entry)
            continue
        if found_target is None:
            entry.update(target=first_ref, sha=sha, result="open",
                         detail=first_proof["detail"] if first_proof else entry["detail"])
            if reverted is not None and (first_proof is None
                                         or first_proof["how"] != "reverted"):
                # #1825 : la cible par défaut ne porte pas le gel (« missing »),
                # mais une ref d'intégration l'a livré puis annulé : le relevé
                # dit le revert plutôt qu'une absence trompeuse
                entry.update(target=reverted[0], how="reverted",
                             detail=reverted[1]["detail"])
            if residue:
                entry.update(result="off-target", detail=residue)
            results.append(entry)
            continue
        entry.update(target=found_target, off_target=off_default, how=found_proof["how"],
                     ref=found_proof["ref"], sha=sha, result="open",
                     detail=found_proof["detail"])
        if dry_run:
            retention = dry_run_retention(db, st, row, sha=entry["ref"] or "",
                                          repo=repo_dir)
            if retention is not None and not retention.get("lift"):
                mark_retained(entry, retention, target=found_target, dry_run=True)
                results.append(entry)
                continue
            if retention is not None:
                mark_lifted(entry, retention)
            entry["result"] = "would-merge"
        else:
            done = work_mod.deliver(
                db, lot, connector=work_mod.merge_connector(),
                evidence={"seen": "content (%s)" % entry["how"], "sha": entry["ref"] or ""},
                actor="git:%s" % found_target, frozen_id=frozen_id,
                unfrozen=frozen_id is None,   # #2444 : un gel apparu entre-temps refuse
                repo=repo_dir,          # L539 : dépôt du relevé, indice de classe
                content_guard=content_retention)   # #2744 : verrou commun avec le hold
            entry["result"] = done["result"]
            if done["result"] in ("withheld", "error"):
                mark_retained(entry, done["retention"], target=found_target)
                if done["result"] == "withheld":
                    record_content_withheld(
                        db, st, row, entry, done["retention"], target=found_target,
                        actor="git:%s" % found_target)
            else:
                entry["detail"] = "%s — %s" % (entry["detail"], done["detail"])
        results.append(entry)
    report = {"git_dir": repo_dir, "target": target if target is not None else (default or ""),
              "auto": target is None, "off_target": default if target is None else None,
              "dry_run": bool(dry_run), "results": results,
              "merged": sum(1 for r in results if r["result"] == "merged")}
    if packet_error:
        report["packet_error"] = packet_error
    return report


# --------------------------------------------------------------------------
# L118 : fusion d'une BRANCHE constatée, avec ou sans PR
# --------------------------------------------------------------------------
#
# Une équipe qui fusionne directement sur sa branche cible ne passe ni par
# une PR (`sync-github`) ni par un gel déclaré (`sync-merges`) : ses lots
# restaient ouverts après la fusion. Un lot peut porter sa branche
# (`work add|assign --branch`, ou déduite d'un message `mail send --lot` qui
# cite une branche `agent/…`) ; le relevé périodique de l'exécuteur
# (`sync_branches`) la cherche dans le dépôt de l'assigné — son dossier de
# travail au registre, posé par `policy.work_dirs`/`work_roots` du canon
# (L35) : tous les worktrees d'un dépôt partagent ses références.
#
# Une branche tout juste créée depuis sa cible en est déjà « ancêtre » : ce
# n'est pas une fusion. La fusion est constatée, dans cet ordre :
#
# 1. la pointe de la branche est ancêtre de la cible SANS être sur sa chaîne
#    de premiers parents (un commit de fusion l'y a fait entrer) ;
# 2. la pointe est sur cette chaîne (avance rapide, ou branche sans travail) :
#    le dernier commit vu EN AVANCE par un relevé précédent (`branch_head`)
#    est entré dans la cible (ancêtre, patch-id ou squash : `probe`) ;
# 3. branche en avance : son contenu est entré par patch-id ou squash
#    (`probe`) ;
# 4. branche supprimée : le dernier commit vu (`probe`), sinon un commit de
#    fusion de la cible dont le message cite la branche (nom entier).
#
# Correctif du 2026-10-11 : une équipe fusionne aussi en local, sans PR, des
# lots SANS branche. Le relevé examine désormais tout lot ouvert assigné à un
# agent de l'hôte, avec ou sans branche : à défaut de preuve par la branche,
# un commit de FUSION de la cible, postérieur à la création du lot, le
# désigne par une ligne `ameesh-work: <id>` (n'importe où dans le message,
# même forme que dans le corps d'une PR) ou, seulement si le dépôt l'active
# (`git config ameesh.lotRef hash`), par `#<id>` dans son TITRE. Ailleurs
# « (#45) » est un numéro de PR GitHub : il fermerait le mauvais lot.
#
# Lecture seule du dépôt, aucun `fetch`. La cible : celle du lot, sinon
# `git config ameesh.target` du dépôt (une valeur introuvable est une
# erreur, jamais un repli silencieux sur main), sinon `origin/HEAD`, sinon
# main. #746 : quand ni le lot ni le dépôt ne déclarent de cible et que
# celle-ci (`origin/HEAD`, figé à la création du clone) ne porte pas la
# fusion, le relevé essaie les refs d'intégration reconnues à leur nom
# (`develop`, `main`, `master`, `release/…`) et ferme sur la première qui la
# porte ; jamais une branche de travail (`agent/`, `lot/`, `review/`…), où
# une fusion n'est pas une livraison.

#: une branche citée dans un message (`agent/…`)
CITED_BRANCH_RE = re.compile(r"(?<![\w./-])(agent/[A-Za-z0-9._/-]*[A-Za-z0-9_-])")
#: nom de référence admis pour une branche ou une cible
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
#: caractères qui prolongent un nom de branche (citation à nom entier)
_REF_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-")
#: commits de fusion lus au plus pour une citation
MAX_MERGE_MENTIONS = 200
#: lots examinés au plus par relevé
MAX_BRANCH_LOTS = 1000
#: #312 r1 : corps de PR lus au plus par relevé (`gh`, 60 s chacun au pire)
MAX_PR_BODY_READS = 20
#: #312 r1 : après un échec de `gh` sur une PR, plus de lecture de son corps
#: pendant ce délai, doublé à chaque échec suivant (plafond), oublié après
#: une lecture réussie ; et au plus un échec par dépôt et par relevé
PR_BODY_BACKOFF_S = 60.0
PR_BODY_BACKOFF_MAX_S = 3600.0
#: (dépôt, PR) → (pas avant, délai courant) ; état du processus (relevés successifs)
_pr_body_backoff: dict = {}
#: #312 r2 : (dépôt, PR) → corps déjà lu d'une PR fusionnée (il ne change
#: plus) : relu sans appel ni lecture comptée, les relevés suivants lisent
#: les PR suivantes ; état du processus, borné
_pr_body_cache: dict = {}
PR_BODY_CACHE_MAX = 5000
#: clé de configuration git du dépôt qui nomme la cible par défaut
TARGET_GIT_KEY = "ameesh.target"
DEFAULT_TARGETS = ("origin/main", "main", "origin/master", "master")
#: #746 : refs d'intégration du remote `origin` où un lot peut être livré,
#: quand la cible déclarée (celle du lot ou `ameesh.target`) ne le porte pas.
#: Liste blanche fermée, essayée dans cet ordre : ni une branche de travail
#: (`agent/`, `lot/`, `review/`…), ni un autre remote — une fusion citante
#: dans une branche de travail (empilement) fermerait le lot à tort.
INTEGRATION_BRANCHES = ("develop", "main", "master")
#: #746 : préfixes d'intégration admis (`release/1.10.0`)
INTEGRATION_PREFIXES = ("release/",)
#: #4562 : une branche de porteur de paquet (`packet_link.PACKET_PREFIX`).
#: Sa tête n'est une observation de la forge que si elle y est publiée : la
#: composition de `paquet-auto` crée la branche locale avant de la pousser.
PACKET_BRANCH_PREFIX = "integration/"
#: #746 : refs d'intégration essayées au plus par dépôt et par relevé
MAX_TARGET_CANDIDATES = 8
#: une ligne `ameesh-work: <id>` désigne un lot précis (message d'un commit de
#: fusion, ou corps d'une PR : `plan_github`) ; le mot `final` après le numéro
#: déclare cette PR comme la dernière étape du lot (#759)
WORK_TRAILER_RE = re.compile(
    r"(?im)^[ \t>*-]*ameesh-work[ \t]*:[ \t]*#?([1-9][0-9]{0,17})"
    r"(?:[ \t]+(final))?[ \t]*$")
#: L118b : une ligne `ameesh-work-partial: <id>` (corps d'une PR, message d'un
#: commit de fusion) : la PR ne livre qu'une partie du lot, qui reste ouvert
#: (lots parapluie, #139)
PARTIAL_TRAILER_RE = re.compile(
    r"(?im)^[ \t>*-]*ameesh-work-partial[ \t]*:[ \t]*#?([1-9][0-9]{0,17})[ \t]*$")
#: L118b : le titre d'un commit de fusion d'une branche `lot/<id>-…` : celui de
#: GitHub (« Merge pull request #N from owner/lot/<id>-… », `pr` : N) ou
#: d'une fusion locale (« Merge branch 'lot/<id>-…' [into …] »)
LOT_BRANCH_MERGE_RE = re.compile(
    r"^Merge (?:pull request #(?P<pr>[1-9][0-9]{0,17}) from [A-Za-z0-9_.-]+/"
    r"|(?:remote-tracking )?branch '(?:origin/)?)"
    r"(?P<branch>lot/(?P<lot>[1-9][0-9]{0,17})(?:-[A-Za-z0-9._/-]*)?)'?"
    r"(?: into [^\s]+)?[ \t]*$")
#: #1005 : le numéro de PR d'un commit de fusion GitHub, QUELLE QUE SOIT la
#: branche fusionnée (« Merge pull request #N from owner/<branche> ») : la PR
#: porteuse d'un gel, même sans citer le lot, a un corps à lire
GITHUB_MERGE_PR_RE = re.compile(r"^Merge pull request #([1-9][0-9]{0,17}) from \S+")
#: #1400 : la source d'une fusion GitHub (« Merge pull request #N from
#: <propriétaire>/<branche> ») : le propriétaire est le premier segment, la
#: branche le reste (elle peut contenir des `/`, `agent/…`)
GITHUB_MERGE_SOURCE_RE = re.compile(
    r"^Merge pull request #(?P<pr>[1-9][0-9]{0,17}) from "
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<branch>\S+?)[ \t]*$")
#: `#<id>` dans le titre d'un commit de fusion (« Merge #93 … », « … (#91) ») ;
#: ni `PR#9`, ni `depot#9`, ni `##9`, ni `#9a`
HASH_REF_RE = re.compile(r"(?<![\w/#&])#([1-9][0-9]{0,17})(?!\w)")
#: #2259 : séparateurs de proposition d'un titre de fusion ; une négation ne
#: vaut que dans sa proposition (« sans #598 / #599 livré » garde #599)
CLAUSE_SPLIT_RE = re.compile(r"[;/|—–]|\s+-\s+|\(|\)|,")
#: #2259 : mots qui nient les `#<id>` de leur proposition — « recomposition
#: sans #598 » (commit 84c68e9d4, Nexlink) désignait #598 et fermait son lot
#: alors que sa PR était ouverte et sa tête hors de la cible. Une préposition
#: ne nie que ce qui la suit (« #2221 sans #598 » garde 2221, « sauf #598 »
#: exclut 598) ; un participe nie toute la proposition (« #598 retiré »,
#: « (#598) exclu »).
PREFIX_NEGATED_REF_RE = re.compile(r"\b(?:sans|sauf|ni|hors|without|except)\b",
                                   re.IGNORECASE)
POSTFIX_NEGATED_REF_RE = re.compile(
    r"\b(?:retir[ée]e?s?|exclue?s?|exclu[ea]nt|reverts?|reverted)\b",
    re.IGNORECASE)
#: #2262 : un groupe parenthésé qui ouvre sur un contraste porte sa propre
#: proposition positive et ne subit pas la négation du parent
#: (« sans #12 (mais #13 livré) » garde 13, « sans (#598) » nie 598).
POSITIVE_GROUP_RE = re.compile(
    r"^(?:mais|however|toutefois|cependant|néanmoins|en\s+revanche|but)\b",
    re.IGNORECASE)
#: clé de configuration git du dépôt : comment ses commits de fusion citent un
#: lot en plus de `ameesh-work:` — `hash` : `#<id>` dans le titre
LOT_REF_GIT_KEY = "ameesh.lotRef"
LOT_REF_HASH = "hash"
#: commits de fusion lus au plus par dépôt et par relevé (les plus récents)
MAX_MERGE_SCAN = 500
#: #1005 B2 : fusions lues au plus sur la chaîne première-parente d'une cible
#: pour trouver le porteur d'un gel (la ligne de trop dit la troncature) ;
#: au-delà, le porteur reste indéterminé et le lot ouvert
MAX_CARRIER_SCAN = 500
#: écart d'horloge admis (s) entre la base (création du lot) et le poste qui
#: a fait le commit de fusion (date à la seconde)
CLOCK_SLACK_S = 120.0


def check_ref(name: str, what: str = "branch") -> str:
    """Un nom de branche (ou de cible) admissible, sinon ValueError."""
    text = (name or "").strip()
    if not _REF_RE.match(text) or ".." in text or "//" in text or text.endswith(
            ("/", ".", ".lock")) or "@{" in text:
        raise ValueError("invalid %s: %r (letters, digits, . _ / -)" % (what, name))
    return text


def cited_branch(text: str) -> str | None:
    """La branche `agent/…` citée par un message, si elle est UNIQUE (deux
    branches citées : on ne devine pas)."""
    found = {m.rstrip(".") for m in CITED_BRANCH_RE.findall(text or "")}
    found = {b for b in found if b != "agent/"}
    return next(iter(found)) if len(found) == 1 else None


def declared_target(repo: str, *, deadline: Deadline | None = None) -> str | None:
    """La cible déclarée du dépôt (`git config ameesh.target`), ou None :
    une cible déclarée fait foi, le relevé ne cherche pas ailleurs."""
    return _out(repo, "config", "--get", TARGET_GIT_KEY, **_bounded(deadline)) or None


def default_target(repo: str, *, deadline: Deadline | None = None) -> str | None:
    """La cible par défaut du dépôt : `git config ameesh.target`, sinon la
    branche par défaut du dépôt distant (`origin/HEAD`), sinon main/master.

    Une cible configurée mais introuvable (faute de frappe, `origin/develop`
    jamais récupérée) lève MergeProbeError : se rabattre sur main ferait
    chercher les fusions au mauvais endroit, sans le dire. Avec ``deadline``
    (#3572), chaque lecture ne prend que le temps qui reste ; épuisée, elle
    lève `GitDeadline`."""
    bound = _bounded(deadline)
    configured = declared_target(repo, **bound)
    if configured:
        if _commit(repo, configured, **bound):
            return configured
        raise MergeProbeError("%s = %s not found in %s (no fetch is done: "
                              "fix the value or fetch the branch)"
                              % (TARGET_GIT_KEY, configured, repo))
    head = _out(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD",
                **bound)
    if head and _commit(repo, head, **bound):
        return head
    for candidate in DEFAULT_TARGETS:
        if _commit(repo, candidate, **bound):
            return candidate
    return None


def integration_branch(name: str) -> bool:
    """#746 : la ref est-elle une ref d'intégration de la liste blanche
    (`develop`, `main`, `master`, `release/*`) ? Rien d'autre : ni une
    branche de travail, ni un autre remote."""
    low = short_branch(name).lower()
    return bool(low) and (low in INTEGRATION_BRANCHES
                          or low.startswith(INTEGRATION_PREFIXES))


def target_candidates(repo: str, primary: str | None,
                      limit: int = MAX_TARGET_CANDIDATES) -> list[str]:
    """#746 : les refs d'intégration du remote `origin` à essayer après
    `primary` (la cible par défaut du clone), quand celle-ci ne porte pas la
    fusion : la liste blanche `INTEGRATION_BRANCHES`, puis `release/*` de la
    plus récente à la plus ancienne, au plus `limit`.

    Lecture seule, sans fetch ; aucun autre remote, aucune branche de
    travail : une branche `agent/…` ou `lot/…` née de la cible porte ses
    commits de fusion sans que le lot y soit livré."""
    out = _out(repo, "for-each-ref", "--sort=-committerdate",
               "--format=%(refname:short)", "refs/remotes/origin")
    prime = short_branch(primary) if primary else None
    found = []
    for ref in (out or "").split():
        name = short_branch(ref)
        if not name or name == "HEAD" or name == prime:
            continue
        if integration_branch(name):
            found.append(ref)
    order = {name: i for i, name in enumerate(INTEGRATION_BRANCHES)}
    found.sort(key=lambda ref: order.get(short_branch(ref), len(order)))
    return found[:limit]


def _tip(repo: str, branch: str) -> str | None:
    for ref in ("refs/heads/" + branch, "refs/remotes/origin/" + branch):
        found = _commit(repo, ref)
        if found:
            return found
    return None


def _target_ref(repo: str, target: str) -> str:
    """#3747 : résoudre une cible nue pour toutes les lectures de sa preuve."""
    if not target.startswith(("origin/", "refs/")):
        published = "refs/remotes/origin/" + target
        if _commit(repo, published):
            return published
    return target


def _target_tip(repo: str, target: str) -> str | None:
    return _commit(repo, _target_ref(repo, target))


def _published_tip(repo: str, branch: str) -> str | None:
    """#2444 : la tête publiée d'abord (`origin/<branche>`), la locale à
    défaut — un avis se confronte à ce que la forge porte, pas à un clone."""
    for ref in ("refs/remotes/origin/" + branch, "refs/heads/" + branch):
        found = _commit(repo, ref)
        if found:
            return found
    return None


#: #2654 : réponse de `git ls-remote --exit-code` quand la ref n'existe pas
_LS_REMOTE_ABSENT = 2


def _remote_head(repo: str, branch: str) -> tuple[str, str | None]:
    """#2654 : la tête de `branch` sur la forge (`git ls-remote origin`), lue
    au moment de conclure — un clone peut ignorer une branche recréée ou
    poussée depuis son dernier fetch. Rend `("local", None)` sans remote
    `origin` (rien à confronter), `("absent", None)` si la forge n'a pas la
    branche, `("sha", <sha>)`, ou `("error", <raison>)`."""
    if _out(repo, "remote", "get-url", "origin") is None:
        return "local", None
    try:
        proc = _git(repo, "ls-remote", "--exit-code", "origin", "refs/heads/" + branch,
                    timeout=FETCH_TIMEOUT)
    except GitError as exc:
        return "error", str(exc)
    if proc.returncode == _LS_REMOTE_ABSENT:
        return "absent", None
    words = proc.stdout.decode("utf-8", "replace").split()
    if proc.returncode != 0 or not words or not _SHA_RE.match(words[0]):
        detail = proc.stderr.decode("utf-8", "replace").strip()
        return "error", "git ls-remote origin: %s" % (detail[-200:] or "unreadable")
    return "sha", words[0]


def _remote_moved(repo: str, branch: str, target: str, out: dict,
                  found: dict) -> dict:
    """#2654 : `found` conclut à la livraison de `branch` sur une pointe du
    clone ou une tête vue (`branch_head`) ; elle ne tient que si la tête
    publiée de la branche est déjà dans la cible. Une forge illisible, ou une
    branche poussée depuis (recréée sous le même nom, #2623) avec du travail
    hors de la cible, laisse le lot ouvert ; la nouvelle pointe, présente au
    clone, est rendue en avance (l'appelant la retient)."""
    kind, value = _remote_head(repo, branch)
    if kind in ("local", "absent"):
        return found
    if kind == "error":
        return dict(out, tip=None, ahead=False,
                    detail="%s; but the head of %s on origin is unreadable (%s): no "
                           "conclusion" % (found.get("detail"), branch, value))
    present = _commit(repo, value)
    if present and (present == found.get("tip")
            or probe(repo, present, target, lot=branch_lot(branch))["merged"]):
        # la tête publiée est celle de la preuve, ou elle est entrée elle
        # aussi (ancêtre, contenu identique : squash)
        return found
    if present:
        return dict(out, tip=present, ahead=True,
                    detail="%s ahead of %s (%s, origin): pushed since the evidence "
                           "(%s), not concluded" % (branch, target, present[:12],
                                                    found.get("detail")))
    return dict(out, tip=None, ahead=False,
                detail="%s is at %s on origin, absent from this clone (pushed since "
                       "the last fetch): no conclusion on %s"
                       % (branch, value[:12], found.get("detail")))


def _on_first_parent(repo: str, commit: str, head: str) -> bool:
    chain = _out(repo, "rev-list", "--first-parent", "--max-count=%d" % MAX_TARGET_COMMITS,
                 head)
    return bool(chain) and commit in chain.split()


def _mentions(message: str, branch: str) -> bool:
    start = message.find(branch)
    while start >= 0:
        end = start + len(branch)
        before = message[start - 1] if start else " "
        after = message[end] if end < len(message) else " "
        if before not in _REF_CHARS and (after not in _REF_CHARS or (
                after == "." and (end + 1 >= len(message)
                                  or message[end + 1] not in _REF_CHARS))):
            return True
        start = message.find(branch, start + 1)
    return False


def recorded_branch_merge(title: str, branch: str, *, owner: str) -> bool:
    """#1400 : ce titre de fusion désigne-t-il la branche ENREGISTRÉE du lot ?

    Deux formes, jamais une simple mention : la fusion locale qui nomme la
    branche telle quelle (« Merge branch 'agent/x' », y compris
    « remote-tracking … 'origin/agent/x' » — `origin` est le remote vérifié du
    clone), et la fusion GitHub (« Merge pull request #N from
    <propriétaire>/<branche> »). Pour la forme GitHub, le propriétaire cité doit
    être `owner`, celui du dépôt canonique du lot : une branche homonyme venue
    d'un fork (dépôt étranger) ne prouve rien. La comparaison est exacte :
    `agent/x-2` ne cite pas `agent/x`.
    """
    text = (title or "").strip()
    branch = (branch or "").strip()
    if not text or not branch:
        return False
    if re.match(r"^Merge (?:remote-tracking )?branch '(?:origin/)?%s'"
                r"(?: into [^\s]+)?$" % re.escape(branch), text):
        return True
    match = GITHUB_MERGE_SOURCE_RE.match(text)
    if not match or match.group("branch") != branch:
        return False
    return match.group("owner").lower() == (owner or "").strip().lower()


def merge_mentioning(repo: str, branch: str, head: str,
                     since_ts: float | None = None) -> str | None:
    """Le plus récent commit de FUSION de `head` dont le message cite la
    branche (nom entier : `agent/x` ne cite pas `agent/x-2`), ou None. Un
    commit ordinaire qui cite la branche (compte rendu de revue) ne compte pas."""
    args = ["log", "--merges", "--format=%H%x00%B%x1e", "-F", "--grep=%s" % branch,
            "--max-count=%d" % MAX_MERGE_MENTIONS]
    if since_ts:
        args.append("--since=@%d" % int(since_ts))
    out = _out(repo, *args, head)
    for record in (out or "").split("\x1e"):
        sha, _sep, message = record.strip().partition("\x00")
        if sha and _mentions(message, branch):
            return sha
    return None


def branch_probe(repo: str, branch: str, target: str, *, seen_head: str | None = None,
                 seen_at: float | None = None,
                 since_ts: float | None = None,
                 frozen_sha: str | None = None) -> dict:
    """La branche est-elle entrée dans `target` ? Rend `{merged, how, ref, tip,
    ahead, detail}` ; `ahead` : la pointe a du travail absent de la cible
    (l'appelant la retient comme `branch_head`).

    #2654 : une livraison prouvée par la branche (pointe du clone ou tête vue)
    n'est rendue qu'après lecture de sa tête sur la forge (`_remote_moved`).

    #3326 : `seen_at` est l'instant (horloge de la base) où `branch_head` a
    été enregistré. Une tête qui était déjà dans la cible à cet instant n'a
    aucun commit propre : le reflog des refs de la cible le dit, et la preuve
    « vu-ancestor » est refusée (`_was_ancestor_at_seen`).

    #4508 : une tête vue en avance n'est une preuve que si c'est un commit
    de la branche du lot. Sa base (le commit d'un autre lot) ou la pointe
    d'un porteur ne la prouvent pas (`_own_commit_evidence`) : la branche
    locale n'a pas bougé depuis sa création, ou une autre branche porte
    déjà cette pointe."""
    target = _target_ref(repo, target)
    head = _commit(repo, target)
    if head is None:
        raise MergeProbeError("target %r not found in %s" % (target, repo))
    tip = _tip(repo, branch)
    out = {"merged": False, "how": None, "ref": None, "tip": tip, "ahead": False}
    found = _branch_probe(repo, branch, target, head, tip, out, seen_head=seen_head,
                          seen_at=seen_at, since_ts=since_ts, frozen_sha=frozen_sha)
    if found["merged"] and found["how"] != "message":
        # la citation par une fusion de la cible ne dépend pas de la pointe
        return _remote_moved(repo, branch, target, out, found)
    if found.pop("check_remote", False):
        # #4508 : la tête vue a été refusée faute de travail propre, mais la
        # forge peut avoir avancé depuis (branche recréée ou poussée, #2654) :
        # la pointe publiée, si elle diffère, est rendue en avance.
        return _remote_moved(repo, branch, target, out, found)
    return found


def _branch_probe(repo: str, branch: str, target: str, head: str, tip: str | None,
                  out: dict, *, seen_head: str | None, seen_at: float | None,
                  since_ts: float | None, frozen_sha: str | None = None) -> dict:
    # #3326 : la tête vue était-elle déjà une ancêtre de la cible quand le
    # relevé l'a vue ? Sa date de commit ne le dit pas (une avance rapide la
    # rend ancêtre après coup) ; le reflog de la cible, si. Le reflog n'est
    # lu qu'avant de conclure (pas pour une branche simplement en avance).
    stale: list[bool | None] = [None]

    def seen_stale() -> bool:
        if stale[0] is None:
            stale[0] = bool(seen_head and seen_at and _was_ancestor_at_seen(
                repo, seen_head, target, seen_at))
        return bool(stale[0])

    # #4508 : la tête vue n'est une preuve que si c'est un commit de la
    # branche — ni sa base (celle d'un autre lot), ni la pointe d'un porteur.
    own: dict[str, str | None] = {}

    def own_refusal() -> str | None:
        if not seen_head:
            return None
        if seen_head not in own:
            ok, why = _own_commit_evidence(repo, branch, seen_head, target, frozen_sha)
            own[seen_head] = None if ok else why
        return own[seen_head]

    def refusal() -> str | None:
        if seen_stale():
            return ("last commit seen ahead (%s) was already an ancestor of %s when "
                    "the sweep saw it: no work of its own" % (seen_head[:12], target))
        why = own_refusal()
        if why:
            return ("last commit seen ahead (%s): %s — no work of its own"
                    % (seen_head[:12], why))
        return None

    def seen() -> dict | None:
        if not seen_head or seen_stale() or own_refusal():
            return None
        found = probe(repo, seen_head, target, lot=branch_lot(branch))
        if found["merged"]:
            return dict(out, merged=True, how="vu-" + found["how"], ref=found["ref"],
                        detail="last commit seen ahead (%s): %s"
                               % (seen_head[:12], found["detail"]))
        return None

    def mention(why: str) -> dict:
        sha = merge_mentioning(repo, branch, head, since_ts)
        if sha:
            stopped = _merge_reverted(repo, sha, target)
            if stopped:
                return dict(out, merged=False, how="reverted",
                            detail="merge commit %s of %s that cites %s was reverted "
                                   "(%s)" % (sha[:12], target, branch,
                                             stopped["ref"][:12]))
            return dict(out, merged=True, how="message", ref=sha,
                        detail="merge commit %s of %s that cites %s" % (sha[:12], target,
                                                                          branch))
        return dict(out, detail=why)

    if tip is None:
        return seen() or mention(
            refusal() or "branch %s not found (neither local nor origin/)" % branch)
    if _git(repo, "merge-base", "--is-ancestor", tip, head).returncode == 0:
        lot = branch_lot(branch)
        stopped = reverted_content(repo, tip, target, lot=lot, branch=branch)
        if stopped:
            return dict(out, merged=False, how="reverted",
                        detail="%s (%s) entered %s through a merge commit, but %s "
                               "reverts its content"
                               % (branch, tip[:12], target, stopped["ref"][:12]))
        if not _on_first_parent(repo, tip, head):
            # #2543 : une pointe entrée par un commit de fusion n'est pas une
            # preuve en soi — une branche créée sur un commit de paquet déjà
            # fusionné a exactement cette forme, sans travail propre. La voie
            # « ancestor » n'est retenue que si cette pointe a déjà été vue en
            # avance pour le lot (`branch_head`), ou qu'une fusion de la cible
            # cite la branche ; sinon le lot ne bouge pas.
            # #3326 : une tête vue alors qu'elle était déjà dans la cible ne
            # prouve rien non plus (cible périmée au relevé).
            if seen_head and not seen_stale() and not own_refusal() \
                    and _commit(repo, seen_head) == tip:
                return dict(out, merged=True, how="ancestor", ref=tip,
                            detail="%s (%s) entered %s through a merge commit; "
                                   "its tip was seen ahead for this lot"
                                   % (branch, tip[:12], target))
            found = seen()
            if found:
                return found
            cited = mention(
                refusal() or
                "%s has no work of its own: its tip %s is an ancestor of %s through "
                "a merge commit, but it was never seen ahead for this lot"
                % (branch, tip[:12], target))
            if cited["merged"]:
                # la fusion cite la branche : la pointe est bien entrée par
                # elle, l'ancêtre est la preuve (comme avant #2543)
                return dict(cited, how="ancestor", ref=tip,
                            detail="%s (%s) entered %s through a merge commit "
                                   "that cites it" % (branch, tip[:12], target))
            if own_refusal():
                # #4508 : refus local (base de création, pointe d'une autre
                # branche) ; la forge peut avoir avancé, `branch_probe` le
                # vérifie sur la tête publiée (#2654).
                cited["check_remote"] = True
            return cited
        why = own_refusal()
        cited = seen() or mention(
            refusal() or "%s has no work of its own: its tip %s is on the line of %s"
                       % (branch, tip[:12], target))
        if why and not cited["merged"]:
            # #4508 : même refus local ; la forge peut avoir avancé (#2654).
            cited["check_remote"] = True
        return cited
    out["ahead"] = True
    found = probe(repo, tip, target, lot=branch_lot(branch))
    if found["merged"]:
        return dict(out, merged=True, how=found["how"], ref=found["ref"],
                    detail="%s: %s" % (branch, found["detail"]))
    return dict(out, detail="%s ahead of %s (%s)" % (branch, target, tip[:12]))


def lot_ref_hash(repo: str) -> bool:
    """`git config ameesh.lotRef hash` : les commits de fusion du dépôt citent
    leurs lots par `#<id>` dans leur titre (à n'activer que si ces numéros sont
    des lots ameesh, jamais des PR GitHub)."""
    return (_out(repo, "config", "--get", LOT_REF_GIT_KEY) or "").strip().lower() == LOT_REF_HASH


def merge_commits(repo: str, target: str, since_ts: float | None = None,
                  limit: int = MAX_MERGE_SCAN, first_parent: bool = False) -> list[dict]:
    """Les commits de FUSION de `target` (au plus `limit`, du plus récent au
    plus ancien, postérieurs à `since_ts`) : `{sha, ts, message, parents}`.
    Avec `first_parent`, seulement ceux de la CHAÎNE première-parente de
    `target` (#1005 B2 : le porteur d'un gel est une fusion de la cible,
    jamais une fusion imbriquée dans une branche porteuse) ; les citations,
    elles, gardent toutes les fusions."""
    args = ["log", "--merges", "--format=%H%x00%ct%x00%P%x00%B%x1e", "--max-count=%d" % int(limit)]
    if first_parent:
        args.append("--first-parent")
    if since_ts:
        args.append("--since=@%d" % int(since_ts))
    proc = _git(repo, *args, target)
    if proc.returncode != 0:
        raise MergeProbeError("git log %s: %s" % (
            target, proc.stderr.decode("utf-8", "replace").strip()))
    rows = []
    for record in proc.stdout.decode("utf-8", "replace").split("\x1e"):
        sha, _sep, rest = record.strip().partition("\x00")
        stamp, _sep, rest = rest.partition("\x00")
        parents, _sep, message = rest.partition("\x00")
        if sha:
            try:
                ts = float(stamp)
            except ValueError:
                ts = 0.0
            rows.append({"sha": sha, "ts": ts, "message": message.strip(),
                         "parents": parents.split()})
    return rows


def _title_clauses(title: str) -> list[tuple[int, int]]:
    """Les bornes des propositions d'un titre (séparateurs exclus)."""
    bounds = []
    begin = 0
    for separator in CLAUSE_SPLIT_RE.finditer(title or ""):
        bounds.append((begin, separator.start()))
        begin = separator.end()
    bounds.append((begin, len(title or "")))
    return bounds


def negated_hash_positions(title: str) -> set[int]:
    """Les départs des `#<id>` que leur proposition nie dans un titre (#2259).

    « paquet 52 v2 : … recomposition sans #598 ni correctif .github » ne
    désigne pas le lot 598 : sa PR peut rester ouverte. Le découpage par
    proposition garde une citation positive voisine (« sans #598, #599
    livré » désigne 599). Un titre sans négation ne change rien. Les
    positions (et non les numéros) laissent compter une citation positive du
    même lot dans une autre proposition. Un groupe parenthésé ouvert par un
    contraste (« (mais #13 livré) ») porte sa propre proposition positive : la
    négation du parent ne l'atteint pas (#2262), alors que « sans (#598) »
    garde 598 nié.
    """
    # R Codex #2259 : une négation vaut aussi pour un groupe qui la suit
    # (« sans (#12) »), tandis que « #14 (sans #12) » garde
    # #14. Les niveaux sont traités sans récursion, même si le titre imbrique
    # beaucoup de parenthèses ; chaque groupe masque ses mots au parent.
    title = title or ""
    levels = [{"begin": 0, "end": len(title), "children": []}]
    stack = [levels[0]]
    for pos, char in enumerate(title):
        if char == "(":
            group = {"begin": pos + 1, "end": len(title), "children": []}
            stack[-1]["children"].append(group)
            levels.append(group)
            stack.append(group)
        elif char == ")" and len(stack) > 1:
            stack.pop()["end"] = pos
    negated: set[int] = set()
    #: #2262 : un groupe qui ouvre sur un contraste (« mais #13 livré ») est
    #: une proposition positive à part entière : la négation d'un ancêtre ne
    #: l'atteint pas. Il est jugé à son propre niveau, où ses négations
    #: internes restent actives.
    positives = [(level["begin"], level["end"]) for level in levels[1:]
                 if POSITIVE_GROUP_RE.match(title[level["begin"]:level["end"]].strip())]
    for level in levels:
        begin, end = level["begin"], level["end"]
        text = title[begin:end]
        for child in level["children"]:
            left, right = child["begin"] - 1 - begin, min(child["end"] + 1, end) - begin
            text = text[:left] + " " * (right - left) + text[right:]
        for left, right in _title_clauses(text):
            clause = text[left:right]
            prefixes = list(PREFIX_NEGATED_REF_RE.finditer(clause))
            postfixes = list(POSTFIX_NEGATED_REF_RE.finditer(clause))
            if not prefixes and not postfixes:
                continue
            for match in HASH_REF_RE.finditer(title, begin + left, begin + right):
                # « (mais #13) retiré » reste nié : un retrait qui suit le
                # groupe l'emporte sur son ouverture positive.
                if any(gb >= begin and ge <= end and (gb, ge) != (begin, end)
                       and gb <= match.start() < ge
                       and not any(begin + left + p.start() >= ge for p in postfixes)
                       for gb, ge in positives):
                    continue
                position = match.start() - begin - left
                # « #14 livré sans #12 » livre #14 : une préposition ne nie
                # que les numéros APRÈS elle. « #12 retiré » et « (#12)
                # exclu » nient aussi ceux d'avant le participe.
                if postfixes or any(m.end() <= position for m in prefixes):
                    negated.add(match.start())
    return negated


def cited_lots(message: str, *, hash_refs: bool = False) -> dict[int, str]:
    """`{lot: forme}` des lots que désigne le message d'un commit de fusion :
    une ligne `ameesh-work: <id>` (forme `ameesh-work`), le titre de la fusion
    d'une branche `lot/<id>-…` (forme `lot-branch`, L118b), et, avec
    `hash_refs`, `#<id>` dans le titre (forme `#id`) — pas dans le corps, qui
    peut recopier les titres des commits fusionnés. Un `#<id>` nié par sa
    proposition ne désigne pas le lot (#2259). Un lot déclaré partiel
    (`ameesh-work-partial: <id>`) n'est pas désigné.

    #5110 : une désignation explicite (trailer `ameesh-work:` ou
    `ameesh-work-partial:`, fusion d'une branche `lot/<id>-…`) fait foi : les
    `#<id>` du titre ne sont alors que du contexte. « Paquet 278 : #4820 B8
    (#659) » porte `ameesh-work: 4820` et ne livre pas #659 (Nexlink,
    commit 281c5be4c3d2, deux fermetures à tort)."""
    found: dict[int, str] = {}
    lines = (message or "").strip().splitlines()
    title = lines[0].strip() if lines else ""
    for match in WORK_TRAILER_RE.finditer(message or ""):
        found.setdefault(int(match.group(1)), "ameesh-work")
    branch = LOT_BRANCH_MERGE_RE.match(title)
    if branch:
        found.setdefault(int(branch.group("lot")), "lot-branch")
    explicit = bool(found) or PARTIAL_TRAILER_RE.search(message or "") is not None
    if hash_refs and not explicit:
        negated = negated_hash_positions(title)
        for match in HASH_REF_RE.finditer(title):
            if match.start() not in negated:
                found.setdefault(int(match.group(1)), "#id")
    for match in PARTIAL_TRAILER_RE.finditer(message or ""):
        found.pop(int(match.group(1)), None)
    return found


def merge_pr_number(message: str) -> int | None:
    """Le numéro de la PR d'un commit de fusion GitHub d'une branche
    `lot/<id>-…` (son corps n'est pas dans le message : `partial_of_pr`)."""
    match = _lot_branch_merge(message)
    return int(match.group("pr")) if match and match.group("pr") else None


def merge_pull_number(message: str) -> int | None:
    """#1005 B2 : le numéro de la PR d'un commit de fusion GitHub, quelle que
    soit la branche fusionnée (`merge_pr_number` ne voit que `lot/<id>-…`).
    La PR PORTEUSE d'un gel — celle qui l'a fait entrer dans la cible — a un
    corps à lire avant de retenir la preuve par le contenu."""
    lines = (message or "").strip().splitlines()
    match = GITHUB_MERGE_PR_RE.match(lines[0].strip() if lines else "")
    return int(match.group(1)) if match else None


def merge_branch(message: str) -> str | None:
    """La branche `lot/<id>-…` que fusionne un commit de fusion (son titre)."""
    match = _lot_branch_merge(message)
    return match.group("branch") if match else None


def _lot_branch_merge(message: str):
    lines = (message or "").strip().splitlines()
    return LOT_BRANCH_MERGE_RE.match(lines[0].strip() if lines else "")


def short_branch(name: str | None) -> str:
    """`lot/12-x` de `refs/heads/lot/12-x`, `origin/lot/12-x`… (comparaison)."""
    name = (name or "").strip()
    for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def partial_lots(body: str | None) -> set[int]:
    """Les lots qu'une PR déclare ne livrer qu'en partie (L118b)."""
    return {int(m.group(1)) for m in PARTIAL_TRAILER_RE.finditer(body or "")}


def final_lots(body: str | None) -> set[int]:
    """Les lots qu'une PR déclare livrer en dernier : une ligne
    `ameesh-work: <id> final` (#759). Une PR finale ferme le lot, même si une
    autre PR ouverte ou une branche déclarée y reste liée ; `ameesh-work-partial:`
    l'emporte (une PR partielle ne ferme jamais)."""
    return {int(m.group(1)) for m in WORK_TRAILER_RE.finditer(body or "")
            if m.group(2)}


def merge_citing(merges: list[dict], lot: int, *, since_ts: float | None = None,
                 hash_refs: bool = False) -> dict | None:
    """Le PREMIER commit de fusion postérieur à `since_ts` (création du lot,
    à `CLOCK_SLACK_S` près) qui désigne le lot `lot` (`cited_lots`), avec
    `how`, ou None."""
    found = None
    for merge in merges:                     # du plus récent au plus ancien
        if since_ts and merge["ts"] < float(since_ts) - CLOCK_SLACK_S:
            continue
        how = cited_lots(merge["message"], hash_refs=hash_refs).get(int(lot))
        if how:
            found = dict(merge, how=how)
    return found


def merges_partial(merges: list[dict], lot: int, *,
                   since_ts: float | None = None) -> list[dict]:
    """#312 r1 : les commits de fusion postérieurs à `since_ts` dont le
    message déclare le lot partiel (`ameesh-work-partial: <id>`), du plus
    ancien au plus récent."""
    return [m for m in reversed(merges)
            if not (since_ts and m["ts"] < float(since_ts) - CLOCK_SLACK_S)
            and int(lot) in partial_lots(m["message"])]


def merges_citing(merges: list[dict], lot: int, *, since_ts: float | None = None,
                  hash_refs: bool = False) -> list[dict]:
    """Les commits de fusion postérieurs à `since_ts` (à `CLOCK_SLACK_S` près)
    qui désignent le lot (`cited_lots`), du plus ancien au plus récent, avec
    `how` (L118b : le premier dont la PR n'est pas partielle ferme le lot)."""
    out = []
    for merge in reversed(merges):           # du plus ancien au plus récent
        if since_ts and merge["ts"] < float(since_ts) - CLOCK_SLACK_S:
            continue
        how = cited_lots(merge["message"], hash_refs=hash_refs).get(int(lot))
        if how:
            out.append(dict(merge, how=how))
    return out


def _citation(merge: dict, lot: int, target: str, *, sha: bool = True) -> str:
    """Comment la fusion a été constatée, en clair (compte rendu ; sans le
    commit, `sha=False`, pour la note du lot, qui le porte déjà)."""
    title = (merge["message"].splitlines() or [""])[0].strip()
    if merge["how"] == "ameesh-work":
        how = "carries \"ameesh-work: %d\"" % lot
    elif merge["how"] == "lot-branch":
        how = "merges the branch lot/%d-…" % lot
    elif merge["how"] == "message":
        # #746 : citation à nom entier de la branche du lot (pas de trailer)
        how = "merges the branch %s" % (merge.get("branch") or "?")
    else:
        how = "cites #%d in its title (%s %s)" % (lot, LOT_REF_GIT_KEY, LOT_REF_HASH)
    return "merge commit %sof %s that %s (\"%s\")" % (
        "%s " % merge["sha"][:12] if sha else "", target, how,
        title[:80] + ("…" if len(title) > 80 else ""))


def implicit_branch(repo: str, lot: int) -> str | None:
    """L118b : la branche `lot/<id>` ou `lot/<id>-…` d'un lot qui n'en porte
    pas, locale ou `origin/`, si elle est UNIQUE (deux candidates : on ne
    devine pas)."""
    out = _out(repo, "for-each-ref", "--format=%(refname)",
               "refs/heads/lot/%d" % lot, "refs/heads/lot/%d-*" % lot,
               "refs/remotes/origin/lot/%d" % lot, "refs/remotes/origin/lot/%d-*" % lot)
    names = set()
    for ref in (out or "").split():
        for prefix in ("refs/heads/", "refs/remotes/origin/"):
            if ref.startswith(prefix):
                names.add(ref[len(prefix):])
    return next(iter(names)) if len(names) == 1 else None


def github_slug(repo: str) -> str | None:
    """`owner/repo` du dépôt `origin` s'il est sur GitHub, sinon None."""
    url = (_out(repo, "remote", "get-url", "origin") or "").strip()
    match = re.match(r"^(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)"
                     r"([A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", url)
    return match.group(1) if match else None


def gh_pr_body(repo: str, number: int) -> str:
    """Le corps de la PR `number` du dépôt GitHub de `repo` (`gh`) ; lève
    GitError s'il est illisible."""
    from ... import plan_github
    slug = github_slug(repo)
    if not slug:
        raise GitError("origin of %s is not a GitHub repository" % repo)
    try:
        data = plan_github._json(plan_github.Gh(), ["pr", "view", str(int(number)),
                                                    "--repo", slug, "--json", "body"])
    except plan_github.GithubError as exc:
        raise GitError(str(exc)) from exc
    return (data or {}).get("body") or ""


def _moved_past(repo: str, seen: str | None, seen_ts: float | None, tip: str,
                merge: dict, merge_seen_ts: float | None) -> bool:
    """#312 r2, L118d : la branche a-t-elle livré du travail au-delà de la
    fusion partielle `merge` ? Il faut que le relevé l'ait vue en avance sur
    sa cible avec une pointe `seen` (`branch_head`) qui n'était pas dans la
    branche fusionnée (second parent) ni dans la cible d'alors, puis la voir
    entrée dans la cible (ancêtre de la pointe fusionnée `tip`). Une branche
    seulement avancée jusqu'à la cible, sans travail nouveau, n'y suffit pas :
    son nouveau sommet est la fusion partielle elle-même ou la suite de la
    cible. #346 : la pointe doit avoir été relevée (`seen_ts`) après que le
    relevé a vu la fusion (`merge_seen_ts`), deux instants de l'horloge de la
    base, jamais une date git, que son auteur choisit ; et toute erreur de git
    laisse le lot ouvert (« non ancêtre » : code 1 seulement)."""
    parents = merge.get("parents") or []
    if len(parents) < 2 or not seen or seen_ts is None or merge_seen_ts is None \
            or float(seen_ts) <= float(merge_seen_ts):
        return False
    for outside in (parents[1], merge["sha"]):
        # ni dans la branche fusionnée, ni dans la cible d'alors
        if _git(repo, "merge-base", "--is-ancestor", seen, outside).returncode != 1:
            return False
    return _git(repo, "merge-base", "--is-ancestor", seen, tip).returncode == 0


def _read_pr_body(pr_body, repo: str, number: int, sweep: dict):
    """#312 r1 : le corps de la PR, ou la GitError qui le rend illisible —
    sans appel pendant le recul de cette PR, après un échec de `gh` sur ce
    dépôt dans ce relevé, ni au-delà de `MAX_PR_BODY_READS` lectures : une
    PR toujours illisible ne cache pas longtemps les suivantes."""
    import time
    if (repo, number) in _pr_body_cache:
        return _pr_body_cache[(repo, number)]
    now = time.time()
    until, delay = _pr_body_backoff.get((repo, number), (0.0, 0.0))
    if now < until:
        return GitError("gh failed on this PR; next read in %ds" % int(until - now + 0.999))
    if repo in sweep["failed"]:
        return GitError("gh already failed in this sweep; the next sweep goes on")
    if sweep["reads"] >= MAX_PR_BODY_READS:
        return GitError("%d PR bodies read in this sweep; the next sweep goes on"
                        % MAX_PR_BODY_READS)
    sweep["reads"] += 1
    try:
        body = pr_body(repo, number)
    except GitError as exc:
        delay = min(delay * 2, PR_BODY_BACKOFF_MAX_S) if delay else PR_BODY_BACKOFF_S
        _pr_body_backoff[(repo, number)] = (time.time() + delay, delay)
        sweep["failed"].add(repo)
        return exc
    _pr_body_backoff.pop((repo, number), None)
    if len(_pr_body_cache) >= PR_BODY_CACHE_MAX:
        _pr_body_cache.pop(next(iter(_pr_body_cache)))     # le plus ancien lu
    _pr_body_cache[(repo, number)] = body
    return body


def _off_target_citations(repo: str, lot: int, row: dict, all_merges: list,
                          *, hash_refs: bool, tips: dict) -> tuple[list, tuple | None]:
    """#746 : les fusions d'une ref d'intégration essayée HORS cible qui
    livrent le lot, du plus ancien au plus récent, et la dernière qui se
    déclare partielle.

    Une fusion ne compte que si elle est postérieure à la création du lot
    (à `CLOCK_SLACK_S` près) et cite le lot : trailer `ameesh-work: <id>`
    (ou `#<id>` si le dépôt l'active), ou citation à nom entier de la
    branche du lot. Un lot à branche enregistrée n'est cité que par une
    fusion qui a réellement fusionné SA branche (deuxième parent = sa
    pointe) : une fusion dans une autre branche de travail (empilement)
    ne ferme pas. `tips` : cache `(dépôt, branche) → pointe`, consulté
    seulement quand une citation est trouvée."""
    since = row.get("created_ts")
    mine = short_branch(row["branch"]) if row.get("branch") else None
    citing, partial, tip = [], None, None
    for merge in reversed(all_merges):            # du plus ancien au plus récent
        if since and merge["ts"] < float(since) - CLOCK_SLACK_S:
            continue
        declared_partial = int(lot) in partial_lots(merge["message"])
        how = cited_lots(merge["message"], hash_refs=hash_refs).get(int(lot))
        if how == "lot-branch" and mine and merge_branch(merge["message"]) != mine:
            continue                    # #312 r1 : une autre lot/<id>-… ne compte pas
        if how is None and mine and _mentions(merge["message"], mine):
            how = "message"             # la fusion cite la branche enregistrée
        if how is None and not declared_partial:
            continue
        if mine:
            if tip is None:
                if (repo, mine) not in tips:
                    tips[(repo, mine)] = _tip(repo, mine)
                tip = tips[(repo, mine)]
            parents = merge.get("parents") or []
            if len(parents) < 2 or parents[1] != tip:
                continue
        if declared_partial:
            partial = (merge, "merge commit %s declares itself partial "
                       "(\"ameesh-work-partial: %d\"): lot kept open"
                       % (merge["sha"][:12], lot))
            continue
        citing.append(dict(merge, how=how, branch=mine))
    return citing, partial


def _is_ancestor(repo: str, sha: str, ref: str) -> bool:
    """`git merge-base --is-ancestor` : une commande, sans lecture d'objet."""
    return _git(repo, "merge-base", "--is-ancestor", sha, ref).returncode == 0


def _is_ancestor_checked(repo: str, sha: str, ref: str) -> bool | None:
    """`merge-base --is-ancestor` en trois états : True, False, None.

    Git sort **1** aussi bien pour « pas ancêtre » que pour une histoire
    illisible (objet manquant) ; dans le second cas il écrit sur stderr
    (`error: Could not read <objet>`). Le code seul confondrait donc absence
    et panne de lecture (#3399 B2 cx1). Toute sortie d'erreur, tout code hors
    0/1, est un doute : l'appelant refuse au lieu de conclure à l'absence."""
    proc = _git(repo, "merge-base", "--is-ancestor", sha, ref)
    if proc.stderr.strip() or proc.returncode not in (0, 1):
        return None
    return proc.returncode == 0


def _target_tip_at(repo: str, target: str, when) -> str | None:
    """Le commit de la cible au plus tard à `when` (horodatage epoch), ou None.

    #2786 (5) : la chaîne première-parente de la cible, lue à l'instant du
    gel, dit si le gel y était déjà. `CLOCK_SLACK_S` est ajouté à l'instant du
    jalon (horloge de la base) pour couvrir l'écart avec la date de committer
    du poste qui a poussé : mieux vaut garder un lot ouvert (doute) que
    fermer sur une fusion qui a suivi le gel."""
    try:
        stamp = float(when)
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    return _out(repo, "rev-list", "-1", "--first-parent",
                "--before=@%d" % int(stamp + CLOCK_SLACK_S), target)


def _was_ancestor_at(repo: str, sha: str, target: str, when) -> bool:
    """Le commit `sha` était-il déjà ancêtre de `target` à l'instant `when` ?"""
    tip = _target_tip_at(repo, target, when)
    return bool(tip) and _is_ancestor(repo, sha, tip)


# ---------------------------------------------------------------------------
# #3326 : une tête « vue en avance » contre une cible périmée
# ---------------------------------------------------------------------------
#
# `branch_head` ne vaut que si la pointe de la branche n'était pas déjà dans
# la cible quand le balayage l'a vue. Un clone peut lire une cible en retard
# (fetch best-effort, ref locale figée) : la pointe est alors « en avance »
# alors qu'elle n'est qu'un commit déjà livré de la cible. Une fois la cible
# rafraîchie, `seen()` la voyait ancêtre et concluait « vu-ancestor » sans
# qu'un commit propre n'ait jamais existé (#2410).


def _target_refs(repo: str, target: str) -> list[str]:
    """La cible et son homologue publié quand elle est locale (#3326).

    `main` et `origin/main` désignent la même branche d'intégration : le
    clone d'un agent peut avoir l'une en retard et l'autre à jour. Une cible
    déjà `origin/…` n'a pas d'homologue local : la branche locale peut porter
    des fusions non poussées, elle ne prouve rien."""
    refs = [target]
    name = _target_branch(target)
    if not name or str(target).startswith(("refs/remotes/", "origin/")):
        return refs
    published = published_target(repo, target)
    if published != target and _commit(repo, published):
        refs.append(published)
    return refs


def _ref_value_at(repo: str, ref: str, when) -> str | None:
    """Le commit de `ref` à l'instant `when` (epoch), d'après son reflog.

    Une entrée datée de la même seconde que `when` n'est pas retenue : elle
    peut avoir suivi l'observation (une avance rapide juste après le relevé).
    None si le reflog ne remonte pas jusque-là : l'appelant ne conclut alors
    rien de l'histoire de la ref."""
    try:
        stamp = int(float(when))
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    out = _out(repo, "reflog", "show", "--date=unix", "--format=%H%x00%gd", ref)
    for line in (out or "").splitlines():
        sha, _sep, selector = line.partition("\x00")
        match = re.search(r"@\{(\d+)\}", selector)
        if not match or not _SHA_RE.match(sha):
            continue
        if int(match.group(1)) < stamp:
            return sha
    return None


def _was_ancestor_at_seen(repo: str, sha: str, target: str, when) -> bool:
    """#3326 : `sha` était-il déjà dans la cible quand elle a été vue ?

    Même question que `_was_ancestor_at` (#2786) pour les gels, mais lue
    dans le reflog et non par la date des commits : une avance rapide rend
    rétroactivement la tête vue une ancêtre de la cible, et la date du commit
    ne dit pas si elle y était AVANT l'observation. Le reflog, lui, date la
    mise à jour de la ref. Une ref sans reflog, ou dont le reflog ne remonte
    pas à `when`, ne conclut pas : le relevé garde son ancien comportement."""
    for ref in _target_refs(repo, target):
        was = _ref_value_at(repo, ref, when)
        if was and _is_ancestor(repo, sha, was):
            return True
    return False


# ---------------------------------------------------------------------------
# #4508 : une tête sans commit propre (la base d'un autre lot) ne prouve rien
# ---------------------------------------------------------------------------
#
# `branch_head` ne vaut que si sa pointe est un commit de la branche du lot.
# Une branche associée avant son premier commit pointe sur sa base — le
# commit d'un autre lot, ou celui d'un porteur — et le relevé la voit « en
# avance » tant que cette base n'a pas atterri. Quand elle atterrit par un
# autre chemin, l'ascendance semblait prouver la fusion (#4482). Deux
# lectures du dépôt, sans écriture :
#   - le reflog de la branche locale : une branche qui n'a pas bougé depuis
#     sa création n'a aucun commit propre ;
#   - les autres refs (locales, `origin/`) : une pointe qui est déjà la
#     pointe d'une autre branche (celle de l'autre lot, le porteur) est un
#     commit de cette branche, pas du lot.
# Le gel du lot est une preuve directe, mais seulement pour la tête gelée ou
# un descendant : un ancêtre du gel — la base de la branche, n'importe quel
# commit de `main` — n'en est pas une (B dev1).
# Sans reflog local ni autre pointe identique (branche connue seulement par
# `origin/`, clone partiel), rien n'est conclu : le comportement antérieur
# est gardé, comme pour #3326.


def _ref_branch(ref: str) -> str:
    """Le nom de branche d'une ref (`refs/heads/x`, `refs/remotes/o/x`)."""
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/"):]
    prefix = "refs/remotes/"
    if ref.startswith(prefix):
        remote, sep, branch = ref[len(prefix):].partition("/")
        return branch if sep else remote
    return ref


def _branch_creation_commit(repo: str, branch: str) -> str | None:
    """#4508 : le commit sur lequel la branche LOCALE a été créée, ou None
    quand le reflog ne le prouve pas.

    Seule une entrée de création explicite (« branch: Created from … ») est
    retenue : après expiration du reflog, la plus ancienne entrée conservée
    est un commit ordinaire, et la prendre pour la création ferait passer un
    vrai commit pour une base (B1 cx2). Sans cette preuve — reflog expiré,
    branche créée par un fetch, reflog absent — rien n'est conclu."""
    if _commit(repo, "refs/heads/" + branch) is None:
        return None
    out = _out(repo, "reflog", "show", "--format=%H%x00%gs",
               "refs/heads/" + branch)
    lines = [line for line in (out or "").splitlines() if line.strip()]
    if not lines:
        return None
    sha, _sep, subject = lines[-1].partition("\x00")
    sha = sha.strip()
    if not _SHA_RE.match(sha) or "created from" not in subject.casefold():
        return None
    return sha


def _other_ref_at(repo: str, branch: str, sha: str,
                  target: str | None = None) -> str | None:
    """#4508 : une autre branche (locale ou `origin/`) a-t-elle `sha` pour
    pointe ? Rend son nom, ou None. La branche elle-même et la cible
    examinée sont exclues (la cible porte la pointe par construction quand
    on conclut), comme les têtes symboliques. Les refs internes d'ameesh
    (`refs/remotes/ameesh/…`) ne sont pas lues : elles ne disent rien de la
    propriété d'un commit."""
    mine = short_branch(branch)
    wanted = _target_branch(target) if target else ""
    out = _out(repo, "for-each-ref", "--format=%(objectname) %(refname)",
               "refs/heads", "refs/remotes/origin")
    for line in (out or "").splitlines():
        obj, _sep, ref = line.partition(" ")
        if obj != sha or not ref:
            continue
        name = _ref_branch(ref)
        if name == mine or name == "HEAD" or integration_branch(name) \
                or (wanted and name == wanted):
            # la cible et les refs d'intégration portent la pointe par
            # construction quand on conclut : elles ne disent rien de sa
            # propriété. `integration/…` (porteur de paquet) n'en est pas
            # une : il compte comme une autre branche.
            continue
        return short_branch(ref)
    return None


def _own_commit_evidence(repo: str, branch: str, sha: str,
                         target: str | None = None,
                         frozen_sha: str | None = None) -> tuple[bool, str]:
    """#4508 : `sha` est-il un commit propre à `branch` ? Rend (vrai, "") ou
    (faux, la raison). Le gel du lot est une preuve directe quand il est
    `sha` lui-même ou un ancêtre de `sha` (la tête vue est le gel ou en
    descend) : elle est alors sur la ligne du travail gelé, même si la
    branche n'a fait que la pointer. Un ancêtre du gel ne prouve rien : c'est
    la base de la branche — le commit d'un autre lot — ou n'importe quel
    commit de `main` (B dev1). Sinon, deux signes qu'il n'est pas propre : la
    branche locale n'a pas bougé depuis sa création, ou une autre branche
    porte déjà cette pointe. Sans signe contraire, le comportement antérieur
    est gardé (un reflog absent ne prouve pas l'absence de travail)."""
    frozen_sha = (frozen_sha or "").strip()
    # B dev1 : le gel ne prouve que lui-même ou ses descendants. Accepter un
    # ancêtre du gel rouvrirait #4482 : la base de la branche (celle d'un
    # autre lot) et tout l'historique de main sont des ancêtres du gel.
    if frozen_sha and (sha == frozen_sha or _is_ancestor(repo, frozen_sha, sha)):
        return True, ""
    base = _branch_creation_commit(repo, branch)
    if base and base == sha:
        return False, ("the branch has not advanced since its creation (%s)"
                       % sha[:12])
    other = _other_ref_at(repo, branch, sha, target)
    if other:
        return False, ("%s is already the tip of %s" % (sha[:12], other))
    return True, ""


def _published_target_state(repo: str, sha: str, target: str,
                            cache: dict) -> tuple[str, str | None]:
    """#3326 : la pointe `sha` est-elle dans la cible publiée ?

    Vérifié avant d'enregistrer `branch_head` : si la forge porte déjà la
    pointe, ce n'est pas un travail propre de la branche, même quand la ref
    du clone est en retard (fetch best-effort). Trois réponses, jamais deux :
    `("contained", ref)`, `("absent", None)` quand l'absence est **prouvée**,
    ou `("unknown", pourquoi)` quand elle ne l'est pas (forge illisible, tête
    publiée absente du clone) — l'appelant ne doit alors rien enregistrer :
    une cible qu'on n'a pas pu lire n'est pas une preuve de non-appartenance
    (relecture cx1 : forge C, clone A, pointe B présente, C absente).

    Lecture `git ls-remote` au plus une fois par relevé et par (dépôt,
    branche) via `cache` ; la tête publiée manquante est rapatriée par le
    fetch borné de #1772 quand le clone est partiel, puis relue."""
    name = _target_branch(target)
    if not name:
        return "unknown", "the target %r names no branch" % target
    for ref in _target_refs(repo, target):
        if ref == target:
            continue
        tip = _commit(repo, ref)
        if tip and _is_ancestor(repo, sha, tip):
            return "contained", ref
    key = (repo, name)
    if key not in cache:
        kind, value = _remote_head(repo, name)
        cache[key] = (kind, value)
    kind, published = cache[key]
    if kind == "local":
        # pas de forge : la cible locale est la référence, et la pointe en est
        # déjà absente (l'appelant est dans la branche « en avance »).
        return "absent", None
    if kind == "absent":
        if str(target).startswith(("refs/remotes/", "origin/")):
            return "unknown", ("origin/%s is absent from the forge while the clone "
                               "still names it" % name)
        return "absent", None
    if kind == "error":
        return "unknown", published
    if published == sha:
        return "contained", "origin/" + name
    tip = _commit(repo, published)
    if tip is None:
        _fetch_missing(repo, published, target)
        tip = _commit(repo, published)
    if tip is None:
        return "unknown", ("the published head %s of %s is absent from the clone"
                           % (published[:12], name))
    if _is_ancestor(repo, sha, tip):
        return "contained", "origin/" + name
    return "absent", None


def _published_branch_state(repo: str, branch: str, tip: str,
                            cache: dict) -> tuple[str, str | None]:
    """#4562 : la pointe d'une branche de porteur est-elle publiée sur la forge ?

    `branch_head` est une observation de la forge, pas d'un clone. Pour une
    branche `integration/**`, `paquet-auto` crée la branche et avance sa tête
    **localement** avant de la pousser : un balayage qui lit le clone d'un
    agent y verrait une tête non publiée, l'enregistrerait comme « vue en
    avance », et la garde #4501 la prendrait ensuite pour une tête poussée.

    `git ls-remote` une fois par relevé et par (dépôt, branche) via `cache`.
    Rend `("published", ref)`, `("unpublished", pourquoi)` (la forge ne porte
    pas cette pointe), ou `("unknown", pourquoi)` (forge illisible, sans
    remote) : l'appelant n'enregistre que `published`.
    """
    key = (repo, branch, "branch")
    if key not in cache:
        cache[key] = _remote_head(repo, branch)
    kind, value = cache[key]
    if kind == "local":
        return "unknown", "no origin remote: the branch head is not published"
    if kind == "absent":
        return "unpublished", "origin has no branch %s" % branch
    if kind == "error":
        return "unknown", value
    if same_commit(value, tip):
        return "published", "origin/" + branch
    # Une pointe plus ancienne, déjà poussée puis dépassée sur la forge, est
    # publiée elle aussi : on ne l'enregistre que si elle est sur la chaîne
    # de la tête distante, avec son commit présent au clone.
    remote = _commit(repo, value)
    if remote is None:
        _fetch_missing(repo, value, branch)
        remote = _commit(repo, value)
    if remote is not None and _is_ancestor(repo, tip, remote):
        return "published", "origin/" + branch
    return "unpublished", ("origin/%s is at %s, not the local head %s"
                           % (branch, str(value)[:12], str(tip)[:12]))


def _content_ancestor(repo: str, sha: str, target: str, *,
                      at_ts: float | None = None, absent=()) -> bool:
    """#1005 : le commit gelé est-il ancêtre de la cible ? C'est la preuve par
    contenu la moins chère (une commande git), celle d'une fusion `--no-ff`
    (ou d'une avance rapide) poussée sur la branche d'intégration sans citer
    le lot. Le patch-id exact (rebase, cherry-pick, squash) reste au relevé de
    contenu (`sync-merges`), dont c'est le travail et le coût assumé.

    #2786 : seul un commit complet est résolu ; une ref symbolique (`HEAD`, une
    branche) n'est jamais passée à `rev-parse`, dont le résultat dépendrait du
    clone du balayage — le cas #2712, qui a fermé un lot sur le `HEAD` d'un
    autre arbre. Et un gel déjà ancêtre de la cible **quand il a été posé**
    (`at_ts`) n'a aucun commit propre : ce n'est pas une livraison (#2784),
    sauf si la marque `absent` dit que la branche cible ne le portait pas au
    gel (une avance rapide ultérieure est alors une vraie livraison)."""
    frozen = full_commit_id(sha)
    if not frozen:
        return False
    target = _target_ref(repo, target)
    head = _commit(repo, target)
    if head is None:
        return False
    if not _is_ancestor(repo, frozen, head):
        return False
    if at_ts and _target_branch(target) not in set(absent) \
            and _was_ancestor_at(repo, frozen, target, at_ts):
        return False
    return True


def _content_carrier(repo: str, sha: str, target: str) -> tuple[dict | None, bool]:
    """#1005 B2 : la fusion qui a FAIT ENTRER le gel `sha` dans la cible — la
    plus ancienne fusion de la CHAÎNE PREMIÈRE-PARENTE de `target` dont la
    première parente ne le porte pas encore et dont une autre parente le porte.
    C'est la PR porteuse, celle dont le corps doit être lu avant de retenir la
    preuve par le contenu.

    La recherche est faite séparément sur la cible (`first_parent`) : la
    fenêtre des citations, bornée par `since_all`, peut ne pas contenir la
    fusion porteuse, et une fusion IMBRIQUÉE dans une branche porteuse (fusion
    locale `--no-ff` puis PR GitHub) n'est jamais sur cette chaîne — la
    retenir ferait lire le mauvais message et fermer le lot sans lire la PR.

    Rend `(porteur, fenêtre_tronquée)`. `(None, False)` : le gel est entré
    autrement (avance rapide, poussée directe), il n'y a aucun corps de PR à
    qualifier — même tronquée, la fenêtre part de la fusion la plus récente,
    et si elle ne porte pas le gel, aucune fusion plus ancienne ne le porte.
    `(None, True)` : la limite de lecture est atteinte sans que le porteur
    soit connu (il peut être plus ancien) : indéterminé, le lot reste ouvert."""
    merges = merge_commits(repo, target, limit=MAX_CARRIER_SCAN + 1, first_parent=True)
    truncated = len(merges) > MAX_CARRIER_SCAN
    if not merges or not _is_ancestor(repo, sha, merges[0]["sha"]):
        return None, False
    # du plus récent au plus ancien ; « porte le gel » y est monotone (une
    # fusion récente de la chaîne descend de toutes les plus anciennes) : la
    # plus ancienne qui le porte est trouvée par dichotomie
    lo, hi = 0, len(merges) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _is_ancestor(repo, sha, merges[mid]["sha"]):
            lo = mid
        else:
            hi = mid - 1
    carrier = merges[lo]
    parents = carrier.get("parents") or []
    if parents and _is_ancestor(repo, sha, parents[0]):
        # le gel était déjà sur la chaîne avant cette fusion : il est entré
        # par une avance rapide ou une poussée directe, sans corps de PR —
        # sauf si la fenêtre s'arrête là, le porteur peut être plus ancien
        return None, bool(truncated and lo == len(merges) - 1)
    return carrier, False


def _hash_ref_doubt(repo: str, lot: int, row: dict, branch: str | None, target: str,
                    freeze_sha: str | None, found: dict | None) -> str | None:
    """#2260 : une citation `#<id>` dans le titre d'une fusion tient-elle ?
    None si oui ; sinon, en clair, les têtes du lot restées hors de `target`.

    Preuve : le dernier gel ou, sans gel, la dernière tête vue en avance
    (`branch_head`) est entré dans la cible (`probe` : ancêtre, patch-id,
    squash). Une ancienne tête livrée ne prouve pas un nouveau gel. Démenti,
    sans cette preuve : l'un d'eux n'y est pas, ou la branche du lot y a
    encore du travail absent (une PR ouverte non fusionnée, Nexlink #598).
    Un lot sans gel, sans tête vue ni branche en avance n'a rien qui démente
    la citation : elle tient, comme avant."""
    heads = []
    if freeze_sha:
        heads.append(("the last freeze", freeze_sha))
    seen = (row.get("branch_head") or "").strip()
    if seen and not freeze_sha:
        heads.append(("the last head seen", seen))
    for _label, sha in heads:
        if probe(repo, sha, target, lot=lot)["merged"]:
            return None
    pending = ["%s %s" % (label, sha[:12]) for label, sha in heads]
    tip = (found or {}).get("tip")
    if tip:
        ahead = bool(found.get("ahead"))
    else:
        # hors cible, la branche n'a pas été sondée (`_sweep_target`)
        tip = _tip(repo, branch) if branch else None
        ahead = bool(tip) and not probe(repo, tip, target, lot=lot)["merged"]
    if tip and ahead and tip not in (sha for _label, sha in heads):
        pending.append("the tip %s of %s" % (tip[:12], branch))
    if not pending:
        return None
    return "%s %s not in %s" % (", ".join(pending), "is" if len(pending) == 1 else "are",
                                target)


def _sweep_target(repo: str, lot: int, row: dict, branch: str | None, target: str, *,
                  st, since_all: float | None, hashes: dict, merges: dict, bodies: dict,
                  sweep: dict, pr_body, dry_run: bool, implicits: dict, tips: dict,
                  freeze: dict | None = None, off_target: bool = False,
                  closing: bool = True) -> dict:
    """L'examen d'un lot contre UNE cible, extrait de `sync_branches` (#746)
    pour essayer la cible par défaut puis, si elle ne porte pas la fusion,
    les refs d'intégration du dépôt.

    `off_target` : la ref n'est ni la cible déclarée ni la cible par défaut ;
    seule une citation prouve alors la livraison, jamais le seul voisinage
    de la branche. `closing` : cette ref peut fermer le lot (une cible
    déclarée ne ferme jamais ailleurs : la preuve n'y est qu'un résidu).
    #1005 : à défaut de branche ou de citation, l'ancêtre du dernier gel
    (`freeze`, id et sha) prouve la livraison, après lecture du corps de sa
    PR porteuse quand la preuve ferme (B2, cherchée sur la chaîne
    première-parente de la cible) ; une fenêtre de lecture tronquée sans
    porteur connu laisse le lot ouvert. La fermeture est conditionnée à
    ce gel (B1). #2260 : une citation `#<id>` démentie par une tête du lot
    restée hors de la cible (`_hash_ref_doubt`) ne ferme pas : `unproven`.
    Rend `{found, branch, implicit, row}` ; lève
    MergeProbeError/GitError comme l'examen d'origine."""
    target = _target_ref(repo, target)
    freeze_sha = ((freeze or {}).get("sha") or "").strip() or None
    found = None
    implicit = False
    if not branch:
        if lot not in implicits:
            implicits[lot] = implicit_branch(repo, lot)
            # #2102 : une branche détachée explicitement n'est plus devinée
            if implicits[lot] and work_mod.is_detached(st, lot, implicits[lot]):
                implicits[lot] = None
        branch = implicits[lot]
        implicit = bool(branch)
    if off_target:
        found = {"merged": False, "how": None, "ref": None, "ahead": False, "tip": None,
                 "detail": "no merge commit of %s delivers the lot" % target}
    elif branch:
        found = branch_probe(repo, branch, target, seen_head=row.get("branch_head"),
                             seen_at=row.get("branch_head_ts"),
                             since_ts=row.get("created_ts"),
                             frozen_sha=freeze_sha)
    if repo not in hashes:
        hashes[repo] = lot_ref_hash(repo)
    if (repo, target) not in merges:
        merges[(repo, target)] = merge_commits(repo, target, since_all)
    all_merges = merges[(repo, target)]
    # L118b : une fusion GitHub de `lot/<id>-…` ne ferme le lot que si
    # sa PR ne se déclare pas partielle ; la première qui livre tout
    unreadable = None                # pourquoi une PR reste illisible
    partial = None                   # la dernière fusion partielle, et pourquoi
    cited = None
    if off_target:
        # #746 : hors cible, une citation (trailer, `#<id>`, nom entier de
        # la branche) prouve la livraison ; le lot à branche enregistrée
        # exige en plus que la fusion ait porté SA branche
        citing, partial = _off_target_citations(repo, lot, row, all_merges,
                                                hash_refs=hashes[repo], tips=tips)
    else:
        citing = merges_citing(all_merges, lot, since_ts=row.get("created_ts"),
                               hash_refs=hashes[repo])
        if row.get("branch"):
            # #312 r1 : une branche enregistrée est seule à compter ; la
            # fusion d'une autre `lot/<id>-…` ne ferme pas le lot
            mine = short_branch(row["branch"])
            citing = [m for m in citing if m["how"] != "lot-branch"
                      or merge_branch(m["message"]) == mine]
        # #312 r1 : une fusion locale qui se déclare partielle retient
        # aussi la preuve par la branche
        for merge in merges_partial(all_merges, lot, since_ts=row.get("created_ts")):
            partial = (merge, "merge commit %s declares itself partial "
                       "(\"ameesh-work-partial: %d\"): lot kept open"
                       % (merge["sha"][:12], lot))
    if not row.get("branch"):
        # #2102 : le nom détaché n'est pas non plus une preuve implicite
        # dans un titre de fusion (cible déclarée ou ref d'intégration).
        citing = [m for m in citing if m["how"] != "lot-branch"
                  or not work_mod.is_detached(st, lot, merge_branch(m["message"]))]
    reverted_cite = None
    if citing:
        # #1825 : une fusion citante annulée par un revert n'a rien livré —
        # y compris sur une ref d'intégration essayée hors cible (la citation
        # seule n'y ferme que si elle a réellement livré, cf. `close`)
        gardees = []
        for merge in citing:
            stopped = _merge_reverted(repo, merge["sha"], target)
            if stopped is None:
                gardees.append(merge)
            elif reverted_cite is None:
                reverted_cite = dict(stopped, cite=merge["sha"])
        citing = gardees
    unproven = None                  # #2260 : la citation `#<id>` démentie, et pourquoi
    doubt: list | None = None
    for merge in citing:
        if merge["how"] == "#id":
            # #2260 : `#<id>` dans un titre n'est qu'une mention ; elle ne
            # ferme pas un lot dont une tête connue reste hors de la cible
            if doubt is None:
                doubt = [_hash_ref_doubt(repo, lot, row, branch, target,
                                         freeze_sha, found)]
            if doubt[0]:
                if unproven is None:
                    unproven = (merge, "merge commit %s of %s cites #%d in its title, "
                                "but %s: a mention is not a proof of delivery, lot "
                                "kept open" % (merge["sha"][:12], target, lot, doubt[0]))
                continue
        number = merge_pr_number(merge["message"])
        if not number:
            cited = merge
            break
        if (repo, number) not in bodies:
            bodies[(repo, number)] = _read_pr_body(pr_body, repo, number, sweep)
        body = bodies[(repo, number)]
        if isinstance(body, GitError):
            # #312 r1 : une PR plus récente lisible et complète peut
            # encore fermer le lot
            unreadable = unreadable or (
                "PR #%d merged, but its body is unreadable (%s): not closed until "
                "it says whether it is partial" % (number, body))
            continue
        if lot in partial_lots(body):
            if partial is None or merge["ts"] >= partial[0]["ts"]:
                partial = (merge, "PR #%d declares itself partial "
                           "(\"ameesh-work-partial: %d\"): lot kept open"
                           % (number, lot))
            continue
        cited = merge
        break
    # une branche enregistrée déjà prouvée fusionnée ferme comme avant
    # quand le corps est illisible (hôte sans gh) : seule la preuve
    # implicite attend de savoir ; une fusion partielle retient toujours
    proven = bool(found and found["merged"] and row.get("branch"))
    if partial and partial[0]["sha"] != row.get("partial_merge") and not off_target:
        # #346 r2 : l'instant où le relevé voit la fusion partielle
        # pour la première fois, à l'horloge de la base ; #403 : le
        # dry-run juge comme le vrai relevé, sans écrire
        if not dry_run:
            st.work.set_partial_seen(lot, partial[0]["sha"])
        row = dict(row, partial_merge=partial[0]["sha"], partial_seen_ts=None)
    if partial and found and found["merged"] and found["tip"] \
            and _moved_past(repo, row.get("branch_head"), row.get("branch_head_ts"),
                            found["tip"], partial[0], row.get("partial_seen_ts")):
        # #312 r2 : seule la dernière fusion de la branche compte ; la
        # branche vue depuis en avance, puis fusionnée (avance rapide),
        # a livré la suite (L118d : pas une simple avance jusqu'à la cible)
        partial = None
    held = partial[1] if partial else unreadable
    # #1005 : ni la branche ni une citation ne prouvent la livraison ; le
    # dernier gel, ancêtre de la cible, la prouve par le contenu (fusion
    # --no-ff poussée sur la branche d'intégration, comme #560/#905). B2 : le
    # corps de la PR PORTEUSE — celle qui a fait entrer le gel — est lu AVANT
    # de retenir la preuve quand elle ferme ; partielle pour ce lot ou
    # illisible, le lot reste ouvert. Sur une ref qui ne ferme pas (cible
    # déclarée), la preuve n'est qu'un résidu signalé : rien à qualifier.
    content = None
    reverted = None
    if freeze_sha and cited is None and not held and not (found and found["merged"]) \
            and _content_ancestor(
                repo, freeze_sha, target, at_ts=(freeze or {}).get("at_ts"),
                absent=freeze_absent((freeze or {}).get("note"))):
        # #1825 : la tête gelée reste ancêtre de la cible quand sa fusion a
        # été annulée par un revert — le contenu n'y est plus, rien n'est livré
        reverted = reverted_content(repo, freeze_sha, target, lot=lot, branch=branch,
                                    hash_refs=hashes[repo])
        if reverted is None and closing:
            # le porteur est cherché sur la chaîne première-parente de la
            # CIBLE, séparément de la fenêtre des citations (B2, R2 cx3) :
            # indéterminé au-delà de la limite, le lot reste ouvert
            carrier, truncated = _content_carrier(repo, freeze_sha, target)
            number = merge_pull_number(carrier["message"]) if carrier else None
            if number:
                if (repo, number) not in bodies:
                    bodies[(repo, number)] = _read_pr_body(pr_body, repo, number, sweep)
                body = bodies[(repo, number)]
                if isinstance(body, GitError):
                    unreadable = ("PR #%d carries the last freeze %s, but its body is "
                                  "unreadable (%s): not closed until it says whether it "
                                  "is partial" % (number, freeze_sha[:12], body))
                elif lot in partial_lots(body):
                    partial = (carrier, "PR #%d carries the last freeze %s and declares "
                               "itself partial (\"ameesh-work-partial: %d\"): lot kept open"
                               % (number, freeze_sha[:12], lot))
            elif carrier is None and truncated:
                unreadable = ("the last freeze %s is an ancestor of %s, but its carrier "
                              "merge is older than the %d merges read on the first-parent "
                              "chain: not closed until it is known"
                              % (freeze_sha[:12], target, MAX_CARRIER_SCAN))
        held = partial[1] if partial else unreadable
        if reverted is None and not held:
            content = {"merged": True, "how": "content-ancestor", "ref": freeze_sha,
                       "tip": None, "ahead": False, "kind": "content",
                       "detail": "the last freeze %s is an ancestor of %s (content)"
                                 % (freeze_sha[:12], target),
                       "note": "the last freeze %s entered %s as an ancestor (content)"
                               % (freeze_sha[:12], target)}
            if freeze.get("id") is not None:
                content["frozen_id"] = int(freeze["id"])
    if held and cited is None and (partial or not proven):
        found = dict(found or {"tip": None, "ahead": False, "ref": None},
                     merged=False, how="partial", detail=held)
    elif found is not None and found["merged"]:
        pass
    elif cited:
        found = dict(found or {"tip": None, "ahead": False}, merged=True,
                     how=cited["how"], ref=cited["sha"],
                     detail=_citation(cited, lot, target),
                     note=_citation(cited, lot, target, sha=False))
        if cited["how"] == "#id" and freeze and freeze.get("id") is not None:
            # #2260 : le gel qui prouve la citation doit encore être le dernier.
            found["frozen_id"] = int(freeze["id"])
    elif content is not None:
        found = content
    elif reverted is not None:
        found = dict(found or {"tip": None, "ahead": False, "ref": None}, merged=False,
                     how="reverted", ref=reverted["ref"], detail=reverted["detail"])
    elif reverted_cite is not None:
        found = dict(found or {"tip": None, "ahead": False, "ref": None}, merged=False,
                     how="reverted", ref=reverted_cite["ref"],
                     detail="merge commit %s of %s that names the lot was reverted (%s)"
                            % (reverted_cite["cite"][:12], target,
                               reverted_cite["ref"][:12]))
    elif unproven is not None:
        found = dict(found or {"tip": None, "ahead": False}, merged=False,
                     how="unproven", ref=unproven[0]["sha"], detail=unproven[1])
    elif found is None:
        found = {"merged": False, "how": None, "ref": None, "tip": None,
                 "ahead": False,
                 "detail": "no merge commit of %s names the lot "
                           "(line \"ameesh-work: %d\", nor a merge of lot/%d-…%s)"
                           % (target, lot, lot, ", nor \"#%d\" in a title" % lot
                              if hashes[repo] else "")}
    return {"found": found, "branch": branch, "implicit": implicit, "row": row}


def _signal_off_target(db, st, lot: int, assignee: str, reg, note: str, key: str,
                       *, actor: str = "") -> None:
    """#746 : une citation hors cible (ou une ref illisible) n'est jamais
    silencieuse : une note au journal du lot et un message dans son fil, une
    fois par observation (`key`), sans jamais fermer le lot."""
    from ... import fil
    if any(key in (event.get("note") or "")
           for event in st.work.events(int(lot), 40)):
        return
    try:
        work_mod.note(db, lot, note, actor=actor)
    except work_mod.WorkError:
        return                          # le lot a changé : rien à journaliser
    fil.record(db.cfg, db, sender=work_mod.SYSTEM_SENDER, recipients=[assignee],
               text=note, project=fil.project_for(db.cfg, fil.agent_project(reg)),
               lot=str(lot), meta={"kind": "event"})


_REMOTE_REFS = "refs/remotes/"
_HEADS_REFS = "refs/heads/"
#: #2302 : la référence d'issue d'un lot (`manaty/ameesh#1692`, ou son URL)
#: nomme le dépôt du projet, pour retrouver un clone local quand le dossier de
#: l'assigné est celui d'un AUTRE dépôt.
_ISSUE_SLUG_RE = re.compile(
    r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#\d+$")
_ISSUE_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/issues/\d+/?$", re.I)


def _git_remotes(repo: str) -> tuple[str, ...]:
    """Les remotes déclarés du dépôt (`origin` par défaut)."""
    return tuple((_out(repo, "remote") or "").split()) or ("origin",)


def _branch_remote(target: str, remotes: tuple[str, ...]) -> tuple[str, str] | None:
    """(remote, branche) d'une cible qui nomme une branche, None pour un SHA
    ou une ref qui n'en est pas une (même lecture que `radar`, #1333)."""
    text = (target or "").strip()
    if not text or _SHA_RE.match(text):
        return None
    if text.startswith(_REMOTE_REFS):
        remote, _, branch = text[len(_REMOTE_REFS):].partition("/")
        return (remote, branch) if remote and branch else None
    if text.startswith(_HEADS_REFS):
        branch = text[len(_HEADS_REFS):]
        return ("origin", branch) if branch else None
    if text == "HEAD":
        return "origin", "HEAD"
    if "/" in text:
        head, _, branch = text.partition("/")
        if branch and head in remotes:
            return head, branch
    return "origin", text


def _symbolic_branch(repo: str, remote: str) -> str | None:
    """La branche que désigne `refs/remotes/<remote>/HEAD`, ou None."""
    resolved = _out(repo, "symbolic-ref", "--quiet",
                    _REMOTE_REFS + remote + "/HEAD")
    prefix = _REMOTE_REFS + remote + "/"
    if resolved and resolved.startswith(prefix) and len(resolved) > len(prefix):
        return resolved[len(prefix):]
    return None


def published_target(repo: str, target: str) -> str:
    """#1333/#2302 : la ref PUBLIÉE que désigne `target`, ou `target`.

    `develop`, `origin/develop`, `refs/remotes/origin/develop`,
    `refs/heads/develop` et `origin/HEAD` nomment la même branche ; quand
    `refs/remotes/<remote>/<branche>` existe, c'est elle qu'on sonde — dans un
    clone partagé, la branche locale reste figée pendant que les agents
    poussent. La branche locale (ou le SHA) ne sert que de repli."""
    split = _branch_remote(target, _git_remotes(repo))
    if split is None:
        return target
    remote, branch = split
    if branch == "HEAD":
        branch = _symbolic_branch(repo, remote) or ""
        if not branch:
            return target
    if _commit(repo, _REMOTE_REFS + remote + "/" + branch):
        return "%s/%s" % (remote, branch)
    return target


def refresh_clone(repo: str, target: str | None, refreshed: dict) -> dict:
    """#2302 : rafraîchit le clone avant de sonder, au plus une fois par
    relevé et par remote. Best-effort : rend `{remote: erreur}` et ne lève
    jamais — un fetch refusé laisse le relevé continuer sur les refs lues."""
    remotes = ["origin"]
    split = _branch_remote(target or "", _git_remotes(repo))
    if split and split[0] not in remotes:
        remotes.append(split[0])
    errors = {}
    for remote in remotes:
        key = (repo, remote)
        if key not in refreshed:
            refreshed[key] = git_fetch(repo, remote)
        if refreshed[key]:
            errors[remote] = refreshed[key]
    return errors


def integration_tips(repo: str, *, refreshed: dict | None = None) -> dict[str, str] | None:
    """Les têtes des branches d'intégration **publiées** (#2786 (5)).

    Une ref `origin/<branche>` périmée ne prouve pas qu'un gel en était absent
    au moment où il a été posé : un clone d'auteur en retard garde `main` à A
    alors que la forge porte déjà le commit gelé B, et la marque d'absence
    rendrait B livrable alors qu'il n'a jamais eu de commit propre (avis cx1).
    Quand `origin` existe, le clone est donc rafraîchi (fetch borné, au plus
    une fois par relevé via `refreshed`) avant la lecture. Un fetch en échec
    rend `None` : l'appelant n'écrit alors **aucune** marque d'absence.

    Sans remote `origin`, rend les refs locales des branches d'intégration :
    il n'y a aucune cible publiée à rafraîchir, le clone est la référence."""
    fresh = refreshed if refreshed is not None else {}
    if _out(repo, "remote", "get-url", "origin") is None:
        prefix = "refs/heads/"
    else:
        if refresh_clone(repo, "", fresh).get("origin"):
            return None
        prefix = _REMOTE_REFS + "origin/"
    out = _out(repo, "for-each-ref", "--format=%(refname) %(objectname)", prefix)
    tips: dict[str, str] = {}
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        name = parts[0][len(prefix):]
        if name and integration_branch(name):
            tips[name] = parts[1].lower()
    return tips


def _repo_slug(repo: str) -> str | None:
    """`owner/repo` (en minuscules) de l'`origin` d'un clone, ou None.

    Sert à reconnaître qu'un dossier de travail est celui d'un AUTRE dépôt
    (#2302) ; une URL locale de la forme `…/<owner>/<repo>` marche aussi."""
    try:
        url = _out(repo, "remote", "get-url", "origin")
    except GitError:
        return None
    if not url:
        return None
    parts = [part for part in _norm_url(url).split("/") if part]
    if len(parts) < 2:
        return None
    return "%s/%s" % (parts[-2].casefold(), parts[-1].casefold())


def _expected_repo(row: dict) -> tuple[str | None, str | None]:
    """(slug attendu, nom de dépôt attendu) d'un lot, d'après sa PR, sinon sa
    référence d'issue, sinon son `app`.

    #3331 : la PR dit où vivent la branche et le gel — un lot du canon
    (`manaty/home#N`) a son issue dans ameesh et sa PR dans le dépôt du
    canon ; lire l'issue d'abord faisait choisir le clone ameesh."""
    for key in ("pr_ref", "issue_ref"):
        ref = (row.get(key) or "").strip()
        match = _ISSUE_SLUG_RE.match(ref) or _ISSUE_URL_RE.match(ref)
        if match:
            return ("%s/%s" % (match.group(1), match.group(2))).casefold(), \
                match.group(2).casefold()
    app = (row.get("app") or "").strip().casefold()
    return None, app or None


def _local_clones(db, host: str | None) -> list[tuple[str, str, dict]]:
    """Les clones git locaux connus du registre sur cet hôte (dossier, slug,
    ligne d'agent), plus le clone du canon configuré (#3331 : un lot du canon
    se lit là, pas dans le dossier de travail de son assigné), une lecture par
    relevé."""
    from ... import registry
    found = []
    seen: set[str] = set()
    cfg = getattr(db, "cfg", None)
    local_host = str(getattr(cfg, "host", "") or "") if cfg is not None else ""
    if cfg is not None and (host is None or not local_host or host == local_host):
        from ... import canon as canon_mod
        try:
            entries = canon_mod.configured(cfg)
        except Exception:  # noqa: BLE001 - canon illisible : rien de plus à lire
            entries = ()
        for entry in entries:
            cwd = os.path.expanduser(str(getattr(entry, "path", "") or ""))
            if not cwd or cwd in seen or not os.path.isdir(cwd):
                continue
            slug = _repo_slug(cwd)
            if slug:
                seen.add(cwd)
                found.append((cwd, slug, {}))
    for agent_row in registry.overview(db):
        if host is not None and (agent_row.get("host") or "") != host:
            continue
        cwd = os.path.expanduser(agent_row.get("cwd") or "")
        if not cwd or cwd in seen or not os.path.isdir(cwd):
            continue
        seen.add(cwd)
        slug = _repo_slug(cwd)
        if slug:
            found.append((cwd, slug, agent_row))
    return found


def _lot_clone(db, host: str | None, row: dict, repo: str,
               local: list[tuple[str, str, dict]]) -> tuple[str | None, str]:
    """#2302 : le clone où lire le lot — celui de l'assigné s'il est le bon
    dépôt, sinon un clone local du dépôt du projet (`issue_ref`/`app`).

    Rend `(dossier, note)` ; `(None, raison)` quand le dossier de l'assigné
    est un autre dépôt et qu'aucun clone du bon dépôt n'est connu sur l'hôte.
    Sans indication de dépôt, le dossier de l'assigné reste la seule lecture
    possible (comportement d'avant)."""
    expected_slug, expected_name = _expected_repo(row)
    if not expected_slug and not expected_name:
        return repo, ""
    current = _repo_slug(repo)
    if expected_slug:
        if current == expected_slug:
            return repo, ""
    elif current and expected_name and current.rsplit("/", 1)[-1] == expected_name:
        return repo, ""
    matches = []
    for cwd, slug, agent_row in local:
        if cwd == repo:
            continue
        if expected_slug:
            if slug != expected_slug:
                continue
        elif slug.rsplit("/", 1)[-1] != expected_name:
            continue
        matches.append((cwd, slug, agent_row))
    if matches:
        from ... import projects as projects_mod
        app = (row.get("app") or "").strip().casefold()

        def rank(match):
            agent_row = match[2]
            names = [name.casefold()
                     for name in projects_mod.agent_projects(agent_row)]
            stopped = (agent_row.get("status") or "") == "stopped"
            return (0 if app and app in names else 1,
                    1 if stopped else 0,
                    -(agent_row.get("last_seen_ts") or 0.0))
        chosen = sorted(matches, key=rank)[0][0]
        return chosen, ("read in %s: the assignee's working directory is %s"
                        % (chosen, current or repo))
    if expected_slug and current and current != expected_slug:
        return None, ("the assignee's working directory is %s, not %s, and no "
                      "local clone of %s was found on this host"
                      % (current, expected_slug, expected_slug))
    return repo, ""


def _project_target(db, app: str) -> str:
    """#5110 : la cible déclarée du projet d'un lot (`radar.projects.<p>.target`,
    `intake_neighbors.project_target`), ou "" ; une configuration illisible
    laisse la résolution par le clone."""
    if not app:
        return ""
    try:
        from ... import intake_neighbors
        return intake_neighbors.project_target(getattr(db, "cfg", None), app)
    except Exception:  # noqa: BLE001 - configuration absente : repli sur le clone
        return ""


def sync_branches(db, *, host: str | None = None, agents=None, dry_run: bool = False,
                  actor: str = "", limit: int = MAX_BRANCH_LOTS, pr_body=None) -> dict:
    """L118 : ferme les lots ouverts dont la fusion dans leur cible est constatée.

    Examinés : les lots ouverts qui portent une branche ou ont un assigné.
    Le dépôt d'un lot est le dossier de travail de son assigné au registre ;
    avec `host`, seuls les assignés de cet hôte sont examinés (les dossiers
    des autres hôtes n'y sont pas), avec `agents`, seulement ceux-là. Preuve :
    la branche du lot (`branch_probe`), sinon un commit de fusion de la cible
    qui désigne le lot (`ameesh-work: <id>`, ou `#<id>` si le dépôt l'active :
    `merge_citing`). La fermeture passe par `work.close_merged` (idempotente,
    jamais de réouverture) avec le commit de fusion et une note qui dit comment
    elle a été constatée, et s'écrit dans le fil du lot. Rend le compte rendu.

    L118b : un lot sans branche prend pour branche implicite son unique
    `lot/<id>[-…]` (`implicit_branch`), et la fusion GitHub d'une branche
    `lot/<id>-…` le désigne (« Merge pull request #N from owner/lot/<id>-… »).
    Le corps de cette PR (`pr_body(repo, N)`, défaut : `gh`) est lu : une PR
    qui se déclare partielle (`ameesh-work-partial: <id>`) ne ferme pas le
    lot, et un corps illisible le laisse ouvert (un lot fermé n'est jamais
    rouvert : on ne ferme pas sans savoir).

    #746 : une cible déclarée — celle du lot (`branch_target`), celle de son
    projet (`radar.projects.<p>.target`, #5110) ou `ameesh.target` du
    dépôt — fait foi : une fusion constatée ailleurs
    n'est qu'un résidu signalé (note au lot, message à l'assigné), jamais
    une fermeture. Quand aucune des deux n'est déclarée et que la cible par
    défaut (`origin/HEAD`, figé à la création du clone) ne porte pas la
    fusion, les refs d'intégration du remote (`develop`, `main`, `master`,
    `release/*`) sont essayées à leur tour : une fusion citante y ferme le
    lot, avec la ref trouvée dans la note. Aucune autre ref (branche de
    travail, autre remote) ; seul le commit d'un gel absent localement est
    rapatrié, et seulement dans un clone partiel (#1772).

    #1005 : à défaut de branche et de citation, le dernier gel du lot,
    ancêtre de la ref examinée, prouve la livraison par le contenu (une
    fusion `--no-ff` poussée sur la branche d'intégration, comme #560/#905,
    sans citer le lot) : le relevé la ferme, en nommant la ref. Le patch-id
    exact (rebase, cherry-pick, squash) reste le travail de `sync-merges`.
    B2 : le corps de la PR PORTEUSE de ce gel (`merge_pull_number`, quelle
    que soit sa branche) est lu comme celui d'une PR citante — partielle pour
    le lot ou illisible, la preuve n'est pas retenue. Le porteur est cherché
    sur la chaîne première-parente de la cible, indépendamment de la fenêtre
    des citations (une fusion imbriquée dans une branche porteuse n'est pas
    la fusion de la cible) ; quand la limite de lecture est atteinte sans
    porteur connu, le lot reste ouvert. B1 : la fermeture est
    conditionnée au jalon `frozen` examiné (`frozen_id`) : un regel
    concurrent, vu après la lecture, laisse le lot ouvert (`refrozen`).

    #2786 : un dernier gel qui ne nomme pas un commit complet (`HEAD`, une
    branche, une abréviation) est un doute (`doubt`) : aucune ref n'est
    résolue dans ce clone, rien n'est fermé, et l'assigné est prévenu pour
    regeler sur la tête complète. Un gel absent ne bloque pas : la branche ou
    une citation peuvent encore prouver la livraison.

    #4508 : une pointe qui n'est pas un commit de la branche (sa base, la
    pointe d'un autre lot ou d'un porteur) n'est pas un travail propre : elle
    n'est pas enregistrée comme `branch_head`, et la preuve « vu-ancestor »
    qu'elle porterait est refusée (`_own_commit_evidence`)."""
    from ... import fil, registry

    st = storage.of(db)
    rows = st.work.open_for_sweep(limit)
    # #1005 : le dernier gel de chaque lot — id ET sha (B1 : la fermeture est
    # conditionnée à ce jalon précis) — lu une fois, comme le relevé de contenu
    marks: dict = {}
    for ms in st.progress.lot_milestones([int(r["id"]) for r in rows]):
        marks.setdefault(int(ms["work_item_id"]), []).append(ms)
    freezes: dict = {}
    for lot, items in marks.items():
        latest = latest_freeze(items) or {}
        if (latest.get("sha") or "").strip():
            freezes[lot] = latest
    # les commits de fusion lus une fois par dépôt et par cible : depuis le
    # lot ouvert le plus ancien, chaque lot ne retient que les siens
    since_all = min((float(r["created_ts"]) - CLOCK_SLACK_S for r in rows
                     if r.get("created_ts")), default=None)
    targets: dict = {}
    declared: dict = {}                      # #746 : le dépôt déclare sa cible ?
    candidates: dict = {}                    # #746 : (dépôt, cible) → refs d'intégration
    implicits: dict = {}                     # #746 : branche implicite, une fois par lot
    tips: dict = {}                          # #746 : (dépôt, branche) → pointe
    projects: dict = {}                      # #5110 : app → cible déclarée du projet
    hashes: dict = {}
    merges: dict = {}
    regs: dict = {}                          # une lecture du registre par assigné
    bodies: dict = {}                        # (dépôt, PR) → corps, ou l'erreur
    refreshed: dict = {}                     # #2302 : (dépôt, remote) → erreur de fetch
    fresh_heads: dict = {}                   # #3326 : (dépôt, branche) → tête publiée
    clones = None                            # #2302 : clones locaux, lus une fois
    sweep = {"reads": 0, "failed": set()}    # lectures de corps de ce relevé
    pr_body = pr_body or gh_pr_body
    results: list[dict] = []
    for row in rows:
        lot = int(row["id"])
        branch = row.get("branch") or None
        assignee = (row.get("assignee") or "").strip()
        entry = {"work_item": lot, "branch": branch,
                 "target": row.get("branch_target"), "assignee": assignee or None,
                 "result": "skipped", "how": None, "ref": None, "detail": ""}
        if agents is not None and assignee not in agents:
            continue
        if assignee and ":" not in assignee and assignee not in regs:
            regs[assignee] = registry.get(db, assignee)
        reg = regs.get(assignee) if assignee and ":" not in assignee else None
        if reg is None:
            if host is None and branch:
                entry["detail"] = "no agent assigned: repository unknown"
                results.append(entry)
            continue
        if host is not None and (reg.get("host") or "") != host:
            continue
        # #2786 : un dernier gel qui ne nomme pas un commit complet (HEAD, une
        # branche, une abréviation) vaut doute : rien n'est résolu dans ce
        # clone ni fermé, et l'assigné est prévenu pour regeler sur la tête
        # complète. Un gel absent ne bloque pas (la branche peut prouver).
        freeze = freezes.get(lot)
        frozen_sha = str((freeze or {}).get("sha") or "").strip()
        if frozen_sha and not full_commit_id(frozen_sha):
            detail = ("last freeze %r is not a full commit id: doubt, nothing merged — "
                      "freeze again on the full head (`ameesh work milestone %d frozen "
                      "--sha <commit>`)" % (frozen_sha[:24], lot))
            entry.update(result="doubt", how="unreadable", detail=detail)
            if not dry_run and assignee:
                _signal_off_target(
                    db, st, lot, assignee, reg, "merge sweep: %s" % detail,
                    "non-full freeze %s" % frozen_sha[:12],
                    actor=actor or "git:sweep")
            results.append(entry)
            continue
        repo = os.path.expanduser(reg.get("cwd") or "")
        if not repo or not os.path.isdir(repo):
            entry.update(result="no-repo", detail="working directory of %s missing (%s)"
                         % (assignee, repo or "not declared"))
            results.append(entry)
            continue
        # #2302 : le dossier de l'assigné peut être celui d'un AUTRE dépôt
        # (nexlink au lieu d'ameesh) ; on lit alors un clone local du dépôt
        # que nomme le lot, quand il y en a un sur cet hôte.
        if clones is None:
            clones = _local_clones(db, host)
        chosen, repo_note = _lot_clone(db, host, row, repo, clones)
        if chosen is None:
            entry.update(result="wrong-repo", detail=repo_note)
            results.append(entry)
            continue
        repo = chosen
        if repo_note:
            entry["repo_note"] = repo_note
        try:
            # #2302 : un clone que personne ne fetch garde une cible et des
            # branches périmées ; le rafraîchir une fois par relevé est ce qui
            # permet de constater la fusion d'un paquet.
            refresh_clone(repo, "", refreshed)
            # #746 : une cible déclarée — celle du lot, ou `ameesh.target` du
            # dépôt — fait foi ; sinon la cible par défaut du clone
            # (`origin/HEAD`, figé à sa création), puis les refs d'intégration
            # du remote, essayées seulement si elle ne porte pas la fusion.
            # #5110 : à défaut, la cible déclarée du projet du lot
            # (`radar.projects.<p>.target`, `develop` pour Nexlink) fait foi
            # comme celle du lot, quel que soit le réglage du clone.
            app = (row.get("app") or "").strip()
            if app not in projects:
                projects[app] = _project_target(db, app)
            if row.get("branch_target"):
                primary, explicit = row["branch_target"], True
            elif projects[app]:
                primary, explicit = projects[app], True
            else:
                if repo not in targets:
                    try:
                        targets[repo] = default_target(repo)
                    except MergeProbeError as exc:
                        targets[repo] = exc
                if isinstance(targets[repo], MergeProbeError):
                    raise targets[repo]
                if not targets[repo]:
                    entry.update(result="no-target", detail="no target: `git config %s "
                                 "<branch>` in %s, or --target" % (TARGET_GIT_KEY, repo))
                    results.append(entry)
                    continue
                if repo not in declared:
                    declared[repo] = bool(declared_target(repo))
                primary, explicit = targets[repo], declared[repo]
            # #2302 : la cible peut nommer un autre remote ; la ref publiée
            # (origin/<branche>) est préférée à une branche locale périmée.
            refresh_clone(repo, primary, refreshed)
            primary = published_target(repo, primary)
            # #990 : la liste dépend de la cible primaire, propre au lot — un
            # cache par dépôt la figerait pour les lots suivants
            if (repo, primary) not in candidates:
                candidates[(repo, primary)] = target_candidates(repo, primary)
            off_refs = candidates[(repo, primary)]
            # une cible déclarée ne ferme jamais ailleurs : les refs hors
            # cible n'y sont examinées que pour signaler le résidu
            to_try = [(primary, False, True)]
            to_try += [(ref, True, explicit is False) for ref in off_refs]
            outcome = None                   # l'examen retenu (cible par défaut)
            off_outcome = None               # une citation hors cible (jamais fermée)
            partial_off = None               # une fusion partielle hors cible
            reverted_off = None              # un contenu annulé hors cible (#1825)
            unreadable = []                  # (ref, pourquoi) : le résidu, jamais silencieux
            for target, off, close in to_try:
                try:
                    probe = _sweep_target(
                        repo, lot, row, branch, target, st=st, since_all=since_all,
                        hashes=hashes, merges=merges, bodies=bodies, sweep=sweep,
                        pr_body=pr_body, dry_run=dry_run, implicits=implicits,
                        tips=tips, freeze=freezes.get(lot), off_target=off,
                        closing=close)
                except (MergeProbeError, GitError) as exc:
                    if off:
                        unreadable.append((target, str(exc)))
                    elif outcome is None:
                        outcome = {"error": str(exc), "target": target, "off": off}
                    continue
                if probe["found"]["merged"]:
                    if close:
                        outcome = dict(probe, target=target, off=off)
                    else:
                        off_outcome = dict(probe, target=target, off=off)
                    break
                if off and probe["found"]["how"] == "partial" and partial_off is None:
                    partial_off = (target, probe["found"]["detail"])
                if off and probe["found"]["how"] == "reverted" and reverted_off is None:
                    reverted_off = (target, probe["found"]["detail"])
                if outcome is None:
                    outcome = dict(probe, target=target, off=off)
        except (MergeProbeError, GitError) as exc:
            entry.update(result="error", detail=str(exc))
            results.append(entry)
            continue
        if outcome.get("error"):
            entry.update(target=outcome["target"], result="error", detail=outcome["error"])
            results.append(entry)
            continue
        found = outcome["found"]
        branch = outcome["branch"]
        implicit = outcome["implicit"]
        row = outcome["row"]
        target = outcome["target"]
        detail = found["detail"]
        if repo_note:
            detail = "%s — %s" % (detail, repo_note)
        entry.update(branch=branch, target=target, repo=repo, how=found["how"],
                     ref=found["ref"], detail=detail, result="open")
        if found["merged"] and outcome["off"]:
            # #746 : la livraison est constatée hors de la cible par défaut
            # (clone sans `ameesh.target`, `origin/HEAD` figé) : la note dit
            # la ref trouvée et quoi déclarer pour l'expliciter
            entry["off_target"] = primary
            entry["detail"] = "%s (merged into %s, off the default target %s: declare " \
                              "`git config %s %s` to make it explicit)" % (
                                  entry["detail"], target, primary, TARGET_GIT_KEY, target)
        if not found["merged"]:
            # #746 : le résidu n'est jamais silencieux — une citation sur une
            # ref d'intégration alors qu'une cible est déclarée, ou une ref
            # illisible : note au lot et message à l'assigné, jamais de
            # fermeture
            note, key = None, None
            if off_outcome:
                note = ("merge sweep: %s, but the declared target %s does not carry "
                        "it; the lot is kept open — declare `git config %s %s` to "
                        "close it explicitly" % (
                            off_outcome["found"]["detail"], primary, TARGET_GIT_KEY,
                            off_outcome["target"]))
                key = (off_outcome["found"]["ref"] or off_outcome["target"])[:12]
            elif unreadable:
                note = ("merge sweep: unreadable integration ref%s %s; the lot is kept "
                        "open — fix or declare the target (`git config %s <branch>`)" % (
                            "s" if len(unreadable) > 1 else "",
                            ", ".join("%s (%s)" % pair for pair in unreadable),
                            TARGET_GIT_KEY))
                key = "unreadable integration ref"
            if note:
                entry.update(result="off-target", detail=note)
                if not dry_run:
                    _signal_off_target(db, st, lot, assignee, reg, note, key,
                                       actor=actor or "git:%s" % target)
            elif partial_off:
                # #746 : une fusion partielle hors cible ne ferme pas, mais
                # se dit (comme la fusion partielle de la cible)
                entry.update(how="partial", detail=partial_off[1])
            elif reverted_off:
                # #1825 : un contenu annulé hors cible ne ferme pas, mais se
                # dit (le lot reste ouvert avec la raison)
                entry.update(how="reverted", detail=reverted_off[1])
            elif found["how"] == "unproven" and not dry_run:
                # #2260 : le doute se trace, une fois par fusion citante
                _signal_off_target(db, st, lot, assignee, reg,
                                   "merge sweep: %s" % found["detail"],
                                   "merge commit %s of %s cites #%d"
                                   % (found["ref"][:12], target, lot),
                                   actor=actor or "git:%s" % target)
            stale = row.get("branch_head_ts") is None or (
                row.get("partial_merge") and (row.get("partial_seen_ts") is None
                                              or row["branch_head_ts"] <= row["partial_seen_ts"]))
            if branch and found["ahead"] and found["tip"] \
                    and (found["tip"] != row.get("branch_head") or stale):
                # #3326 : une cible en retard (fetch best-effort) fait voir en
                # avance une pointe que la forge porte déjà : ce n'est pas un
                # travail propre, rien n'est enregistré, le lot reste ouvert.
                # Une cible illisible ou une tête publiée absente du clone ne
                # prouve pas l'absence : l'observation n'est pas enregistrée
                # (relecture cx1 : distinguer « absent » de « inconnu »).
                state, detail = _published_target_state(
                    repo, found["tip"], target, fresh_heads)
                if state == "contained":
                    entry["how"] = "no-work"
                    if entry["result"] != "off-target":
                        entry["detail"] = (
                            "%s (%s) is already in %s, read fresh: no work of "
                            "its own (the clone's target %s is behind)"
                            % (branch, found["tip"][:12], detail, target))
                elif state == "unknown":
                    entry["how"] = "unverified"
                    if entry["result"] != "off-target":
                        entry["detail"] = (
                            "%s (%s) is not recorded: the published target %s "
                            "cannot be read (%s)"
                            % (branch, found["tip"][:12], target, detail))
                elif not dry_run:
                    # #4562 : pour un porteur de paquet (`integration/**`), une
                    # tête locale non poussée n'est pas un relevé de la forge.
                    # `paquet-auto` crée et avance la branche dans le clone
                    # avant de la pousser ; l'enregistrer ferait croire à la
                    # garde #4501 que cette tête est déjà poussée.
                    record_head = True
                    if str(branch).startswith(PACKET_BRANCH_PREFIX):
                        published, why = _published_branch_state(
                            repo, branch, found["tip"], fresh_heads)
                        if published != "published":
                            record_head = False
                            entry["how"] = ("unpublished" if published == "unpublished"
                                            else "unverified")
                            if entry["result"] != "off-target":
                                entry["detail"] = (
                                    "%s (%s) is not recorded as seen ahead: its "
                                    "head is not published on the forge (%s)"
                                    % (branch, found["tip"][:12], why))
                    if record_head:
                        # #346 r2 : une pointe inchangée relevée avant la fusion
                        # partielle l'est de nouveau, puisqu'elle est encore là
                        if implicit:
                            # L118b : la branche implicite devient celle du lot (journal) ;
                            # #2102 : sauf détachement ou branche posés pendant le relevé
                            work_mod.attach_implicit_branch(
                                db, lot, branch, actor=actor or "git:%s" % target,
                                source="the merge sweep: lot/%d branch" % lot)
                        # #4508 : une pointe qui n'est pas un commit de la branche
                        # (sa base, la pointe d'un autre lot ou d'un porteur) n'est
                        # pas un travail propre : rien n'est enregistré, le lot
                        # reste ouvert.
                        own, why = _own_commit_evidence(
                            repo, branch, found["tip"], target, frozen_sha)
                        if not own:
                            if entry["result"] != "off-target" \
                                    and entry["how"] in (None, "unverified"):
                                entry["how"] = "no-work"
                                entry["detail"] = (
                                    "%s (%s): %s — no work of its own; the lot is kept "
                                    "open" % (branch, found["tip"][:12], why))
                        else:
                            st.work.set_branch_head(lot, branch, found["tip"])
            results.append(entry)
            continue
        if dry_run:
            retention = dry_run_retention(db, st, row, sha=found["ref"] or "", repo=repo)
            if retention is not None and not retention.get("lift"):
                mark_retained(entry, retention, target=target, dry_run=True)
                results.append(entry)
                continue
            if retention is not None:
                mark_lifted(entry, retention)
            entry["result"] = "would-merge"
            results.append(entry)
            continue
        if found.get("note"):
            source = "the merge sweep, %s" % found["note"]
        else:
            source = "branch %s (%s)" % (branch, found["how"])
        if outcome["off"]:
            # #746 : la cible par défaut ne portait pas la fusion ; la ref
            # trouvée est dite, et quoi déclarer pour l'expliciter
            source = ("%s — merged into %s, off the default target %s: declare "
                      "`git config %s %s` to make it explicit"
                      % (source, target, primary, TARGET_GIT_KEY, target))
        done = work_mod.deliver(
            db, lot, connector=work_mod.merge_connector(),
            evidence={"seen": source, "sha": found["ref"] or ""},
            actor=actor or "git:%s" % target, frozen_id=found.get("frozen_id"),
            repo=repo,                          # L539 : dépôt du relevé, indice de classe
            content_guard=content_retention)    # #2744 : verrou commun avec le hold
        entry["result"] = done["result"]
        if done["result"] in ("withheld", "error"):
            mark_retained(entry, done["retention"], target=target)
            if done["result"] == "withheld":
                record_content_withheld(
                    db, st, row, entry, done["retention"], target=target,
                    actor=actor or "git:%s" % target)
        else:
            entry["detail"] = "%s — %s" % (entry["detail"], done["detail"])
        if done["result"] == "merged":
            if found.get("kind") == "content":
                delivered = "its last freeze %s is an ancestor of %s" % (
                    (found["ref"] or "?")[:12], target)
            elif found.get("note"):
                delivered = "a merge commit of %s names it" % target
            else:
                delivered = "branch %s entered %s" % (branch, target)
            text = ("Lot #%d \"%s\" delivered: %s (%s, commit %s). Closed by the runner's "
                    "merge sweep."
                    % (lot, row.get("title") or "", delivered, found["how"],
                       (found["ref"] or "?")[:12]))
            if outcome["off"]:
                text = ("%s Merged into %s, off the default target %s: declare "
                        "`git config %s %s` to make it explicit."
                        % (text, target, primary, TARGET_GIT_KEY, target))
            fil.record(db.cfg, db, sender=work_mod.SYSTEM_SENDER, recipients=[assignee],
                       text=text, project=fil.project_for(db.cfg, fil.agent_project(reg)),
                       lot=str(lot), meta={"kind": "event"})
        results.append(entry)
    # L539 (#1104) : les relevés ne revoient jamais un lot déjà fusionné ; ce
    # rattrapage borné rouvre les relectures dont l'ouverture avait échoué à la
    # fusion (R1 #1104 P2-2). Jamais en dry-run, jamais bloquant.
    review_catch_up: dict = {}
    if not dry_run:
        try:
            from ... import review_lot
            review_catch_up = review_lot.catch_up(
                db, limit=review_lot.CATCH_UP_LIMIT, host=host or "")
        except Exception:  # noqa: BLE001 - un rattrapage ne casse pas le relevé
            review_catch_up = {}
    return {"host": host, "dry_run": bool(dry_run), "results": results,
            "merged": sum(1 for r in results if r["result"] == "merged"),
            "review_catch_up": review_catch_up}
