# SPDX-License-Identifier: AGPL-3.0-only
"""L234 : rôles et fiches protégées, résolus dans le canon propriétaire."""
from . import canon as canon_mod
from .alert_briefs import AUDITOR_NAME, AUDITOR_ROLES


def _readable(loaded):
    # Un frontmatter invalide peut masquer le rôle de l'auditeur, même
    # classé warning quand son type est indéterminable.
    return (loaded.readable and not loaded.scope_unread
            and not canon_mod.errors(loaded.load_findings)
            and not any(f.code == "frontmatter-invalid" for f in loaded.load_findings))


def load_all(cfg, *, strict=True):
    """Charge tous les canons sans omission ; vérifie leurs identités.

    En mode non strict, la prise vérifie ensuite le canon de chaque candidat.
    """
    out = []
    for entry in canon_mod.configured(cfg):
        loaded = canon_mod.from_config(cfg, entry=entry)
        if strict and not _readable(loaded):
            raise canon_mod.CanonError("canon unreadable or scope incomplete: %s" % entry.path)
        out.append(loaded)
    canon_mod.check_identities(out)
    return out


def may_take(row, canons):
    """Refuse une prise si le canon propriétaire est absent ou incomplet."""
    if not canons:  # installations historiques sans canon configuré
        return True
    return any(c.owns(row.get("canon")) and _readable(c) for c in canons)


def cards(listing, canons):
    """name -> fiche de l'auditeur, seulement dans le canon propriétaire.

    La persona sans fiche reste reconnue ; une fiche en conflit dans un autre
    canon ne donne jamais de rôle ni de permission au nom déjà au registre.
    """
    found = {}
    for row in listing:
        name = row["name"]
        if name == AUDITOR_NAME:
            found[name] = None
        card = owned_card(row, canons, AUDITOR_ROLES, persona=name == AUDITOR_NAME)
        if card is not None:
            found[name] = card
    return found


def owned_card(row, canons, roles, *, persona=False):
    """La fiche de l'agent `row` qui nomme l'un des `roles`, prise seulement
    dans son canon propriétaire (None sinon) ; `persona` : sa fiche, quel que
    soit son rôle."""
    wanted = {str(role).strip().lower() for role in roles}
    card = None
    for canon in canons:
        if not canon.readable or not canon.owns(row.get("canon")):
            continue
        definition = canon.agent(row["name"])
        if definition is None:
            continue
        data = definition.fiche.data
        named = data.get("roles") or data.get("role") or []
        if isinstance(named, str):
            named = [named]
        if persona or any(str(role).strip().lower() in wanted for role in named):
            card = definition.fiche
    return card


def card_markers(card):
    """Référence sans version et chemin réel, indépendants du nom du persona."""
    if card is None:
        return ()
    markers = {card.path.casefold(), card.ref.rsplit("@", 1)[0].casefold()}
    return tuple(marker for marker in markers if marker)


def is_auditor(db, name, canons, agent=None):
    """L234 : persona auditeur ou rôle auditeur/auditor dans le canon
    propriétaire. Le registre n'est lu que pour un nom candidat ; `agent`, la
    ligne fraîche de l'appelant, évite cette lecture."""
    from . import registry, sous_utilisation as su
    if name == AUDITOR_NAME:
        return True
    if name not in su.role_orchestrators(canons, AUDITOR_ROLES):
        return False
    row = agent if agent is not None else registry.get(db, name)
    return bool(row and name in cards([row], canons))


def is_critical(cfg, db, name, canons, *, declared=(), agent=None):
    """L242b : agent critique, jamais en pause budget tant qu'un compte a de la
    marge — déclaré (`critical_agents`, `AMEESH_CRITICAL_AGENTS`),
    orchestrateur (`declared`, d'`AMEESH_ALERT_ORCHESTRATORS`, ou rôle au
    canon), conception (rôle `conception` ou `design` au canon) ou auditeur
    (`is_auditor`). Un rôle ne compte que dans le canon propriétaire de
    l'agent : une fiche en conflit dans un autre canon ne le rend pas critique
    (#324 r1). Partagé par l'exécuteur et la reprise sur pause budget (L233b)."""
    from . import registry, sous_utilisation as su
    if name in (getattr(cfg, "critical_agents", None) or ()):
        return True
    if name in (declared or ()):
        return True
    roles = su.ORCHESTRATOR_ROLES + su.DESIGN_ROLES
    if name in su.role_orchestrators(canons, roles):
        row = agent if agent is not None else registry.get(db, name)
        if row and owned_card(row, canons, roles) is not None:
            return True
    return is_auditor(db, name, canons, agent)
