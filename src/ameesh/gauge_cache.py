# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded process-local cache of journal observations, never budget decisions."""
from collections import OrderedDict
from copy import copy
from threading import Lock


class GaugeCache:
    """Keep compact immutable observations across short-lived CostBooks.

    Callers must check the journal signature on every lookup. Limit both the
    number of journals and account observations per journal; removed agent
    directories therefore cannot cause unlimited retention. No transcript
    text, credentials, registry entries or budget decisions are retained.

    Une observation est `(estampille, production, gauges)` (#1132) : la copie
    garde le TYPE de la liste (son `overage` pour `Gauges`) sans recopier ses
    éléments, qui sont gelés.
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
            # #1132 : un relevé est (estampille, production, Gauges) ; le cache
            # en garde une copie — la lecture partagée ne doit pas être mutée
            # par un appelant — avec l'overage porté par les `Gauges` (#1649).
            # #1661 : jamais l'objet mutable du producteur ; `copy` rend un
            # `Gauges` neuf (type et overage conservés), les fenêtres étant
            # gelées (`replace`) par le producteur, et `_claude_gauges_of`
            # rend une copie fraîche à chaque appelant.
            frozen = {account: (stamp, produced, copy(gauges))
                      for account, (stamp, produced, gauges) in readings.items()}
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
