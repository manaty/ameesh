# SPDX-License-Identifier: AGPL-3.0-only
"""La fonction : requête HTTP (format Scaleway Functions) → réponse JSON.

Ordre des contrôles, du moins cher au plus cher : méthode et origine (CORS),
taille du corps, forme et taille de la question, débit global puis par adresse,
plafond de dépense (réservation), recherche, appel du modèle, compte réel.

**Rien n'est conservé ni journalisé du contenu** : la question, l'historique et
la réponse ne vivent que le temps de la requête. Le journal reçoit une ligne
JSON par requête avec des champs fixes — code, statut, durée, jetons, coût —
jamais un texte venu du visiteur ou du modèle, ni une adresse.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field

from . import prompt, transport
from .guard import CapReached, Pricing, RateLimiter, SpendGuard, input_token_bound
from .search import Hit, Index, select
from .store import MemoryStore, S3Store, StoreError

DOCS_URL = prompt.DOCS_URL
MAX_BODY_BYTES = 16_000
#: meilleur score sous lequel la vue d'ensemble est ajoutée en tête
WEAK_SCORE = 2.5
_URL = re.compile(r"https://[^\s<>()\[\]\"'`]+")
REFUSAL = ("Sorry, I can only answer questions about ameesh. "
           "The documentation is at " + DOCS_URL)


def _windows(spec: str) -> list[tuple[float, int]]:
    """`"5/60,30/3600"` → `[(60, 5), (3600, 30)]` (maximum / secondes)."""
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        count, seconds = part.split("/", 1)
        out.append((float(seconds), int(count)))
    return out


@dataclass
class Config:
    api_key: str = ""
    api_base: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    allowed_origins: tuple = ("https://ameesh.manaty.net",)
    monthly_cap_usd: float = 20.0
    daily_cap_usd: float | None = None
    max_requests_per_day: int = 2000
    pricing: Pricing = field(default_factory=Pricing)
    max_question_chars: int = 500
    context_budget_tokens: int = 7000
    max_output_tokens: int = 700
    rate_per_ip: str = "5/60,30/3600,80/86400"
    rate_global: str = "30/60,600/3600"
    client_ip_hops: int = 1
    model_timeout: float = 25.0

    def __post_init__(self):
        # bornes entières strictement positives : sinon la borne de coût ne tient plus
        for name, top in (("max_question_chars", 4000), ("context_budget_tokens", 100_000),
                          ("max_output_tokens", 8192), ("max_requests_per_day", 10**7)):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= top:
                raise ValueError(name)

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = os.environ if env is None else env

        def num(name, default, kind=float):
            raw = env.get(name)
            return kind(raw) if raw not in (None, "") else default

        origins = env.get("ALLOWED_ORIGINS", "https://ameesh.manaty.net")
        return cls(
            api_key=env.get("DEEPSEEK_API_KEY", ""),
            api_base=env.get("CHAT_API_BASE", cls.api_base),
            model=env.get("CHAT_MODEL", cls.model),
            allowed_origins=tuple(o.strip().rstrip("/") for o in origins.split(",") if o.strip()),
            monthly_cap_usd=num("MONTHLY_CAP_USD", 20.0),
            daily_cap_usd=num("DAILY_CAP_USD", None),
            max_requests_per_day=num("MAX_REQUESTS_PER_DAY", 2000, int),
            pricing=Pricing(num("PRICE_INPUT_MISS_PER_M", 0.27), num("PRICE_INPUT_HIT_PER_M", 0.07),
                            num("PRICE_OUTPUT_PER_M", 1.10)),
            max_question_chars=num("MAX_QUESTION_CHARS", 500, int),
            context_budget_tokens=num("CONTEXT_BUDGET_TOKENS", 7000, int),
            max_output_tokens=num("MAX_OUTPUT_TOKENS", 700, int),
            rate_per_ip=env.get("RATE_PER_IP", cls.rate_per_ip),
            rate_global=env.get("RATE_GLOBAL", cls.rate_global),
            client_ip_hops=num("CLIENT_IP_HOPS", 1, int),
            model_timeout=num("CHAT_TIMEOUT_S", 25.0),
        )


def store_from_env(env=None):
    env = os.environ if env is None else env
    backend = env.get("STATE_BACKEND", "s3")
    if backend == "memory":
        return MemoryStore()
    if backend != "s3":
        raise ValueError("STATE_BACKEND")
    region = env.get("STATE_S3_REGION", "fr-par")
    return S3Store(
        endpoint=env.get("STATE_S3_ENDPOINT", f"https://s3.{region}.scw.cloud"),
        bucket=env["STATE_BUCKET"], key=env.get("STATE_KEY", "chatbot/spend.json"),
        region=region, access_key=env["STATE_ACCESS_KEY"], secret_key=env["STATE_SECRET_KEY"],
    )


def deepseek_client(config: Config, open_with=None):
    """Client compatible OpenAI (chat completions), sans redirection, HTTPS."""
    url = config.api_base.rstrip("/") + "/chat/completions"
    transport.check_endpoint(url)

    def call(messages: list[dict], max_tokens: int) -> tuple[int, object]:
        payload = {"model": config.model, "messages": messages, "max_tokens": max_tokens,
                   "temperature": 0.2, "stream": False}
        return transport.post_json(url, payload, timeout=config.model_timeout, open_with=open_with,
                                   headers={"Authorization": "Bearer " + config.api_key})
    return call


def log_event(**fields) -> None:
    """Une ligne JSON, champs fixes et non textuels seulement."""
    allowed = {"evt", "status", "code", "ms", "in_tok", "out_tok", "usd", "passages"}
    line = {k: v for k, v in fields.items() if k in allowed}
    print(json.dumps(line, sort_keys=True), file=sys.stdout, flush=True)


class App:
    def __init__(self, config: Config, index: Index, store, model_call=None, *,
                 clock=time.time, monotonic=time.monotonic, log=log_event):
        self.config = config
        self.index = index
        self.model_call = model_call or deepseek_client(config)
        self.spend = SpendGuard(store, monthly_cap=config.monthly_cap_usd,
                                daily_cap=config.daily_cap_usd,
                                max_requests_per_day=config.max_requests_per_day, clock=clock)
        self.per_ip = RateLimiter(_windows(config.rate_per_ip), clock=monotonic)
        self.global_rate = RateLimiter(_windows(config.rate_global), clock=monotonic)
        self.canary = prompt.new_canary()
        self.log = log
        self.monotonic = monotonic

    # -- HTTP ---------------------------------------------------------------
    def _cors(self, origin: str | None) -> dict:
        headers = {"Vary": "Origin", "Cache-Control": "no-store",
                   "X-Content-Type-Options": "nosniff"}
        if origin and origin.rstrip("/") in self.config.allowed_origins:
            headers.update({
                "Access-Control-Allow-Origin": origin.rstrip("/"),
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "600",
            })
        return headers

    def _reply(self, status: int, body: dict | None, origin, *, extra: dict | None = None) -> dict:
        headers = self._cors(origin)
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        headers.update(extra or {})
        return {"statusCode": status, "headers": headers,
                "body": json.dumps(body, ensure_ascii=False) if body is not None else ""}

    def client_ip(self, headers: dict) -> str:
        chain = [p.strip() for p in headers.get("x-forwarded-for", "").split(",") if p.strip()]
        hops = max(self.config.client_ip_hops, 1)
        if len(chain) >= hops:
            return chain[-hops]
        return headers.get("x-real-ip", "") or "unknown"

    def handle(self, event: dict) -> dict:
        started = self.monotonic()
        try:
            response, fields = self._handle(event)
        except Exception as error:  # noqa: BLE001 - jamais de trace (elle pourrait citer la question)
            response = {"statusCode": 500, "headers": {"Cache-Control": "no-store"},
                        "body": json.dumps({"error": "internal", "docs": DOCS_URL})}
            fields = {"code": "internal_" + type(error).__name__}
        fields.setdefault("evt", "chat")
        fields["status"] = response["statusCode"]
        fields["ms"] = int((self.monotonic() - started) * 1000)
        self.log(**fields)
        return response

    def _handle(self, event: dict) -> tuple[dict, dict]:
        headers = {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}
        origin = headers.get("origin")
        allowed = bool(origin) and origin.rstrip("/") in self.config.allowed_origins
        method = (event.get("httpMethod") or "").upper()
        unavailable = {"error": "unavailable", "docs": DOCS_URL}

        if method == "OPTIONS":
            return self._reply(204 if allowed else 403, None, origin), {"code": "preflight"}
        if method != "POST":
            return self._reply(405, {"error": "method"}, origin, extra={"Allow": "POST, OPTIONS"}), {"code": "method"}
        if not allowed:
            return self._reply(403, {"error": "origin"}, origin), {"code": "origin"}

        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            try:
                raw = base64.b64decode(raw).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return self._reply(400, {"error": "bad_request"}, origin), {"code": "bad_body"}
        if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > MAX_BODY_BYTES:
            return self._reply(413, {"error": "too_large"}, origin), {"code": "too_large"}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return self._reply(400, {"error": "bad_request"}, origin), {"code": "bad_json"}
        question = data.get("question") if isinstance(data, dict) else None
        if not isinstance(question, str) or not question.strip():
            return self._reply(400, {"error": "bad_request"}, origin), {"code": "no_question"}
        question = question.strip()
        if len(question) > self.config.max_question_chars:
            return (self._reply(400, {"error": "too_long", "max": self.config.max_question_chars}, origin),
                    {"code": "too_long"})
        history = prompt.clean_history(data.get("history"))

        wait = self.global_rate.check("*")
        if wait:
            return (self._reply(429, {"error": "busy", "retry_after": int(wait) + 1}, origin,
                                extra={"Retry-After": str(int(wait) + 1)}), {"code": "rate_global"})
        wait = self.per_ip.check(self.client_ip(headers))
        if wait:
            return (self._reply(429, {"error": "rate_limited", "retry_after": int(wait) + 1}, origin,
                                extra={"Retry-After": str(int(wait) + 1)}), {"code": "rate_ip"})

        # contexte : la question, plus la question précédente à poids réduit
        previous = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        hits = self.index.search(question, context=previous)
        passages = select(hits, budget_tokens=self.config.context_budget_tokens)
        if not hits or hits[0].score < WEAK_SCORE:
            # question vague (« c'est quoi ameesh ? ») : la vue d'ensemble d'abord
            overview = [p for p in self.index.fallback() if p not in passages]
            passages = select([Hit(p, 1.0) for p in overview + passages],
                              budget_tokens=self.config.context_budget_tokens)
        messages = prompt.messages(question, passages, history, self.canary)
        # BORNE (pas estimation) : octets UTF-8 de tout le texte envoyé + gabarit ;
        # la sortie est bornée par `max_tokens`, imposé à l'API
        input_bound = input_token_bound(messages)
        ceiling = self.config.pricing.ceiling(input_bound, self.config.max_output_tokens)

        try:
            rid = self.spend.reserve(ceiling)
        except CapReached as cap:
            reason = "closed" if cap.code == "closed" else "cap"
            return self._reply(503, dict(unavailable, reason=reason), origin), {"code": cap.code}
        except StoreError as error:
            return self._reply(503, unavailable, origin), {"code": error.code}

        try:
            status, answer = self.model_call(messages, self.config.max_output_tokens)
        except transport.TransportError as error:
            # la requête a pu partir : on compte la réservation, prudemment
            return self._finish_error(rid, ceiling, origin, "upstream_" + error.code)
        if 400 <= status < 500 and status != 429:
            # requête refusée par le fournisseur (clé, modèle, format) : non facturée
            code = f"upstream_{status}"
            try:
                self.spend.release(rid)
            except StoreError as error:
                code += "_" + error.code   # la réservation durable reste comptée
            return self._reply(502, {"error": "upstream", "docs": DOCS_URL}, origin), {"code": code}
        if status != 200 or not isinstance(answer, dict):
            return self._finish_error(rid, ceiling, origin, f"upstream_{status}")

        usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
        cost = self.config.pricing.cost(usage)
        over = cost is not None and self.config.pricing.over_bound(usage, input_bound,
                                                                   self.config.max_output_tokens)
        code = "ok" if cost is not None else "ok_usage_invalid"
        if over:
            code = "usage_over_bound"   # contrat ou barème violé : coût connu compté, admissions fermées
        fields = {"code": code, "passages": len(passages),
                  "in_tok": _count(usage.get("prompt_tokens")),
                  "out_tok": _count(usage.get("completion_tokens"))}
        try:
            fields["usd"] = round(self.spend.commit(rid, ceiling, cost,
                                                    close="usage_over_bound" if over else None), 6)
        except StoreError as error:
            fields["code"] = "ok_" + error.code
        try:
            text = answer["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            text = None
        if not isinstance(text, str) or not text.strip():
            fields["code"] = "empty"
            return self._reply(502, {"error": "upstream", "docs": DOCS_URL}, origin), fields
        text = text.strip()
        if prompt.leaked(text, self.canary):
            fields["code"] = "leak_blocked"
            text = REFUSAL
        return self._reply(200, {"answer": text, "sources": self.sources(text, passages)}, origin), fields

    def _finish_error(self, rid: str, reserved: float, origin, code: str) -> tuple[dict, dict]:
        fields = {"code": code}
        try:
            fields["usd"] = round(self.spend.commit(rid, reserved, None), 6)
        except StoreError as error:
            fields["code"] = code + "_" + error.code
        return self._reply(502, {"error": "upstream", "docs": DOCS_URL}, origin), fields

    @staticmethod
    def sources(answer: str, passages) -> list[dict]:
        """Les pages fournies que la réponse cite (lien exact, avec ou sans ancre)."""
        cited = {u.rstrip(".,;:!?") for u in _URL.findall(answer)}
        out, seen = [], set()
        for p in passages:
            page = p.url.split("#", 1)[0]
            if page in seen or not ({p.url, page} & cited):
                continue
            seen.add(page)
            out.append({"title": p.title, "url": p.url if p.url in cited else page})
        return out[:5]


def _count(value) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def load_index(path: str) -> Index:
    with open(path, encoding="utf-8") as fh:
        return Index.from_json(json.load(fh))
