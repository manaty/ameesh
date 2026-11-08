# SPDX-License-Identifier: AGPL-3.0-only
"""#6317 (étude #6308, A4) : corrélation pure d'un signal à la version active.

Le diagnostic d'un signal n'est pas la corrélation : ce lecteur joint un
signal à la version réellement attestée sur chaque hôte, et rend
`version-active`, `version-anterieure`, `version-differente`, `mixte` ou
`inconnu`, avec ses références. Une proximité temporelle ne prouve rien :
seule une attestation `actif-<sha>.json` (ou sa forme partagée
`staging.attested`, #3981) nomme le contenu actif d'un hôte. Le journal de
staging (`staging-<jour>.jsonl`) sert de contrôle : une attestation
antérieure à un événement postérieur (autre commit, retour, hôte absent) ne
prouve plus l'état courant.

Contrat d'entrée (les champs utiles de l'enveloppe `repair-signal/1` d'A1) :

  {"project": "ameesh", "environment": "staging",
   "observed_version": "<commit observé>"}

Le reçu de U2 (#6315) est donc accepté tel quel ; `revision` est l'alias
court, `environnement`/`hote` les alias français. L'enveloppe A1 ne porte
**pas** d'hôte : le rapport U1 fournit `hosts=` (ou `hote=`/`hotes` dans le
dict). Les autres champs (horodatage, source, identifiant natif) sont
ignorés : la corrélation ne lit que le projet, l'environnement, l'hôte et la
révision. `hotes` (liste) décrit une flotte ; `hote` un seul émetteur.
`active` est la vue `staging_pending.active_hosts()` (dernière attestation
par hôte) et `journal` les lignes de `release_cutoff.read_journal()` ;
`active_view()` et `journal_view()` les rendent sans dupliquer ces lecteurs.
Le cœur `correlate()` est pur : ni base, ni fichier, ni horloge implicite
quand `now` est fourni.

**Ce que la relation prouve, et ce qu'elle ne prouve pas.**
`version-active` : sur chaque hôte de la portée, l'attestation explicite
nomme la révision observée par le signal ; l'hôte exécute le contenu qui a
émis le signal. `version-anterieure` : la révision observée est un ancêtre
**strict** de la version attestée, selon la relation d'ascendance injectée
(`is_ancestor`) ; le signal précède la version active. `version-differente` :
les deux SHA ne sont ni égaux ni ancêtres, ou aucune relation d'ascendance
n'est fournie ; l'étiquette couvre le rollback et la divergence sans
affirmer de direction. `mixte` : les hôtes connus se partagent entre plusieurs
de ces relations. `inconnu` couvre l'absence de preuve : attestation absente
ou périmée, hôte reporté absent, journal postérieur à l'attestation, projet
ou environnement distinct, hôte ou révision illisible.

Aucune de ces relations ne dit qu'une version **corrige** le symptôme :
déduire la causalité reste du diagnostic, et une version absente/inconnue
n'autorise ni fermeture ni relance de demande. Juger qu'une version de
correction est active (son SHA contenu dans la version attestée) appartient à
U16 (A10), qui prend ce SHA en entrée et peut réutiliser la même relation
d'ascendance.

Le lecteur est borné : `MAX_HOTES` hôtes et des références tronquées. Il ne
lit aucun secret, aucun contenu brut de signal, et ne journalise rien.
"""
from __future__ import annotations

import datetime as _dt
import math
import re
import time
from collections.abc import Mapping, Sequence

#: schéma du rapport rendu, stable pour le lecteur U1 (#6313)
SCHEMA = "ameesh-repair-correlation/1"
#: seul projet corrélé : les attestations de staging ne portent aucun projet
#: (`activation.py`), et ce lecteur est celui d'ameesh.
PROJET = "ameesh"
#: seul environnement corrélé : les attestations sont celles du staging
#: d'ameesh (canon §14), sans champ projet ni environnement.
ENVIRONNEMENT = "staging"
#: la révision observée est la version active de l'hôte
VERSION_ACTIVE = "version-active"
#: la révision observée est un ancêtre strict de la version active
VERSION_ANTERIEURE = "version-anterieure"
#: ni égale ni ancêtre, ou relation d'ascendance non fournie : sans direction
VERSION_DIFFERENTE = "version-differente"
#: les hôtes connus se partagent entre plusieurs relations
MIXTE = "mixte"
#: aucune preuve suffisante pour conclure
INCONNU = "inconnu"
VERDICTS = (VERSION_ACTIVE, VERSION_ANTERIEURE, VERSION_DIFFERENTE, MIXTE,
            INCONNU)
#: les relations concluantes d'un hôte, dans l'ordre du rapport global
RELATIONS = (VERSION_ACTIVE, VERSION_ANTERIEURE, VERSION_DIFFERENTE)
#: fraîcheur par défaut d'une attestation : la fenêtre du rapport U1 est
#: d'une semaine et les coupures sont deux fois par jour (canon §11 bis) ;
#: une preuve plus vieille ne décrit plus la version active. Le lecteur
#: appelant peut la réduire (tests, rapport court).
DUREE_MAX_ATTESTATION_S = 7 * 24 * 3600.0
#: borne d'un rapport : la flotte canon en compte cinq, une alerte peut en
#: citer davantage ; au-delà, la portée est tronquée.
MAX_HOTES = 64
#: longueur maximale d'une référence (chemin d'attestation, ligne de journal)
_REFERENCE_MAX = 240
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


def _texte(value) -> str:
    """Un champ texte borné, ou vide : aucun objet n'est interprété."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:_REFERENCE_MAX]


def _sha(value) -> str:
    """Un commit hexadécimal court ou complet, en minuscules, ou vide."""
    text = _texte(value).lower()
    return text if _SHA.fullmatch(text) else ""


def _instant(value) -> float | None:
    """Un instant epoch ou ISO 8601, en secondes UTC ; None si illisible."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = _texte(value)
    if not text:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # le journal et les attestations écrivent UTC (`...Z`) ; un instant
        # sans fuseau n'est pas une heure locale devinée.
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


def _meme(left: str, right: str) -> bool:
    """Deux commits désignent le même contenu (préfixe, comme les attestations)."""
    if not left or not right:
        return False
    return left.startswith(right) or right.startswith(left)


def _signal(signal) -> dict:
    """Les champs utiles de l'enveloppe, sans interpréter le reste."""
    if not isinstance(signal, Mapping):
        return {"projet": "", "environnement": "", "revision": "", "hotes": []}
    projet = _texte(signal.get("project") or signal.get("projet")
                    or signal.get("app"))
    environnement = _texte(signal.get("environnement")
                           or signal.get("environment")
                           or signal.get("env")).lower()
    revision = _sha(signal.get("observed_version")
                    or signal.get("revision")
                    or signal.get("version")
                    or signal.get("sha_observe")
                    or signal.get("sha"))
    hotes = []
    declared = signal.get("hotes")
    if isinstance(declared, (list, tuple)):
        for value in declared:
            hote = _texte(value)
            if hote and hote not in hotes:
                hotes.append(hote)
    single = _texte(signal.get("hote") or signal.get("host")
                    or signal.get("hote_id"))
    if single and single not in hotes:
        hotes.insert(0, single)
    return {"projet": projet, "environnement": environnement,
            "revision": revision, "hotes": hotes}


def _portee(signal, hosts) -> list[str]:
    """Les hôtes à corréler : argument explicite, puis `hotes`, puis `hote`."""
    if hosts is not None:
        values = hosts
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple)):
            return []
    else:
        values = signal["hotes"]
    out = []
    for value in values:
        hote = _texte(value)
        if hote and hote not in out:
            out.append(hote)
    return out[:MAX_HOTES]


def _journal_par_hote(journal) -> dict[str, dict]:
    """La dernière ligne valide de chaque hôte, pour le contrôle de fraîcheur.

    Une ligne sans instant ou sans hôte est ignorée : elle ne prouve rien.
    À instant égal, la dernière du fichier l'emporte (ordre append-only).
    Seule la dernière ligne est retenue : une séquence postérieure non
    terminée peut donc passer si sa dernière ligne nomme le commit attesté
    (rejeu, #6289) ; un passage intermédiaire non terminé n'est pas vu.
    """
    if not isinstance(journal, Sequence) or isinstance(journal, (str, bytes)):
        return {}
    out: dict[str, dict] = {}
    for row in journal:
        if not isinstance(row, Mapping):
            continue
        hote = _texte(row.get("hote") or row.get("host"))
        stamp = _instant(row.get("ts") or row.get("at"))
        if not hote or stamp is None:
            continue
        previous = out.get(hote)
        if previous is not None and stamp < previous["at"]:
            continue
        out[hote] = {
            "at": stamp,
            "sha": _sha(row.get("sha")),
            "precedent": _sha(row.get("precedent")),
            "etape": _texte(row.get("etape") or row.get("step")).lower(),
            "verdict": _texte(row.get("verdict")).lower(),
        }
    return out


def _contradiction(ligne: dict | None, attestation_at: float,
                   actif: str) -> str:
    """Pourquoi une ligne postérieure invalide l'attestation, ou vide.

    L'attestation nomme le contenu actif au moment où elle a été écrite. Une
    ligne de journal plus récente décrit un passage postérieur : si elle a
    changé le contenu (autre commit, retour, hôte absent) ou si elle a échoué
    sans prouver que le contenu est resté, l'attestation ne prouve plus l'état
    courant. Une ligne postérieure qui vise le **même** commit et réussit ne
    la contredit pas.
    """
    if ligne is None or ligne["at"] <= attestation_at:
        return ""
    etape, verdict = ligne["etape"], ligne["verdict"]
    if etape == "absent":
        return "hôte déclaré absent après l'attestation"
    if etape == "retour" and verdict == "ok":
        precedent = ligne["precedent"]
        if precedent and _meme(precedent, actif):
            return ""
        return "retour vers %s après l'attestation" % (precedent or "un commit inconnu")
    if verdict != "ok":
        # un passage en échec ne prouve pas un nouveau contenu : si le journal
        # nomme le contenu précédent et que c'est l'attesté, l'état tient.
        precedent = ligne["precedent"]
        if precedent and _meme(precedent, actif):
            return ""
        return "dernier passage en échec (%s/%s) après l'attestation" % (
            etape or "?", verdict or "?")
    cible = ligne["sha"]
    if cible and not _meme(cible, actif):
        return "journal postérieur sur %s" % cible
    return ""


def _rapport_hote(hote: str, active, journal: Mapping[str, dict], *,
                  revision: str, now: float, max_age_s: float,
                  is_ancestor) -> dict:
    """L'état d'un hôte : relation, preuve active et référence bornée."""
    rapport = {"hote": hote, "verdict": INCONNU, "actif": None,
               "attestation": "", "at": None, "raison": "", "journal": None}
    evidence = active.get(hote) if isinstance(active, Mapping) else None
    if not isinstance(evidence, Mapping):
        rapport["raison"] = "attestation absente"
        return rapport
    actif = _sha(evidence.get("sha") or evidence.get("actif"))
    stamp = _instant(evidence.get("at") or evidence.get("date"))
    rapport["actif"] = actif or None
    rapport["attestation"] = _texte(evidence.get("attestation"))
    rapport["at"] = stamp
    if evidence.get("absent"):
        raison = _texte(evidence.get("raison")) or "hôte reporté absent"
        rapport["raison"] = "hôte reporté absent : " + raison
        return rapport
    if not actif:
        rapport["raison"] = "attestation sans commit actif"
        return rapport
    if stamp is None:
        rapport["raison"] = "attestation sans date"
        return rapport
    if now - stamp > max_age_s:
        rapport["raison"] = "attestation périmée (%s)" % _dt.datetime.fromtimestamp(
            stamp, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return rapport
    ligne = journal.get(hote)
    if ligne is not None:
        rapport["journal"] = {
            "at": ligne["at"], "sha": ligne["sha"] or None,
            "etape": ligne["etape"], "verdict": ligne["verdict"]}
        raison = _contradiction(ligne, stamp, actif)
        if raison:
            rapport["raison"] = raison
            return rapport
    if _meme(actif, revision):
        rapport["verdict"] = VERSION_ACTIVE
        return rapport
    if is_ancestor is not None:
        try:
            if is_ancestor(revision, actif) is True:
                rapport["verdict"] = VERSION_ANTERIEURE
                return rapport
        except Exception:  # noqa: BLE001 — un prédicat défaillant ne prouve rien
            pass
    rapport["verdict"] = VERSION_DIFFERENTE
    rapport["raison"] = "relation d'ascendance absente ou non prouvée"
    return rapport


def _refus(raison: str, *, projet: str = "", environnement: str = "",
           revision: str = "") -> dict:
    """Un rapport `inconnu` motivé, de forme stable pour le lecteur U1."""
    return {"schema": SCHEMA, "verdict": INCONNU, "projet": projet or PROJET,
            "environnement": environnement, "revision": revision,
            "hotes": [], "raison": raison}


def correlate(signal, active, *, journal=(), hosts=None, now=None,
              max_age_s: float = DUREE_MAX_ATTESTATION_S,
              is_ancestor=None) -> dict:
    """Joint un signal à la version active par hôte (A4, #6317).

    `signal` : enveloppe `repair-signal/1` (environnement, hôte(s), révision
    observée). `active` : la vue `staging_pending.active_hosts()`, la dernière
    attestation explicite par hôte. `journal` : les lignes
    `release_cutoff.read_journal()`, pour refuser une attestation antérieure
    à un passage postérieur. `now` : l'instant du rapport (secondes epoch ;
    défaut : l'horloge courante, à fournir dans un test).

    `is_ancestor(observe, actif)` : prédicat pur injecté par l'appelant, vrai
    si `observe` est un **ancêtre strict** de `actif` (l'égalité est vérifiée
    avant). Sans lui, ou s'il ne prouve pas la relation, une paire inégale est
    `version-differente` : le lecteur n'affirme aucune direction et couvre
    ainsi le rollback et la divergence. U16 (A10) peut réutiliser le même
    prédicat pour son propre jugement d'activation.

    Rend un rapport `SCHEMA` : verdict global, un rapport par hôte avec sa
    référence d'attestation, et la liste des hôtes connus/inconnus. Une portée
    vide, un environnement non-staging, une révision ou une attestation
    illisible rendent `inconnu` : ce lecteur ne complète jamais une preuve
    manquante par une proximité temporelle ou par défaut.
    """
    view = _signal(signal)
    revision = view["revision"]
    projet = view["projet"] or PROJET
    environnement = view["environnement"]
    if not environnement:
        return _refus("environnement absent", projet=projet, revision=revision)
    if environnement != ENVIRONNEMENT:
        return _refus("environnement distinct : %s" % environnement,
                      projet=projet, environnement=environnement,
                      revision=revision)
    if projet != PROJET:
        return _refus("projet distinct : %s" % projet, projet=projet,
                      environnement=environnement, revision=revision)
    if not revision:
        return _refus("révision observée absente ou illisible", projet=projet,
                      environnement=environnement)
    scope = _portee(view, hosts)
    if not scope:
        return _refus("aucun hôte à corréler", environnement=environnement,
                      revision=revision)
    instant = time.time() if now is None else float(now)
    try:
        max_age = float(max_age_s)
    except (TypeError, ValueError):
        max_age = DUREE_MAX_ATTESTATION_S
    rows = _journal_par_hote(journal)
    rapports = [_rapport_hote(hote, active, rows, revision=revision,
                              now=instant, max_age_s=max_age,
                              is_ancestor=is_ancestor)
                for hote in sorted(scope)]
    connus = [r for r in rapports if r["verdict"] in RELATIONS]
    inconnus = [r for r in rapports if r["verdict"] == INCONNU]
    if not connus:
        verdict = INCONNU
        raisons = sorted({r["raison"] for r in inconnus})
        raison = "aucune attestation utilisable : " + "; ".join(raisons)
    elif inconnus:
        verdict = INCONNU
        raison = ("%d hôte(s) sans preuve (%s) ; %d relation(s) connue(s)"
                  % (len(inconnus), ", ".join(r["hote"] for r in inconnus),
                     len(connus)))
    else:
        comptes = {relation: len([r for r in connus
                                  if r["verdict"] == relation])
                   for relation in RELATIONS}
        presentes = [relation for relation in RELATIONS if comptes[relation]]
        if len(presentes) == 1:
            verdict = presentes[0]
            raison = {
                VERSION_ACTIVE: "révision observée active sur %d hôte(s)",
                VERSION_ANTERIEURE: "révision observée antérieure à la "
                                    "version active sur %d hôte(s)",
                VERSION_DIFFERENTE: "révision observée ni active ni "
                                    "antérieure sur %d hôte(s)",
            }[verdict] % comptes[verdict]
        else:
            verdict = MIXTE
            raison = "flotte mixte : " + ", ".join(
                "%d %s" % (comptes[relation], relation)
                for relation in presentes)
    return {"schema": SCHEMA, "verdict": verdict, "projet": projet,
            "environnement": environnement, "revision": revision,
            "hotes": rapports,
            "connus": sorted(r["hote"] for r in connus),
            "inconnus": sorted(r["hote"] for r in inconnus),
            "raison": raison}


def active_view(db=None) -> dict:
    """La dernière attestation par hôte, au format de `staging_pending`.

    Le lecteur existant (#3981) fusionne les fichiers `actif-<sha>.json` et les
    événements `staging.attested` ; ce module ne duplique ni leur validation
    ni leur précédence. `db=None` garde la lecture locale seule.
    """
    from . import staging_pending
    return staging_pending.active_hosts(db=db)


def journal_view(since: str = "") -> list[dict]:
    """Les lignes du journal de staging du jour (`release_cutoff`).

    Le journal est quotidien et append-only ; `since` borne les instants en
    ISO UTC, comme le lecteur existant. Le rapport U1 peut passer directement
    ces lignes à :func:`correlate`.
    """
    from . import release_cutoff
    return release_cutoff.read_journal(since)
