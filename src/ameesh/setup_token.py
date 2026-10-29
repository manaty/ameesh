# SPDX-License-Identifier: AGPL-3.0-only
"""Private, host-local Claude setup tokens. Never expose a token in a repr/error.

The account home stores this independently of Claude's rotating OAuth cache.
Expiry is supplied by the issuer/operator; no lifetime is invented for opaque tokens.
"""
from __future__ import annotations

import json
import math
import os
import stat
import subprocess
import tempfile

FILENAME = ".ameesh-setup-token.json"
MAX_BYTES = 16384
VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"


class TokenError(ValueError):
    pass


def path(profile, environ=None):
    return os.path.join(profile.home(environ), FILENAME)


def present(profile, environ=None):
    return (profile.harness == "claude" and profile.kind == "config_dir"
            and os.path.lexists(path(profile, environ)))


def private_read(filename):
    try:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o177):
                raise TokenError("token file must be owned by this user and private (0600)")
            content = handle.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise TokenError("token file too large")
        return content
    except (OSError, UnicodeError):
        raise TokenError("cannot read private token file") from None


def validate(token):
    if (not isinstance(token, str) or not token.startswith("sk-ant-oat01-")
            or len(token) <= len("sk-ant-oat01-") or len(token) > MAX_BYTES // 2
            or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in token)):
        raise TokenError("expected a Claude setup-token OAuth token (no API key)")
    return token


def read(profile, environ=None):
    try:
        home = profile.home(environ)
        info = os.lstat(home)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or os.path.realpath(home) != os.path.abspath(home)):
            raise TokenError("setup-token home must be private and owned by this user")
        node = json.loads(private_read(path(profile, environ)))
        token = validate(node["token"])
        expiry = node.get("expires_at")
        if expiry is not None:
            if isinstance(expiry, bool) or not math.isfinite(float(expiry)) or float(expiry) <= 0:
                raise ValueError
            expiry = float(expiry)
        return token, expiry
    except (OSError, ValueError, TypeError, KeyError):
        raise TokenError("invalid setup-token store") from None


def install(profile, content, *, expires_at=None, environ=None):
    token = validate(content.strip())
    if expires_at is not None:
        if isinstance(expires_at, bool) or not math.isfinite(float(expires_at)) or float(expires_at) <= 0:
            raise TokenError("expiry must be a positive Unix timestamp")
        expires_at = float(expires_at)
    home = profile.home(environ)
    if profile.harness != "claude" or profile.kind != "config_dir":
        raise TokenError("setup-token is only supported for Claude plan accounts")
    try:
        info = os.lstat(home)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or os.path.realpath(home) != os.path.abspath(home)):
            raise TokenError("account home must be a private directory owned by this user")
        result = subprocess.run(["git", "-C", home, "rev-parse", "--is-inside-work-tree"],
                                capture_output=True, timeout=5, check=False)
        if result.returncode == 0 and result.stdout.strip() == b"true":
            raise TokenError("token store must be outside a Git worktree")
        fd, temporary = tempfile.mkstemp(prefix=".setup-token-", dir=home)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"token": token, "expires_at": expires_at}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path(profile, environ))
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
    except (OSError, subprocess.SubprocessError):
        raise TokenError("cannot install private setup-token store") from None
