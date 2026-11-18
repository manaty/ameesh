# SPDX-License-Identifier: AGPL-3.0-only
"""L234 : rôles et fiches protégées, résolus dans le canon propriétaire.

#7661 : l'auditeur est l'agent dont la fiche du canon déclare le rôle
(`roles: [auditeur]`, ou `auditor`, `role:`) ; son nom de persona ne compte
ni pour le reconnaître ni pour lui donner une permission. Un mesh sans ce rôle
n'a pas d'auditeur — ni fiche protégée, ni tour exempté de pression.
"""
from . import canon as canon_mod
from .alert_briefs import AUDITOR_ROLES


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

    Seule une fiche qui déclare le rôle compte : une persona sans fiche, ou
    dont la fiche ne le déclare pas, n'est pas auditrice. Une fiche en conflit
    dans un autre canon ne donne jamais de rôle ni de permission au nom déjà
    au registre. Sans canon chargé (installation historique, hôte sans dépôt),
    la projection `roles` du registre — copiée de la carte par `canon sync` —
    est la déclaration : le nom est reconnu, sans fiche (None) (#7661).
    """
    from . import sous_utilisation as su
    found = {}
    for row in listing:
        card = owned_card(row, canons, AUDITOR_ROLES)
        if card is not None:
            found[row["name"]] = card
        elif not canons and su.row_has_role(row, AUDITOR_ROLES):
            found[row["name"]] = None
    return found


def owned_card(row, canons, roles):
    """La fiche de l'agent `row` qui nomme l'un des `roles`, prise seulement
    dans son canon propriétaire (None sinon)."""
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
        if any(str(role).strip().lower() in wanted for role in named):
            card = definition.fiche
    return card


def card_markers(card):
    """Référence sans version et chemin réel, indépendants du nom du persona."""
    if card is None:
        return ()
    markers = {card.path.casefold(), card.ref.rsplit("@", 1)[0].casefold()}
    return tuple(marker for marker in markers if marker)


def is_auditor(db, name, canons, agent=None):
    """L234 : rôle auditeur/auditor déclaré par la fiche du canon propriétaire.
    Le nom de la persona ne suffit jamais. Canon chargé, la fiche fait foi ;
    sans canon (hôte sans dépôt, #3327 B/P1), la projection `roles` du registre
    — copiée de la carte par `canon sync` — est la déclaration disponible. Le
    registre n'est lu que pour un nom candidat ; `agent`, la ligne fraîche de
    l'appelant, évite cette lecture."""
    from . import registry, sous_utilisation as su
    if canons:
        if name not in su.role_orchestrators(canons, AUDITOR_ROLES):
            return False
        row = agent if agent is not None else registry.get(db, name)
        return bool(row and name in cards([row], canons))
    row = agent if agent is not None else registry.get(db, name)
    return bool(row and su.row_has_role(row, AUDITOR_ROLES))


def is_critical(cfg, db, name, canons, *, declared=(), agent=None):
    """L242b : agent critique, jamais en pause budget tant qu'un compte a de la
    marge. L242c : déclaré (`critical_agents`, `AMEESH_CRITICAL_AGENTS` ;
    `declared`, d'`AMEESH_ALERT_ORCHESTRATORS`), sinon par un rôle critique de
    sa fiche, dans son canon propriétaire seulement (`critical_roles` de
    l'organisation). Canon lisible : le verdict rafraîchit le cache (#424 r3) ;
    illisible ou non chargé : le dernier verdict lu, sinon critique (#424 r6) ;
    configuré sur aucun canon de l'hôte : non critique (#424 r7). Sans mémoire
    de processus : partagé par la reprise sur pause budget (L233b) et `ameesh
    resume` ; l'exécuteur a la sienne (`Runner.is_critical`)."""
    import sys
    from . import registry, sous_utilisation as su
    if name in (getattr(cfg, "critical_agents", None) or ()):
        return True
    if name in (declared or ()):
        return True
    row = dict((agent if agent is not None else registry.get(db, name)) or {}, name=name)
    try:
        entries = canon_mod.configured(cfg) if cfg is not None else tuple(canons or ())
    except Exception:
        entries = None                  # on ne sait pas : jamais « sans canon »
    configured = entries is None or bool(entries)
    complete = entries is not None and bool(canons) and len(canons) == len(entries)
    foreign = su.foreign_canon(row, canons, complete=complete)
    if foreign is not None:
        # #424 r7 : état connu, pas une panne ; jamais retenu au cache
        if su.warn_foreign(name):
            print("ameesh: %s: owner canon %s is configured on no canon of this host:"
                  " not critical" % (name, foreign), file=sys.stderr)
        return False
    verdict, unknown = su.critical_verdict(
        row, canons, configured=configured,
        known=su.remembered(su.critical_memory(cfg), name))
    if not unknown and configured:
        su.remember_critical(cfg, name, verdict)
    return verdict
