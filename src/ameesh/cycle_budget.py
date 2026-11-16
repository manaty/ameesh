# SPDX-License-Identifier: AGPL-3.0-only
"""Per-request credit gate (#7518), independent of provider and harness.

An adapter must supply a proven input-token upper bound, a server-enforced
output cap INCLUDING reasoning, and tariffs for EVERY allowed service tier.
Tool schemas/results/history count as input. Each billable attempt, including
SDK retries, compaction, titles and subagents, needs a fresh attempt id.

This library sends nothing itself. `execute` wraps one single-attempt transport
and consumes its complete result before settlement. An incomplete stream,
missing usage, cancellation or crash retains the full reservation. It is NOT
a claim that existing dsh adapters are guarded (see cycle-credit-gate.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import re

from . import storage

MICRO = Decimal(1_000_000)
MAX_MICRO = 2**63 - 1


class CreditRefused(RuntimeError):
    """No permission to dispatch a paid request."""


def micro(value, *, credit=False):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError('nonnegative finite USD required')
        units = int((amount * MICRO).to_integral_value(
            rounding=ROUND_FLOOR if credit else ROUND_CEILING))
        if units > MAX_MICRO:
            raise ValueError('USD amount too large')
        return units
    except (InvalidOperation, TypeError) as exc:
        raise ValueError('invalid USD amount') from exc


def instant(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('timestamp must include timezone')
    return parsed.isoformat()


def _api(db):
    api = getattr(storage.of(db), 'budgets', None)
    if api is None or not hasattr(api, 'cycle_reserve'):
        raise CreditRefused('cycle credit storage unavailable')
    return api


def attest(db, account, *, credit_usd, cap_usd, observed_at, expires_at,
           fresh_until, source, actor):
    """Install a nonsecret statement, authenticated by the CLI before this call.

    Re-uploading the same observation never resets the ledger. A strictly newer
    statement of NET remaining credit creates the next projection; pending
    attempts and late settlements carry over. Freshness is explicit and may
    be shorter than the credit's expiry.
    """
    if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,63}', account):
        raise ValueError('invalid billing account')
    if not source.strip() or not actor:
        raise ValueError('statement source and actor required')
    cap, credit = micro(cap_usd, credit=True), micro(credit_usd, credit=True)
    if cap > credit:
        raise ValueError('cap exceeds attested credit')
    observed, expires, fresh = map(instant, (observed_at, expires_at, fresh_until))
    if not datetime.fromisoformat(observed) < datetime.fromisoformat(fresh) <= datetime.fromisoformat(expires):
        raise ValueError('expected observed_at < fresh_until <= expires_at')
    return _api(db).cycle_put(account, cap, credit, observed, expires, fresh, source, actor)


@dataclass(frozen=True)
class RequestBound:
    input_tokens: int
    max_output_tokens: int
    # {service tier: (uncached input USD/million, output USD/million)}
    tariffs: dict
    # Bound of all other billable items, e.g. paid tool calls. Zero only if
    # the transport permits no such items; the adapter must enforce this.
    extra_usd: str = '0'

    def quote(self):
        for n in (self.input_tokens, self.max_output_tokens):
            if type(n) is not int or n < 0:
                raise ValueError('token bounds must be nonnegative integers')
        if self.max_output_tokens == 0 or not self.tariffs:
            raise ValueError('output cap and service-tier tariffs required')
        ins, outs = [], []
        for tier, rates in self.tariffs.items():
            if not isinstance(tier, str) or not tier or len(rates) != 2:
                raise ValueError('invalid service-tier tariff')
            a, b = (Decimal(str(v)) for v in rates)
            if not all(v.is_finite() and v > 0 for v in (a, b)):
                raise ValueError('positive uncached input and output prices required')
            ins.append(a)
            outs.append(b)
        extra = micro(self.extra_usd)
        # Take the maximum of EACH rate across tiers; cache discounts cannot
        # reduce admission. Reasoning must fit in the enforced output cap.
        usd = (self.input_tokens * max(ins) + self.max_output_tokens * max(outs)) / MICRO
        bound = micro(usd) + extra
        if bound <= 0 or bound > MAX_MICRO:
            raise ValueError('invalid request bound')
        return bound, dict(input_tokens=self.input_tokens, max_output_tokens=self.max_output_tokens,
                           input_usd_per_million=str(max(ins)),
                           output_usd_per_million=str(max(outs)), extra_micro=extra)


def reserve(db, account, agent, attempt_id, bound):
    """Commit a fresh liability BEFORE the adapter dispatches; never fail open."""
    try:
        units, pricing = bound.quote()
        return _api(db).cycle_reserve(account, agent, attempt_id, units, pricing)
    except Exception as exc:
        raise CreditRefused('request not admitted: %s' % exc) from exc


def settle(db, account, attempt_id, actual_usd):
    """None = uncertain; the full liability is kept, never reclaimed on timeout."""
    actual = None if actual_usd is None else micro(actual_usd)
    return _api(db).cycle_settle(account, attempt_id, actual)


def execute(db, account, agent, attempt_id, bound, *, daily_admit, send, actual_cost):
    """One complete attempt. The existing daily guard remains independently required.

    `daily_admit` must run the runner's daily admission; False refuses dispatch.
    `send` must disable implicit retries and consume the stream fully. It must
    enforce the bounds quoted above, including auxiliary charges. `actual_cost`
    returns validated total USD (all billable categories), or None when unknown.
    No automatic retry of admission, dispatch or settlement is performed.
    """
    if daily_admit() is not True:
        raise CreditRefused('daily budget admission refused')
    reserve(db, account, agent, attempt_id, bound)
    try:
        result = send()
        settle(db, account, attempt_id, actual_cost(result))
        return result
    except BaseException:
        try:
            settle(db, account, attempt_id, None)
        except Exception:
            pass  # The committed open reservation already counts in full.
        raise


def add_parser(sub):
    p = sub.add_parser('cycle', help='expiring credit envelope (per-request gate)')
    actions = p.add_subparsers(dest='cycle_command', required=True)
    put = actions.add_parser('attest', help='install a nonsecret credit statement (human only)')
    for name in ('account', 'credit-usd', 'cap-usd', 'observed-at', 'expires-at',
                 'fresh-until', 'source'):
        put.add_argument('--' + name, required=True)
    show = actions.add_parser('show', help='inspect the latest cycle and liabilities')
    show.add_argument('--account', required=True)


def command(cfg, db, args):
    import json
    from . import budget
    if args.cycle_command == 'show':
        print(json.dumps(_api(db).cycle_status(args.account), default=str, ensure_ascii=False))
        return 0
    actor = budget.authorize(cfg, db)
    cycle = attest(db, args.account, credit_usd=args.credit_usd, cap_usd=args.cap_usd,
                   observed_at=args.observed_at, expires_at=args.expires_at,
                   fresh_until=args.fresh_until, source=args.source, actor=actor)
    print('Credit cycle #%s recorded for %s by %s' % (cycle, args.account, actor))
    return 0
