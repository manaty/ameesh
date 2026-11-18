# SPDX-License-Identifier: AGPL-3.0-only
"""The `nexlink` channel of `ameesh notify` (#7793, #7637 tranche 3b).

The channel (#3536, owner decision #138440) pushes an alert into the owner's
Nexlink app through a signed HMAC request; the content is encrypted to the
account's X25519 key-wrap key (generation 1) and wrapped per active key, so
the hub only stores the envelope (:mod:`ameesh.modules.nexlink.alerts`).
Without an active key the alert is sent *content-free* (the app shows "mesh
alert" and opens the console): no detail leaves the mesh. The optional
`fallback` names an `ntfy`/`slack` channel that receives the same text,
prefixed "repli nexlink —", after the second consecutive failure (or at once
on a permanent error), once per alert; the primary keeps being retried, and a
fallback success does not mark the alert delivered on the Nexlink side.

This is a notification channel provider of the built-in `nexlink` tooling
module: the core reaches it through the Module interface
(``registry.notify_channel``), never by importing this package. The bare
profile (``AMEESH_MODULES=bare``) has no built-in module, hence no `nexlink`
channel.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse

from ... import notify as core
from . import alerts

#: Notification channel kinds this module provides, read by
#: ``registry.notify_channel``.
KINDS = ("nexlink",)

#: Options admitted for the kind, read by the core's configuration parser.
OPTIONS = {"nexlink": ("type", "timeout", "url", "token_env", "token_file",
                       "fallback")}

#: #3383: the kind reaches a human from ANY host — the alerts role timer
#: pushes from whichever candidate holds the lease.
REMOTE_PUSH = True

#: #3536: the kind carries a declared `fallback` channel: a remote push that
#: receives the same text when the primary keeps failing. Never itself (no
#: chain, no coverage recursion).
FALLBACK = True
FALLBACK_KINDS = ("ntfy", "slack")
FALLBACK_PREFIX = "repli nexlink — "

#: Lifetime of the key-wrap key cache (`GET /mesh/alerts/keys`).
KEYS_TTL_S = 3600.0

_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def validate(name, raw):
    """Kind-specific checks of one `nexlink` declaration (#3536).

    The core has already refused a plaintext secret, an unknown option and a
    wrongly named secret variable; the URL and the required secret belong to
    the channel."""
    if not raw.get("url"):
        raise core.NotifyConfigError("notify.channels.%s: `url` is required" % name)
    core.check_url(str(raw["url"]), "notify.channels.%s.url" % name)
    parts = urllib.parse.urlsplit(str(raw["url"]))
    if parts.query or parts.fragment:
        raise core.NotifyConfigError(
            "notify.channels.%s.url: query or fragment not allowed (the "
            "channel appends `/mesh/alerts`)" % name)
    if not raw.get("token_env") and not raw.get("token_file"):
        raise core.NotifyConfigError(
            "notify.channels.%s: name the shared secret (`token_env` or "
            "`token_file`), never a value in the configuration" % name)


def _state(sender):
    """Per-`Sender` provider state (the key-wrap cache), never global."""
    return sender.channel_state.setdefault("nexlink", {})


def _secret(sender, channel):
    """The named HMAC secret; never a value from the configuration."""
    secret = core._secret(channel, sender.env, "token_env", "token_file", None, True)
    return core.check_secret_value(secret, "channel %s: mesh secret" % channel.name)


def _key_id(sender):
    """The HMAC identity of the emitter (`AMEESH_NEXLINK_KEY_ID`, else v1)."""
    value = str(sender.env.get("AMEESH_NEXLINK_KEY_ID")
                or alerts.MESH_ALERT_KEY_ID)
    # the value enters the signed string AND a header: no whitespace, no
    # newline, no field separator
    if not _KEY_ID_RE.match(value):
        raise core.ChannelError("AMEESH_NEXLINK_KEY_ID: invalid key id",
                               permanent=True)
    return value


def signed_call(sender, channel, method, path, payload, timeout):
    """A signed request to the Nexlink API; returns the 2xx body."""
    url = str(channel.options["url"]).rstrip("/") + path
    body = (b"" if payload is None else
            json.dumps(payload, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8"))
    secret = _secret(sender, channel)
    headers = alerts.mesh_headers(secret, body, key_id=_key_id(sender))
    headers["Accept"] = "application/json"
    if payload is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    _, raw = core._request(url, body if method != "GET" else None, headers, timeout,
                           method=method, show_url=True, secrets=(secret,))
    return raw


def _json_object(raw, what):
    try:
        data = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise core.ChannelError("%s: invalid JSON response" % what,
                                permanent=True) from None
    if not isinstance(data, dict):
        raise core.ChannelError("%s: JSON object expected" % what, permanent=True)
    return data


def _keys_for(sender, channel, timeout):
    """The active key-wrap keys of the owner account (1 h cache)."""
    url = str(channel.options["url"]).rstrip("/") + "/mesh/alerts/keys"
    cache = _state(sender).setdefault("keys", {})
    cached = cache.get(url)
    now = time.time()
    if cached and now - cached[0] < KEYS_TTL_S:
        return cached[1]
    raw = signed_call(sender, channel, "GET", "/mesh/alerts/keys", None, timeout)
    data = _json_object(raw, "mesh alerts keys")
    account = data.get("account_id")
    try:
        alerts.uuid_bytes(account)
    except ValueError:
        raise core.ChannelError("mesh alerts keys: invalid account_id",
                                permanent=True) from None
    keys = data.get("keys")
    if not isinstance(keys, list):
        raise core.ChannelError("mesh alerts keys: `keys` list expected",
                                permanent=True)
    active = []
    for entry in keys:
        if not isinstance(entry, dict):
            raise core.ChannelError("mesh alerts keys: object expected", permanent=True)
        if entry.get("active") is False:
            continue
        alg = entry.get("alg")
        if alg not in (None, alerts.KEY_ALG):
            raise core.ChannelError("mesh alerts keys: unsupported alg %r" % (alg,),
                                    permanent=True)
        try:
            gen = int(entry.get("gen"))
            public = bytes.fromhex(str(entry.get("public_key") or ""))
        except (TypeError, ValueError):
            raise core.ChannelError("mesh alerts keys: invalid key entry",
                                    permanent=True) from None
        key_id = str(entry.get("key_id") or "")
        if not key_id or gen <= 0 or len(public) != 32:
            raise core.ChannelError("mesh alerts keys: invalid key entry",
                                    permanent=True)
        active.append({"key_id": key_id, "gen": gen, "public_key": public})
    data = {"account_id": str(account), "keys": active}
    cache[url] = (now, data)
    return data


def _envelope(message, keys):
    """The clear envelope: identity + ciphertext + one wrap per active key."""
    alert_id = core.message_alert_id(message)
    occurred_at = message.occurred_at or core.iso_utc(time.time())
    # #3537 refuses any unknown field (`forbidNonWhitelisted`): the clear part
    # only carries the DTO fields; the event stays inside the ciphertext.
    envelope = {"v": alerts.ENVELOPE_V, "alert_id": alert_id,
                "severity": "urgent" if message.urgent else "normal",
                "occurred_at": occurred_at}
    active = keys.get("keys") or []
    if not active:
        # content-free: no detail leaves the mesh, this is not a failure
        envelope["content_free"] = True
        return envelope
    try:
        cek = alerts.new_cek()
        nonce, sealed = alerts.seal_content(
            cek,
            alerts.content_plaintext(message.title, message.body,
                                     kind=message.kind,
                                     event=message.event,
                                     context=message.context),
            alerts.content_aad(alert_id, occurred_at))
        wraps = []
        for key in active:
            wrapped = alerts.wrap_to_x25519(
                key["public_key"],
                alerts.WrapContext(keys["account_id"], key["gen"]),
                cek)
            wraps.append({"key_id": key["key_id"], "gen": key["gen"],
                          "wrap": alerts.b64e(wrapped)})
    except alerts.CryptoUnavailable as exc:
        raise core.ChannelError(str(exc), permanent=True) from None
    except (ValueError, TypeError) as exc:
        raise core.ChannelError("nexlink encryption: %s" % core._clean(exc, 120),
                                permanent=True) from None
    envelope.update({"ciphertext": alerts.b64e(sealed),
                     "nonce": alerts.b64e(nonce), "wraps": wraps})
    return envelope


def _post(sender, channel, envelope, delivery, timeout):
    url = str(channel.options["url"]).rstrip("/") + "/mesh/alerts"
    body = json.dumps(envelope, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    secret = _secret(sender, channel)
    headers = alerts.mesh_headers(secret, body, key_id=_key_id(sender))
    headers.update({"Accept": "application/json",
                    "Content-Type": "application/json; charset=utf-8",
                    "X-Mesh-Idempotency-Key": delivery})
    _, raw = core._request(url, body, headers, timeout, method="POST",
                           show_url=True, secrets=(secret,))
    return raw


def send(sender, channel, message, timeout):
    """Push an alert into the Nexlink app (signed POST, encrypted content)."""
    alert_id = core.message_alert_id(message)
    if (message.event or "raised") != "raised":
        # #3537 v1 only stores the raised alert (no resolution state in its
        # schema); the app line stays raised, without a 400.
        sender.last[channel.name] = {"alert_id": alert_id,
                                     "skipped": message.event or "raised"}
        return
    delivery = alerts.delivery_key(message.event or "raised", alert_id)
    keys = _keys_for(sender, channel, timeout)
    envelope = _envelope(message, keys)
    try:
        raw = _post(sender, channel, envelope, delivery, timeout)
    except core.ChannelError as exc:
        if exc.status == 409 and b"key_unknown" in (exc.body or b""):
            # unknown wrap key: read the keys again and retry once
            _state(sender).setdefault("keys", {}).pop(
                str(channel.options["url"]).rstrip("/") + "/mesh/alerts/keys", None)
            keys = _keys_for(sender, channel, timeout)
            envelope = _envelope(message, keys)
            try:
                raw = _post(sender, channel, envelope, delivery, timeout)
            except core.ChannelError as again:
                if again.status == 409:
                    raise core.ChannelError("key_unknown after refreshing keys",
                                            permanent=True, status=409,
                                            body=again.body) from None
                raise
        elif exc.status and 400 <= exc.status < 500 and exc.status not in (409, 429):
            raise core.ChannelError(str(exc), permanent=True, status=exc.status,
                                    body=exc.body) from None
        else:
            raise
    accepted = _json_object(raw, "mesh alert accepted")
    if accepted.get("accepted") is False:
        raise core.ChannelError("mesh alert not accepted", permanent=True)
    sender.last[channel.name] = {"alert_id": alert_id, "delivery": delivery,
                                 "accepted": accepted.get("accepted"),
                                 "duplicate": accepted.get("duplicate"),
                                 "id": accepted.get("id")}


def receipt(sender, channel, alert_id, timeout=None):
    """Signed `GET /mesh/alerts/{id}`: the read receipt (never polled)."""
    if timeout is None:
        timeout = float(channel.options.get("timeout") or sender.timeout)
    path = "/mesh/alerts/" + urllib.parse.quote(str(alert_id), safe="")
    raw = signed_call(sender, channel, "GET", path, None, timeout)
    return _json_object(raw, "mesh alert receipt")


def push_problem(channel, env, ncfg):
    """'' when this channel can push from this environment, else why.

    The secret is resolved exactly as `Sender` does; the reason names the
    variable or the file, never the value. The channel also needs the
    `cryptography` extra; when either the extra or the secret is missing but
    its configured `fallback` is a usable remote push, the channel is still
    covered (#3536)."""
    problem = ""
    if not alerts.crypto_available():
        problem = ("channel %s: the `cryptography` module is missing "
                   "(install the `ameesh[crypto]` extra)" % channel.name)
    else:
        try:
            value = core._secret(channel, env, "token_env", "token_file", None, True)
            core.check_secret_value(value, "channel %s: mesh secret" % channel.name)
        except core.ChannelError as exc:
            problem = str(exc)
    if not problem:
        return ""
    fallback = (ncfg.channels.get(str(channel.options.get("fallback") or ""))
                if ncfg is not None else None)
    if (fallback is not None and fallback.name != channel.name
            and not core.remote_push_problem(fallback, env)):
        return ""
    return problem


def activation_problem(channel, env, ncfg):
    """#3619: the missing `cryptography` extra of a routed channel, or ''.

    Encryption is not optional: without `cryptography`, a routed channel does
    not activate. A usable declared `ntfy`/`slack` fallback covers it, as for
    `role timers --check`. The primary channel's secret is not looked at: this
    check bears on the host dependency, not a credential (#3402)."""
    if alerts.crypto_available():
        return ""
    fallback = (ncfg.channels.get(str(channel.options.get("fallback") or ""))
                if ncfg is not None else None)
    if (fallback is not None and fallback.name != channel.name
            and not core.remote_push_problem(fallback, env)):
        return ""
    return ("the `cryptography` module is missing (install the "
            "`ameesh[psycopg,crypto]` extra)")
