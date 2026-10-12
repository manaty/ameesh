# SPDX-License-Identifier: AGPL-3.0-only
"""L234 : rôles et fiches protégées, résolus dans le canon propriétaire."""
from . import canon as canon_mod
from .alert_briefs import AUDITOR_NAME, AUDITOR_ROLES


def load_all(cfg):
    """La prise automatique exige tous les canons, sans omission silencieuse."""
    out = []
    for entry in canon_mod.configured(cfg):
        loaded = canon_mod.from_config(cfg, entry=entry)
        # Un frontmatter invalide peut masquer le rôle de l'auditeur, même
        # classé warning quand son type est indéterminable.
        if (not loaded.readable or loaded.scope_unread
                or canon_mod.errors(loaded.load_findings)
                or any(f.code == "frontmatter-invalid" for f in loaded.load_findings)):
            raise canon_mod.CanonError("canon unreadable or scope incomplete: %s"
 % entry.path)
        out.append(loaded)
    canon_mod.check_identities(out)
    return out


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
        for canon in canons:
            if not canon.readable or not canon.owns(row.get("canon")):
                continue
            definition = canon.agent(name)
            if definition is None:
                continue
            data = definition.fiche.data
            roles = data.get("roles") or data.get("role") or []
            if isinstance(roles, str):
                roles = [roles]
            if name == AUDITOR_NAME or any(str(role).strip().lower() in AUDITOR_ROLES
                                          for role in roles):
                found[name] = definition.fiche
    return found


def card_markers(card):
    """Référence sans version et chemin réel, indépendants du nom du persona."""
    if card is None:
        return ()
    markers = {card.path.casefold(), card.ref.rsplit("@", 1)[0].casefold()}
    return tuple(marker for marker in markers if marker)
