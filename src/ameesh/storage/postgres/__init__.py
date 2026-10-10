# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote de stockage Postgres (v1) : délègue à `ameesh.db` (psql | psycopg).

Chaque domaine porte le SQL déplacé tel quel depuis son module métier ; la
connexion (ou la transaction ouverte) est celle passée à `storage.of(db)`.
"""
from __future__ import annotations

from typing import Any

from .. import interface
from .action_source import ActionSource
from .accounts import Accounts
from .actions import Actions
from .authenticators import Authenticators
from .authority import Approvals, Keys, Nonces
from .canon import Canon, Ephemerals
from .catalog import Catalog
from .costs import TurnCosts
from .grants import Grants
from .hosts import Hosts, TurnResources, Visibility
from .mailbox import Mailbox
from .operations import Operations
from .packages import WorkPackages
from .placement import Placements
from .progress import Progress
from .projects import Projects
from .registry import Agents, Leases, PendingSpend
from .session_bindings import SessionBindings
from .threads import Threads
from .wakeups import Wakeups
from .work import WorkItems


class PostgresStorage(interface.Storage):
    """Les domaines du stockage sur une connexion Postgres de `ameesh.db`."""

    driver = "postgres"

    def __init__(self, db: Any):
        self.db = db
        self.agents = Agents(db)
        self.leases = Leases(db)
        self.pending_spend = PendingSpend(db)
        self.turn_costs = TurnCosts(db)
        self.accounts = Accounts(db)
        self.catalog = Catalog(db)
        self.mailbox = Mailbox(db)
        self.wakeups = Wakeups(db)
        self.keys = Keys(db)
        self.approvals = Approvals(db)
        self.nonces = Nonces(db)
        self.work = WorkItems(db)
        self.packages = WorkPackages(db)
        self.actions = Actions(db)
        self.action_source = ActionSource(db)
        self.canon = Canon(db)
        self.ephemerals = Ephemerals(db)
        self.authenticators = Authenticators(db)
        self.threads = Threads(db)
        self.grants = Grants(db)
        self.placements = Placements(db)
        self.progress = Progress(db)
        self.projects = Projects(db)
        self.operations = Operations(db)
        self.hosts = Hosts(db)
        self.turn_resources = TurnResources(db)
        self.visibility = Visibility(db)
        self.session_bindings = SessionBindings(db)
