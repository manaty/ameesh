# SPDX-License-Identifier: AGPL-3.0-only
"""Connection saturation warning shared by alerts and doctor (#653)."""
from . import db as db_mod

THRESHOLD_PERCENT = 70


def read(db):
    if getattr(db.cfg, 'backend', '') == 'mediated':
        return None
    from .storage.postgres import db_capacity
    return db_capacity.read(db)


def pressured(reading):
    # Equality belongs to the warning boundary; integer arithmetic avoids
    # rounding a server just below 70% into an alert.
    return reading['used'] * 100 >= reading['maximum'] * THRESHOLD_PERCENT


def line(reading):
    return '%d/%d client connections (%.1f%%, %d idle); warning at %d%%' % (
        reading['used'], reading['maximum'],
        100 * reading['used'] / reading['maximum'], reading['idle'], THRESHOLD_PERCENT)


def alerts(db):
    try:
        reading = read(db)
    except db_mod.DbError:
        return []  # The existing database availability diagnostics still apply.
    if reading is None or not pressured(reading):
        return []
    return [dict(schema='ameesh-alert/1', type='db_connections_high', agent=None,
                 since=None, value=reading['used'], threshold=THRESHOLD_PERCENT,
                 maximum=reading['maximum'], idle=reading['idle'],
                 detail=line(reading))]
