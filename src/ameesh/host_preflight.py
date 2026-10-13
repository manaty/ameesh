# SPDX-License-Identifier: AGPL-3.0-only
"""L232e: evaluate enrolled host readiness without reading credential values.

The controller supplies inventory from its authenticated host helper and
credential_health rows for the receiving host. Missing attestations refuse
placement; this module does not log in, transfer secrets or contact a host.
"""
from dataclasses import dataclass
import math
import re
import time
from collections.abc import Mapping


_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


@dataclass(frozen=True)
class Requirements:
    host: str
    harness: str
    account: str = ""
    tools: tuple[str, ...] = ("gh", "gcloud")
    secret_refs: tuple[str, ...] = ()

    def __post_init__(self):
        if (not isinstance(self.account, str) or not isinstance(self.tools, tuple)
                or not isinstance(self.secret_refs, tuple)):
            raise ValueError("preflight requires an account name and tuples of references")
        names = (self.host, self.harness, *self.tools, *self.secret_refs)
        if self.account:
            names += (self.account,)
        if any(not isinstance(name, str) or not _REFERENCE.fullmatch(name) for name in names):
            raise ValueError("preflight requires enrolled names and secret references")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _fresh(record, now, max_age):
    stamp = record.get("checked_ts")
    return _number(stamp) and 0 <= now - stamp < max_age


def check(required, inventory, credentials, *, now=None, max_age=300):
    """Return readiness and named missing prerequisites, never raw diagnostics.

    `inventory`: host, checked_ts, connected, tools {name: available bool},
    secrets {reference: ready bool}. Credentials use credential_health's
    host/harness/account/state/checked_ts/expires_ts fields. gh and gcloud
    require a healthy default login as well as an available tool.
    """
    now = time.time() if now is None else now
    if not isinstance(required, Requirements) or not _number(now) or not _number(max_age) or max_age <= 0:
        raise ValueError("invalid host preflight requirements or freshness window")
    missing = []
    inventory = inventory if isinstance(inventory, Mapping) else {}
    usable = inventory.get("host") == required.host and _fresh(inventory, now, max_age)
    if not usable:
        missing.append("host_inventory:" + required.host)
    if not usable or inventory.get("connected") is not True:
        missing.append("host_connection:" + required.host)
    tools = inventory.get("tools", {}) if usable else {}
    secrets = inventory.get("secrets", {}) if usable else {}
    tools = tools if isinstance(tools, Mapping) else {}
    secrets = secrets if isinstance(secrets, Mapping) else {}
    for name in sorted(set(required.tools)):
        if tools.get(name) is not True:
            missing.append("tool:" + name)
    for reference in sorted(set(required.secret_refs)):
        if secrets.get(reference) is not True:
            missing.append("secret:" + reference)
    logins = {(required.harness, required.account)}
    logins.update((name, "") for name in required.tools if name in ("gh", "gcloud"))
    records = [row for row in (credentials or ()) if isinstance(row, Mapping)]
    for harness, account in sorted(logins):
        candidates = [row for row in records if row.get("host") == required.host
                      and row.get("harness") == harness and row.get("account") == account
                      and _number(row.get("checked_ts"))]
        stamp = max((row["checked_ts"] for row in candidates), default=None)
        newest = [row for row in candidates if row["checked_ts"] == stamp]
        # A database snapshot has one row per host/harness/account. If a
        # supplied history has tied reports, no arbitrarily chosen healthy
        # report may hide a simultaneous failure or revocation.
        latest = newest[0] if len(newest) == 1 else {}
        expiry = latest.get("expires_ts")
        if (latest.get("state") != "healthy" or not _fresh(latest, now, max_age)
                or (expiry is not None and (not _number(expiry) or expiry <= now))):
            missing.append("login:%s/%s" % (harness, account or "default"))
    return {"host": required.host, "ready": not missing, "missing": missing}
