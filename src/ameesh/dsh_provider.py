# SPDX-License-Identifier: AGPL-3.0-only
"""#7519: explicit dsh Responses route and one-attempt credit boundary.

No credentials are discovered here. The launcher supplies the selected account's
key, held only by the broker; dsh receives a short-lived loopback credential.
Every HTTP retry is a new request/credit reservation. No SDK is used upstream.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import http.client
import json
import re
import secrets
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import cycle_budget


class ProviderError(ValueError):
    pass


def _key_reference(key):
    """Require a dedicated destination, never an automatic credential route."""
    from . import accounts, harnesses
    from .platform import child_environment
    reserved = (accounts._all_account_keys('key_env')
                | accounts._all_account_keys('config_env')
                | set().union(*child_environment.TOOL_CREDENTIALS.values())
                | set().union(*(child_environment.harness({}, family).credentials
                                for family in harnesses.known_ids()))
                | set(child_environment.AUDITOR_CREDENTIALS)
                | {'HOME', 'PATH', 'TMPDIR', 'USER', 'SHELL', 'PYTHONPATH',
                   'LD_PRELOAD', 'LD_LIBRARY_PATH', 'NODE_OPTIONS',
                   'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'})
    if (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key)
            or key.upper() in reserved):
        raise ProviderError('credential destination must be a dedicated environment reference')
    return key


@dataclass(frozen=True)
class Provider:
    name: str
    endpoint: str
    key_env: str
    billing_account: str
    models: dict


def parse(raw):
    if not isinstance(raw, dict) or not raw:
        raise ProviderError('dsh_provider must be a nonempty object')
    if set(raw) != {'provider', 'endpoint', 'key_env', 'billing_account', 'models'}:
        raise ProviderError('dsh_provider requires provider, endpoint, key_env, billing_account, models only')
    name = raw['provider']
    if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', name):
        raise ProviderError('invalid provider route name')
    endpoint = raw['endpoint']
    url = urllib.parse.urlsplit(endpoint)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.query or url.fragment):
        raise ProviderError('provider endpoint must be credential-free HTTPS')
    key = _key_reference(raw['key_env'])
    account = raw['billing_account']
    if not isinstance(account, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,63}', account):
        raise ProviderError('invalid billing account')
    models = raw['models']
    if not isinstance(models, dict) or not models:
        raise ProviderError('explicit allowed models and tariffs required')
    clean = {}
    for model, entry in models.items():
        if (not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}', model)
                or not isinstance(entry, dict)):
            raise ProviderError('invalid model')
        if (set(entry) - {'context_tokens', 'max_output_tokens', 'tariffs', 'source', 'reasoning_efforts', 'response_models'}
                or not {'context_tokens', 'max_output_tokens', 'tariffs', 'source'} <= set(entry)):
            raise ProviderError('model requires context_tokens, max_output_tokens, tariffs, source')
        # context_tokens is the provider-enforced maximum input context, not a
        # character-count estimate. Its dated authority must be supplied locally.
        if not isinstance(entry['source'], str) or not entry['source'].strip():
            raise ProviderError('context limit and tariff evidence required')
        aliases = entry.get('response_models', [])
        if (not isinstance(aliases, list) or any(
                not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}', alias)
                for alias in aliases)):
            raise ProviderError('response_models must list exact model identities at these tariffs')
        for field in ('context_tokens', 'max_output_tokens'):
            if type(entry[field]) is not int or not 0 < entry[field] <= 10_000_000:
                raise ProviderError('invalid model token bound')
        try:
            for rates in entry['tariffs'].values():
                if len(rates) != 3:
                    raise ValueError('input/cache/output tariffs required')
                cached = Decimal(str(rates[1]))
                if not cached.is_finite() or not 0 <= cached <= Decimal(str(rates[0])):
                    raise ValueError('invalid cached input tariff')
            cycle_budget.RequestBound(entry['context_tokens'], entry['max_output_tokens'],
                                      {tier: (rates[0], rates[2]) for tier, rates in entry['tariffs'].items()}).quote()
        except Exception as exc:
            raise ProviderError('invalid model tariffs or bounds') from exc
        clean[model] = dict(entry)
    return Provider(name, endpoint.rstrip('/'), key, account, clean)


def configured(cfg, profile=None):
    raw = getattr(cfg, 'dsh_provider', None)
    if profile is not None:
        for item in (getattr(cfg, 'accounts', {}) or {}).get('deepseek', ()):
            if item.get('name') == getattr(profile, 'name', '') and 'dsh_provider' in item:
                raw = item['dsh_provider']
                break
    if raw is None or raw == {}:
        return None
    route = parse(raw)
    if profile is not None and route.billing_account != getattr(profile, 'billing_account', ''):
        raise ProviderError('dsh provider and selected account must share billing_account')
    return route


# Direct DeepSeek web routes and implicit title generation are deliberately off.
# Subagents and compaction keep the default LLM route, hence the same broker.
DISABLED = ('llm-deepseek', 'llm-deepseek-account', 'deepseek-account',
            'web-search-deepseek', 'web-deepseek-api', 'session-title-llm', 'credentials')


def patch_document(route, model, effort, base, key_env):
    if model not in route.models:
        raise ProviderError('model not allowed by selected dsh provider')
    entries = [{'id': ident, 'disabled': True} for ident in DISABLED]
    entries += [
        {'id': 'agent-default-model', 'config': {
            'provider': route.name, 'model': model, 'reasoningEffort': effort}},
        {'id': 'llm-pi-ai', 'config': {'providers': {route.name: {
            'api': 'openai-responses', 'baseURL': base, 'apiKeyEnv': key_env,
            'retryPolicy': {'mode': 'normal', 'maxRetries': 0},
            'models': [{'id': name, 'contextWindow': spec['context_tokens'],
                        'maxTokens': spec['max_output_tokens'], 'input': ['text'],
                        **({'reasoningEfforts': spec['reasoning_efforts']} if 'reasoning_efforts' in spec else {})}
                       for name, spec in route.models.items()]}}}},
    ]
    return entries


# Only text, local function tools and inline history: no paid server tools,
# hidden retained context, media, background jobs or additional REST endpoints.
FIELDS = {'model', 'input', 'instructions', 'tools', 'tool_choice',
          'parallel_tool_calls', 'reasoning', 'text', 'temperature', 'top_p',
          'max_output_tokens', 'service_tier', 'stream', 'store', 'metadata',
          'prompt_cache_key', 'safety_identifier', 'truncation', 'include'}


def prepare(route, body):
    if not isinstance(body, dict) or set(body) - FIELDS:
        raise ProviderError('unsupported Responses request field')
    if body.get('include', []) not in ([], ['reasoning.encrypted_content']):
        raise ProviderError('unsupported response expansion')
    spec = route.models.get(body.get('model'))
    if spec is None:
        raise ProviderError('model not allowed')
    if body.get('truncation', 'disabled') != 'disabled' or body.get('store', False):
        raise ProviderError('retained or truncated context is not supported')
    for tool in body.get('tools') or ():
        if not isinstance(tool, dict) or tool.get('type') != 'function':
            raise ProviderError('only local function tools are admitted')
    def check(value):
        if isinstance(value, dict):
            kind = value.get('type', '')
            if any(word in str(kind) for word in ('image', 'audio', 'video', 'file', 'computer', 'search')):
                raise ProviderError('paid media and server tools are not admitted')
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    check(body.get('input'))
    tier = body.get('service_tier', 'default')
    if tier not in spec['tariffs']:
        raise ProviderError('service tier has no declared tariff')
    cap = body.get('max_output_tokens', spec['max_output_tokens'])
    if type(cap) is not int or not 0 < cap <= spec['max_output_tokens']:
        raise ProviderError('output cap outside declared bound')
    outgoing = dict(body, max_output_tokens=cap, service_tier=tier,
                    store=False, truncation='disabled')
    bound = cycle_budget.RequestBound(spec['context_tokens'], cap,
                                      {tier: (rates[0], rates[2]) for tier, rates in spec['tariffs'].items()})
    return outgoing, bound


def response_cost(route, requested, response):
    """Complete cumulative usage only; reasoning is INCLUDED in output_tokens.

    Exact cached input usage is required; no guessed discount or cost.
    The dated provider statement remains the authority for reconciliation.
    """
    if not isinstance(response, dict) or response.get('status') not in ('completed', 'incomplete'):
        return None
    spec = route.models[requested['model']]
    if response.get('model') not in [requested['model'], *spec.get('response_models', [])]:
        return None
    tier = response.get('service_tier', requested['service_tier'])
    if not isinstance(tier, str):
        return None
    rates = spec['tariffs'].get(tier)
    usage = response.get('usage')
    if not rates or not isinstance(usage, dict):
        return None
    counts = [usage.get('input_tokens'), usage.get('output_tokens')]
    if any(type(n) is not int or n < 0 for n in counts):
        return None
    details = usage.get('input_tokens_details')
    if not isinstance(details, dict):
        return None
    cached = details.get('cached_tokens')
    if type(cached) is not int or not 0 <= cached <= counts[0]:
        return None
    return ((counts[0] - cached) * Decimal(str(rates[0]))
            + cached * Decimal(str(rates[1])) + counts[1] * Decimal(str(rates[2]))) / 1_000_000


def terminal(raw, stream):
    if not stream:
        return json.loads(raw)
    final = None
    for line in raw.splitlines():
        if line.startswith(b'data:'):
            data = line[5:].strip()
            if data == b'[DONE]':
                continue
            event = json.loads(data)
            if event.get('type') in ('response.completed', 'response.incomplete'):
                final = event.get('response')
    return final


def transport(route, key, body):
    """Exactly one HTTP exchange, no redirects, discovery or retries."""
    url = urllib.parse.urlsplit(route.endpoint)
    conn = http.client.HTTPSConnection(url.hostname, url.port, timeout=300)
    try:
        payload = json.dumps(body).encode()
        conn.request('POST', url.path.rstrip('/') + '/responses', payload,
                     {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        reply = conn.getresponse()
        raw = reply.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024:
            raise ProviderError('provider response exceeds bound')
        # Never return provider error bodies (may contain a credential or prompt).
        if reply.status != 200:
            raise ProviderError('provider request did not complete')
        return raw, reply.getheader('Content-Type', 'application/json')
    finally:
        conn.close()


class Broker:
    """Turn-scoped, serialized loopback endpoint. The real key never reaches dsh.

    `database` opens a fresh owning connection per attempt. `daily_admit` and
    `record` are production callbacks, never optional success defaults.
    """
    def __init__(self, route, key, *, database, agent, daily_admit, record, daily_finish, send=transport):
        if not key:
            raise ProviderError('selected provider credential is absent')
        self.route, self._key = route, key
        self.database, self.agent = database, agent
        self.daily_admit, self.record, self.send = daily_admit, record, send
        self.daily_finish = daily_finish
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.failed = False
        self.closed = False
        broker = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                try:
                    if self.path != '/v1/responses' or self.headers.get('Authorization') != 'Bearer ' + broker.token:
                        raise ProviderError('route refused')
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 8 * 1024 * 1024:
                        raise ProviderError('request size refused')
                    raw = self.rfile.read(length)
                    body = json.loads(raw)
                    data, content_type = broker.request(body)
                    self.send_response(200)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception:
                    self.send_error(503, 'guarded provider request refused')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self):
        return 'http://127.0.0.1:%d/v1' % self.server.server_port

    def start(self):
        with self.lock:
            if self.closed:
                raise ProviderError('broker is closed')
            self.thread.start()
        return self

    def close(self):
        with self.lock:
            self.closed = True
            self._key = ''
        if self.thread.ident is not None:
            self.server.shutdown()
        self.server.server_close()

    def request(self, body):
        outgoing, bound = prepare(self.route, body)
        with self.lock:
            if self.closed:
                raise ProviderError('broker is closed')
            if self.failed:
                raise ProviderError('broker accounting unavailable')
            db = self.database()
            attempt = uuid.uuid4().hex
            response = None
            actual = None
            dispatched = False
            recorded = False
            def send():
                nonlocal response, actual, dispatched
                dispatched = True
                result = self.send(self.route, self._key, outgoing)
                try:
                    response = terminal(result[0], outgoing.get('stream', False))
                    actual = response_cost(self.route, outgoing, response)
                except (ValueError, TypeError):
                    pass
                return result
            try:
                result = cycle_budget.execute(
                    db, self.route.billing_account, self.agent, attempt, bound,
                    daily_admit=lambda: self.daily_admit(db, attempt, bound), send=send,
                    actual_cost=lambda _: actual)
                # Unknown usage is charged at the reserved maximum until an
                # operator proves its cost. Failure to write closes this broker.
                self.record(db, attempt, outgoing, response,
                            actual if actual is not None else Decimal(bound.quote()[0]) / 1_000_000)
                recorded = True
                return result
            except BaseException:
                self.failed = True
                if dispatched and not recorded:
                    try:
                        self.record(db, attempt, outgoing, response,
                                    actual if actual is not None else Decimal(bound.quote()[0]) / 1_000_000)
                    except Exception:
                        pass  # cycle liability remains durable; broker stays closed
                raise
            finally:
                try:
                    self.daily_finish(db, attempt, dispatched)
                finally:
                    db.close()


def provider_argv(argv, path):
    """Last launcher overlay, before the native dsh app starts parsing."""
    try:
        boundary = argv.index('--json')
    except ValueError as exc:
        raise ProviderError('configured provider requires native dsh JSON launch') from exc
    return [*argv[:boundary], '--patch', path, *argv[boundary:]]


def launch(worker, route, profile, env, argv, model, effort, turn_id):
    """Wire the broker to real daily admission and the existing cost ledger.

    Called inside the runner's launch/finally scope. No production key or
    account is created by this feature; configuring/activating it is separate.
    """
    from . import cost, db as db_mod, resident_runner, storage
    from .platform import child_environment
    if getattr(worker.runner, 'mediated', False):
        raise ProviderError('configured dsh providers require their owning executor')
    if profile is None or profile.kind != 'api_key_env' or profile.env != route.key_env:
        raise ProviderError('configured dsh route requires a matching selected api_key_env account')
    key = env.pop(route.key_env, '')
    if not key:
        raise ProviderError('provider credential absent from owning executor environment')
    def daily(db, attempt, bound):
        if worker.lease_lost.is_set():
            return False
        limits = worker.runner.budget_limits(db)
        book = cost.CostBook(state_dir=worker.cfg.state_dir, db=db,
                             tools={worker.name: 'deepseek'}, **limits.book_kwargs())
        if book.over(worker.name, pace=False, account_checked=True):
            return False
        # A separate request identity preserves the runner's still-open turn
        # reservation. Each retry has a distinct ledger key. The same account
        # advisory lock serializes admission across hosts.
        admitted = storage.of(db).budgets.account_reserve(
            route.billing_account, worker.name + ':dsh-request',
            bound.quote()[0] / 1_000_000, 'dsh-request:' + attempt)
        return admitted.get('ok') is True
    def record(db, attempt, request, response, amount):
        usage = response.get('usage') if isinstance(response, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get('input_tokens_details')
        details = details if isinstance(details, dict) else {}
        def count(value):
            return value if type(value) is int and value >= 0 else 0
        with db.transaction() as tx:
            storage.of(tx).turn_costs.insert(
                agent=worker.name, harness='deepseek', turn='provider-response',
                model=request['model'], session=None, usd=float(amount),
                input_tokens=count(usage.get('input_tokens')),
                cached_input_tokens=min(count(details.get('cached_tokens')), count(usage.get('input_tokens'))),
                output_tokens=count(usage.get('output_tokens')), cum_usd=None,
                cum_input_tokens=None, cum_cached_input_tokens=None,
                cum_output_tokens=None, account=profile.name,
                spend_key='dsh-request:' + attempt, source='relay',
                turn_id=turn_id,
                price_fallback=None if response_cost(route, request, response) is not None
                else 'unknown request cost: full reserved bound pending reconciliation')
            storage.of(tx).budgets.account_settle('dsh-request:' + attempt,
                                                  state='settled', actor=worker.name)
    def daily_finish(db, attempt, dispatched):
        if not dispatched:
            # Proven no dispatch: closing a zero-cost daily reservation is
            # distinct from forgiving an uncertain provider attempt.
            storage.of(db).budgets.account_settle('dsh-request:' + attempt,
                                                  state='released', actor=worker.name)
    broker = Broker(route, key, database=lambda: db_mod.open_db(worker.cfg),
                    agent=worker.name, daily_admit=daily, record=record, daily_finish=daily_finish)
    try:
        path = worker._path('provider.patch.yml')
        # JSON is a YAML subset; unlike template interpolation it cannot inject
        # extra Cordis entries through a model, endpoint or effort string.
        import os
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(patch_document(route, model, effort or 'max', broker.base,
                                     route.key_env), handle)
        if not resident_runner.write_usage_event(worker._path('events.jsonl'),
                                                 {'type': 'dsh.provider-ledger', 'turn_id': turn_id}):
            raise ProviderError('provider accounting marker unavailable')
        # No provider secret reaches dsh or its subprocesses. The loopback
        # credential grants only the finite model/tariff policy above.
        clean = child_environment.for_child(None, env)
        clean[route.key_env] = broker.token
        env.clear()
        env.update(clean)
        command = provider_argv(argv, path)
        broker.start()
        # Launcher options must precede app arguments; after the prompt they
        # are interpreted by the headless app rather than the dsh launcher.
        return broker, command
    except BaseException:
        broker.server.server_close()
        raise
