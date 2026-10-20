# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in client of the external Memoria service; no mesh database access.

The host provisions one private configuration and scoped credential per caller.
Memoria remains responsible for project/person admission, citations and receipts.
This module neither captures turns nor changes the neutral persona memory.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib import error, request
from urllib.parse import urlsplit


MAX_BYTES = 1024 * 1024
PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z")


class MemoriaError(ValueError):
    """A stable diagnostic, with no token, source text or server error body."""


def _private_file(path: Path, limit: int) -> bytes:
    try:
        if path.is_symlink():
            raise MemoriaError("private_file_required")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise MemoriaError("private_file_required")
            if os.name == "posix" and (info.st_uid != os.geteuid()
                                      or stat.S_IMODE(info.st_mode) & 0o077):
                raise MemoriaError("private_file_required")
            data = fh.read(limit + 1)
    except OSError:
        raise MemoriaError("private_file_unavailable") from None
    if len(data) > limit:
        raise MemoriaError("file_too_large")
    return data


def _json(data: bytes):
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise MemoriaError("invalid_json") from None


@dataclass(frozen=True)
class Settings:
    endpoint: str
    token_file: Path
    project: str
    max_tokens: int = 1500

    @classmethod
    def load(cls, path: str):
        config_path = Path(path).expanduser().absolute()
        raw = _json(_private_file(config_path, 16384))
        if not isinstance(raw, dict) or set(raw) - {
                "endpoint", "token_file", "project", "max_tokens"}:
            raise MemoriaError("invalid_config")
        endpoint = raw.get("endpoint")
        if not isinstance(endpoint, str) or any(c.isspace() or ord(c) < 33 or ord(c) == 127
                                                for c in endpoint):
            raise MemoriaError("invalid_endpoint")
        try:
            url = urlsplit(endpoint)
            port = url.port
        except ValueError:
            raise MemoriaError("invalid_endpoint") from None
        if (url.scheme not in ("http", "https") or not url.hostname
                or url.username is not None or url.password is not None
                or url.query or url.fragment or port == 0):
            raise MemoriaError("invalid_endpoint")
        if url.scheme == "http" and url.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise MemoriaError("https_required")
        project = raw.get("project")
        budget = raw.get("max_tokens", 1500)
        token_file = raw.get("token_file")
        if (not isinstance(project, str) or not PROJECT.fullmatch(project)
                or type(budget) is not int or not 64 <= budget <= 4000
                or not isinstance(token_file, str) or not token_file.strip()):
            raise MemoriaError("invalid_config")
        token_path = Path(token_file).expanduser()
        if not token_path.is_absolute():
            token_path = config_path.parent / token_path
        return cls(endpoint.rstrip("/"), token_path, project, budget)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, settings: Settings):
        self.settings = settings

    def call(self, operation: str, data: dict):
        if operation not in ("context", "ask", "tell"):
            raise MemoriaError("invalid_operation")
        if not isinstance(data, dict):
            raise MemoriaError("invalid_request")
        allowed = ({"what", "channel", "date", "pointer", "direction", "idempotency_key"}
                   if operation == "tell" else
                   {"purpose"} if operation == "context" else {"question"})
        if set(data) - allowed:
            raise MemoriaError("scope_override_refused")
        if operation == "tell":
            if any(not isinstance(data.get(k), str) or not data[k].strip()
                   for k in ("what", "channel", "date", "pointer", "idempotency_key")):
                raise MemoriaError("cited_idempotent_write_required")
            if len(data["what"]) > 4000:
                raise MemoriaError("invalid_request")
            body = {**data, "visibility": "private"}
        else:
            key = "purpose" if operation == "context" else "question"
            if (not isinstance(data.get(key), str) or not data[key].strip()
                    or len(data[key]) > 2000):
                raise MemoriaError("invalid_request")
            body = {**data, "audience": "private", "max_tokens": self.settings.max_tokens}
        body["scope"] = {"project": self.settings.project}
        try:
            token = _private_file(self.settings.token_file, 512).decode("ascii").strip()
        except UnicodeError:
            raise MemoriaError("invalid_token_file") from None
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
            raise MemoriaError("invalid_token_file")
        req = request.Request(
            self.settings.endpoint + "/v1/" + operation,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                     "Accept": "application/json"},
        )
        # A proxy inherited by a harness must not receive private local traffic.
        opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(req, timeout=15 if operation == "context" else 630) as response:
                raw = response.read(MAX_BYTES + 1)
        except error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise MemoriaError("memoria_http_%s" % code) from None
        except (OSError, error.URLError, http.client.HTTPException):
            # A failed write has an unknown outcome. Replay only the identical
            # payload/key; there is deliberately no automatic retry here.
            raise MemoriaError("memoria_unavailable") from None
        if len(raw) > MAX_BYTES:
            raise MemoriaError("response_too_large")
        reply = _json(raw)
        if not isinstance(reply, dict):
            raise MemoriaError("invalid_response")
        if operation == "tell":
            if (reply.get("accepted") is not True or not isinstance(reply.get("id"), str)
                    or type(reply.get("duplicate")) is not bool
                    or type(reply.get("observations")) is not int):
                raise MemoriaError("invalid_response")
        else:
            sources = reply.get("sources")
            count = reply.get("token_count")
            if (not isinstance(reply.get("text"), str) or type(reply.get("unknown")) is not bool
                    or type(count) is not int or not 0 <= count <= self.settings.max_tokens
                    or not isinstance(sources, list)
                    or any(not isinstance(s, dict) or any(not isinstance(s.get(k), str)
                           for k in ("id", "channel", "date", "pointer")) for s in sources)
                    or (not reply["unknown"] and not sources)):
                raise MemoriaError("invalid_response")
        return reply


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ameesh memoria", description=__doc__)
    parser.add_argument("--config", default=os.environ.get("AMEESH_MEMORIA_CONFIG"),
                        help="private per-caller JSON config (or AMEESH_MEMORIA_CONFIG)")
    subs = parser.add_subparsers(dest="operation", required=True)
    subs.add_parser("context").add_argument("--purpose", required=True)
    subs.add_parser("ask").add_argument("--question", required=True)
    subs.add_parser("tell").add_argument("--input", required=True,
                                       help="private cited interaction JSON file, or - for stdin")
    args = parser.parse_args(argv)
    if not args.config:
        parser.error("--config or AMEESH_MEMORIA_CONFIG is required (no default credential)")
    try:
        settings = Settings.load(args.config)
        if args.operation == "tell":
            raw = (sys.stdin.buffer.read(32769) if args.input == "-" else
                   _private_file(Path(args.input).expanduser(), 32768))
            if len(raw) > 32768:
                raise MemoriaError("file_too_large")
            data = _json(raw)
        else:
            data = {k: getattr(args, k) for k in ("purpose", "question") if hasattr(args, k)}
        result = Client(settings).call(args.operation, data)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except MemoriaError as exc:
        print("ameesh memoria: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
