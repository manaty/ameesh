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
`git-merge` et de `ameesh work merged`. La classe est alors relue sur le diff
que la fusion a fait entrer (`class_at_merge`, `lot_measures.diff_range`) —
jamais `cible..gel`, vide dès que le gel est un ancêtre de la cible — avec les
indices de l'appelant ; seule `light` ouvre, un doute n'ouvre rien, et
l'idempotence reste celle de `open_after_merge`.

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

#2374 : une fusion classée `light` dont le diff touche pourtant la porte de
fusion (`merge_queue.CRITICAL_SCOPE`) ouvre une relecture **sensible** à deux
avis de familles distinctes. L'ouverture écrit en outre, au journal du lot
source, une marque structurée (`requirement_mark`) que `work.close --delivered`
relit (`guard_delivery`) : la clôture est refusée tant que les avis dus ne sont
pas rendus sur le sha fusionné. Le nombre d'avis n'est donc plus seulement
écrit dans le corps.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Optional

from . import lot_class, storage, work

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
#: léger) ou un doute épuisé après `CATCH_UP_MAX_ATTEMPTS` tentatives. Le
#: premier mot de la note est `TERMINAL_PREFIX + <id>` ; la sélection l'exclut
#: toujours — la marque reste au journal, visible, mais aucune tentative
#: horaire perpétuelle (arbitrage pilote du 15/10 sur le P2 de la R2 #1104).
TERMINAL_PREFIX = "review-after-merge:closed:#"
#: le nombre de tentatives bornées d'un candidat **douteux** (classe non
#: confirmée : lecture impossible, dépôt absent) avant sa marque terminale.
#: Un non-light confirmé, lui, est terminal dès la première lecture.
CATCH_UP_MAX_ATTEMPTS = 3
#: l'échéance du canon §2 : le jour même de la fusion, heure de Paris
TZ = "Europe/Paris"
HOUR = "23:59"
#: les familles de modèle connues du mesh
FAMILIES = ("claude", "codex", "deepseek")
#: la famille de relecture opposée à celle de l'auteur (règle du pilote,
#: 2026-10-13) : codex si l'auteur est Claude ou DeepSeek, sinon Claude.
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
            "(`ameesh work backlog add --title … --value … --score …`), regroupé avec les "
            "autres défauts mineurs du même chantier (canon §2, owner du 15/10/2026 ; sur "
            "ameesh, un P2 de la classe sensible bloque encore)")


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


def family_of(harness: Optional[str]) -> str:
    """La famille de modèle d'un harnais (`claude`, `codex`, `deepseek`), ou "".

    Le harnais peut être un nom précis (`claude-opus-5-5`, `deepseek-flash`) :
    la famille est celle du mot qu'il porte. Un harnais inconnu ne rend pas une
    famille par défaut : l'indépendance d'un avis ne se suppose pas (#1374)."""
    text = (harness or "").strip().lower()
    for family in FAMILIES:
        if family in text:
            return family
    return ""


def reviewer_family(harness: Optional[str]) -> str:
    """La famille de relecture opposée au harnais de l'auteur."""
    return OPPOSITE.get(family_of(harness), UNKNOWN_FAMILY)


def review_families(harness: Optional[str]) -> tuple:
    """Les deux familles d'une relecture sensible, hors de celle de l'auteur.

    La première est la famille opposée à l'auteur (`reviewer_family`) ; la
    seconde est la première des familles connues qui n'est ni celle de
    l'auteur ni la première. Un harnais inconnu laisse les deux familles
    ouvertes, sans supposer l'indépendance (#2374)."""
    author = family_of(harness)
    first = reviewer_family(harness)
    if first == UNKNOWN_FAMILY:
        return ("codex", "claude")
    for family in FAMILIES:
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


def required_family(db, item: dict) -> Optional[str]:
    """La famille de harnais qu'exige la prise d'un lot de relecture (#2377).

    `None` quand ce n'est pas un lot de relecture (`issue_ref` hors du préfixe
    `review-after-merge:`) : aucune garde. Une chaîne vide quand la famille de
    l'auteur au gel est inconnue, ou quand sa trace est illisible : le lot
    n'est alors **pris par personne** au repos (doute -> non pris, canon §8) et
    attend une attribution explicite. Sinon la famille opposée à celle de
    l'auteur au gel (`reviewer_family`), celle que le canon §2 demande au
    relecteur.

    La lecture réutilise `_author_at_freeze` : c'est la même preuve que celle
    qui a écrit le corps du lot (gel, acteur, tours tracés), jamais le registre
    courant — il ne dit rien du harnais qui a produit le travail (#1053)."""
    ref = str((item or {}).get("issue_ref") or "").strip().lower()
    if not ref.startswith(PREFIX):
        return None
    try:
        lot = int(ref[len(PREFIX):].strip().lstrip("#"))
        source = work.get(db, lot)
        if source is None:
            return ""
        family = reviewer_family(_author_at_freeze(db, lot, source).get("harness"))
    except Exception:  # noqa: BLE001 - lecture impossible : famille inconnue
        return ""
    return "" if family == UNKNOWN_FAMILY else family


def may_take(db, item: dict, agent: dict) -> bool:
    """Un agent au repos peut-il prendre ce lot ? (#2377)

    Hors lot de relecture, oui, sans autre condition. Pour un lot de
    relecture, seule la famille opposée à celle de l'auteur au gel le prend ;
    la session vivante du preneur fait foi (`registry.live_harness`, #1037),
    et un harnais hors des familles connues (`family_of`) ne compte pas.
    Famille de l'auteur inconnue ou famille du preneur inconnue : non — la
    prise ne devine jamais une indépendance d'avis (#1374)."""
    family = required_family(db, item)
    if family is None:
        return True
    from . import registry
    mine = family_of(registry.live_harness(agent or {}))
    return bool(family) and family == mine


def title_of(item: dict, lot: int) -> str:
    """Le titre du lot de relecture."""
    return "REVUE #%d : relecture après fusion — %s" % (
        int(lot), " ".join(str(item.get("title") or "").split())[:90])


def body_of(item: dict, lot: int, *, klass: dict, merged_sha: str = "", pr=None,
            target: str = "", author: Optional[dict] = None, family: str = "") -> str:
    """Le corps du lot de relecture : quoi relire, par qui, avant quand."""
    author = author or {}
    who = author.get("name") or "inconnu"
    # #2384 : la famille se lit sur le harnais du gel, jamais sur le texte
    # affiché (le nom d'un tiers qui a reposé le gel peut contenir un mot de
    # famille) ; le corps doit annoncer exactement ce que la garde relira.
    author_family = family_of(author.get("harness"))
    harness = author.get("harness") or UNTRACED
    if author.get("refroze"):
        # #1859 : un tiers a reposé le gel ; il n'est pas l'auteur
        harness += "; gel reposé par %s, qui n'a pas tenu le lot" % author["refroze"]
    where = "PR #%s" % pr if pr else (merged_sha[:12] if merged_sha else "?")
    why = " ".join(str((klass or {}).get("why") or "").split())
    critical = bool((klass or {}).get("critical"))
    families = review_families(author.get("harness")) if critical else ()
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
            "%s (règle du pilote du 13/10)." % " et ".join(families),
            "- Avis requis : **%d** verdicts indépendants sur la fusion ; le lot n'est "
            "clos qu'après le second." % CRITICAL_REVIEWS,
        ])
    else:
        lines.append(
            "- Relecteur : une **autre famille de modèle** — %s (règle du pilote du 13/10)."
            % (family or reviewer_family(harness)))
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


def open_after_merge(db, *, lot: int, klass: dict, item: Optional[dict] = None,
                     merged_sha: str = "", pr=None, target: str = "",
                     actor: str = work.SYSTEM_SENDER) -> dict:
    """Ouvre le lot de relecture du lot léger `lot`, s'il n'existe pas déjà.

    Rend `{lot, id, created, class, merged_class, critical, reviews, why}` —
    ne lève jamais. `klass` est la classe lue sur le diff fusionné ; seule
    `light` ouvre un lot de relecture, un doute ou une classe supérieure
    n'ouvre rien (le canon §2 ne demande une relecture après fusion que pour
    les lots légers). Une classe `light` dont le diff touche pourtant un
    chemin critique (porte de fusion, SQL, sécurité, prod : `CRITICAL_SCOPE`)
    ouvre une relecture **sensible**, avec **deux** avis de familles
    distinctes (#2374) : le classement du canon ne peut pas dispenser la
    sécurité d'une seconde lecture quand il a omis le chemin.

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
    reviews = (CRITICAL_REVIEWS if critical
               else LIGHT_REVIEWS if merged_class == lot_class.LIGHT else 0)
    out = {"lot": lot, "id": None, "created": False, "class": review_class,
           "merged_class": merged_class, "critical": critical,
           "reviews": reviews, "why": ""}
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
    family = reviewer_family(author.get("harness"))
    author_family = family_of(author.get("harness"))
    mark = requirement_mark(lot, required=reviews, merged_sha=merged_sha,
                            author=author.get("name") or "",
                            author_family=author_family)
    try:
        created = work.add(
            db, title=title_of(item, lot), type="evolution", source=ref,
            app=str(item.get("app") or ""),
            body=body_of(item, lot, klass=klass, merged_sha=merged_sha, pr=pr,
                         target=target, author=author, family=family),
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
        family = merge_queue.actor_family(db, actor)
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

    Le diff lu est celui que la fusion a fait entrer : `sha` (le commit de la
    fusion constatée, le gel à défaut) est ramené à son intervalle de
    changement par `lot_measures.diff_range` — commit de fusion, fusion qui a
    introduit le gel, ou gel hors cible. Lire `cible..gel` serait un piège :
    une fois la fusion faite, le gel est un ancêtre de la cible, ce diff est
    vide et toute classe y serait « light ».

    Un merge sans delta (arbre identique à son premier parent, le contenu du
    lot ayant été porté par un autre commit de la cible) a lui aussi un diff
    vide : la classe est alors relue sur le **delta propre au lot**
    (`lot_measures.lot_diff_range`, #2374 / #2340), jamais conclue « light »
    sur le vide.

    `repo` est le dépôt où la fusion a été constatée (le relevé des branches
    le connaît), sinon le dossier de travail de l'assigné au registre, sinon
    le dépôt du projet (`lot_class.repository`) — toujours pour y lire le diff
    de la fusion, jamais `cible..gel`. `target` la cible déjà résolue.

    Sans intervalle **prouvé** (diff illisible, commit de fusion absent du
    dépôt, ni gel ni commit), la classe n'est pas demandée à `of_lot` : son
    repli gel/cible rendrait `cible..gel`, vide dès que le gel est un ancêtre
    de la cible — « light » sur un lot SQL fusionné (R1 #1104 P2-1). On rend un
    doute conservateur, et d'une classe inconnue on n'ouvre pas de relecture."""
    from . import lot_measures, plan_git
    if item is None:
        item = work.get(db, lot)
    if item is None:
        return {"class": "", "why": "lot #%d introuvable" % int(lot), "doubt": True}
    where = repo or _repo_of_agent(db, str(item.get("assignee") or ""))
    if not where:
        where = lot_class.repository(getattr(db, "cfg", None), item) or ""
    base = head = ""
    proven = False
    why = "aucun intervalle de fusion lisible"
    try:
        frozen = ""
        if where:
            freeze = plan_git.latest_freeze(work.milestones(db, int(lot)))
            frozen = str((freeze or {}).get("sha") or "").strip()
        tips = [t for t in ((sha or "").strip(), frozen) if t]
        if not where:
            why = "dépôt inconnu : aucun intervalle de fusion lisible"
        elif not tips:
            why = "ni commit de fusion ni gel : aucun intervalle de fusion lisible"
        # le commit de la fusion constatée d'abord ; à défaut le gel (fusion
        # déclarée sans commit lisible, `ameesh work merged` à la main)
        for tip in tips:
            rng = lot_measures.diff_range(where, tip, target or None)
            if not rng:
                why = "le dépôt %s ne contient pas le commit %s" % (where, tip[:12])
                continue
            paths = lot_measures.diff_paths(where, *rng)
            if paths is None:
                why = "diff de fusion illisible pour %s" % tip[:12]
                break
            if paths:
                base, head = rng
                proven = True
                break
            # #2374 (2e cas, #2340) : un merge dont l'arbre égale son premier
            # parent a un diff vide — un delta déjà porté par un autre commit
            # de la cible (rebase, adaptation). Classer ce vide dirait « light »
            # sans jamais lire le lot : on relit le delta propre au lot
            # (`merge-base(premier parent, tête du lot)..tête du lot`).
            alt = lot_measures.lot_diff_range(where, tip, target or None)
            alt_paths = lot_measures.diff_paths(where, *alt) if alt else None
            if alt_paths:
                base, head = alt
                proven = True
                why = "diff de fusion vide : classe lue sur le diff du lot"
                break
            why = ("diff de fusion vide et aucun delta du lot lisible pour %s"
                   % tip[:12])
            break
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
        return lot_class.of_lot(db, int(lot), cfg=getattr(db, "cfg", None), item=item,
                                repo=where or None, base=base or None, head=head or None)
    except Exception as exc:  # noqa: BLE001 - un doute, jamais une exception
        return {"class": "", "why": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
                "doubt": True}


def open_for_merge(db, lot: int, *, merged_sha: str = "", pr=None,
                   repo: Optional[str] = None, target: str = "",
                   actor: str = work.SYSTEM_SENDER) -> dict:
    """L539 : le lot de relecture d'une fusion enregistrée **hors file**.

    Toute voie qui enregistre une fusion (`work.close_merged`) passe ici : le
    relevé des branches (L118), `sync-merges`, `sync-github`, la porte
    `git-merge` et `ameesh work merged`. La classe est relue sur le diff de la
    fusion constatée (`class_at_merge`) avec les indices de l'appelant ; seule
    `light` ouvre, un doute n'ouvre rien, et un `light` qui touche un chemin
    critique ouvre une relecture sensible à deux avis (#2374). Idempotent : un
    lot de relecture déjà écrit, **quel que soit son état**, est rendu sans
    relire la classe. `merged_class` garde la classe du diff fusionné (pour le
    rattrapage), `class` celle du lot de relecture ouvert. Ne lève jamais —
    la fusion est un fait déjà enregistré."""
    lot = int(lot)
    out = {"lot": lot, "id": None, "created": False, "class": "",
           "merged_class": "", "critical": False, "reviews": 0, "why": ""}
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
        opened = open_after_merge(
            db, lot=lot, klass=klass, item=item, merged_sha=merged_sha, pr=pr,
            target=where_target, actor=actor)
        out.update({"id": opened.get("id"), "created": bool(opened.get("created")),
                    "class": opened.get("class") or out["class"],
                    "merged_class": opened.get("merged_class") or out["merged_class"],
                    "critical": bool(opened.get("critical")),
                    "reviews": int(opened.get("reviews") or 0),
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


def catch_up(db, *, repo: str = "", target: str = "",
             limit: int = CATCH_UP_LIMIT,
             retry_s: float = ATTEMPT_RETRY_S,
             max_attempts: int = CATCH_UP_MAX_ATTEMPTS) -> dict:
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

    Ne lève jamais ; rend `{examined, missing, opened, already, attempted,
    terminal, failed, bound}` (`attempted` : tentatives sans création marquées,
    `already` : relecture ouverte entre la sélection et le geste, `terminal` :
    candidats clos par la marque terminale)."""
    out = {"examined": 0, "missing": 0, "opened": 0, "already": 0,
           "attempted": 0, "terminal": 0, "failed": 0, "bound": ""}
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

    def mark(lot, state, detail):
        """Marque la tentative sans création (best effort : le lot reste)."""
        try:
            ops.log_review_attempt(lot, state, "%s%d %s"
                                   % (ATTEMPT_PREFIX, int(lot), detail),
                                   work.SYSTEM_SENDER)
        except Exception:  # noqa: BLE001 - marqueur best effort
            pass

    def terminal(lot, state, detail):
        """Marque le candidat terminal (best effort : le journal le garde)."""
        try:
            ops.log_review_attempt(lot, state, "%s%d %s"
                                   % (TERMINAL_PREFIX, int(lot), detail),
                                   work.SYSTEM_SENDER)
        except Exception:  # noqa: BLE001 - marqueur best effort
            pass

    def attempts(lot):
        """Le nombre de tentatives déjà marquées au journal du lot.

        #1448 N2 : compté sur les seules notes de tentative du lot
        (`_noted`), pas sur les 200 derniers événements — un lot bavard ne
        remet pas son compteur à zéro et ne retente pas sans fin."""
        token = "%s%d" % (ATTEMPT_PREFIX, int(lot))
        return sum(1 for e in _noted(ops, lot, ATTEMPT_PREFIX)
                   if str(e.get("note") or "").split(" ")[0] == token)

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
            done = open_for_merge(db, lot, merged_sha=merged_sha(db, lot), repo=repo,
                                  target=target or str(row.get("branch_target") or ""))
            if done.get("created"):
                out["opened"] += 1
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
                terminal(lot, state, "classe %s confirmée non-light" % merged_class)
                continue
            seen = attempts(lot) + 1
            if seen >= int(max_attempts):
                out["terminal"] += 1
                terminal(lot, state, "doute après %d tentatives" % seen)
            else:
                out["attempted"] += 1
                mark(lot, state, "sans ouverture: %s" % (done.get("why") or "?"))
        except Exception:  # noqa: BLE001 - un lot en panne n'arrête pas le rattrapage
            out["failed"] += 1
            mark(lot, state, "erreur")
    return out


def catch_up_eligible(db, lot: int, *, retry_s: float = ATTEMPT_RETRY_S,
                      max_attempts: int = CATCH_UP_MAX_ATTEMPTS) -> bool:
    """L539 (#1104, B1 de la R1) : ce lot déjà fusionné serait-il encore
    sélectionné par le rattrapage borné (`catch_up`) ?

    La même vérité que le balayage (filtre partagé, `missing_ref_eligible`) :
    borne d'activation lisible (migration 0438, fail-closed sinon) et jalon
    `merged` à elle ou après, relecture toujours manquante, aucune marque
    terminale, tentatives sous le plafond et espacées du délai. Sert au chemin
    `already` de `close_merged` : sans cette borne, chaque passage de
    `sync-github` relancerait l'ouverture pour les ~1019 fusions antérieures à
    l'activation (B1). Best effort : toute panne de lecture ferme la porte, le
    lot reste au rattrapage et n'est jamais rouvert par ce chemin."""
    try:
        ops = storage.of(db).work
        bound = ops.review_catch_up_bound()
        if bound is None:      # fail-closed : jamais de balayage historique
            return False
        reader = getattr(ops, "missing_ref_eligible", None)
        if reader is None:     # magasin d'une version antérieure : rien tenté
            return False
        return bool(reader(int(lot), ref_prefix=PREFIX + "#",
                           attempt_prefix=ATTEMPT_PREFIX, retry_s=float(retry_s),
                           terminal_prefix=TERMINAL_PREFIX,
                           max_attempts=int(max_attempts), since=bound))
    except Exception:  # noqa: BLE001 - pas de base : rien à reprendre
        return False
