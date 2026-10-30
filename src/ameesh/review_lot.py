# SPDX-License-Identifier: AGPL-3.0-only
"""L539 — le lot de relecture ouvert après la fusion d'un lot léger.

Canon `processus/lots.md` §2 : un lot léger fusionne sans verdict préalable
(tests ciblés de l'auteur et CI verte), puis reçoit « une relecture par une
autre famille de modèle, dans la journée ». Ce module ouvre ce lot de
relecture au moment où la fusion est **confirmée** : `intake`, sans assigné,
dans la file de l'équipe du projet — la famille opposée à celle de l'auteur et
l'échéance du jour sont écrites dans le corps, à charge du relecteur qui prend
le lot.

Rien ici n'autorise ni ne bloque une fusion : la classe est relue sur le diff
gelé par l'appelant (`lot_class`), la fusion est un fait déjà enregistré, et
toute erreur de cette écriture est **rendue**, jamais levée — le lot d'origine
reste fusionné. L'idempotence tient à deux marques écrites **dans la même
transaction** que la création (revue #544 r1) : le lot de relecture lui-même
(`issue_ref = review-after-merge:#<lot>`, **quel que soit son état**) et une
ligne de journal sur le lot fusionné, qui reste un confort. Deux appels
**concurrents** sont sérialisés par le verrou de la ligne marquée, qui est
posé avant l'insertion : le perdant rend le lot du gagnant (revue #544 r2).

#1053 (suite #1037 N1) : la famille de l'auteur est celle du **gel**, pas
celle de la session courante à la fusion. Le gel est le geste de livraison :
son acteur est l'auteur du lot produit, et les tours comptés de cet acteur
(`turn_costs`, 0014) jusqu'à l'instant du gel sont la trace du harnais qui a
réellement tourné. Entre le gel et la fusion, une session peut être remplacée
ou changer de harnais (rotation, `adopt`, L242b) : le registre courant ne
prouve rien du passé, et quand aucun tour n'est tracé la famille reste
inconnue (`codex ou claude`) — elle n'est jamais devinée du registre. #1777 :
le tour du gel est celui dont la ligne `turn_costs` suit le gel (elle est
écrite à la fin du tour), dans la fenêtre d'un tour ; au-delà c'est une
session suivante. #1859 : un gel reposé par un tiers qui n'a jamais tenu le lot
ne fait pas de lui l'auteur — le gel retenu est le plus récent dont l'acteur a
tenu le lot (assigné courant, ou nommé au journal), le tiers écarté est nommé
au corps du lot de relecture, et si aucun acteur retenu n'a de tour tracé on
relit les tours de l'assigné.

#1104 : l'ouverture ne dépend plus de la **voie** de fusion. La file l'ouvre à
l'atterrissage (`land`), et toute fusion **enregistrée** ailleurs l'ouvre par
`open_for_merge` — le point d'entrée de `work.close_merged`, donc du relevé
des branches (L118), de `sync-merges`, de `sync-github`, de la porte
`git-merge` et de `ameesh work merged`. La classe est alors relue sur le delta
**du lot** (`class_at_merge`, `lot_measures.lot_diff_range`) — celui de son
gel, jamais le diff du paquet qui l'a porté ni `cible..gel`, vide dès que le
gel est un ancêtre de la cible — avec les indices de l'appelant ; seule
`light` ouvre, un doute n'ouvre rien, et l'idempotence reste celle de
`open_after_merge`.

#1280 (R codex3 B2) : un avis (`review-after-merge:#P`, `REVUE #N` ou mission
`R1`/`R2`) n'ouvre **jamais** la relecture d'un avis — la garde est posée ici,
avant toute lecture de classe, pour couper la récursion des fausses fusions
d'avis. Le corps du lot de relecture dit aussi au lecteur de clore sa mission
par `ameesh work close <id> --done` après avoir livré son verdict.

#2377 : la famille écrite au corps n'était qu'une consigne — la prise au repos
(L165) ouvrait le lot à toute l'équipe, et un agent de la famille de l'auteur
pouvait le prendre (#2340). `required_family` relit la même preuve au moment
de la prise et `may_take` n'ouvre le lot qu'à la famille opposée ; l'auteur
inconnu ne se devine pas, et le lot attend alors une attribution explicite.

#2552 : la référence d'un lot de relecture se lit aussi sur sa `source`. Des
lots historiques la portent là avec un `issue_ref` GitHub (ou aucun) et
échappaient à la garde de famille de #2377 ; la prise les écarte désormais de
la famille de l'auteur de #N comme les lots courants.

#2374 : une fusion classée `light` dont le diff touche pourtant la porte de
fusion (`merge_queue.CRITICAL_SCOPE`) ouvre une relecture **sensible** à deux
avis de familles distinctes. L'ouverture écrit en outre, au journal du lot
source, une marque structurée (`requirement_mark`) que `work.close --delivered`
relit (`guard_delivery`) : la clôture est refusée tant que les avis dus ne sont
pas rendus sur le sha fusionné. Le nombre d'avis n'est donc plus seulement
écrit dans le corps.

#2585 : un lot léger peut déjà porter l'avis d'une autre famille sur la tête
qui entre, rendu avant la fusion (gel relu, revue désignée). Ouvrir une seconde
mission `REVUE #N` demanderait une relecture déjà faite. Quand assez d'avis
`ok` d'une famille **tracée à l'instant du verdict** (`turn_costs`, jamais le
registre courant) et autre que celle de l'auteur couvrent la tête fusionnée —
le sha fusionné, ou le dernier gel dont Git prouve que le **delta net est
identique** à celui du lot fusionné (`_entered`, patch-id) — l'ouverture est
close sans création. Un avis bloquant, une famille inconnue, un sha différent,
un delta différent ou une preuve Git absente ne dispensent de rien : la
relecture reste due, et le doute n'ouvre jamais une dispense (§8).
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from typing import Optional

from . import lot_class, plan_git, projects, storage, work

#: la référence qui identifie le lot de relecture d'un lot fusionné
PREFIX = "review-after-merge:"
#: le marqueur de journal d'une tentative de rattrapage sans création ; le
#: premier mot de la note est `ATTEMPT_PREFIX + <id>` (reconnu par la sélection)
ATTEMPT_PREFIX = "review-after-merge:attempt:#"
#: le délai (s) avant de réessayer une tentative sans création : le temps que
#: les candidats plus anciens passent, qu'un doute se résolve ou qu'une panne
#: se reprenne. Délai, jamais suppression : le journal garde l'historique.
ATTEMPT_RETRY_S = 3600
#: le marqueur de journal d'un candidat **terminal** : un lot non-light
#: confirmé (le canon §2 ne demande une relecture après fusion que pour un lot
#: léger), une tête déjà relue par une autre famille (#2585) ou un doute épuisé
#: après `CATCH_UP_MAX_ATTEMPTS` tentatives. Le
#: premier mot de la note est `TERMINAL_PREFIX + <id>` ; la sélection l'exclut
#: toujours — la marque reste au journal, visible, mais aucune tentative
#: horaire perpétuelle (arbitrage pilote du 15/10 sur le P2 de la R2 #1104).
TERMINAL_PREFIX = "review-after-merge:closed:#"
#: le nombre de tentatives bornées d'un candidat **douteux** (classe non
#: confirmée : lecture impossible, dépôt absent) avant sa marque terminale.
#: Un non-light confirmé, lui, est terminal dès la première lecture.
CATCH_UP_MAX_ATTEMPTS = 3
#: le nombre de lots nommés dans un avertissement aux pilotes (#3201), le
#: reste restant lisible au journal des lots.
WARN_LIMIT = 15
#: l'échéance du canon §2 : le jour même de la fusion, heure de Paris
TZ = "Europe/Paris"
HOUR = "23:59"
#: les familles de modèle du mesh ne sont plus recopiées ici : elles viennent
#: du canon compilé (`compiled_families`, lu au dernier commit audité de
#: l'hôte), et sans lecture aucune famille n'est supposée (#2449/#2664).
#: la famille de relecture opposée à celle de l'auteur (règle du pilote du
#: 2026-10-13) : codex si l'auteur est Claude ou DeepSeek, sinon Claude. C'est
#: une règle pilote datée, hors `processus/mecanique.yaml` (décision
#: mesh-design du 19/10) ; elle deviendra sans objet quand #2642 aura retiré
#: la relecture après fusion — son retrait ira alors au panier.
OPPOSITE = {"claude": "codex", "deepseek": "codex", "codex": "claude"}
UNKNOWN_FAMILY = "codex ou claude"
#: avis requis pour la relecture après fusion : un pour un lot léger, deux
#: lorsque la fusion touche la porte de fusion (SQL, sécurité, prod), même si
#: le canon a classé le chemin « light » (#2374).
LIGHT_REVIEWS = 1
CRITICAL_REVIEWS = 2
#: #2374 : la marque structurée de la relecture, écrite au journal du lot
#: source dans la même transaction que la création du lot de relecture. Le
#: corps dit le détail ; la marque porte les faits que `work.close --delivered`
#: relit (`guard_delivery`) : lot source, sha fusionné, nombre d'avis dus et
#: famille de l'auteur. Sans elle, la clôture d'une relecture sensible
#: resterait déclarative.
MARK_PREFIX = "review-after-merge:required:"
MARK_SCHEMA = "ameesh-review-after-merge/1"
#: #1777 N1 : au-delà de cette fenêtre après le gel, la première ligne
#: `turn_costs` de l'auteur n'est plus le tour qui a gelé mais une session
#: suivante. `cost.record` écrit la ligne du tour à sa FIN, donc le tour du gel
#: apparaît APRÈS l'instant du gel ; 30 min = `turn_max_seconds` par défaut
#: (config.Config), la durée maximale d'un tour qui peut encore porter ce gel.
FREEZE_TURN_WINDOW_S = 1800.0
#: la règle du canon §2, recopiée dans le corps du lot (consigne injectée au
#: relecteur, avec la route du défaut non bloquant — owner du 15/10/2026)
BLOCKING = ("un défaut **bloquant** (B, P1) trouvé après la fusion ouvre aussitôt un lot de "
            "correction, qui passe avant tout nouveau travail ; un défaut **non bloquant** "
            "(N, P2, P3) ne crée pas de lot : il va au backlog "
            "(`ameesh work backlog add --title … --value … --score … --review-of <lot>`), "
            "regroupé avec les autres défauts mineurs du même chantier — sous le seuil de "
            "lot du canon, une ligne du panier du chantier — et un défaut trouvé en "
            "relisant une suite d'avis ne crée rien (canon §2, owner du 15/10/2026 puis du "
            "19/10/2026 ; sur ameesh, un P2 de la classe sensible bloque encore)")


def ref_of(lot: int) -> str:
    """La référence du lot de relecture de `lot`."""
    return "%s#%d" % (PREFIX, int(lot))


def deadline(now: Optional[datetime] = None) -> str:
    """Le jour même de la fusion, à 23:59 heure de Paris."""
    if now is None:
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(TZ))
        except Exception:  # noqa: BLE001 - fuseau absent : l'échéance reste écrite
            return "%s %s UTC" % (datetime.now(tz=None).date().isoformat(), HOUR)
    return "%s %s %s" % (now.date().isoformat(), HOUR, TZ)


class FamiliesError(Exception):
    """Les familles de modèle du canon compilé ne peuvent pas être lues."""


#: le document compilé est immuable par commit : un canon lu une fois est
#: relu seulement pour un nouveau commit audité (#2664).
_FAMILIES_CACHE: dict = {}


def _families_at(repo: str, commit: str) -> tuple:
    """Les familles du document global (`reviews.families`) à ce commit exact."""
    key = (str(repo), str(commit))
    if key not in _FAMILIES_CACHE:
        from . import mechanics
        _FAMILIES_CACHE[key] = tuple(mechanics.load_global(repo, commit).families)
    return _FAMILIES_CACHE[key]


def compiled_families(db, cfg=None) -> tuple:
    """Les familles de modèle du canon compilé, au dernier commit audité.

    Le canon n'est pas recopié ici (#2449) : `processus/mecanique.yaml` est
    compilé par `mechanics.load_global`, au commit que l'hôte a réellement
    audité (`canon_state.last_good_commit`, #2447 B2) et non à la référence
    configurée, qui peut être en avance d'un fetch sans synchro. Un état
    enregistré fait foi même si le passage courant est invalide ; sans état du
    tout (banc, premier démarrage), le commit exact de la source est le seul
    connu.

    Lève `FamiliesError` quand aucun canon ne donne de familles lisibles : une
    seule synchro ratée ne bloque pas les fusions tant qu'un commit audité
    reste lisible ; en l'absence de tout commit lisible, l'appelant échoue
    fermé et nomme le canon. Rend un tuple non vide, dans l'ordre du canon.
    """
    from . import canon as canon_mod
    from . import canon_sync

    cfg = cfg if cfg is not None else getattr(db, "cfg", None)
    if cfg is None or not str(getattr(cfg, "canon", "") or "").strip():
        raise FamiliesError("no canon configured: the model families are unknown")
    try:
        canons = canon_mod.load_configured(cfg)
    except Exception as exc:  # noqa: BLE001 - canon illisible : diagnostic rendu
        raise FamiliesError("cannot read the configured canon: %s: %s"
                            % (type(exc).__name__, str(exc)[:200])) from exc
    if not canons:
        raise FamiliesError("no readable canon configured: the model families are unknown")
    errors = []
    for canon in canons:
        root = str(getattr(canon, "root", "") or "") or "canon"
        for source in getattr(canon, "sources", ()) or ():
            if getattr(source, "mode", "") != "git" \
                    or not getattr(source, "repo", "") \
                    or not getattr(source, "commit", ""):
                errors.append("%s: canon source is not an exact Git commit" % root)
                continue
            if getattr(source, "prefix", ""):
                errors.append("%s: canon under a sub-directory, mechanics are read"
                              " at the Git root only" % getattr(source, "repo", ""))
                continue
            try:
                commit = canon_sync.audited_commit(db, cfg, canon, source)
            except Exception as exc:  # noqa: BLE001 - état illisible : canon suivant
                errors.append("%s: cannot read the audited commit: %s" % (root, exc))
                continue
            if not commit:
                errors.append("%s: no audited commit readable" % root)
                continue
            try:
                return _families_at(source.repo, commit)
            except Exception as exc:  # noqa: BLE001 - document illisible : suivant
                errors.append("%s@%s: %s"
                              % (source.repo, commit[:12], str(exc)[:240]))
    raise FamiliesError("; ".join(errors[:4])
                        or "no canon declares the model families")


def family_of(harness: Optional[str], families) -> str:
    """La famille de modèle d'un harnais (`claude`, `codex`, `deepseek`), ou "".

    Le harnais peut être un nom précis (`claude-opus-5-5`, `deepseek-flash`) :
    la famille est celle du mot qu'il porte, **dans les familles du canon**
    (`compiled_families`). Un harnais inconnu, ou un canon illisible (liste
    vide), ne rend pas une famille par défaut : l'indépendance d'un avis ne se
    suppose pas (#1374/#2664)."""
    text = (harness or "").strip().lower()
    for family in families or ():
        family = str(family or "").strip().lower()
        if family and family in text:
            return family
    return ""


def reviewer_family(harness: Optional[str], families) -> str:
    """La famille de relecture opposée au harnais de l'auteur.

    `OPPOSITE` est la règle pilote du 13/10 ; si la famille opposée n'est pas
    déclarée par le canon, elle n'est pas proposée (aucune famille supposée)."""
    known = tuple(families or ())
    family = family_of(harness, known)
    if not family:
        return UNKNOWN_FAMILY
    opposite = OPPOSITE.get(family)
    return opposite if opposite in known else UNKNOWN_FAMILY


def review_families(harness: Optional[str], families) -> tuple:
    """Les deux familles d'une relecture sensible, hors de celle de l'auteur.

    La première est la famille opposée à l'auteur (`reviewer_family`) ; la
    seconde est la première des familles du canon qui n'est ni celle de
    l'auteur ni la première. Un harnais inconnu laisse les deux familles
    ouvertes, dans l'ordre historique d'affichage, sans supposer
    l'indépendance (#2374)."""
    known = tuple(str(f or "").strip().lower() for f in (families or ()) if f)
    author = family_of(harness, known)
    first = reviewer_family(harness, known)
    if first == UNKNOWN_FAMILY:
        preferred = tuple(f for f in ("codex", "claude") if f in known)
        return (preferred + tuple(f for f in known if f not in preferred))[:2]
    for family in known:
        if family not in (author, first):
            return (first, family)
    return (first,)



def _critical_changed(klass) -> bool:
    """Le diff fusionné touche-t-il un chemin critique, *même classé léger* ?

    La porte de fusion (canon §2, incident #876) ne se lit pas seulement dans
    la classe : un canon qui n'a pas encore déclaré un de ses chemins le rend
    « light », et la relecture après fusion ne doit pas en rester à un avis
    (#2374). Les motifs de secours de `merge_queue.CRITICAL_SCOPE` et le
    drapeau `critical` du canon sont lus sur tous les chemins du diff, sans
    dépendre de la classe que la politique leur a donnée."""
    files = (klass or {}).get("files") or ()
    if not files:
        return False
    from . import merge_queue
    for entry in files:
        if entry.get("critical") is True:
            return True
        path = str(entry.get("path") or "")
        if not path or any(rx.search(path) for rx in merge_queue.CRITICAL_SCOPE):
            return True
    return False


def _repo_of_agent(db, name: str) -> str:
    """Le dossier de travail déclaré de l'agent, s'il existe (indice de dépôt).

    Même source que le relevé des branches (`sync_branches`) : le `cwd` du
    registre est le worktree où le gel vit encore. Un nom inconnu, un chemin
    disparu ou un registre illisible ne rendent rien — l'appelant doubtera,
    jamais ne devinera."""
    name = (name or "").strip()
    if not name or ":" in name:
        return ""
    try:
        from . import registry
        row = registry.get(db, name) or {}
    except Exception:  # noqa: BLE001 - registre illisible : pas d'indice
        return ""
    path = os.path.expanduser(str(row.get("cwd") or ""))
    return path if path and os.path.isdir(path) else ""


#: #3426 : la déduplication du rattrapage entre les hôtes. Un hôte sans
#: dépôt ne tente rien pour un lot ; l'hôte qui en a un le classe, et le
#: verrou de la ligne du lot sérialise la marque de tentative (donc
#: l'avertissement, qui n'est envoyé qu'une fois, par le porteur de la
#: deuxième tentative).


def _candidate_repos(db, item: dict, repo: str = "") -> list[str]:
    """Les dépôts candidats pour lire le diff d'un lot (#3426).

    `repo` explicite est autoritaire, sans repli (#2968) ; sinon le dossier de
    travail de l'assigné au registre, puis le dépôt du projet
    (`lot_class.repository`). C'est exactement l'ordre de `class_at_merge` :
    la couverture d'un hôte et la lecture effective ne peuvent pas diverger.
    """
    if repo:
        return [repo]
    candidates: list[str] = []
    for path in (_repo_of_agent(db, str(item.get("assignee") or "")),
                 lot_class.repository(getattr(db, "cfg", None), item) or ""):
        path = str(path or "").strip()
        if path and path not in candidates:
            candidates.append(path)
    return candidates


def _host_holds_lot_repo(db, item: dict, repo: str = "", *,
                         slug_of=None) -> bool:
    """#3426 : cet hôte a-t-il un dépôt candidat pour ce lot ?

    Un hôte **sans dépôt** ne peut rien classer : le rattrapage s'abstient
    pour ce lot (aucune tentative, aucun avertissement) et laisse l'hôte qui
    porte le clone le classer. C'est le correctif des avertissements « aucun
    dépôt lisible » partis d'ameesh-agents-1 à chaque fusion légère, alors
    qu'ameesh-agents-2 classait ensuite (#134889, #135218, #135227, #135228).

    Un `repo` explicite et le dépôt du projet (`radar.projects`, ou le clone
    du canon) comptent sans autre preuve : c'est là qu'un avertissement est
    actionnable si le clone ne contient pas la fusion. Le dossier de
    l'assigné ne compte pas s'il est reconnu comme un AUTRE dépôt (un agent
    d'un autre projet sur le même hôte) ; un dossier dont le `origin` n'est
    pas lisible reste un candidat — on ne prouve pas qu'il est le mauvais.
    """
    if repo:
        return True
    project = str(lot_class.repository(getattr(db, "cfg", None), item) or "").strip()
    if project and os.path.isdir(os.path.expanduser(project)):
        return True
    cwd = _repo_of_agent(db, str(item.get("assignee") or ""))
    if not cwd:
        return False
    expected_slug, expected_name = plan_git._expected_repo(item)
    if not expected_slug and not expected_name:
        return True
    # #3426 : le slug est relu une fois par dossier dans un passage — le
    # même dossier d'agent revient pour chaque lot, et `git remote` n'est pas
    # gratuit quand une vingtaine de lots sont candidats.
    slug = (slug_of or plan_git._repo_slug)(cwd)
    if not slug:
        return True          # dépôt non reconnu : on ne peut pas prouver le contraire
    if expected_slug:
        return slug == expected_slug
    return slug.rsplit("/", 1)[-1] == expected_name


def _open(ops, ref: str) -> Optional[dict]:
    """Le lot de relecture déjà écrit pour `ref`, **quel que soit son état**.

    L'identité tient à la `issue_ref` (`review-after-merge:#<lot>`), pas à
    l'état du lot : un lot de relecture fermé reste celui de sa fusion, et la
    marque au journal peut manquer (écriture ratée avant cette version, journal
    tronqué). `open_by_ref` reste le repli d'un magasin sans `by_issue_ref`.
    """
    for name in ("by_issue_ref", "open_by_ref"):
        reader = getattr(ops, name, None)
        if reader is None:
            continue
        try:
            rows = reader(ref) or []
        except Exception:  # noqa: BLE001 - magasin incomplet : on essaie l'autre
            continue
        if rows:
            return rows[0]
    return None


def opened_for(db, lot: int) -> Optional[dict]:
    """Le lot de relecture déjà écrit pour `lot`, quel que soit son état (#3413).

    Même lecture que `open_for_merge` (idempotente) ; le chemin `already` de
    `close_merged` la consulte avant la borne du rattrapage, pour dire
    « relecture #N déjà ouverte » plutôt qu'un motif de refus trompeur quand
    l'ouverture existe. Best effort : une panne de lecture rend None, la
    reprise suit son cours habituel."""
    try:
        return _open(storage.of(db).work, ref_of(lot))
    except Exception:  # noqa: BLE001 - l'appelant garde son chemin normal
        return None


def _noted(ops, lot: int, prefix: str) -> list:
    """Les notes du journal du lot `lot` qui commencent par `prefix`.

    #1448 N2 : la lecture est bornée par le préfixe et le lot, jamais par les
    200 derniers événements — une marque ancienne d'un lot très bavard reste
    lue. Best effort : un journal illisible ne rend aucune trace."""
    try:
        rows = ops.events_noted(prefix, 0.0, ids=[int(lot)])
        return list(rows or [])
    except Exception:  # noqa: BLE001 - journal illisible : pas de trace
        return []


def _marked(ops, lot: int, ref: str) -> bool:
    """Vrai si le journal du lot `lot` porte déjà la marque `ref`."""
    try:
        events = ops.events(int(lot), 200) or []
    except Exception:  # noqa: BLE001 - journal illisible : on retombe sur `_open`
        return False
    return any(ref in str(e.get("note") or "") for e in events)


#: ce qu'écrit le corps quand le harnais de l'auteur n'est pas tracé (#1053)
UNTRACED = "non tracé au gel"


def _freezes(db, lot: int) -> list:
    """Tous les gels du lot (jalons `frozen`), du plus ancien au plus récent."""
    try:
        rows = work.milestones(db, int(lot)) or []
    except Exception:  # noqa: BLE001 - jalons illisibles : pas de trace
        return []
    return sorted((r for r in rows if r.get("kind") == "frozen"),
                  key=lambda r: (r.get("at_ts") or 0, int(r.get("id") or 0)))


#: #1859 : les notes du journal qui nomment un détenteur du lot
_HELD = re.compile(r"(?:^|; )(?:assigned to|delegated to) ([^\s,;()]+)|\(before: ([^\s,;()]+)")


def _holders(db, lot: int, item: dict) -> set:
    """Les agents qui ont tenu le lot : l'assigné courant et ceux que le
    journal nomme (`assigned to X`, `delegated to X`, `(before: X)`).

    #1859 : un pilote ou le bureau qui repose le gel n'a jamais tenu le lot ;
    son gel ne fait pas de lui l'auteur. Un ancien détenteur qui repose le gel,
    lui, en reste l'auteur."""
    names = {str(item.get("assignee") or "").strip()}
    try:
        for e in storage.of(db).work.events(int(lot), 500) or []:
            for m in _HELD.finditer(str(e.get("note") or "")):
                names.add(m.group(1) or m.group(2))
    except Exception:  # noqa: BLE001 - journal illisible : l'assigné seul
        pass
    names.discard("")
    names.discard("nobody")
    return names


def _harness_of(row: Optional[dict]) -> str:
    return str((row or {}).get("harness") or "")


def _turns_harness(db, agent: str, at_ts: float, *, contains: bool = True) -> str:
    """Le harnais des tours de `agent` autour de `at_ts` (epoch).

    `turn_costs` (0014) est la seule trace du harnais qui a réellement tourné :
    une ligne par tour fini, écrite avec le harnais du flux. Le registre, lui,
    ne dit que l'état courant. Les lignes écartées (0043) sont gardées : le
    tour a eu lieu, seule sa comptabilisation a été corrigée.

    #1777 N1 : `cost.record` écrit la ligne du tour à sa FIN, donc le tour qui
    a posé le gel est le premier écrit APRÈS l'instant du gel — tant qu'il
    reste dans `FREEZE_TURN_WINDOW_S`, faute de quoi c'est une session
    suivante et l'on retombe sur le dernier tour antérieur. `contains=False`
    (gel posé par un tiers : l'agent n'a pas forcément tourné) lit d'abord le
    dernier tour antérieur, et ne prend le suivant qu'à défaut."""
    ledger = storage.of(db).turn_costs
    after = ledger.first_after(agent, float(at_ts))
    before = ledger.last_before(agent, float(at_ts))
    within_window = after is not None and (
        0 <= float(after.get("recorded_ts") or 0) - float(at_ts) <= FREEZE_TURN_WINDOW_S)
    if contains and within_window:
        # le tour qui a posé le gel : la première ligne écrite après lui, si
        # elle reste dans la fenêtre d'un tour, même sans tour antérieur.
        return _harness_of(after)
    if before is not None:
        return _harness_of(before)
    return _harness_of(after) if within_window else ""


def _author_at_freeze(db, lot: int, item: dict) -> dict:
    """L'auteur du lot produit, et le harnais sous lequel il tournait au gel.

    #1053 : le gel est le geste de livraison — son acteur est l'auteur du lot
    produit, et le registre de ses tours est relu autour de l'instant du gel
    (`_turns_harness`). L'assigné courant ne le remplace pas : la relecture
    réassigne le lot après le gel. Sans gel, sans acteur ou sans tour tracé, la
    famille reste inconnue : le registre courant n'est jamais consulté pour la
    deviner, il ne prouve rien d'historique (L242b, rotation, `adopt`).

    #1777 N2 / #1859 : un gel reposé par un tiers qui n'a jamais tenu le lot
    (pilote, bureau, `--actor`) ne fait pas de lui l'auteur : les gels sont
    relus du plus récent au plus ancien, et l'on garde le premier dont l'acteur
    a tenu le lot (`_holders`). Le tiers écarté est nommé au corps (`refroze`).
    Si aucun acteur retenu n'a de tour tracé, on relit les tours de l'assigné,
    qui porte la production."""
    assignee = str(item.get("assignee") or "").strip()
    out = {"name": assignee, "harness": ""}
    try:
        freezes = _freezes(db, lot)
        if not freezes:
            return out
        holders = _holders(db, lot, item)
        third, freeze = "", None
        for row in reversed(freezes):
            actor = str(row.get("actor") or "").strip()
            name = actor or assignee
            if name and name not in holders:
                third = third or name
                continue
            freeze = row
            break
        if freeze is None:
            # aucun gel d'un détenteur : l'assigné est le producteur, et n'a
            # pas posé ce gel (#1777 N2)
            freeze = freezes[-1]
            at_ts = freeze.get("at_ts")
            out["name"] = assignee
            if assignee and at_ts is not None:
                out["harness"] = _turns_harness(db, assignee, at_ts, contains=False)
        else:
            actor = str(freeze.get("actor") or "").strip()
            name = actor or assignee
            out["name"] = name
            at_ts = freeze.get("at_ts")
            if name and at_ts is not None:
                harness = _turns_harness(db, name, at_ts,
                                         contains=(not actor or actor == name))
                if not harness and actor and actor != assignee:
                    # N2 : le gel par un tiers sans tour tracé ne prouve pas la
                    # famille ; l'assigné est le producteur, et n'a pas posé ce
                    # gel.
                    out["name"] = assignee
                    harness = _turns_harness(db, assignee, at_ts, contains=False)
                out["harness"] = harness
        if third:
            out["refroze"] = third
    except Exception:  # noqa: BLE001 - toute trace illisible : famille inconnue
        pass
    return out


def review_ref(item: dict) -> str:
    """La référence `review-after-merge:#<lot>` que porte le lot (#2552).

    `issue_ref` d'abord — c'est la marque écrite par l'ouverture —, puis
    `source` : des lots de relecture plus anciens portent la référence en
    `source` seulement, avec un `issue_ref` GitHub ou vide (#1218, #1231,
    #1269). Rend "" hors lot de relecture ; la chaîne brute (minuscule) sinon,
    que `required_family` valide."""
    item = item or {}
    for field in ("issue_ref", "source"):
        ref = str(item.get(field) or "").strip().lower()
        if ref.startswith(PREFIX):
            return ref
    return ""


def _required_family(db, item: dict, families) -> Optional[str]:
    """`required_family` avec les familles du canon déjà lues."""
    ref = review_ref(item)
    if not ref:
        return None
    try:
        lot = int(ref[len(PREFIX):].strip().lstrip("#"))
        source = work.get(db, lot)
        if source is None:
            return ""
        family = reviewer_family(_author_at_freeze(db, lot, source).get("harness"),
                                 families)
    except Exception:  # noqa: BLE001 - lecture impossible : famille inconnue
        return ""
    return "" if family == UNKNOWN_FAMILY else family


def required_family(db, item: dict) -> Optional[str]:
    """La famille de harnais qu'exige la prise d'un lot de relecture (#2377).

    `None` quand ce n'est pas un lot de relecture (ni `issue_ref` ni `source`
    sous le préfixe `review-after-merge:`, #2552) : aucune garde. Une chaîne
    vide quand la famille de l'auteur au gel est inconnue, ou quand sa trace
    est illisible : le lot n'est alors **pris par personne** au repos (doute ->
    non pris, canon §8) et attend une attribution explicite. Sinon la famille
    opposée à celle de l'auteur au gel (`reviewer_family`), celle que le canon
    §2 demande au relecteur.

    Les familles viennent du canon compilé (`compiled_families`, #2664) ; un
    canon illisible rend la famille inconnue, jamais supposée. La lecture
    réutilise `_author_at_freeze` : c'est la même preuve que celle qui a écrit
    le corps du lot (gel, acteur, tours tracés), jamais le registre courant —
    il ne dit rien du harnais qui a produit le travail (#1053)."""
    if not review_ref(item):
        return None
    try:
        families = compiled_families(db)
    except FamiliesError:
        return ""
    return _required_family(db, item, families)


def may_take(db, item: dict, agent: dict) -> bool:
    """Un agent au repos peut-il prendre ce lot ? (#2377)

    Hors lot de relecture, oui, sans autre condition. Pour un lot de
    relecture, seule la famille opposée à celle de l'auteur au gel le prend ;
    la session vivante du preneur fait foi (`registry.live_harness`, #1037),
    et un harnais hors des familles du canon (`family_of`) ne compte pas.
    Famille de l'auteur inconnue, canon illisible ou famille du preneur
    inconnue : non — la prise ne devine jamais une indépendance d'avis
    (#1374/#2664)."""
    if not review_ref(item):
        return True
    try:
        families = compiled_families(db)
    except FamiliesError:
        return False
    family = _required_family(db, item, families)
    if family is None:
        return True
    from . import registry
    mine = family_of(registry.live_harness(agent or {}), families)
    return bool(family) and family == mine



def title_of(item: dict, lot: int) -> str:
    """Le titre du lot de relecture."""
    return "REVUE #%d : relecture après fusion — %s" % (
        int(lot), " ".join(str(item.get("title") or "").split())[:90])


def body_of(item: dict, lot: int, *, klass: dict, merged_sha: str = "", pr=None,
            target: str = "", author: Optional[dict] = None, family: str = "",
            families=()) -> str:
    """Le corps du lot de relecture : quoi relire, par qui, avant quand."""
    author = author or {}
    who = author.get("name") or "inconnu"
    # #2384 : la famille se lit sur le harnais du gel, jamais sur le texte
    # affiché (le nom d'un tiers qui a reposé le gel peut contenir un mot de
    # famille) ; le corps doit annoncer exactement ce que la garde relira.
    author_family = family_of(author.get("harness"), families)
    harness = author.get("harness") or UNTRACED
    if author.get("refroze"):
        # #1859 : un tiers a reposé le gel ; il n'est pas l'auteur
        harness += "; gel reposé par %s, qui n'a pas tenu le lot" % author["refroze"]
    where = "PR #%s" % pr if pr else (merged_sha[:12] if merged_sha else "?")
    why = " ".join(str((klass or {}).get("why") or "").split())
    critical = bool((klass or {}).get("critical"))
    pair = review_families(author.get("harness"), families) if critical else ()

    author_human = who.lower().startswith(work.HUMAN_PREFIX)
    untraced = critical and not author_family and not author_human
    lines = [
        ("Relecture après fusion (canon `processus/lots.md` §2, **porte de fusion** : "
         "deux avis indépendants)." if critical
         else "Relecture après fusion (canon `processus/lots.md` §2, lot léger)."),
        "",
        "Lot fusionné : #%d « %s »" % (int(lot), " ".join(str(item.get("title") or "").split())),
        "- Classe : %s%s." % ((klass or {}).get("class") or "?",
                              " (%s)" % why if why else ""),
        "- Fusion : %s%s sur %s." % (merged_sha[:12] if merged_sha else "?",
                                     " (%s)" % where if pr else "",
                                     target or item.get("branch_target") or "sa cible"),
        "- Auteur : %s (harnais %s)." % (who, harness),
    ]
    if untraced:
        # #2384 : sans famille d'auteur tracée, les deux avis ne peuvent pas
        # prouver leur indépendance et la garde de livraison refuse `--done`.
        # Le corps le dit avant les verdicts, avec la route de sortie.
        lines.extend([
            "- **Famille de l'auteur non tracée** : l'indépendance des deux avis "
            "ne peut pas être prouvée ; `ameesh work close <id> --done` sera refusé "
            "même après deux verdicts `ok` (garde de livraison, #2374).",
            "- Route : livrer les verdicts sur le lot source, puis saisir le pilote ; "
            "il tranche l'abandon de la mission (`ameesh work close <id> --abandoned`), "
            "son remplacement (`--superseded-by <id>`) ou une dispense, consignée dans "
            "la note de clôture. Ne pas clore `--done` : la garde le refuse.",
        ])
    if critical:
        lines.extend([
            "- Relecteurs : **deux familles distinctes**, hors de celle de l'auteur — "
            "%s (règle du pilote du 13/10)." % (" et ".join(pair) or
                                                 "familles non lisibles au canon"),
            "- Avis requis : **%d** verdicts indépendants sur la fusion ; le lot n'est "
            "clos qu'après le second." % CRITICAL_REVIEWS,
        ])
    else:
        lines.append(
            "- Relecteur : une **autre famille de modèle** — %s (règle du pilote du 13/10)."
            % (family or reviewer_family(author.get("harness"), families)))
    lines.extend([
        "- Échéance : **%s**, le jour même de la fusion." % deadline(),
        "",
        "À faire :",
        "- lire le diff fusionné (`git show %s`) et vérifier les cas limites ;"
        % (merged_sha[:12] if merged_sha else "<sha>"),
    ])
    if critical:
        lines.append(
            "- livrer un verdict par relecteur (`ameesh work milestone %d verdict ok|blocked "
            "--sha %s`) ; les deux familles restent distinctes de celle de l'auteur ;"
            % (int(lot), merged_sha or "<sha>"))
    else:
        lines.append(
            "- livrer le verdict (`ameesh work milestone %d verdict ok|blocked --sha %s`) ;"
            % (int(lot), merged_sha or "<sha>"))
    lines.extend([
        ("- **livrés les verdicts ? saisir le pilote** pour clore cette mission "
         "selon la route ci-dessus ;" if untraced else
         "- **livré le verdict ? clore cette mission** : `ameesh work close <id> --done` — "
         "elle n'a ni branche ni fusion, et le verdict du parent relu en est la preuve "
         "(#1280) ;"),
        "- si un défaut **non bloquant** se corrige en moins de 5 minutes, le corriger "
        "et pousser la correction sur la cible : le relecteur corrige lui-même dès le "
        "premier tour (canon §2, home#81) ; les autres défauts non bloquants vont au "
        "backlog ;",
        "- **correction** (canon §2, home#81) : le relecteur corrige lui-même, y compris "
        "un défaut **bloquant** (B, P1) — après la fusion, le correctif passe par le lot "
        "de correction ; il pousse sa correction au lieu de la renvoyer à l'auteur ; "
        "au-delà de ~50 lignes ou pour un changement de conception, la correction "
        "revient à l'auteur ; en classe **sensible**, la seconde famille relit le lot "
        "**avec** la correction, dans le même tour, et reste distincte de chaque "
        "auteur ;",
        "- les commits du correcteur portent leur preuve d'origine "
        "(`ameesh provenance show --commit <sha>`) : il en est l'auteur, et son avis "
        "ne couvre pas sa correction ;",
        "- %s." % BLOCKING,
        "",
        ("Ce lot n'est assigné à personne : le premier relecteur disponible de la bonne "
         "famille le prend ; une seconde famille doit rendre le second avis avant la "
         "clôture." + (" La clôture `--done` reste soumise à la route ci-dessus tant "
                       "que la famille de l'auteur n'est pas tracée." if untraced else "")
         if critical else
         "Ce lot n'est assigné à personne : il est dans la file de l'équipe, le premier "
         "relecteur disponible de la bonne famille le prend."),
    ])
    return "\n".join(lines)


def _review_recursion(item: dict) -> str:
    """Garde anti-récursion (#1280 B2) : rendre la raison si `item` est
    lui-même un avis, "" sinon. Un avis n'a ni code ni fusion à relire."""
    if work.is_review(item):
        return ("lot #%d : c'est un avis (relecture), la relecture d'un avis ne "
                "s'ouvre pas" % int(item.get("id") or 0))
    return ""


def _entered(repo: Optional[str], merged: str, frozen: str) -> bool:
    """Le contenu du lot fusionné **égale-t-il** le gel relu ?

    Preuve par le delta net, jamais par l'ancêtre seul (B1/P1 des revues cx1
    et dev1 #2585) : le diff du commit fusionné depuis son **premier parent**
    doit avoir le même patch-id que le diff du gel depuis sa base commune avec
    ce premier parent (`plan_git._diff_patch_id`). Le premier parent est la
    cible au moment de la fusion pour un commit de fusion, la base du commit
    pour un squash ou un cherry-pick.

    Ainsi une tête C poussée après le gel A puis fusionnée (merge ou voie
    `pr`, `merged_sha = C`) a un delta A+C, différent de A : la REVUE reste
    due. Une résolution de fusion qui ajoute du contenu change aussi le delta
    du commit de fusion : refusée. Un gel A simplement **ancêtre** du commit
    fusionné ne suffit jamais. L'égalité exacte des commits est le seul
    raccourci (même contenu par construction). Sans dépôt, commit illisible ou
    patch-id indisponible : rien n'est prouvé."""
    if not repo or not merged or not frozen:
        return False
    try:
        head = plan_git._commit(str(repo), str(merged))
        tip = plan_git._commit(str(repo), str(frozen))
        if not head or not tip:
            return False
        if plan_git.same_commit(head, tip):
            return True
        parents = plan_git._commit_parents(str(repo), head)
        if not parents:
            return False
        base = parents[0]
        reviewed_base = plan_git._out(str(repo), "merge-base", tip, base)
        if not reviewed_base:
            return False
        delivered = plan_git._diff_patch_id(str(repo), base, head)
        reviewed = plan_git._diff_patch_id(str(repo), reviewed_base, tip)
        return bool(delivered and reviewed and delivered == reviewed)
    except Exception:  # noqa: BLE001 - preuve impossible : pas de dispense
        return False


def _heads_on_merge(db, lot: int, merged: str, repo: Optional[str] = None) -> list:
    """Les têtes qu'une fusion a fait entrer : le sha fusionné, le gel **prouvé**.

    La file en mode `pr` nomme la tête du lot (`landing_sha`) ; une fusion
    locale ou un relevé de branches nomme le commit de fusion sur la cible.
    Dans ce second cas, l'avis est porté par le dernier gel : il n'est compté
    que si Git prouve que le **delta du lot fusionné** est celui du gel
    (`_entered`). Un gel A relu puis une tête B différente fusionnée ne
    couvrent pas B : `_entered` est faux et la REVUE reste due (B1 cx1/dev1).
    Une lecture de jalon impossible ne rend que les têtes connues, jamais une
    tête inventée."""
    heads: list = []
    merged_heads: list = []

    def add(value, into: list) -> None:
        text = str(value or "").strip()
        if text and not any(plan_git.same_commit(text, head) for head in into):
            into.append(text)

    add(merged, merged_heads)
    if not merged_heads:
        # Le sha de la fusion constatée fait foi quand l'appelant le connaît :
        # un jalon `merged` plus ancien ne doit pas élargir les têtes au-delà
        # du fait examiné (sinon un gel égal au jalon dispenserait une tête C
        # différente). Le jalon n'est qu'un repli quand le sha manque.
        add(merged_sha(db, lot), merged_heads)
    for head in merged_heads:
        add(head, heads)
    freezes = _freezes(db, lot)
    if freezes:
        frozen = str(freezes[-1].get("sha") or "").strip()
        # Le gel n'est une tête relue que si Git prouve que le delta livré est
        # le sien ; sans cette preuve, l'avis sur le gel ne couvre pas la
        # fusion.
        if frozen and any(_entered(repo, head, frozen) for head in merged_heads):
            add(frozen, heads)
    return heads


def _family_at(db, actor: str, at_ts, *, families) -> str:
    """La famille de modèle tracée de `actor` **à l'instant du verdict**.

    Le registre courant ne prouve rien : une rotation de harnais (Codex vers
    Claude) ferait compter un avis rendu sous Codex comme indépendant d'un
    auteur Codex. La trace est celle des tours (`turn_costs`), relue autour de
    l'instant du verdict par `_turns_harness` — la même preuve que la famille
    de l'auteur au gel (#1053). Les familles connues sont celles du canon
    compilé (#2664), jamais une liste locale. Sans tour tracé, aucune famille
    n'est prouvée et la relecture reste due (autre point de la revue cx1
    #2585)."""
    actor = str(actor or "").strip()
    if not actor or actor.startswith(work.HUMAN_PREFIX) or ":" in actor:
        return ""
    try:
        return family_of(_turns_harness(db, actor, float(at_ts or 0.0)), families)
    except Exception:  # noqa: BLE001 - trace illisible : famille non prouvée
        return ""


def _reviewed_on_head(db, item: dict, *, heads: list, required: int,
                      author: Optional[dict] = None, families) -> dict:
    """Les avis `ok` d'une **autre famille** qui couvrent déjà `heads` (#2585).

    Rend `{done, accepted, blocked}`. `done` n'est vrai qu'avec assez d'avis
    indépendants : dernier verdict de chaque relecteur, `ok`, sur une des
    têtes, d'un acteur distinct de l'auteur, de famille **tracée à l'instant
    du verdict** (`_family_at`) et différente de celle de l'auteur (un auteur
    humain n'a pas de famille). Un `blocked` courant interdit la dispense :
    la correction puis un `ok` restent dus.

    L'indépendance ne se suppose jamais (#1374) : une famille illisible ou
    inconnue ne prouve pas « une autre famille », donc la relecture reste due.
    Les familles connues viennent du canon compilé (#2664), comme celle de
    l'auteur au gel. C'est plus strict que `guard_delivery`, qui garde la règle
    de clôture de la mission ; ici on décide de **ne pas ouvrir** une mission,
    jamais de fermer une preuve manquante."""
    out = {"done": False, "accepted": [], "blocked": []}
    author = author or {}
    name = str(author.get("name") or "").strip()
    if not name or not heads:
        return out
    author_family = family_of(author.get("harness"), families)
    author_human = name.lower().startswith(work.HUMAN_PREFIX)
    try:
        rows = work.milestones(db, int(item["id"]), limit=200) or []
    except Exception:  # noqa: BLE001 - lecture impossible : la relecture reste due
        return out
    latest: dict = {}
    for mark in rows:
        if str(mark.get("kind") or "") != "verdict":
            continue
        # `work.milestones` rend du plus récent au plus ancien : le premier
        # verdict de chaque relecteur est son dernier.
        latest.setdefault(str(mark.get("actor") or "").strip().lower() or "?", mark)
    accepted, accepted_families, blocked = [], set(), []
    for mark in latest.values():
        kind = str(mark.get("verdict") or "")
        if kind not in ("ok", "blocked"):
            continue
        if not any(plan_git.same_commit(mark.get("sha"), head) for head in heads):
            continue
        actor = str(mark.get("actor") or "").strip()
        if kind == "blocked":
            blocked.append(actor or "?")
            continue
        if actor and actor.lower() == name.lower():
            continue                      # on ne relit pas son propre travail
        family = _family_at(db, actor, mark.get("at_ts"), families=families)
        if not family:
            continue                      # aucune preuve d'une autre famille
        if not author_human:
            if not author_family or family == author_family:
                continue
        if required >= 2 and family in accepted_families:
            continue
        accepted_families.add(family)
        accepted.append({"actor": actor or "?", "family": family})
    if blocked:
        # Un avis bloquant n'est pas une relecture rendue : la relecture
        # après fusion reste due (la correction suit son propre lot).
        return {"done": False, "accepted": accepted, "blocked": blocked}
    out.update({"done": len(accepted) >= int(required),
                "accepted": accepted, "blocked": blocked})
    return out


def open_after_merge(db, *, lot: int, klass: dict, item: Optional[dict] = None,
                     merged_sha: str = "", pr=None, target: str = "",
                     actor: str = work.SYSTEM_SENDER,
                     repo: Optional[str] = None) -> dict:
    """Ouvre le lot de relecture du lot léger `lot`, s'il n'existe pas déjà.

    Rend `{lot, id, created, class, merged_class, critical, reviews,
    already_reviewed, reviewed_by, why}` — ne lève jamais. `klass` est la
    classe lue sur le diff fusionné ; seule `light` ouvre un lot de relecture,
    un doute ou une classe supérieure n'ouvre rien (le canon §2 ne demande une
    relecture après fusion que pour les lots légers). Une classe `light` dont
    le diff touche pourtant un chemin critique (porte de fusion, SQL, sécurité,
    prod : `CRITICAL_SCOPE`) ouvre une relecture **sensible**, avec **deux**
    avis de familles distinctes (#2374) : le classement du canon ne peut pas
    dispenser la sécurité d'une seconde lecture quand il a omis le chemin.

    #2585 : si assez d'avis `ok` d'une autre famille couvrent déjà la tête
    fusionnée, rien n'est créé : `already_reviewed` est vrai et `reviewed_by`
    nomme les avis retenus. Le gel relu n'est compté que si Git prouve son
    entrée dans la fusion (`repo` : `_entered`). Un avis bloquant, une famille
    inconnue, un sha différent ou une preuve Git absente laissent la relecture
    due.

    #4747 : un porteur de paquet (`integration/**`) n'ouvre rien — ses unités
    ont leurs avis sur leur tête gelée, et son éventuel delta propre de
    résolution a passé la porte avant la fusion (§12).

    La création et la marque au journal de `lot` sont **une seule écriture**
    (revue #544 r1) : un échec de la marque annule la création, donc un nouvel
    appel ne peut pas ouvrir un second lot de relecture. La marque de journal
    porte en outre la donnée structurée `requirement_mark` (nombre d'avis, sha
    fusionné, auteur) que `guard_delivery` relit pour refuser une clôture sans
    les avis dus (#2374)."""
    lot = int(lot)
    klass = dict(klass or {})
    merged_class = str(klass.get("class") or "")
    critical = merged_class == lot_class.LIGHT and _critical_changed(klass)
    if critical:
        why = " ".join(str(klass.get("why") or "").split())
        klass.update({
            "class": lot_class.SENSITIVE,
            "critical": True,
            "why": ((why + "; ") if why else "")
                   + "porte de fusion : chemin critique, deux avis requis",
        })
    review_class = str(klass.get("class") or "")
    # #2642 : le plancher léger vient du canon quand la classe le porte (le
    # lecteur `lot_class` l'a lu pour ce même diff) ; le plancher historique
    # d'un avis reste le repli d'une classe sans paramètre, jamais zéro.
    reviews = (CRITICAL_REVIEWS if critical
               else int(klass.get("light_reviews") or LIGHT_REVIEWS)
               if merged_class == lot_class.LIGHT else 0)
    out = {"lot": lot, "id": None, "created": False, "class": review_class,
           "merged_class": merged_class, "critical": critical,
           "reviews": reviews, "already_reviewed": False, "reviewed_by": [],
           "why": ""}
    if merged_class != lot_class.LIGHT:
        out["why"] = ("class %s: pas de lot de relecture après fusion (§2)"
                      % (merged_class or "?"))
        return out
    if item is None:
        item = work.get(db, lot)
    if item is None:
        out["why"] = "lot #%d introuvable" % lot
        return out
    recursion = _review_recursion(item)
    if recursion:
        out["why"] = recursion
        return out
    # #4747 : un porteur de paquet (`integration/**`) n'a pas de delta propre à
    # relire après fusion — ses unités ont leurs avis sur leur tête gelée, et
    # son éventuel delta de résolution a passé la porte avant la fusion (§12).
    # Lui ouvrir une relecture dépensait un tour de relecteur par paquet léger
    # fusionné (#4742 ouvert sur le porteur #4739).
    from . import packet_reviews
    if packet_reviews.is_vehicle(item):
        out["why"] = ("véhicule de paquet (%s) : les avis portent sur les membres "
                      "(§12) — pas de relecture après fusion"
                      % (item.get("branch") or "integration/**"))
        return out
    ref = ref_of(lot)
    ops = storage.of(db).work
    existing = _open(ops, ref)
    if existing is not None:
        out.update({"id": int(existing["id"]),
                    "why": "lot de relecture #%d déjà ouvert (état %s)"
                           % (int(existing["id"]), existing.get("state") or "?")})
        return out
    if _marked(ops, lot, ref):
        out["why"] = "lot de relecture déjà marqué au journal de #%d" % lot
        return out
    author = _author_at_freeze(db, lot, item)
    # #2664 : familles du canon compilé ; un canon illisible laisse la famille
    # inconnue (le corps et la marque le disent, la garde de livraison refuse
    # `--done` à deux avis). L'ouverture d'une relecture reste un fait dû.
    try:
        families = compiled_families(db)
    except FamiliesError:
        families = ()
    family = reviewer_family(author.get("harness"), families)
    author_family = family_of(author.get("harness"), families)
    # #2585 : la revue déjà rendue avant la fusion est la relecture due ; ne
    # pas en ouvrir une seconde. `merged_sha` peut être le commit de fusion
    # (voie locale) : `_heads_on_merge` ne compte le dernier gel que si Git
    # prouve que le delta livré est le sien (`repo`) — un gel A relu ne couvre
    # pas une tête B différente fusionnée, ni une résolution qui ajoute du
    # contenu. Les familles de l'auteur et des relecteurs sont celles du canon
    # compilé (#2664), jamais une liste locale.
    try:
        reviewed = _reviewed_on_head(
            db, item, heads=_heads_on_merge(db, lot, merged_sha, repo=repo),
            required=reviews, author=author, families=families)
    except Exception:  # noqa: BLE001 - lecture impossible : la relecture reste due
        reviewed = {"done": False, "accepted": [], "blocked": []}
    if reviewed["done"]:
        reviewed_by = ", ".join("%s (%s)" % (entry["actor"], entry["family"])
                                for entry in reviewed["accepted"])
        out.update({
            "already_reviewed": True,
            "reviewed_by": reviewed["accepted"],
            "why": ("avis déjà rendu sur la tête fusionnée : %s — pas de lot de "
                    "relecture ouvert" % reviewed_by),
        })
        # La marque terminale est le fait durable : le rattrapage ne
        # réexaminera pas un lot déjà relu (souvent sans dépôt pour prouver le
        # gel) et le journal dit pourquoi aucune mission n'est ouverte. Best
        # effort : si elle échoue, le rattrapage réévalue, jamais ne perd un
        # avis dû.
        try:
            ops.log_review_attempt(
                lot, str(item.get("state") or ""),
                "%s%d avis déjà rendu sur la tête fusionnée : %s"
                % (TERMINAL_PREFIX, lot, reviewed_by), actor)
        except Exception:  # noqa: BLE001 - marque de confort, jamais bloquante
            pass
        return out
    mark = requirement_mark(lot, required=reviews, merged_sha=merged_sha,
                            author=author.get("name") or "",
                            author_family=author_family)
    try:
        created = work.add(
            db, title=title_of(item, lot), type="evolution", source=ref,
            app=str(item.get("app") or ""),
            body=body_of(item, lot, klass=klass, merged_sha=merged_sha, pr=pr,
                         target=target, author=author, family=family,
                         families=families),
            issue_ref=ref, actor=actor, mark_item_id=lot,
            mark_note="%s lot #{lot} ouvert (classe %s, %d avis, relecture due %s) — %s"
                      % (ref, review_class, reviews, deadline(), mark))
    except Exception as exc:  # noqa: BLE001 - la fusion reste un fait
        # revue #544 r2 : un appel concurrent a pu créer le lot pendant
        # l'attente du verrou ; on rend l'identifiant du gagnant, pas un échec
        existing = _open(ops, ref)
        if existing is not None:
            out.update({"id": int(existing["id"]),
                        "why": "lot de relecture #%d déjà ouvert (course perdue : %s)"
                               % (int(existing["id"]), type(exc).__name__)})
            return out
        out["why"] = "lot de relecture non ouvert : %s: %s" % (
            type(exc).__name__, str(exc)[:200])
        return out
    out.update({"id": int(created["id"]), "created": True, "why": "ouvert"})
    return out


def source_of_ref(ref) -> int:
    """Le numéro du lot source d'une référence `review-after-merge:#<lot>`."""
    text = str(ref or "").strip()
    if not text.startswith(PREFIX):
        return 0
    tail = text[len(PREFIX):].lstrip("#").split(":", 1)[0].strip()
    return int(tail) if tail.isdigit() else 0


def requirement_mark(lot: int, *, required: int, merged_sha: str,
                     author: str = "", author_family: str = "") -> str:
    """La marque structurée de la relecture, écrite au journal du lot source.

    Le corps est la consigne lisible ; cette ligne de journal est la donnée
    que `guard_delivery` relit. Elle est écrite dans la même transaction que
    la création (voir `open_after_merge`), donc jamais absente pour un lot
    ouvert par ce code."""
    payload = {"schema": MARK_SCHEMA, "source": int(lot),
               "sha": str(merged_sha or "").strip().lower(),
               "required": int(required),
               "author": str(author or "").strip(),
               "author_family": str(author_family or "").strip()}
    return MARK_PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True)


def requirement_of(db, item: dict) -> Optional[dict]:
    """La marque structurée de la relecture, lue au journal du lot **source**.

    La marque est écrite par `open_after_merge` dans la ligne de journal du
    lot fusionné, dans la même transaction que la création du lot de
    relecture (voir `mark_note`). Le lot de relecture n'a pas de marque à lui :
    son `issue_ref` nomme le lot source, où la preuve vit.

    Un lot ouvert avant ce correctif n'a pas de marque : son avis historique
    reste un (voir `guard_delivery`). Une marque présente mais illisible ne se
    convertit jamais en « un avis » : la clôture est refusée."""
    source_lot = source_of_ref(item.get("issue_ref") or item.get("source"))
    if not source_lot:
        return None
    rows = storage.of(db).work.events_noted(
        ref_of(source_lot), 0.0, ids=[source_lot])
    # La première marque fait foi : elle est écrite dans la transaction qui
    # crée le lot de relecture (créé une seule fois) ; une note postérieure
    # ne peut pas abaisser le nombre d'avis dus.
    for row in rows:
        text = str(row.get("note") or "")
        at = text.find(MARK_PREFIX)
        if at < 0:
            continue
        payload = text[at + len(MARK_PREFIX):].strip()
        try:
            found = json.loads(payload)
        except ValueError as exc:
            raise work.WorkError(
                "lot #%s: marque de relecture illisible (%s) ; clôture refusée"
                % (item["id"], exc)) from exc
        if not isinstance(found, dict) or found.get("schema") != MARK_SCHEMA:
            raise work.WorkError(
                "lot #%s: marque de relecture incomplète ; clôture refusée"
                % item["id"])
        return found
    return None


def _merged_sha(db, item: dict) -> str:
    """Le sha du jalon `merged` du lot source, "" s'il n'en porte pas."""
    for mark in work.milestones(db, int(item["id"]), limit=200):
        if (mark.get("kind") or "") == "merged":
            sha = str(mark.get("sha") or "").strip().lower()
            if sha:
                return sha
    return ""


def guard_delivery(db, item: dict) -> dict:
    """#2374 : refuse la clôture `delivered` d'une relecture après fusion tant
    que ses avis ne sont pas rendus.

    Le corps du lot dit « clos après le second » ; cette garde le rend vrai.
    La marque structurée (`requirement_of`) donne le lot source, le sha
    fusionné, le nombre d'avis et la famille de l'auteur ; les jalons
    `verdict` du lot source sont comptés à ce sha, jamais lus dans le corps.
    Pour deux avis, les familles des relecteurs doivent être connues,
    distinctes et hors de celle de l'auteur ; un avis bloqué ne clôt rien
    (la correction puis un verdict `ok` restent dus).

    Un lot de relecture antérieur au correctif, sans marque, garde la règle
    historique (un avis). Ne rend rien ; lève `work.WorkError` quand il manque
    un avis.
    """
    source_lot = source_of_ref(item.get("issue_ref") or item.get("source"))
    if not source_lot:
        return {}
    source = work.get(db, source_lot)
    if source is None:
        raise work.WorkError(
            "lot #%s: relecture après fusion du lot #%s introuvable ; clôture refusée"
            % (item["id"], source_lot))
    data = requirement_of(db, item) or {}
    try:
        required = max(1, int(data.get("required") or 1))
    except (TypeError, ValueError) as exc:
        raise work.WorkError(
            "lot #%s: nombre d'avis illisible dans la marque de relecture ; "
            "clôture refusée" % item["id"]) from exc
    sha = str(data.get("sha") or "").strip().lower() or _merged_sha(db, source)
    author = str(data.get("author") or source.get("assignee") or "").strip()
    author_family = str(data.get("author_family") or "").strip()
    author_human = bool(author) and author.lower().startswith(work.HUMAN_PREFIX)
    if required >= 2 and not author_family and not author_human:
        raise work.WorkError(
            "lot #%s: famille de l'auteur inconnue ; indépendance des deux "
            "avis non prouvée, clôture refusée — route : le pilote tranche "
            "(abandon/remplacement de la mission, ou dispense consignée dans "
            "la note de clôture)" % item["id"])
    created = float(item.get("created_ts") or 0.0)
    repo = (_repo_of_agent(db, str(source.get("assignee") or ""))
            or lot_class.repository(getattr(db, "cfg", None), source) or "")
    from . import merge_queue
    # Le dernier verdict de chaque relecteur compte, comme à la porte des
    # avis : un `ok` ancien ne survit pas à un blocage plus récent du même
    # relecteur (`work.milestones` rend du plus récent au plus ancien).
    latest: dict = {}
    for mark in work.milestones(db, source_lot, limit=200):
        if (mark.get("kind") or "") != "verdict":
            continue
        actor = str(mark.get("actor") or "").strip()
        latest.setdefault(actor.lower() or "?", mark)
    accepted, families, blocked = [], set(), []
    for mark in latest.values():
        if (mark.get("verdict") or "") not in ("ok", "blocked"):
            continue
        if sha:
            try:
                same = merge_queue._same_sha(mark.get("sha"), sha, repo)
            except Exception:  # noqa: BLE001 - dépôt illisible : égalité stricte
                same = str(mark.get("sha") or "").strip().lower() == sha
            if not same:
                continue
        elif float(mark.get("at_ts") or 0.0) < created:
            continue                  # fusion sans sha : seuls les avis d'après
        actor = str(mark.get("actor") or "").strip()
        if (mark.get("verdict") or "") == "blocked":
            # un blocage courant n'est jamais masqué par l'avis d'un autre
            # relecteur, comme à la porte des avis (`blocked_verdicts`)
            blocked.append(actor or "?")
            continue
        if author and actor and actor.lower() == author.lower():
            continue                  # on ne relit pas son propre travail
        try:
            # #3578 : la famille du verdict (son reçu), pas la session courante
            family = merge_queue.verdict_family(db, mark)
        except merge_queue.RegistryReadError:
            family = ""              # #2521 : lecture impossible, famille non prouvée
        if family and author_family and family == author_family:
            continue                  # la famille de l'auteur ne compte pas
        if required >= 2:
            if not family or family in families:
                continue              # famille inconnue ou déjà comptée
            families.add(family)
        accepted.append(actor or "?")
    if blocked:
        raise work.WorkError(
            "lot #%s: relecture après fusion du lot #%s non livrable — verdict "
            "bloquant en vigueur sur %s (%s) : la correction puis un verdict ok du "
            "même relecteur restent dus"
            % (item["id"], source_lot, sha[:12] or "<sha>", ", ".join(sorted(blocked))))
    if len(accepted) >= required:
        return {"required": required, "accepted": accepted, "sha": sha}
    need = ("deux avis de familles distinctes" if required >= 2
            else "un avis d'une autre famille")
    raise work.WorkError(
        "lot #%s: relecture après fusion du lot #%s non livrable — %d avis "
        "indépendant%s requis sur %s, %d rendu(s) (%s). Livrez le(s) verdict(s) : "
        "ameesh work milestone %d verdict ok --sha %s"
        % (item["id"], source_lot, required, "" if required == 1 else "s",
           sha[:12] or "<sha>", len(accepted), need, source_lot, sha or "<sha>"))


def class_at_merge(db, lot: int, *, item: Optional[dict] = None,
                   repo: Optional[str] = None, target: str = "",
                   sha: str = "") -> dict:
    """La classe du lot au moment d'une fusion constatée (L539, canon §8).

    Le diff lu est celui du **lot** : `sha` (le commit de la fusion constatée)
    ou, à défaut, son gel, ramené au delta du lot par
    `lot_measures.lot_diff_range` — la fusion d'un paquet est lue sur le gel
    du lot, pas sur le diff du paquet (#2702), et une cible locale périmée
    n'élargit plus le diff à l'historique entre les deux. Lire `cible..gel`
    serait un piège : une fois la fusion faite, le gel est un ancêtre de la
    cible, ce diff est vide et toute classe y serait « light ».

    Un merge sans delta (arbre identique à son premier parent, le contenu du
    lot ayant été porté par un autre commit de la cible) a lui aussi un diff
    vide : la classe est alors relue sur le **delta propre au lot**
    (`lot_measures.lot_diff_range`, #2374 / #2340), jamais conclue « light »
    sur le vide.

    Les dépôts candidats sont essayés dans l'ordre : `repo` explicite
    (autoritaire, sans repli), sinon le dossier de travail de l'assigné au
    registre, puis le dépôt du projet (`lot_class.repository`) — toujours pour
    y lire le diff du lot. On retient le premier qui porte le commit de fusion
    ou le gel **et** dont le delta est lisible : un répertoire qui n'est pas un
    dépôt git, un clone partiel ou un diff illisible laisse la place au
    suivant (#2968). `target` la cible déjà résolue. Avant la lecture, chaque
    candidat qui ne prouve pas encore le commit rafraîchit sa cible exacte puis
    le commit (`lot_measures.refresh_target`, #3275) : un clone partagé non
    fetché depuis la fusion n'est pas un trou de fusion.

    Sans intervalle **prouvé** (aucun candidat ne contient le commit, diff
    illisible, ni gel ni commit), la classe n'est pas demandée à `of_lot` : son
    repli gel/cible rendrait `cible..gel`, vide dès que le gel est un ancêtre
    de la cible — « light » sur un lot SQL fusionné (R1 #1104 P2-1). On rend un
    doute conservateur, qui nomme les dépôts essayés, et d'une classe inconnue
    on n'ouvre pas de relecture."""
    from . import lot_measures, plan_git
    if item is None:
        item = work.get(db, lot)
    if item is None:
        return {"class": "", "why": "lot #%d introuvable" % int(lot), "doubt": True}
    # Le répertoire de l'assigné peut n'être qu'un dossier de travail (pas un
    # dépôt git) ou un clone partiel sans le commit : on essaie les candidats
    # dans l'ordre (--repo explicite, dépôt de l'assigné, dépôt du projet) et
    # on retient le premier qui prouve l'intervalle. --repo explicite reste
    # autoritaire, sans repli (#2968). #3426 : la même liste sert au rattrapage
    # pour savoir si CET hôte peut classer le lot.
    candidates = _candidate_repos(db, item, repo or "")
    where = candidates[0] if candidates else ""
    base = head = ""
    proven = False
    why = "aucun intervalle de fusion lisible"
    tried: list[tuple[str, str]] = []
    try:
        frozen = ""
        if candidates:
            freeze = plan_git.latest_freeze(work.milestones(db, int(lot)))
            frozen = str((freeze or {}).get("sha") or "").strip()
        tips = list(dict.fromkeys(t for t in ((sha or "").strip(), frozen) if t))
        if not candidates:
            why = "dépôt inconnu : aucun intervalle de fusion lisible"
        elif not tips:
            why = "ni commit de fusion ni gel : aucun intervalle de fusion lisible"
        unproven = ""
        for candidate in candidates:
            reason = ""
            # #3275 : un clone partagé n'est frais que de son dernier fetch ;
            # une fusion poussée depuis y est absente (Nexlink #3268). La
            # cible exacte, puis le commit de fusion, sont rafraîchis avant la
            # lecture ; un commit encore absent reste un doute nommé, que le
            # rattrapage retente au balayage suivant.
            refreshed = lot_measures.refresh_target(candidate, target or None, tips)
            # le commit de la fusion constatée d'abord ; à défaut le gel (fusion
            # déclarée sans commit lisible, `ameesh work merged` à la main)
            for tip in tips:
                # #2702 : lire le delta **du lot** d'abord — une fusion de paquet
                # ne classe pas ses lots sur le diff du paquet, et une cible
                # locale périmée n'élargit pas le diff à l'historique entre les
                # deux ; l'intervalle de la fusion reste le repli (#2374).
                # UnprovenLotDiff (#2823) interrompt aussi ce repli et l'essai
                # du gel seul : le paquet ne prouve pas le delta du lot.
                try:
                    rng = lot_measures.lot_diff_range(candidate, tip, target or None,
                                                      frozen=frozen or None)
                    if rng is None:
                        rng = lot_measures.diff_range(candidate, tip, target or None)
                    if not rng:
                        # #3056 : un dépôt qui porte le commit doute sur sa
                        # cible publiée (illisible, absente ou pointe non
                        # récupérée), il ne « manque » pas le commit
                        if lot_measures.has_commit(candidate, tip):
                            reason = ("le dépôt %s porte le commit %s, mais aucune "
                                      "cible publiée lisible ne situe sa fusion "
                                      "(cible absente, ls-remote illisible ou pointe "
                                      "non récupérée)" % (candidate, tip[:12]))
                        else:
                            reason = "le dépôt %s ne contient pas le commit %s" % (
                                candidate, tip[:12])
                        if refreshed:
                            reason += " (après %s)" % refreshed
                        continue
                    paths = lot_measures.diff_paths(candidate, *rng)
                except lot_measures.UnprovenLotDiff as exc:
                    # Ce n'est pas un dépôt illisible : c'est la preuve que le
                    # gel n'appartient pas à la fusion observée. Le doute vaut
                    # pour la lecture entière — ni un autre dépôt ni un autre
                    # commit ne doivent relire un gel séparé de sa fusion.
                    unproven = str(exc)
                    break
                except Exception as exc:  # noqa: BLE001 - candidat illisible : le suivant
                    reason = "lecture du diff impossible : %s: %s" % (
                        type(exc).__name__, str(exc)[:160])
                    break
                if paths is None:
                    reason = "diff de fusion illisible pour %s" % tip[:12]
                    break
                if paths:
                    where = candidate
                    base, head = rng
                    proven = True
                    break
                # #2374 (2e cas, #2340) : un intervalle vide — arbre du merge égal
                # à son premier parent, ou gel déjà porté par la cible — n'est pas
                # une preuve. Classer ce vide dirait « light » sans jamais lire le
                # lot : on doute.
                reason = ("diff de fusion vide et aucun delta du lot lisible pour %s"
                          % tip[:12])
                break
            if unproven:
                break
            tried.append((candidate, reason or "aucun intervalle lisible"))
            if proven:
                break
        if unproven:
            proven = False
            why = "lecture du diff impossible : UnprovenLotDiff: %s" % unproven
        elif not proven and tips and tried:
            if len(tried) == 1:
                why = tried[0][1]
            else:
                why = ("aucun dépôt candidat ne contient le commit %s : %s"
                       % (tips[0][:12], "; ".join(
                           "%s: %s" % (path, reason) for path, reason in tried)))
        # ni commit ni gel : `why` garde le motif posé plus haut, sans nommer
        # des dépôts qui n'ont rien eu à essayer.
    except Exception as exc:  # noqa: BLE001 - pas d'intervalle : doute, jamais de classe
        proven = False
        why = "lecture du diff impossible : %s: %s" % (type(exc).__name__, str(exc)[:160])
    if not proven:
        # R1 #1104 P2-1 : `of_lot` ne reçoit jamais ce repli (base/head vides),
        # qui relirait `cible..gel` — vide après fusion — et classerait light.
        return {"lot": int(lot), "class": lot_class.SENSITIVE, "why": why, "files": [],
                "policy_ref": "", "repo": where or "", "target": target or "",
                "frozen_sha": "", "doubt": True}
    try:
        # #2464 : la base est un commit, illisible pour l'axe release ; la
        # cible déjà résolue (`target`) porte cet axe quand la ligne du lot ne
        # le fait pas. Sans elle, `of_lot` relirait `origin/HEAD` — ou ne
        # verrait plus une release.
        class_item = item
        if target and not item.get("branch_target"):
            class_item = dict(item, branch_target=target)
        return lot_class.of_lot(db, int(lot), cfg=getattr(db, "cfg", None),
                                item=class_item, repo=where or None,
                                base=base or None, head=head or None)
    except Exception as exc:  # noqa: BLE001 - un doute, jamais une exception
        return {"class": "", "why": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
                "doubt": True}


def open_for_merge(db, lot: int, *, merged_sha: str = "", pr=None,
                   repo: Optional[str] = None, target: str = "",
                   actor: str = work.SYSTEM_SENDER) -> dict:
    """L539 : le lot de relecture d'une fusion enregistrée **hors file**.

    Toute voie qui enregistre une fusion (`work.close_merged`) passe ici : le
    relevé des branches (L118), `sync-merges`, `sync-github`, la porte
    `git-merge` et `ameesh work merged`. La classe est relue sur le delta du
    lot (`class_at_merge`) avec les indices de l'appelant ; seule
    `light` ouvre, un doute n'ouvre rien, et un `light` qui touche un chemin
    critique ouvre une relecture sensible à deux avis (#2374). Idempotent : un
    lot de relecture déjà écrit, **quel que soit son état**, est rendu sans
    relire la classe. `merged_class` garde la classe du diff fusionné (pour le
    rattrapage), `class` celle du lot de relecture ouvert. #2585 : un avis
    d'une autre famille déjà rendu sur la tête fusionnée donne
    `already_reviewed` sans création ; `repo` sert aussi de preuve que le gel
    relu est entré dans le commit fusionné. Ne lève jamais — la fusion est un
    fait déjà enregistré."""
    lot = int(lot)
    out = {"lot": lot, "id": None, "created": False, "class": "",
           "merged_class": "", "critical": False, "reviews": 0,
           "already_reviewed": False, "reviewed_by": [], "why": ""}
    try:
        item = work.get(db, lot)
        if item is None:
            out["why"] = "lot #%d introuvable" % lot
            return out
        recursion = _review_recursion(item)
        if recursion:
            out["why"] = recursion
            return out
        existing = _open(storage.of(db).work, ref_of(lot))
        if existing is not None:
            out.update({"id": int(existing["id"]),
                        "why": "lot de relecture #%d déjà ouvert (état %s)"
                               % (int(existing["id"]), existing.get("state") or "?")})
            return out
        where_target = target or str(item.get("branch_target") or "")
        klass = class_at_merge(db, lot, item=item, repo=repo, target=where_target,
                               sha=merged_sha)
        out["class"] = klass.get("class") or ""
        out["merged_class"] = out["class"]
        out["doubt"] = bool(klass.get("doubt"))
        if out["doubt"]:
            # #2823 : garder la cause, sans annoncer une classe confirmée.
            out["why"] = klass.get("why") or "classe après fusion incertaine"
            return out
        opened = open_after_merge(
            db, lot=lot, klass=klass, item=item, merged_sha=merged_sha, pr=pr,
            target=where_target, actor=actor, repo=repo)
        out.update({"id": opened.get("id"), "created": bool(opened.get("created")),
                    "class": opened.get("class") or out["class"],
                    "merged_class": opened.get("merged_class") or out["merged_class"],
                    "critical": bool(opened.get("critical")),
                    "reviews": int(opened.get("reviews") or 0),
                    "already_reviewed": bool(opened.get("already_reviewed")),
                    "reviewed_by": list(opened.get("reviewed_by") or []),
                    "why": opened.get("why") or ""})
        return out
    except Exception as exc:  # noqa: BLE001 - la fusion reste un fait
        out["why"] = "lot de relecture non ouvert : %s: %s" % (
            type(exc).__name__, str(exc)[:200])
        return out


#: le nombre de relectures **manquantes** examinées par un rattrapage (borné)
CATCH_UP_LIMIT = 20


def merged_sha(db, lot: int) -> str:
    """Le commit du dernier jalon `merged` EFFECTIF du lot, ou "" (best effort).
    Un jalon annulé par une correction `merged → build` est ignoré (#2317/N5)."""
    try:
        rows = [m for m in work.milestones(db, int(lot))
                if m.get("kind") == "merged" and not m.get("annulled")]
    except Exception:  # noqa: BLE001 - pas de jalon lisible : pas de commit
        return ""
    if not rows:
        return ""
    last = max(rows, key=lambda r: (r.get("at_ts") or 0, int(r.get("id") or 0)))
    return str(last.get("sha") or "").strip()


def _warn_unopened(db, entries: list[dict], host: str = "") -> str:
    """#3201/#3426 : les relectures qu'un hôte muni d'un clone n'a pas pu classer.

    Un simple journal ne se lit pas. La **deuxième** tentative d'un lot sur un
    hôte qui porte un dépôt candidat envoie un courrier durable aux pilotes
    déclarés, qui nomme les lots et le geste de reprise (`ameesh work merged
    <id> --sha <merge> --repo <clone>`, idempotent). #3413 : ce geste relance
    réellement la classification (le clone fourni n'attend ni le délai ni le
    plafond de tentatives du balayage) — il répondait « already merged:
    nothing to do ». #3426 : seuls les hôtes munis d'un dépôt tentent et
    avertissent — un hôte sans clone s'abstient sans courrier, et la marque de
    tentative partagée (verrou de la ligne du lot) fait qu'une deuxième
    tentative n'est écrite que par un seul hôte. Best effort : sans pilote
    déclaré ou sans dépôt de courrier, le journal reste et le compte rendu le
    dit — un avertissement ne casse jamais le relevé. Rend une ligne lisible.

    #3453 : chaque pilote ne reçoit que les lots de ses projets
    (`alert_briefs._of_project` : projet du lot, repli sur tous les pilotes
    quand aucun ne le déclare) — un lot ameesh ne part plus à l'orchestrateur
    nexlink, ni l'inverse."""
    try:
        from . import alert_briefs, mail, registry, sous_utilisation
        cfg = getattr(db, "cfg", None)
        listing = registry.overview(db)
        names = sous_utilisation.alert_orchestrators(
            cfg, db, listing,
            declared=sous_utilisation.declared_orchestrators())
        by_name = {row["name"]: row for row in listing}
        per_pilot: dict[str, list[dict]] = {}
        for entry in entries:
            for name in alert_briefs._of_project(
                    names, by_name, entry.get("project"), set()):
                per_pilot.setdefault(name, []).append(entry)
    except Exception as exc:  # noqa: BLE001 - un avertissement n'arrête rien
        return "aucun pilote joignable : %s: %s" % (
            type(exc).__name__, str(exc)[:120])
    if not names:
        return "aucun pilote déclaré : avertissement laissé au journal"
    sent, failed = [], []
    for name in sorted(per_pilot):
        mine = per_pilot[name]
        try:
            mail.send(db, work.SYSTEM_SENDER, name, _unopened_body(mine, host),
                      work_item_id=(str(mine[0]["lot"]) if len(mine) == 1 else None))
            sent.append(name)
        except Exception as exc:  # noqa: BLE001 - le journal reste, rien d'autre
            failed.append("%s: %s" % (name, str(exc)[:80]))
    if sent:
        return "avertissement envoyé à %s" % ", ".join(sent)
    return "avertissement non remis (%s)" % "; ".join(failed)


def _unopened_body(entries: list[dict], host: str = "") -> str:
    """Le courrier de `_warn_unopened` pour les lots d'un pilote."""
    shown = entries[:WARN_LIMIT]
    lines = [
        "Relecture après fusion (canon §2) non ouverte : le clone du",
        "rattrapage ne prouve pas la fusion.",
        "",
        "Ces lots fusionnés n'ont pas pu être classés sur un hôte muni d'un",
        "dépôt candidat :",
    ]
    for entry in shown:
        lines.append("- #%s « %s » (fusion %s)" % (
            entry["lot"],
            " ".join(str(entry.get("title") or "").split())[:80],
            (entry.get("sha") or "?")[:12]))
    if len(entries) > len(shown):
        lines.append("… et %d autre(s), au journal des lots."
                     % (len(entries) - len(shown)))
    if host:
        lines += ["", "Constat fait sur l'hôte %s." % host]
    lines += [
        "",
        "Un lot léger doit être relu après fusion ; sans classement, la",
        "relecture n'est jamais ouverte. Reprendre chaque lot avec le clone qui",
        "porte la fusion : le clone fourni relance la classification, la fusion",
        "déjà écrite n'est pas rouverte (la déclaration est idempotente) :",
        "  ameesh work merged <id> --sha <commit de fusion> --repo <clone>",
        "Ou corriger `radar.projects.<projet>.repo` (ou le dossier de l'assigné)",
        "sur l'hôte du rattrapage — un hôte sans clone s'abstient désormais,",
        "sans avertir (#3426).",
    ]
    return "\n".join(lines)


def catch_up(db, *, repo: str = "", target: str = "",
             limit: int = CATCH_UP_LIMIT,
             retry_s: float = ATTEMPT_RETRY_S,
             max_attempts: int = CATCH_UP_MAX_ATTEMPTS,
             host: str = "") -> dict:
    """L539 (#1104) : rattrapage des relectures manquantes après une panne.

    Les relevés ne sélectionnent que des lots **ouverts** : un lot déjà `merged`
    dont l'ouverture de relecture a échoué (panne d'écriture, base indisponible)
    ne serait plus jamais revu — la relecture manquerait définitivement (R1
    #1104 P2-2). Ici les relectures manquantes sont sélectionnées **comme
    telles** (`WorkItems.items_missing_ref`) puis `open_for_merge` (idempotent,
    best effort, jamais d'exception) les ouvre.

    R2 #1104 P2-2 : la borne porte sur les **manquants examinés**, pas sur un
    balayage préalable de lots déjà pourvus — un parent ancien dont
    l'ouverture a échoué n'est plus caché par `limit` parents plus récents
    déjà pourvus. `merged` et `promoted` sont un seul ensemble ordonné par
    récence : un `promoted` manquant n'est pas affamé par des `merged`.

    Borne d'activation (arbitrage pilote du 15/10, P2 de la R2 #1104) : le
    rattrapage ne remonte **jamais** avant `review_catch_up_bound()` — la ligne
    écrite par la migration 0438 à l'activation du lot, commune aux exécuteurs
    et conservée après redémarrage. Un lot déjà fusionné avant cette activation
    ne devient pas une relecture due aujourd'hui ; sans borne lisible, le
    rattrapage est fermé (aucun balayage historique, qui reste une décision du
    pilote). Le contenu d'un candidat non-light **confirmé** est terminal : une
    seule lecture, aucune tentative horaire perpétuelle. Un candidat **douteux**
    (classe non confirmée) est réessayé au plus `max_attempts` fois, espacées de
    `retry_s`, puis marqué terminal à son tour — le journal garde la trace.

    #2585 : un candidat dont la tête est déjà relue par une autre famille
    n'ouvre rien ; il reçoit la marque terminale (`reviewed`) au lieu d'être
    retenté à chaque balayage — la relecture est rendue, pas manquante.

    #3426 (pilote, 24/10/2026) : le rattrapage tourne sur **chaque hôte**, mais
    seul un hôte qui peut nommer un dépôt pour le lot le tente
    (`_host_holds_lot_repo`). Un hôte sans clone s'abstient — aucune tentative,
    aucun avertissement — et laisse l'hôte muni du clone classer. Les hôtes
    munis d'un clone se dédupliquent **par lot** : le verrou de la ligne du lot
    sérialise la marque de tentative, qui n'est écrite qu'une fois par délai
    même si deux hôtes ont sélectionné le lot ; la deuxième tentative, et donc
    le courrier de `_warn_unopened`, n'a qu'un seul auteur. C'est le correctif
    des avertissements « aucun dépôt lisible » partis d'ameesh-agents-1 à
    chaque fusion légère alors qu'ameesh-agents-2 classait ensuite.

    Ne lève jamais ; rend `{host, examined, missing, opened, already, reviewed,
    attempted, terminal, deferred, busy, failed, bound, unopened, warning}`
    (`deferred` : lots sans dépôt candidat sur cet hôte, laissés à un autre ;
    `busy` : lots qu'une autre passe a déjà tentés dans le délai, `attempted` :
    tentatives sans création marquées, `already` : relecture ouverte entre la
    sélection et le geste, `reviewed` : avis déjà rendu sur la tête fusionnée,
    `terminal` : candidats clos par la marque terminale, `unopened` : lots de
    la deuxième tentative restés sans classement — #3384, `warning` : ce qui a
    été fait de l'avertissement aux pilotes)."""
    out = {"host": str(host or getattr(getattr(db, "cfg", None), "host", "") or ""),
           "examined": 0, "missing": 0, "opened": 0,
           "already": 0, "reviewed": 0, "attempted": 0, "terminal": 0,
           "deferred": 0, "busy": 0, "failed": 0, "bound": "",
           "unopened": [], "warning": ""}
    try:
        ops = storage.of(db).work
        bound = ops.review_catch_up_bound()
        if bound is None:  # fail-closed : jamais de balayage historique
            return out
        out["bound"] = bound.isoformat() if hasattr(bound, "isoformat") else str(bound)
        rows = ops.items_missing_ref(states=work.MERGED_STATES,
                                     ref_prefix=PREFIX + "#", limit=int(limit),
                                     attempt_prefix=ATTEMPT_PREFIX,
                                     retry_s=float(retry_s),
                                     terminal_prefix=TERMINAL_PREFIX,
                                     max_attempts=int(max_attempts),
                                     since=bound)
    except Exception:  # noqa: BLE001 - pas de base : rien à rattraper
        return out

    def mark(store, lot, state, detail):
        """Marque la tentative sans création (best effort : le lot reste).

        #3426 : `store` est le magasin de la transaction courante
        (`storage.of(tx).work`), jamais celui de la connexion extérieure —
        sur le pilote psql, les deux sessions sont distinctes et un
        `FOR UPDATE` pris sur l'une ne protège pas l'autre."""
        try:
            store.log_review_attempt(lot, state, "%s%d %s"
                                     % (ATTEMPT_PREFIX, int(lot), detail),
                                     work.SYSTEM_SENDER)
        except Exception:  # noqa: BLE001 - marqueur best effort
            pass

    def terminal(store, lot, state, detail):
        """Marque le candidat terminal (best effort : le journal le garde)."""
        try:
            store.log_review_attempt(lot, state, "%s%d %s"
                                     % (TERMINAL_PREFIX, int(lot), detail),
                                     work.SYSTEM_SENDER)
        except Exception:  # noqa: BLE001 - marqueur best effort
            pass

    slug_cache: dict[str, str | None] = {}

    def slug_of(path: str) -> str | None:
        if path not in slug_cache:
            slug_cache[path] = plan_git._repo_slug(path)
        return slug_cache[path]

    def attempt_marks(store, lot):
        """Le nombre de tentatives et l'instant de la dernière.

        #1448 N2 : compté sur les seules notes de tentative du lot (`_noted`),
        pas sur les 200 derniers événements — un lot bavard ne remet pas son
        compteur à zéro et ne retente pas sans fin. #3426 : l'instant sert au
        délai partagé entre les hôtes (une tentative récente écrite par un
        autre hôte n'est pas doublée), et `store` est celui de la transaction
        qui tient le verrou du lot."""
        token = "%s%d" % (ATTEMPT_PREFIX, int(lot))
        notes = [e for e in _noted(store, lot, ATTEMPT_PREFIX)
                 if str(e.get("note") or "").split(" ")[0] == token]
        last = max((float(e.get("created_ts") or 0.0) for e in notes), default=0.0)
        return len(notes), last

    def close_terminal(lot, state, detail):
        """Pose la marque terminale une seule fois, sous le verrou du lot.

        #3426 : deux hôtes peuvent constater la même classe non-light ou le
        même avis déjà rendu ; un seul laisse la marque. Rend True si la
        marque est là (posée ici ou déjà par un autre), False sinon."""
        try:
            with db.transaction() as tx:
                store = storage.of(tx).work
                if store.lock(lot) is None:
                    return False
                if _noted(store, lot, TERMINAL_PREFIX):
                    return True
                terminal(store, lot, state, detail)
                return True
        except Exception:  # noqa: BLE001 - le journal reste best effort
            return False

    def record_attempt(lot, state, why):
        """Marque la tentative sous le verrou du lot (#3426).

        Rend `(résultat, vu)` : `terminal` (la borne de tentatives est
        atteinte), `attempted` (la tentative est écrite), `already` (un autre
        hôte a ouvert la relecture), `closed` (un autre hôte a posé la marque
        terminale), `busy` (un autre hôte a tenté dans le délai) ou `failed`
        (lecture/écriture impossible)."""
        try:
            with db.transaction() as tx:
                store = storage.of(tx).work
                if store.lock(lot) is None:
                    return "failed", 0
                if _open(store, ref_of(lot)) is not None:
                    return "already", 0
                if _noted(store, lot, TERMINAL_PREFIX):
                    return "closed", 0
                count, last_ts = attempt_marks(store, lot)
                if count and float(retry_s) > 0 \
                        and (time.time() - last_ts) < float(retry_s):
                    return "busy", 0
                seen = count + 1
                if seen >= int(max_attempts):
                    terminal(store, lot, state, "doute après %d tentatives" % seen)
                    return "terminal", seen
                mark(store, lot, state, "sans ouverture: %s" % why)
                return "attempted", seen
        except Exception:  # noqa: BLE001 - un lot en panne n'arrête pas le rattrapage
            return "failed", 0

    for row in rows:
        lot = int(row["id"])
        state = str(row.get("state") or "")
        out["examined"] += 1
        try:
            # la sélection a exclu les pourvus ; le recontrôle couvre une
            # ouverture concurrente entre la sélection et ici
            if _open(ops, ref_of(lot)) is not None:
                continue
            out["missing"] += 1
            # #3426 : sans dépôt candidat sur cet hôte, rien n'est tenté ni
            # dit — l'hôte qui porte le clone classera le lot.
            if not _host_holds_lot_repo(db, row, repo, slug_of=slug_of):
                out["deferred"] += 1
                continue
            sha = merged_sha(db, lot)
            done = open_for_merge(db, lot, merged_sha=sha, repo=repo,
                                  target=target or str(row.get("branch_target") or ""))
            if done.get("created"):
                out["opened"] += 1
                continue
            if done.get("already_reviewed"):
                # #2585 : la relecture est déjà rendue sur la tête fusionnée ;
                # aucune mission n'est due, et le candidat ne doit pas revenir
                # à chaque balayage (marque terminale, pas une tentative).
                out["reviewed"] += 1
                out["terminal"] += 1
                reviewed_by = ", ".join(
                    "%s (%s)" % (entry.get("actor"), entry.get("family"))
                    for entry in done.get("reviewed_by") or ()) or "?"
                # `open_after_merge` a déjà posé la marque terminale avec la
                # même raison ; ne pas la doubler (elle est best effort, le
                # repli la pose si elle a manqué).
                close_terminal(lot, state, "avis déjà rendu sur la tête fusionnée : %s"
                               % reviewed_by)
                continue
            if done.get("id") or "déjà" in str(done.get("why") or ""):
                # un autre exécuteur a ouvert la relecture entre la sélection
                # et ici : rien à marquer, le lot est pourvu
                out["already"] += 1
                continue
            merged_class = str(done.get("merged_class") or done.get("class") or "")
            if not done.get("doubt") and merged_class and merged_class != lot_class.LIGHT:
                # classe confirmée non-light : le canon §2 ne demande une
                # relecture après fusion que pour un lot léger — terminal.
                # On lit la classe du diff fusionné, pas celle du lot de
                # relecture : un light critique a bien une relecture due (deux
                # avis) et doit rester réessayable (#2374).
                out["terminal"] += 1
                close_terminal(lot, state, "classe %s confirmée non-light"
                               % merged_class)
                continue
            why = str(done.get("why") or "?")
            result, seen = record_attempt(lot, state, why)
            if result == "attempted":
                out["attempted"] += 1
            elif result == "terminal":
                out["terminal"] += 1
            elif result == "already":
                out["already"] += 1
            elif result == "closed":
                out["terminal"] += 1
            elif result == "busy":
                out["busy"] += 1
            else:
                out["failed"] += 1
            # #3384 : laisser une passe au clone pour devenir lisible.
            # Une classe confirmée au passage suivant sort plus haut, sans
            # courrier. La deuxième tentative avertit une seule fois, même
            # si elle épuise la borne configurée ; #3426 : seule la passe qui
            # a écrit cette deuxième tentative (sous le verrou du lot) avertit.
            if seen == 2 and result in ("attempted", "terminal"):
                out["unopened"].append({
                    "lot": lot, "why": why, "state": state,
                    "title": row.get("title") or "", "sha": sha or "",
                    "project": projects.lot_project(row) or ""})
        except Exception:  # noqa: BLE001 - un lot en panne n'arrête pas le rattrapage
            out["failed"] += 1
            mark(ops, lot, state, "erreur")
    if out["unopened"]:
        out["warning"] = _warn_unopened(db, out["unopened"], host=out["host"])
    return out


def catch_up_eligible(db, lot: int, *, retry_s: float = ATTEMPT_RETRY_S,
                      max_attempts: int = CATCH_UP_MAX_ATTEMPTS,
                      explicit: bool = False) -> bool:
    """L539 (#1104, B1 de la R1) : ce lot déjà fusionné serait-il encore
    sélectionné par le rattrapage borné (`catch_up`) ?

    La même vérité que le balayage (filtre partagé, `missing_ref_eligible`) :
    borne d'activation lisible (migration 0438, fail-closed sinon) et jalon
    `merged` à elle ou après, relecture toujours manquante, aucune marque
    terminale, tentatives sous le plafond et espacées du délai. Sert au chemin
    `already` de `close_merged` : sans cette borne, chaque passage de
    `sync-github` relancerait l'ouverture pour les ~1019 fusions antérieures à
    l'activation (B1). Best effort : toute panne de lecture ferme la porte, le
    lot reste au rattrapage et n'est jamais rouvert par ce chemin.

    `explicit` (#3413) : un geste **explicite** qui fournit le clone manquant
    (`ameesh work merged <id> --sha … --repo …`, le geste que conseille
    l'avertissement) ne subit ni le délai de reprise ni le plafond de
    tentatives du balayage. Ces marques disent qu'une passe automatique a
    échoué, pas qu'un clone désormais lisible serait inutile ; les faire
    respecter rendait l'avertissement inopérant pendant une heure, puis pour
    toujours après la marque terminale. La borne d'activation reste lue dans
    les deux cas : aucun historique antérieur n'est relu rétroactivement."""
    try:
        ops = storage.of(db).work
        bound = ops.review_catch_up_bound()
        if bound is None:      # fail-closed : jamais de balayage historique
            return False
        reader = getattr(ops, "missing_ref_eligible", None)
        if reader is None:     # magasin d'une version antérieure : rien tenté
            return False
        if explicit:
            return bool(reader(int(lot), ref_prefix=PREFIX + "#", since=bound))
        return bool(reader(int(lot), ref_prefix=PREFIX + "#",
                           attempt_prefix=ATTEMPT_PREFIX, retry_s=float(retry_s),
                           terminal_prefix=TERMINAL_PREFIX,
                           max_attempts=int(max_attempts), since=bound))
    except Exception:  # noqa: BLE001 - pas de base : rien à reprendre
        return False
