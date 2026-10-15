# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded process-local cache of journal observations, never budget decisions."""
from collections import OrderedDict
from threading import Lock


class GaugeCache:
    """Keep compact immutable observations across short-lived CostBooks.

    Callers must check the journal signature on every lookup. Limit both the
    number of journals and account observations per journal; removed agent
    directories therefore cannot cause unlimited retention. No transcript
    text, credentials, registry entries or budget decisions are retained.
    """

    def __init__(self, max_entries=128, max_accounts=64):
        self.max_entries = max_entries
        self.max_accounts = max_accounts
        self._entries = OrderedDict()
        self._lock = Lock()

    def get(self, path):
        with self._lock:
            value = self._entries.get(path)
            if value is not None:
                self._entries.move_to_end(path)
            return value

    def __setitem__(self, path, value):
        signature, readings = value
        with self._lock:
            self._entries.pop(path, None)
            if len(readings) > self.max_accounts:
                return
            # #1661 : un relevé est (stamp, fenêtres, overage) ; le cache en
            # garde une observation immuable — jamais l'objet mutable du
            # producteur — sans perdre les drapeaux d'overage.
            frozen = {}
            for account, reading in readings.items():
                stamp, gauges = reading[0], reading[1]
                overage = (reading[2] if len(reading) > 2
                           else getattr(gauges, "overage", None))
                frozen[account] = (stamp, tuple(gauges), overage)
            self._entries[path] = (signature, frozen)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def pop(self, path, default=None):
        with self._lock:
            return self._entries.pop(path, default)

    def __len__(self):
        with self._lock:
            return len(self._entries)


CLAUDE_GAUGES = GaugeCache()
