# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote de stockage Postgres (v1) : délègue à `ameesh.db` (psql | psycopg).

Chaque domaine porte le SQL déplacé tel quel depuis son module métier ; la
connexion (ou la transaction ouverte) est celle passée à `storage.of(db)`.
"""
from __future__ import annotations

from typing import Any

from .. import interface
from .action_source import ActionSource
from .api_requests import ApiRequests
from .alert_acknowledgements import AlertAcknowledgements
from .accounts import Accounts
from .account_windows import AccountWindows
from .provider_credits import ProviderCredits
from .actions import Actions
from .budgets import Budgets
from .authenticators import Authenticators
from .authority import Approvals, Keys, Nonces
from .canon import Canon, Ephemerals
from .catalog import Catalog
from .veille import Veille
from .ci import CiResults
from .decisions import Decisions
from .costs import TurnCosts
from .conversations import Conversations
from .credentials import Credentials
from .events import Events
from .grants import Grants
from .host_lifecycle import HostLifecycle
from .handoffs import Handoffs
from .hosts import Hosts, TurnResources, Visibility
from .github_watch import GithubWatch
from .fingerprint_watch import FingerprintWatch
from .mailbox import Mailbox
from .notify_liveness import NotifyLiveness
from .menage import Housekeeping
from .operations import Operations
from .turn_observation import TurnObservation
from .packages import WorkPackages
from .placement import Placements
from .prevalidation import PrevalidationReports
from .progress import Progress
from .projects import Projects
from .provenance import Provenance
from .roadmap import Roadmap
from .registry import Agents, Leases, PendingSpend
from .session_bindings import SessionBindings
from .threads import Threads
from .wakeups import Wakeups
from .watches import Watches
from .work import WorkItems
from .work_dependencies import WorkDependencies
from .work_footprints import WorkFootprints
from .migration_reservations import MigrationReservations
from .lot_sessions import LotSessions


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
        self.account_windows = AccountWindows(db)
        self.provider_credits = ProviderCredits(db)
        self.credentials = Credentials(db)
        self.budgets = Budgets(db)
        self.api_requests = ApiRequests(db)
        self.alert_acknowledgements = AlertAcknowledgements(db)
        self.watches = Watches(db)
        self.events = Events(db)
        self.catalog = Catalog(db)
        self.veille = Veille(db)
        self.mailbox = Mailbox(db)
        self.github_watch = GithubWatch(db)
        self.fingerprints = FingerprintWatch(db)
        self.notify_liveness = NotifyLiveness(db)
        self.wakeups = Wakeups(db)
        self.keys = Keys(db)
        self.approvals = Approvals(db)
        self.nonces = Nonces(db)
        self.work = WorkItems(db)
        self.work_dependencies = WorkDependencies(db)
        self.work_footprints = WorkFootprints(db)
        self.provenance = Provenance(db)
        self.migration_reservations = MigrationReservations(db)
        self.lot_sessions = LotSessions(db)
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
        self.decisions = Decisions(db)
        self.roadmap = Roadmap(db)
        self.operations = Operations(db)
        self.handoffs = Handoffs(db)
        self.hosts = Hosts(db)
        self.host_lifecycle = HostLifecycle(db)
        self.turn_resources = TurnResources(db)
        self.turn_observation = TurnObservation(db)
        self.visibility = Visibility(db)
        self.housekeeping = Housekeeping(db)
        self.session_bindings = SessionBindings(db)
        self.ci = CiResults(db)
        self.prevalidation = PrevalidationReports(db)
        self.conversations = Conversations(db)
