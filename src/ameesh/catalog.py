# SPDX-License-Identifier: AGPL-3.0-only
"""Le catalogue des modèles : ce qu'on écrit, et ce qu'on refuse d'en retirer (L14).

Ce module ne connaît ni HTTP ni SQL : il reçoit ce que les sources ont vu
(`discovery`) et l'état actif du catalogue, et rend une **décision**. C'est ce qui
permet de tester les règles de retrait sans base et sans réseau — et ces règles
sont la partie qui compte, parce qu'une erreur ici retire des modèles en silence.

Les trois gardes, dans l'ordre :

1. **liste incomplète ou en erreur** : aucun retrait (la source n'a pas tout vu) ;
2. **liste complète mais vide** : aucun retrait, et l'événement le dit — un
   incident de source ne doit pas vider le catalogue d'un coup ;
3. **retrait massif** (plus de la moitié des modèles actifs de la source) : rien
   n'est retiré, un événement « retrait massif refusé » est produit, et **un
   humain tranche**.
"""
from __future__ import annotations

import dataclasses

from .discovery.base import SourceResult

#: Au-delà de cette part des modèles actifs d'une source, un retrait est refusé.
MASS_WITHDRAWAL_RATIO = 0.5

#: Les sources qui apportent des prix ou des options, jamais une présence. La
#: liste est ici PARCE QU'UN APPELANT PEUT OUBLIER `kind` : une sonde de revue
#: construisait un `SourceResult(source="prices")` sans `kind`, et un barème
#: ressuscitait alors un modèle retiré. Le nom de la source fait foi.
LOCAL_SOURCE_NAMES = ("prices", "harness")


def is_local(result: SourceResult) -> bool:
    """Vrai si cette source ne dit rien de la présence des modèles."""
    return result.kind == "local" or result.source in LOCAL_SOURCE_NAMES


@dataclasses.dataclass(frozen=True)
class ActiveModel:
    """Une ligne active du catalogue, réduite à ce dont la décision a besoin."""

    provider: str
    model_id: str
    source: str


@dataclasses.dataclass(frozen=True)
class Decision:
    """Ce qu'il faut écrire, ce qu'il faut retirer, et ce qui a été refusé."""

    source: str
    seen: tuple[str, ...] = ()
    to_retire: tuple[ActiveModel, ...] = ()
    mass_withdrawal_refused: tuple[ActiveModel, ...] = ()
    refused_reason: str = ""
    detail: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.to_retire or self.mass_withdrawal_refused)


def reconcile(active: list[ActiveModel], result: SourceResult) -> Decision:
    """Décide des retraits d'une source, avec les trois gardes.

    Quatrième garde, ajoutée après la revue (B2) : une source LOCALE — le barème,
    les options des harnais — ne retire jamais rien, même si elle publie une liste
    de modèles. Seule une source de présence, complète et réussie, peut retirer.
    """
    seen = tuple(sorted({model.model_id for model in result.models}))
    own = [row for row in active if row.source == result.source]

    if is_local(result):
        return Decision(
            source=result.source,
            seen=seen,
            refused_reason="local-source",
            detail="source locale : elle apporte des prix, elle ne retire rien",
        )

    if not result.complete:
        return Decision(
            source=result.source,
            seen=seen,
            refused_reason="incomplete",
            detail=result.detail or "source incomplète : rien n'est retiré",
        )

    if not seen:
        # Complet, mais aucune liste : soit la source n'en publie pas (les harnais,
        # le barème), soit elle a répondu une liste vide. Dans les deux cas, rien.
        return Decision(
            source=result.source,
            refused_reason="empty",
            detail=result.detail or "liste complète mais sans modèle : rien n'est retiré",
        )

    missing = tuple(row for row in own if row.model_id not in set(seen))
    if not missing:
        return Decision(source=result.source, seen=seen)

    if len(missing) > len(own) * MASS_WITHDRAWAL_RATIO:
        return Decision(
            source=result.source,
            seen=seen,
            mass_withdrawal_refused=missing,
            refused_reason="mass-withdrawal",
            detail=(
                f"{len(missing)} modèles sur {len(own)} actifs auraient été retirés "
                f"({result.source}) : retrait massif refusé, un humain tranche"
            ),
        )

    return Decision(source=result.source, seen=seen, to_retire=missing)


@dataclasses.dataclass(frozen=True)
class CatalogEvent:
    """Un message à déposer : nouveau, retiré, ou retrait massif refusé."""

    body: str
    payload: dict


def events_for(
    decision: Decision,
    result: SourceResult,
    appeared: tuple[str, ...] = (),
    repriced: tuple[str, ...] = (),
) -> list[CatalogEvent]:
    """Les événements qu'un cycle de découverte produit — sans base ni envoi.

    Les séparer de l'envoi, c'est ce qui permet de tester le TEXTE et la charge
    utile sans messagerie : ce qui est dit d'un retrait massif refusé doit rester
    lisible par un humain qui n'a pas lu ce module.
    """
    source = decision.source
    events: list[CatalogEvent] = []
    if appeared:
        events.append(
            CatalogEvent(
                body=f"catalogue {source} : {len(appeared)} nouveau(x) modèle(s) — "
                     + ", ".join(appeared[:12])
                     + ("…" if len(appeared) > 12 else ""),
                payload={"source": source, "new": list(appeared)},
            )
        )
    if repriced:
        events.append(
            CatalogEvent(
                body=f"catalogue {source} : {len(repriced)} prix modifié(s) — "
                     + ", ".join(repriced[:12]),
                payload={"source": source, "repriced": list(repriced)},
            )
        )
    if decision.to_retire:
        events.append(
            CatalogEvent(
                body=f"catalogue {source} : {len(decision.to_retire)} modèle(s) retiré(s) — "
                     + ", ".join(row.model_id for row in decision.to_retire[:12]),
                payload={
                    "source": source,
                    "retired": [row.model_id for row in decision.to_retire],
                    "reason": "absence dans une liste complète et réussie",
                },
            )
        )
    if decision.mass_withdrawal_refused:
        events.append(
            CatalogEvent(
                body=f"catalogue {source} : retrait massif REFUSÉ — "
                     f"{len(decision.mass_withdrawal_refused)} modèles auraient disparu, "
                     "rien n'a été retiré ; un humain doit trancher",
                payload={
                    "source": source,
                    "refused": [row.model_id for row in decision.mass_withdrawal_refused],
                    "urgent": True,
                    "reason": "plus de la moitié des modèles actifs de la source",
                },
            )
        )
    return events


def record(
    store,
    result: SourceResult,
    *,
    emit=None,
    source_models: list | None = None,
) -> Decision:
    """Enregistre ce qu'une source a vu, applique la décision, et prévient.

    `store` est le domaine du stockage (`storage.of(db).catalog`) ; `emit` reçoit
    `(body, payload)` — laissé injectable pour que le test n'ait besoin ni de base
    ni de messagerie, et pour que la CLI décide seule à qui l'événement s'adresse.
    """
    if source_models is None:
        source_models = store.active(result.source) if hasattr(store, "active") else []
    # Le stockage rend des LIGNES (dicts) ; `reconcile` raisonne sur des ActiveModel.
    # La conversion est ici, à un seul endroit : c'est un dict qu'on aurait pris pour
    # un objet qui a fait échouer trois tests avant la revue.
    raw_rows = list(source_models)
    rows = [
        row if isinstance(row, ActiveModel) else ActiveModel(
            provider=row["provider"], model_id=row["model_id"], source=row.get("source", result.source)
        )
        for row in raw_rows
    ]
    # L'état antérieur se lit PAR CLÉS, toutes sources et tous états : un barème qui
    # touche une ligne détenue par un fournisseur doit la comparer à ce qu'elle était
    # (revue, cas croisé), et un prix en cache compte autant que les autres.
    keys = [(model.provider, model.model_id) for model in result.models]
    try:
        previous = store.known(keys) if keys and hasattr(store, "known") else []
    except AttributeError:  # pragma: no cover - domaine sans `known`
        previous = []
    existed = {(row["provider"], row["model_id"]) for row in previous}
    prices_before = {
        (row["provider"], row["model_id"]): (
            row.get("price_input"), row.get("price_cached"), row.get("price_output"),
        )
        for row in previous
    }

    seen_rows = [
        {
            "provider": model.provider,
            "model_id": model.model_id,
            "context_window": model.context_window,
            "price_input": model.price_input,
            "price_cached": model.price_cached,
            "price_output": model.price_output,
            "source": model.source or result.source,
            "raw_digest": result.raw_digest or None,
        }
        for model in result.models
    ]
    if seen_rows:
        # Une source LOCALE met à jour les prix sans toucher à la présence ni à la
        # propriété de la ligne ; une source de présence enregistre une apparition.
        if is_local(result):
            store.update_prices(seen_rows)
        else:
            store.upsert_models(seen_rows)
    if result.harnesses:
        store.upsert_harnesses([
            {
                "provider": item.provider,
                "model_id": item.model_id,
                "harness": item.harness,
                "efforts": list(item.efforts),
                "source": item.source or result.source,
            }
            for item in result.harnesses
        ])

    decision = reconcile(rows, result)
    if decision.to_retire:
        store.retire([
            {"provider": row.provider, "model_id": row.model_id} for row in decision.to_retire
        ])

    appeared = tuple(
        sorted(
            model.model_id
            for model in result.models
            if (model.provider, model.model_id) not in existed
        )
    )
    # Un prix qui change est un événement (décision 0020), y compris pour une source
    # locale : c'est ce que le propriétaire veut savoir d'un barème.
    repriced = tuple(
        sorted(
            model.model_id
            for model in result.models
            if (model.provider, model.model_id) in existed
            and (model.price_input, model.price_cached, model.price_output)
            != prices_before.get((model.provider, model.model_id))
            and any(
                value is not None
                for value in (model.price_input, model.price_cached, model.price_output)
            )
        )
    )
    if emit is not None:
        for event in events_for(decision, result, appeared, repriced):
            emit(event.body, event.payload)
    return decision
