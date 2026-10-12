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
