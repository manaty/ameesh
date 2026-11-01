# SPDX-License-Identifier: AGPL-3.0-only
"""Compteurs de contexte normalisés — contrat ``ameesh-context/1`` (CTX-02a).

Ce module porte les **conventions de comptage par harnais** du contrat
``ameesh-context/1`` (docs/design/ctx-01-compteurs-contexte.md, §2.2 et §5.3)
et les accumule pour un tour :

* ``C_last`` : entrée du dernier appel du **fil principal**, cache lu et écrit
  compris quand le harnais les sépare (Claude) ; l'entrée seule quand le cache
  y est déjà inclus (Codex) ;
* ``O_last`` : sortie du dernier appel, distincte de ``O`` (sortie du tour) ;
* ``R_turn`` : entrées cumulées du tour, cache **lu** compris, cache **écrit**
  exclu (c'est une écriture, pas une relecture) ;
* ``R_session`` : cumul des ``R_turn`` de la session, à partir d'un point de
  départ fourni par l'appelant (le grand livre) ;
* le cache lu et écrit, les sous-agents mesurés à part, la déduplication des
  blocs d'un même appel et des mises à jour cumulatives.

**Observation seule.** Rien ici ne décide d'une rotation, ne change le seuil
legacy de 150 000, ni n'écrit au grand livre ou en SQL. Les fonctions
historiques d'``adapters`` (``usage_tokens``, ``reread_tokens``,
``context_tokens``) gardent exactement leurs valeurs ; elles prennent
seulement leur lecture des champs dans :func:`usage_fields`, pour qu'une seule
table de clés serve aux deux chemins. Ce module ne lit ni le prompt, ni le
résultat d'un outil, ni un secret : seulement les dictionnaires d'usage.

Absence et qualité (contrat §2.4) : une valeur absente vaut ``None`` avec une
raison, jamais ``0``. Un ``0`` réel reste une mesure. Les qualités sont
``measured``, ``derived``, ``fallback``, ``partial`` et ``unknown`` ; le nom du
modèle peut en plus être ``requested`` quand seul le lancement le nomme, jamais
une mesure runtime (§3.1).

Conventions retenues (§2.2) :

===============  =================  ================  ==========================
Harnais          entrée             cache lu          cache écrit
===============  =================  ================  ==========================
Claude           ``input_tokens``   ``cache_read_…``  ``cache_creation_…``
Codex            ``input_tokens``   ``cached_input…``  —
                 (cache **inclus**) (sous-ensemble)
DeepSeek (dsh)   ``inputTokens``    ``cacheReadTokens`` —
ACP              ``used`` puis      ``cacheReadTokens`` —
                 ``inputTokens``    (0 dans le pont)
===============  =================  ================  ==========================

Le pont ACP publie ``output_tokens: 0`` faute de champ de sortie : pour ACP,
un zéro de sortie n'est **pas** une mesure d'``O_last`` (contrat §2.2).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

SCHEMA = "ameesh-context/1"

#: qualités du contrat §2.4
MEASURED = "measured"
DERIVED = "derived"
FALLBACK = "fallback"
PARTIAL = "partial"
UNKNOWN = "unknown"
QUALITIES = (MEASURED, DERIVED, FALLBACK, PARTIAL, UNKNOWN)
#: le nom **demandé** au lancement n'est pas une mesure runtime (§3.1) ; cette
#: qualité ne s'applique qu'au nom du modèle, jamais à un compteur.
REQUESTED = "requested"
#: qualités admises pour le modèle effectif : les mesures, plus ``requested``
MODEL_QUALITIES = QUALITIES + (REQUESTED,)

#: provenances d'une mesure
SOURCE_STREAM = "stream"
SOURCE_JOURNAL = "journal"
SOURCE_TOTAL = "total"
SOURCE_LEDGER = "ledger"

#: raisons publiées avec une valeur absente ou partielle. Elles sont stables :
#: un consommateur peut les compter, pas les traduire en décision.
REASON_NO_CALL = "no_call"
REASON_NO_MEASURE = "no_measure"
REASON_CONTEXT_UNKNOWN = "context_unknown"
REASON_INPUT_UNKNOWN = "input_unknown"
REASON_OUTPUT_UNKNOWN = "output_unknown"
REASON_CACHE_READ_UNKNOWN = "cache_read_unknown"
REASON_CACHE_WRITE_UNKNOWN = "cache_write_unknown"
REASON_NO_CACHE_WRITE = "harness_without_cache_write"
REASON_UNQUALIFIED_CACHE_WRITE = "unqualified_cache_write"
REASON_CUMULATIVE_BASELINE_UNKNOWN = "cumulative_baseline_unknown"
REASON_CUMULATIVE_DECREASED = "cumulative_decreased"
REASON_TOTAL_INPUT_UNKNOWN = "total_input_unknown"
REASON_TOTAL_OUTPUT_UNKNOWN = "total_output_unknown"
REASON_TOTAL_CACHE_READ_UNKNOWN = "total_cache_read_unknown"
REASON_NO_TOTAL = "no_total_measure"
REASON_TURN_INTERRUPTED = "turn_interrupted"
REASON_TURN_NOT_MEASURED = "turn_not_measured"
REASON_SESSION_BASELINE_UNKNOWN = "session_baseline_unknown"
REASON_NO_SESSION = "no_session"
REASON_SESSION_MISMATCH = "session_mismatch"
REASON_CALL_ID_UNKNOWN = "call_id_unknown"
REASON_CALL_BOUND_REACHED = "call_bound_reached"
#: #4870 : une collecte de tour perdue (worker mort, réparation par un autre)
#: laisse un trou dans `R_session` ; la base reste inconnue, jamais un total
#: supposé reconstruit.
REASON_COLLECTION_LOST = "collection_lost"

#: #5148 : relevé du journal de session Codex pour ``C_last``/``O_last``. Ces
#: motifs disent **pourquoi** le journal n'a rien donné ; une valeur absente
#: reste ``null`` avec ``context_unknown`` (ou ``output_unknown``), jamais 0.
REASON_JOURNAL_MISSING = "journal_missing"
REASON_JOURNAL_UNREADABLE = "journal_unreadable"
REASON_TURN_NOT_IN_JOURNAL = "turn_not_in_journal"
REASON_LAST_USAGE_MISSING = "last_token_usage_missing"

#: une valeur de flux est un entier positif ; au-delà, un flux pathologique ne
#: fait pas grossir la mémoire du tour (contrat §2.2, budget de performance).
MAX_CALLS = 10_000

#: noms de harnais reconnus, y compris les clés de format de flux
_HARNESS_ALIASES = {
    "claude": "claude",
    "claude-stream-json": "claude",
    "codex": "codex",
    "codex-json": "codex",
    "deepseek": "deepseek",
    "dsh": "deepseek",
    "dsh-json": "deepseek",
    "acp": "acp",
    "acp-json": "acp",
}


class ContextMetricsError(ValueError):
    """Un appel de l'API de normalisation est invalide."""


def _token(usage: Mapping | None, *keys: str) -> int | None:
    """Première clé numérique de ``usage`` parmi ``keys``, bornée à zéro.

    Même lecture que l'historique ``adapters.usage_tokens`` : la première clé
    présente et lisible gagne, une valeur illisible ou booléenne est ignorée.
    ``None`` quand aucune clé ne porte de mesure (jamais ``0`` par défaut).
    """
    if not isinstance(usage, Mapping):
        return None
    for key in keys:
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(int(value), 0)
    return None


@dataclass(frozen=True)
class UsageFields:
    """Les quatre champs d'usage d'un événement, avant convention de harnais."""

    input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "output_tokens": self.output_tokens,
        }


def usage_fields(usage: Mapping | None) -> UsageFields:
    """Lit les champs d'usage bruts, sans les confondre avec zéro.

    C'est la source unique des clés : ``adapters.usage_tokens`` garde son
    contrat historique (absence = 0) en reprenant ces champs, tandis que le
    contrat ``ameesh-context/1`` les publie en ``None`` et raison.
    """
    return UsageFields(
        input_tokens=_token(usage, "input_tokens", "inputTokens"),
        cache_read_tokens=_token(
            usage, "cache_read_input_tokens", "cached_input_tokens", "cacheReadTokens"),
        cache_write_tokens=_token(usage, "cache_creation_input_tokens"),
        output_tokens=_token(usage, "output_tokens", "outputTokens"),
    )


@dataclass(frozen=True)
class Convention:
    """Comment un harnais compte le cache (contrat §2.2)."""

    harness: str
    #: l'entrée inclut déjà le cache lu (Codex) : ne jamais l'additionner
    input_includes_cache: bool = False
    #: le harnais publie un champ d'écriture de cache (Claude)
    publishes_cache_write: bool = False
    #: l'écriture de cache appartient à ``C_last`` (Claude), jamais à ``R_turn``
    cache_write_in_context: bool = False
    #: un zéro de sortie n'est pas une mesure (pont ACP)
    output_zero_unknown: bool = False


CONVENTIONS = {
    "claude": Convention("claude", input_includes_cache=False,
                         publishes_cache_write=True,
                         cache_write_in_context=True),
    "codex": Convention("codex", input_includes_cache=True),
    "deepseek": Convention("deepseek"),
    "acp": Convention("acp", output_zero_unknown=True),
}


def convention(harness: str | None) -> Convention:
    """La convention d'un harnais ; un harnais inconnu suit le cas générique.

    Le cas générique est celui de DeepSeek/Claude pour l'entrée (champs
    séparés) mais sans écriture de cache : un harnais non qualifié qui en
    publierait une est marqué ``unqualified_cache_write`` plutôt qu'agrégé
    silencieusement (contrat §2.2).
    """
    key = str(harness or "").strip().lower()
    key = _HARNESS_ALIASES.get(key, key)
    return CONVENTIONS.get(key, Convention(key or "unknown"))


def _reasons(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """Union ordonnée des raisons, sans doublon."""
    out: list[str] = []
    for group in groups:
        for reason in group:
            if reason and reason not in out:
                out.append(reason)
    return tuple(out)


@dataclass(frozen=True)
class Measure:
    """Une valeur publiée : valeur, qualité, raisons, granularité et source.

    ``granularity`` vaut ``call`` (un appel), ``turn`` (une remise de fin de
    tour, Codex exec), ``session`` (cumul de session) ou ``mixed``. ``source``
    dit d'où vient la mesure (flux, journal, total cumulé, grand livre).
    """

    value: int | None = None
    quality: str = UNKNOWN
    reasons: tuple[str, ...] = ()
    granularity: str = "call"
    source: str = SOURCE_STREAM

    @property
    def known(self) -> bool:
        return self.value is not None

    def degraded(self, reason: str) -> "Measure":
        """La même valeur, marquée incomplète par un motif de tour."""
        if self.value is None:
            return Measure(None, UNKNOWN, _reasons(self.reasons, (reason,)),
                           self.granularity, self.source)
        quality = self.quality if self.quality in (PARTIAL, UNKNOWN) else PARTIAL
        return Measure(self.value, quality, _reasons(self.reasons, (reason,)),
                       self.granularity, self.source)

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "quality": self.quality,
            "reasons": list(self.reasons),
            "granularity": self.granularity,
            "source": self.source,
        }


def _aggregate_granularity(measures: list[Measure]) -> str:
    kinds = {measure.granularity for measure in measures}
    return kinds.pop() if len(kinds) == 1 else ("mixed" if kinds else "call")


def _aggregate_source(measures: list[Measure]) -> str:
    sources = {measure.source for measure in measures}
    return sources.pop() if len(sources) == 1 else ("mixed" if sources else "none")


def _sum_measures(measures: list[Measure], *, missing_reason: str) -> Measure:
    """Somme de mesures : valeur partielle si une composante manque.

    Une somme dont toutes les composantes sont inconnues reste ``None`` : on
    ne publie pas ``0`` à la place d'une absence (contrat §2.4).
    """
    if not measures:
        return Measure(None, UNKNOWN, (missing_reason,), "call", "none")
    known = [measure for measure in measures if measure.value is not None]
    reasons = _reasons(*(measure.reasons for measure in measures
                         if measure.quality not in (MEASURED, DERIVED)))
    if not known:
        return Measure(None, UNKNOWN, reasons or (missing_reason,),
                       _aggregate_granularity(measures),
                       _aggregate_source(measures))
    value = sum(int(measure.value) for measure in known)
    degraded = any(measure.quality not in (MEASURED, DERIVED)
                   for measure in measures)
    return Measure(value, PARTIAL if degraded else MEASURED, reasons,
                   _aggregate_granularity(measures),
                   _aggregate_source(measures))


@dataclass(frozen=True)
class CallMeasure:
    """Les mesures d'UN appel au modèle, dans la convention de son harnais."""

    usage: UsageFields
    c_last: Measure
    o_last: Measure
    reread: Measure
    cache_read: Measure
    cache_write: Measure


def call_measure(harness: str | None, usage: Mapping | None, *,
                 source: str = SOURCE_STREAM,
                 call_id: str | None = None) -> CallMeasure:
    """Normalise l'usage d'un appel selon la convention du harnais.

    ``C_last`` inclut le cache lu et, pour Claude, le cache écrit ; ``R_turn``
    n'inclut jamais le cache écrit. Codex ne fait jamais ``input + cached`` :
    son entrée contient déjà le cache. Une clé absente rend la valeur ``None``
    avec une raison, sauf pour la relecture où la partie connue est publiée
    ``partial`` (une somme partielle reste une information nommée).
    """
    conv = convention(harness)
    fields = usage_fields(usage)

    # ACP : `used` est l'entrée du dernier usage_update ; le pont publie déjà
    # `inputTokens`, mais le contrat nomme `used` en premier.
    input_tokens = fields.input_tokens
    if conv.harness == "acp":
        used = _token(usage, "used")
        if used is not None:
            input_tokens = used

    cache_read = fields.cache_read_tokens
    cache_write = fields.cache_write_tokens
    output = fields.output_tokens

    # -- C_last ---------------------------------------------------------
    c_value: int | None
    c_quality: str
    c_reasons: tuple[str, ...] = ()
    if conv.input_includes_cache:
        if input_tokens is not None:
            c_value, c_quality = input_tokens, MEASURED
        elif cache_read is not None:
            # max(entrée, cache) avec l'entrée manquante : repli nommé
            c_value, c_quality = cache_read, FALLBACK
            c_reasons = (REASON_INPUT_UNKNOWN,)
        else:
            c_value, c_quality = None, UNKNOWN
            c_reasons = (REASON_CONTEXT_UNKNOWN,)
        if cache_write is not None:
            c_reasons = _reasons(c_reasons, (REASON_UNQUALIFIED_CACHE_WRITE,))
            c_quality = PARTIAL if c_value is not None else UNKNOWN
    else:
        if input_tokens is None:
            c_value, c_quality = None, UNKNOWN
            c_reasons = (REASON_CONTEXT_UNKNOWN,)
        elif cache_read is None:
            c_value, c_quality = None, UNKNOWN
            c_reasons = (REASON_CACHE_READ_UNKNOWN,)
        elif conv.cache_write_in_context and cache_write is None:
            c_value, c_quality = None, UNKNOWN
            c_reasons = (REASON_CACHE_WRITE_UNKNOWN,)
        else:
            c_value = input_tokens + cache_read
            if conv.cache_write_in_context:
                c_value += int(cache_write or 0)
            c_quality = MEASURED
            if cache_write is not None and not conv.cache_write_in_context:
                c_reasons = (REASON_UNQUALIFIED_CACHE_WRITE,)
                c_quality = PARTIAL

    # -- R_turn (relecture de l'appel) ---------------------------------
    r_value: int | None
    r_quality: str
    r_reasons: tuple[str, ...] = ()
    if conv.input_includes_cache:
        if input_tokens is not None:
            r_value, r_quality = input_tokens, MEASURED
        elif cache_read is not None:
            r_value, r_quality = cache_read, FALLBACK
            r_reasons = (REASON_INPUT_UNKNOWN,)
        else:
            r_value, r_quality = None, UNKNOWN
            r_reasons = (REASON_CONTEXT_UNKNOWN,)
    elif input_tokens is None:
        r_value, r_quality = None, UNKNOWN
        r_reasons = (REASON_INPUT_UNKNOWN,)
    elif cache_read is None:
        # la partie connue est publiée, explicitement partielle
        r_value, r_quality = input_tokens, PARTIAL
        r_reasons = (REASON_CACHE_READ_UNKNOWN,)
    else:
        r_value, r_quality = input_tokens + cache_read, MEASURED

    if conv.harness == "claude" and call_id is None:
        r_reasons = _reasons(r_reasons, (REASON_CALL_ID_UNKNOWN,))
        r_quality = PARTIAL if r_value is not None else UNKNOWN

    # -- O_last ---------------------------------------------------------
    if output is None:
        o_value, o_quality, o_reasons = None, UNKNOWN, (REASON_OUTPUT_UNKNOWN,)
    elif conv.output_zero_unknown and output == 0:
        o_value, o_quality, o_reasons = None, UNKNOWN, (REASON_OUTPUT_UNKNOWN,)
    else:
        o_value, o_quality, o_reasons = output, MEASURED, ()

    # -- cache lu et écrit ---------------------------------------------
    if cache_read is None:
        cr_value, cr_quality = None, UNKNOWN
        cr_reasons: tuple[str, ...] = (REASON_CACHE_READ_UNKNOWN,)
    else:
        cr_value, cr_quality, cr_reasons = cache_read, MEASURED, ()

    if conv.publishes_cache_write:
        if cache_write is None:
            cw_value, cw_quality = None, UNKNOWN
            cw_reasons = (REASON_CACHE_WRITE_UNKNOWN,)
        else:
            cw_value, cw_quality, cw_reasons = cache_write, MEASURED, ()
    else:
        cw_value, cw_quality = 0, DERIVED
        cw_reasons = (REASON_NO_CACHE_WRITE,)
        if cache_write is not None:
            cw_reasons = _reasons(cw_reasons, (REASON_UNQUALIFIED_CACHE_WRITE,))

    return CallMeasure(
        usage=fields,
        c_last=Measure(c_value, c_quality, c_reasons, "call", source),
        o_last=Measure(o_value, o_quality, o_reasons, "call", source),
        reread=Measure(r_value, r_quality, r_reasons, "call", source),
        cache_read=Measure(cr_value, cr_quality, cr_reasons, "call", source),
        cache_write=Measure(cw_value, cw_quality, cw_reasons, "call", source),
    )


class TurnNormalizer:
    """Accumule les compteurs d'un tour, fil principal et sous-agents séparés.

    Le normalisateur est volontairement **hors décision** : il ne lit aucun
    seuil et ne rend pas de verdict de rotation. Il est remis à zéro à chaque
    tour (l'appelant en crée un neuf) ; ``session_reread`` porte le cumul des
    tours précédents de la même session, tel que le grand livre le connaît.

    Déduplication : un appel est identifié par ``call_id`` quand le flux en
    publie un. Des blocs répétés du même message (Claude répète l'usage sur
    chaque bloc) sont ainsi comptés une fois ; une mise à jour du même appel
    remplace la mesure précédente au lieu de s'y ajouter. Sans ``call_id``,
    chaque événement est un appel distinct — l'information manque, elle est
    nommée ``call_id_unknown`` pour Claude.

    Codex : ``turn.completed`` publie un cumul de **fil**, pas un tour. On le
    remet via :meth:`total`, qui travaille en delta à partir d'un point de
    départ (:meth:`baseline` ou ``cumulative``). Le flux ``codex exec --json``
    ne publie pas d'usage d'appel : le ``last_token_usage`` du journal de
    session se remet via :meth:`journal_call`, qui alimente ``C_last`` et
    ``O_last`` **sans** entrer dans ``R_turn`` ni dans les caches cumulés (le
    journal ne publie qu'un appel, pas le tour). :meth:`journal_unavailable`
    nomme le motif d'un journal muet ; la source de ``R_turn`` reste choisie
    automatiquement : un cumul observé remplace la somme des appels, jamais
    les deux additionnés.
    """

    def __init__(self, harness: str | None, *, session: str | None = None,
                 turn: str | None = None, model: str | None = None,
                 mode: str | None = None, role: str | None = None,
                 source: str = SOURCE_STREAM, session_reread: int | None = None,
                 session_reread_quality: str = MEASURED,
                 session_reread_reasons: Iterable[str] = (),
                 cumulative: Mapping | None = None,
                 reread_source: str = "auto", max_calls: int = MAX_CALLS):
        if reread_source not in ("auto", "calls", "totals"):
            raise ContextMetricsError(
                "reread_source must be auto, calls or totals")
        if session_reread_quality not in QUALITIES:
            raise ContextMetricsError(
                "session_reread_quality must be one of %s" % ", ".join(QUALITIES))
        self.convention = convention(harness)
        self.harness = self.convention.harness
        self.session = str(session) if session else None
        self.turn = str(turn) if turn else None
        self.model = model
        #: provenance du nom : ``requested`` tant que le flux n'a pas annoncé
        #: le modèle effectif, ``measured`` après cette annonce (§3.1). Un nom
        #: de lancement n'est jamais publié comme une mesure runtime.
        self.model_quality = REQUESTED if model else UNKNOWN
        self.mode = mode
        self.role = role
        self.source = source
        self.session_reread = session_reread
        #: qualité et raisons de la base ``R_session`` : une base partielle
        #: (trou dans un tour précédent) ne devient jamais ``measured`` après
        #: l'ajout d'un tour complet (#4870, B cx1). ``None`` = inconnue.
        self.session_reread_quality = session_reread_quality
        self.session_reread_reasons = tuple(str(r) for r in
                                            (session_reread_reasons or ()))
        self.reread_source = reread_source
        self.max_calls = max(1, int(max_calls))
        self._calls: list[CallMeasure] = []
        self._subcalls: list[CallMeasure] = []
        #: #5148 : dernier appel lu au journal Codex (hors flux), et motif
        #: d'absence du journal. Le premier alimente C_last/O_last seulement.
        self._journal_call: CallMeasure | None = None
        self._journal_reason: str | None = None
        self._index: dict[tuple[bool, str], int] = {}
        self._count = 0
        self._duplicates = 0
        self._replaced = 0
        self._dropped = 0
        self._session_mismatch = False
        self._interrupted = False
        self._finished: dict | None = None
        self._totals_seen = False
        self._total_observations = 0
        self._cumulative: dict[str, int | None] = {
            "input": None, "cache_read": None, "output": None}
        self._total = {
            "reread": {"value": 0, "known": 0, "missing": False, "reasons": []},
            "output": {"value": 0, "known": 0, "missing": False, "reasons": []},
            "cache_read": {"value": 0, "known": 0, "missing": False, "reasons": []},
        }
        if cumulative is not None:
            self.baseline(cumulative)

    # -- alimentation ---------------------------------------------------
    def baseline(self, usage: Mapping | None) -> "TurnNormalizer":
        """Fixe le cumul de fil Codex **avant** le tour, sans le compter."""
        fields = usage_fields(usage)
        self._cumulative = {
            "input": fields.input_tokens,
            "cache_read": fields.cache_read_tokens,
            "output": fields.output_tokens,
        }
        return self

    def call(self, usage: Mapping | None, call_id: str | None = None, *,
             subagent: bool = False, source: str | None = None) -> "TurnNormalizer":
        """Ajoute l'usage d'un appel (ou met à jour le même ``call_id``)."""
        self._ensure_open()
        if not isinstance(usage, Mapping):
            raise ContextMetricsError("a call usage must be a mapping")
        if self._session_mismatch:
            return self
        measure = call_measure(self.harness, usage, source=source or self.source,
                               call_id=call_id)
        key: tuple[bool, str] | None = None
        if call_id is not None and str(call_id) != "":
            key = (bool(subagent), str(call_id))
        target = self._subcalls if subagent else self._calls
        if key is not None and key in self._index:
            position = self._index[key]
            previous = target[position]
            if previous == measure:
                self._duplicates += 1
                return self
            target[position] = measure
            self._replaced += 1
            return self
        if self._count >= self.max_calls:
            self._dropped += 1
            return self
        target.append(measure)
        if key is not None:
            self._index[key] = len(target) - 1
        self._count += 1
        return self

    def total(self, usage: Mapping | None) -> "TurnNormalizer":
        """Ajoute un cumul de fil (Codex) et n'en retient que le **delta**.

        Sans point de départ connu, le premier cumul ne peut pas être attribué
        au tour : ``R_turn``/``O`` restent inconnus avec
        ``cumulative_baseline_unknown``, puis le cumul suivant donne un delta
        valide. Un fil neuf se déclare avec ``cumulative={...: 0}``.
        """
        self._ensure_open()
        if not isinstance(usage, Mapping):
            raise ContextMetricsError("a total usage must be a mapping")
        if self._session_mismatch:
            return self
        fields = usage_fields(usage)
        self._totals_seen = True
        self._total_observations += 1

        def take(kind: str, key: str, value: int | None, missing_reason: str) -> None:
            slot = self._total[kind]
            if value is None:
                slot["missing"] = True
                if missing_reason not in slot["reasons"]:
                    slot["reasons"].append(missing_reason)
                return
            base = self._cumulative.get(key)
            if base is None:
                slot["missing"] = True
                if REASON_CUMULATIVE_BASELINE_UNKNOWN not in slot["reasons"]:
                    slot["reasons"].append(REASON_CUMULATIVE_BASELINE_UNKNOWN)
            else:
                delta = int(value) - int(base)
                if delta < 0:
                    # un cumul qui recule (fil repris, autre fil) ne compte
                    # pas négativement ; le tour reste partiel et le dit
                    slot["missing"] = True
                    if REASON_CUMULATIVE_DECREASED not in slot["reasons"]:
                        slot["reasons"].append(REASON_CUMULATIVE_DECREASED)
                else:
                    slot["value"] += delta
                    slot["known"] += 1
            self._cumulative[key] = int(value)

        take("reread", "input", fields.input_tokens, REASON_TOTAL_INPUT_UNKNOWN)
        take("output", "output", fields.output_tokens, REASON_TOTAL_OUTPUT_UNKNOWN)
        take("cache_read", "cache_read", fields.cache_read_tokens,
             REASON_TOTAL_CACHE_READ_UNKNOWN)
        return self

    def observe(self, parsed: Mapping | None) -> "TurnNormalizer":
        """Ajoute les mesures d'un événement déjà analysé par un adaptateur.

        Seuls ``call_usage``/``subagent_call_usage`` (usage d'un appel) et
        ``usage_total`` (cumul de fil) sont comptés. Pour Codex, un
        ``usage`` sans ``call_usage`` est le cumul de ``turn.completed`` ; pour
        les autres harnais, ``usage`` est un agrégat de tour et n'est pas un
        appel : il est ignoré (jamais compté comme un appel).

        Le ``model`` annoncé par le flux est le modèle **effectif** : il
        remplace le nom demandé au lancement et sa qualité devient
        ``measured``. Sans annonce, le nom garde sa provenance ``requested``.
        """
        self._ensure_open()
        if not isinstance(parsed, Mapping):
            return self
        announced_model = parsed.get("model")
        if isinstance(announced_model, str) and announced_model.strip():
            self.model = announced_model.strip()
            self.model_quality = MEASURED
        session = parsed.get("session")
        if session:
            session = str(session)
            if self.session is None:
                self.session = session
            elif session != self.session:
                # un relevé d'une autre session n'est ni reporté ni additionné
                self._session_mismatch = True
        call = parsed.get("call_usage")
        sub = parsed.get("subagent_call_usage")
        if isinstance(call, Mapping):
            self.call(call, parsed.get("call_id"))
        if isinstance(sub, Mapping):
            self.call(sub, parsed.get("call_id"), subagent=True)
        total = parsed.get("usage_total")
        if isinstance(total, Mapping):
            self.total(total)
        elif not isinstance(call, Mapping) and not isinstance(sub, Mapping) \
                and self.harness == "codex" \
                and isinstance(parsed.get("usage"), Mapping):
            self.total(parsed["usage"])
        return self

    # -- journal de session Codex (#5148) --------------------------------
    def journal_call(self, usage: Mapping | None) -> "TurnNormalizer":
        """Le dernier appel du fil principal, lu au journal Codex.

        ``codex exec --json`` ne publie pas d'usage d'appel : le journal de
        session du fil porte le ``last_token_usage`` du dernier ``token_count``.
        Cette mesure est un appel réel, mais **seul** : elle alimente
        ``C_last``/``O_last`` et n'entre ni dans ``R_turn`` (cumul du tour) ni
        dans les caches cumulés, qui restent mesurés par le flux. Un appel de
        flux, s'il existe, reste prioritaire — jamais les deux additionnés.
        """
        self._ensure_open()
        if not isinstance(usage, Mapping) or self._session_mismatch:
            return self
        self._journal_call = call_measure(self.harness, usage,
                                          source=SOURCE_JOURNAL)
        return self

    def journal_unavailable(self, reason: str | None = None) -> "TurnNormalizer":
        """Nomme le motif d'un journal Codex muet pour ce tour (#5148).

        ``C_last`` et ``O_last`` restent ``None`` avec ``context_unknown`` (ou
        ``output_unknown``) et ce motif distinct : ``journal_missing``,
        ``journal_unreadable``, ``turn_not_in_journal``,
        ``last_token_usage_missing`` ou ``no_call``. La mesure d'un tour
        précédent n'est jamais reprise.
        """
        self._ensure_open()
        self._journal_reason = str(reason) if reason else REASON_NO_CALL
        return self

    # -- sortie ---------------------------------------------------------
    def live_reread(self) -> dict:
        """Les relectures à cet instant du tour, sans clore le normalisateur.

        ``R_turn`` et ``R_session`` portent ``None`` si la mesure manque (jamais
        ``0``), et tous deux si un relevé vient d'une autre session ;
        ``base`` est le cumul des tours précédents. Le verdict reste à
        l'appelant : le normalisateur ne lit aucun seuil.
        """
        if self._session_mismatch:
            return {"R_turn": None, "R_session": None, "base": None}
        principal = self._principal_measures()
        return {"R_turn": principal["R_turn"].value,
                "R_session": principal["R_session"].value,
                "base": self.session_reread}

    def finish(self, *, interrupted: bool = False) -> dict:
        """Clôt le tour et rend l'observation ``ameesh-context/1``.

        Idempotent : un second appel rend le même document sans additionner
        deux fois ``R_turn`` au cumul de session.
        """
        if self._finished is not None:
            return self._finished
        self._interrupted = bool(interrupted)
        principal = self._principal_measures()
        subagents = self._subagent_measures()
        if self._dropped:
            # la borne a écarté des appels : le dernier appel retenu n'est plus
            # forcément le dernier du tour, et les sommes sont des minorants.
            principal = {key: value.degraded(REASON_CALL_BOUND_REACHED)
                         for key, value in principal.items()}
            subagents = {key: value.degraded(REASON_CALL_BOUND_REACHED)
                         for key, value in subagents.items()}
        if self._interrupted:
            principal = {key: value.degraded(REASON_TURN_INTERRUPTED)
                         for key, value in principal.items()}
            subagents = {key: value.degraded(REASON_TURN_INTERRUPTED)
                         for key, value in subagents.items()}
        if self._session_mismatch:
            principal = {key: value.degraded(REASON_SESSION_MISMATCH)
                         for key, value in principal.items()}
            subagents = {key: value.degraded(REASON_SESSION_MISMATCH)
                         for key, value in subagents.items()}

        r_turn = principal["R_turn"]
        if self.session is not None and not self._session_mismatch \
                and r_turn.value is not None and self.session_reread is not None:
            # La base garde la qualité du cumul : un tour partiel laisse la
            # session partielle pour les tours suivants du même normalisateur.
            self.session_reread_quality = MEASURED if (
                self.session_reread_quality in (MEASURED, DERIVED)
                and r_turn.quality in (MEASURED, DERIVED)) else PARTIAL
            self.session_reread_reasons = _reasons(
                self.session_reread_reasons, r_turn.reasons)
            self.session_reread += int(r_turn.value)

        all_measures = list(principal.values())
        if self._subcalls:
            all_measures += [value for key, value in subagents.items()
                             if key != "calls"]
        partial = any(measure.quality in (PARTIAL, UNKNOWN)
                      for measure in all_measures) \
            or bool(self._dropped) or self._session_mismatch or self._interrupted
        reasons = _reasons(*(measure.reasons for measure in all_measures))
        if self._dropped:
            reasons = _reasons(reasons, (REASON_CALL_BOUND_REACHED,))

        core = ("C_last", "O_last", "R_turn", "R_session", "O")
        sub_core = ("C_last", "O_last", "R_turn", "O")
        self._finished = {
            "schema": SCHEMA,
            "harness": self.harness,
            "model": self.model,
            "model_quality": self.model_quality,
            "mode": self.mode,
            "role": self.role,
            "session": self.session,
            "turn": self.turn,
            "source": self.source,
            "partial": partial,
            "measures": {key: principal[key].to_dict() for key in core},
            "cache": {
                "read_tokens": principal["cache_read_tokens"].to_dict(),
                "write_tokens": principal["cache_write_tokens"].to_dict(),
                "input_includes_read": self.convention.input_includes_cache,
                "write_in_context": self.convention.cache_write_in_context,
                "write_in_reread": False,
            },
            "subagents": {
                "calls": len(self._subcalls),
                **{key: subagents[key].to_dict() for key in sub_core},
                "cache": {
                    "read_tokens": subagents["cache_read_tokens"].to_dict(),
                    "write_tokens": subagents["cache_write_tokens"].to_dict(),
                },
            },
            "counts": {
                "calls": len(self._calls),
                "subagent_calls": len(self._subcalls),
                "journal_calls": 1 if self._journal_call is not None else 0,
                "totals": self._total_observations,
                "duplicates": self._duplicates,
                "replaced": self._replaced,
                "dropped": self._dropped,
            },
            "reasons": list(reasons),
            "finished": True,
        }
        return self._finished

    # -- interne --------------------------------------------------------
    def _ensure_open(self) -> None:
        if self._finished is not None:
            raise ContextMetricsError("the turn is already finished")

    def _reread_source(self) -> str:
        if self.reread_source != "auto":
            return self.reread_source
        return "totals" if self._totals_seen else "calls"

    def _principal_measures(self) -> dict[str, Measure]:
        source = self._reread_source()
        c_last = self._last_call_measure("c_last", REASON_CONTEXT_UNKNOWN)
        o_last = self._last_call_measure("o_last", REASON_OUTPUT_UNKNOWN)
        if source == "totals":
            r_turn = self._total_measure("reread")
            output = self._total_measure("output", missing_reason=REASON_NO_MEASURE)
            cache_read = self._total_measure("cache_read")
        else:
            r_turn = _sum_measures([call.reread for call in self._calls],
                                   missing_reason=REASON_NO_CALL)
            output = _sum_measures([call.o_last for call in self._calls],
                                   missing_reason=REASON_NO_CALL)
            cache_read = _sum_measures([call.cache_read for call in self._calls],
                                       missing_reason=REASON_NO_CALL)
        cache_write = self._cache_write_measure()
        return {
            "C_last": c_last,
            "O_last": o_last,
            "R_turn": r_turn,
            "R_session": self._session_measure(r_turn),
            "O": output,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
        }

    def _last_call_measure(self, field: str, unknown_reason: str) -> Measure:
        """C_last/O_last : appel du flux, sinon appel du journal, sinon motif.

        Un appel de flux reste prioritaire (le journal n'est qu'une source de
        remplacement) ; l'appel de journal ne compte pas dans ``R_turn``.
        """
        if self._calls:
            return getattr(self._calls[-1], field)
        if self._journal_call is not None:
            return getattr(self._journal_call, field)
        if self._journal_reason is not None:
            return Measure(None, UNKNOWN,
                           _reasons((unknown_reason,), (self._journal_reason,)),
                           "call", SOURCE_JOURNAL)
        return Measure(None, UNKNOWN, (REASON_NO_CALL,), "call", "none")

    def _subagent_measures(self) -> dict[str, Measure]:
        calls = self._subcalls
        return {
            "C_last": (calls[-1].c_last if calls else
                       Measure(None, UNKNOWN, (REASON_NO_CALL,), "call", "none")),
            "O_last": (calls[-1].o_last if calls else
                       Measure(None, UNKNOWN, (REASON_NO_CALL,), "call", "none")),
            "R_turn": _sum_measures([call.reread for call in calls],
                                    missing_reason=REASON_NO_CALL),
            "O": _sum_measures([call.o_last for call in calls],
                               missing_reason=REASON_NO_CALL),
            "cache_read_tokens": _sum_measures([call.cache_read for call in calls],
                                               missing_reason=REASON_NO_CALL),
            "cache_write_tokens": self._cache_write_measure(calls),
        }

    def _total_measure(self, kind: str, *,
                       missing_reason: str = REASON_NO_TOTAL) -> Measure:
        if not self._totals_seen:
            return Measure(None, UNKNOWN, (missing_reason,), "turn", "none")
        slot = self._total[kind]
        reasons = tuple(slot["reasons"])
        if not slot["known"]:
            return Measure(None, UNKNOWN, reasons or (missing_reason,),
                           "turn", SOURCE_TOTAL)
        quality = PARTIAL if slot["missing"] else MEASURED
        return Measure(slot["value"], quality, reasons, "turn", SOURCE_TOTAL)

    def _cache_write_measure(self,
                             calls: list[CallMeasure] | None = None) -> Measure:
        calls = self._calls if calls is None else calls
        if self.convention.publishes_cache_write:
            if not calls:
                return Measure(None, UNKNOWN, (REASON_NO_CALL,), "call", "none")
            return _sum_measures([call.cache_write for call in calls],
                                 missing_reason=REASON_CACHE_WRITE_UNKNOWN)
        # Un harnais sans champ d'écriture : 0 est structurel (derived), pas
        # une mesure de flux ; un champ inattendu reste non qualifié.
        reasons: tuple[str, ...] = (REASON_NO_CACHE_WRITE,)
        if any(REASON_UNQUALIFIED_CACHE_WRITE in call.cache_write.reasons
               or (call.cache_write.value or 0) != 0 for call in calls):
            reasons = _reasons(reasons, (REASON_UNQUALIFIED_CACHE_WRITE,))
        return Measure(0, DERIVED, reasons, "call",
                       _aggregate_source([call.cache_write for call in calls])
                       if calls else "none")

    def _session_measure(self, r_turn: Measure) -> Measure:
        if self.session is None:
            return Measure(None, UNKNOWN, (REASON_NO_SESSION,), "session", "none")
        if self.session_reread is None:
            return Measure(None, UNKNOWN,
                           _reasons((REASON_SESSION_BASELINE_UNKNOWN,),
                                    self.session_reread_reasons),
                           "session", SOURCE_LEDGER)
        if r_turn.value is None:
            # §2.1 : sans tour mesuré, R_session est null, jamais le seul cumul
            # des tours précédents présenté comme la session courante.
            return Measure(None, UNKNOWN,
                           _reasons((REASON_TURN_NOT_MEASURED,), r_turn.reasons),
                           "session", SOURCE_LEDGER)
        # Une base partielle le reste : la somme est un minorant nommé, jamais
        # un total « measured » parce que le tour courant, lui, est complet.
        quality = MEASURED if (self.session_reread_quality in (MEASURED, DERIVED)
                               and r_turn.quality in (MEASURED, DERIVED)) \
            else PARTIAL
        reasons = _reasons(self.session_reread_reasons, r_turn.reasons)
        return Measure(int(self.session_reread) + int(r_turn.value), quality,
                       reasons, "session", SOURCE_LEDGER)


def normalize_turn(harness: str | None, events, **kwargs) -> dict:
    """Normalise une suite d'événements analysés (commodité pour les tests).

    Chaque élément de ``events`` est un dictionnaire au contrat de
    ``adapters.parse``. Le tour est clôturé en fin d'itération ; ``kwargs``
    sont ceux de :class:`TurnNormalizer`.
    """
    normalizer = TurnNormalizer(harness, **kwargs)
    for event in events:
        normalizer.observe(event)
    return normalizer.finish()
