# SPDX-License-Identifier: AGPL-3.0-only
"""#5529 (issue #4065, mechanism 2) — positive proof of a refusal before any request.

A harness that exits non-zero with no stream line and no usage does **not**
prove that the provider was never called: a configurable wrapper can send a
billed request first and then fail silently (B cx1 on #5503). Such a turn
keeps its account reservation (`kept`) until a human releases it.

This module defines the opt-in contract by which a harness (or the wrapper
that drives it) *positively* states that it refused the turn before any
provider request:

* the descriptor of the harness declares
  ``ameesh.refusal = {"schema": "ameesh-refusal/1", "exit_code": 86}``;
* the runner writes a private skeleton (mode 0600, in the agent's state
  directory) holding ``schema``, ``harness``, ``token`` and ``turn_id``, and
  exports its path as ``AMEESH_REFUSAL_FILE`` for the turn (the token never
  goes into the environment: variables whose name ends in ``TOKEN`` are
  filtered from a harness environment);
* a harness that refuses the turn **before** issuing any provider request
  writes the same document plus a non-empty ``reason`` and exits with the
  reserved code;
* the runner releases the reservation only when that proof is valid **and**
  the process exited on its own with exactly the reserved code, wrote no
  stream line and no usage, and was not interrupted.

Anything else stays `kept`. The contract is only honoured for the three
qualified families (`claude`, `codex`, `deepseek`) using their own stream
format: a host-configured `stream = "text"` command, or a descriptor without
the `refusal` block, is not qualified and its marker is ignored — a marker is
not a proof when the command that printed it is not the declared harness.

A wrapper can emit the proof with ``ameesh refusal emit --reason "…"`` (no
database, no provider call): it validates the turn's skeleton and rewrites the
file atomically.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import stat
import sys

#: the only schema understood by this version
SCHEMA = "ameesh-refusal/1"
#: environment variable carrying the proof path of the turn
ENV = "AMEESH_REFUSAL_FILE"
#: name of the proof file in the agent's state directory
FILE = "refusal.json"
#: reserved exit code declared by the packaged descriptors
DEFAULT_EXIT = 86
#: hard bounds of the proof document
MAX_BYTES = 4096
MAX_REASON = 500
#: A qualified family is identified by its descriptor id **and** its own
#: stream format: a configured `text` command is never qualified.
QUALIFIED = {
    "claude": "claude-stream-json",
    "codex": "codex-json",
    "deepseek": "dsh-json",
}
#: exit codes a harness may not use as the reserved refusal code
_RESERVED_EXIT = frozenset({0, 126, 127})
_OPEN_KEYS = frozenset({"schema", "harness", "token", "turn_id"})
_PROOF_KEYS = frozenset({"schema", "harness", "token", "turn_id", "reason"})


def contract(descriptor) -> dict | None:
    """The refusal contract of a descriptor, or None when it is not qualified.

    Qualification is deliberately narrow: one of the three families, its own
    stream format, and a valid declaration. A host descriptor that changes the
    command to `stream = "text"` (the configurable case of #4065) is not
    qualified, even if it prints a marker and declares a contract.
    """
    if descriptor is None:
        return None
    ident = getattr(descriptor, "id", "") or ""
    if QUALIFIED.get(ident) != (getattr(descriptor, "stream", "") or ""):
        return None
    declared = getattr(descriptor, "refusal", None)
    if not isinstance(declared, dict) or declared.get("schema") != SCHEMA:
        return None
    code = declared.get("exit_code")
    if isinstance(code, bool) or not isinstance(code, int) or not 1 <= code <= 125:
        return None
    if code in _RESERVED_EXIT:
        return None
    return {"schema": SCHEMA, "exit_code": code}


def _atomic_write(path: str, document: dict) -> None:
    """Replace `path` with `document` atomically, mode 0600, never following a
    link at the target (os.replace replaces the link itself)."""
    directory = os.path.dirname(path) or "."
    temporary = os.path.join(directory, ".%s.%d.tmp" % (os.path.basename(path), os.getpid()))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def prepare(state_dir: str, harness: str, turn_id: str) -> dict:
    """Write the private skeleton of this turn and return its record.

    The previous turn's proof (if any) is replaced; a stale file can never be
    mistaken for this turn's proof because the token is new. Raises OSError:
    the caller keeps the turn uncertain rather than trusting a proof it could
    not prepare.
    """
    token = secrets.token_hex(32)
    path = os.path.join(state_dir, FILE)
    document = {"schema": SCHEMA, "harness": str(harness), "token": token,
                "turn_id": str(turn_id)}
    _atomic_write(path, document)
    return {"path": path, "token": token, "harness": str(harness),
            "turn_id": str(turn_id)}


def _read_document(path: str) -> tuple[dict, os.stat_result] | None:
    """Read a private regular file at most MAX_BYTES long; None otherwise.

    O_NOFOLLOW plus fstat on the acquired descriptor: a symlink, a FIFO or a
    file swapped between the open and the read is refused, not followed.
    The open is non-blocking: a FIFO without a writer must not suspend the
    worker before its accounting (B cx1 on #5529); fstat then rejects it.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW
                     | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        if info.st_mode & 0o077:
            return None
        euid = os.geteuid() if hasattr(os, "geteuid") else None
        if euid is not None and info.st_uid != euid:
            return None
        if info.st_size > MAX_BYTES:
            return None
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            raw = handle.read(MAX_BYTES + 1)
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw.encode("utf-8")) > MAX_BYTES:
        return None
    try:
        document = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    return document, info


def _reason(value) -> str | None:
    """A one-line, bounded, printable reason, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_REASON:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        return None
    return text


def _matches(document: dict, record: dict) -> bool:
    """The document names exactly this turn."""
    if set(document) != _OPEN_KEYS and set(document) != _PROOF_KEYS:
        return False
    if document.get("schema") != SCHEMA:
        return False
    if document.get("harness") != record["harness"]:
        return False
    if document.get("turn_id") != record["turn_id"]:
        return False
    token = document.get("token")
    expected = record.get("token")
    if not isinstance(token, str) or not isinstance(expected, str):
        return False
    # hmac.compare_digest refuses non-ASCII str with a TypeError (B cx1 on
    # #5529): the contract's token is ASCII, so a non-ASCII one is no proof
    # rather than an exception that would skip the turn accounting.
    if not token.isascii() or not expected.isascii():
        return False
    return hmac.compare_digest(token.encode("ascii"), expected.encode("ascii"))


def opened(record: dict) -> bool:
    """The proof file still holds the untouched skeleton (no refusal emitted)."""
    got = _read_document(record["path"])
    return bool(got and _matches(got[0], record)
                and set(got[0]) == _OPEN_KEYS)


def proof(record: dict) -> str | None:
    """The reason of a valid refusal proof for this turn, or None.

    A valid proof is exactly one JSON object, owned by this user, mode 0600,
    bounded, naming this turn, with a non-empty one-line reason. The file is
    removed once read (success or not): a proof is consumed by one turn.
    """
    try:
        got = _read_document(record["path"])
        if not got:
            return None
        document, _info = got
        if not _matches(document, record) or set(document) != _PROOF_KEYS:
            return None
        return _reason(document.get("reason"))
    finally:
        try:
            os.unlink(record["path"])
        except OSError:
            pass


def emit(path: str | None = None, reason: str = "") -> int:
    """Write the refusal proof for the turn (``ameesh refusal emit``).

    Reads the skeleton the runner wrote (`AMEESH_REFUSAL_FILE` by default),
    checks that it is this user's private, valid `ameesh-refusal/1` skeleton,
    then replaces it atomically with the proof and the given reason. No
    database and no provider call: safe to run from inside a harness wrapper.
    """
    path = path or os.environ.get(ENV, "")
    if not path:
        print("refusal: no %s in this environment" % ENV, file=sys.stderr)
        return 1
    got = _read_document(path)
    if not got:
        print("refusal: no valid turn skeleton at %s" % path, file=sys.stderr)
        return 1
    document, _info = got
    if (set(document) != _OPEN_KEYS or document.get("schema") != SCHEMA
            or not all(isinstance(document.get(key), str) and document.get(key)
                       for key in ("harness", "token", "turn_id"))):
        print("refusal: %s is not an ameesh-refusal/1 skeleton" % path,
              file=sys.stderr)
        return 1
    text = _reason(reason)
    if text is None:
        print("refusal: a non-empty one-line reason (at most %d characters) "
              "is required" % MAX_REASON, file=sys.stderr)
        return 1
    record = {"path": path, "token": str(document.get("token") or ""),
              "harness": str(document.get("harness") or ""),
              "turn_id": str(document.get("turn_id") or "")}
    if not _matches(document, record):
        print("refusal: %s is not a valid turn skeleton" % path, file=sys.stderr)
        return 1
    try:
        _atomic_write(path, {"schema": SCHEMA, "harness": record["harness"],
                             "token": record["token"], "turn_id": record["turn_id"],
                             "reason": text})
    except OSError as exc:
        print("refusal: proof not written (%s)" % (exc.strerror or exc),
              file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ameesh refusal",
        description="Refusal proof of a turn (ameesh-refusal/1, #5529).")
    sub = parser.add_subparsers(dest="action", required=True)
    p_emit = sub.add_parser(
        "emit", help="write the turn's refusal proof before any provider request")
    p_emit.add_argument("--reason", required=True,
                        help="one-line reason of the refusal")
    p_emit.add_argument("--file", default="",
                        help="proof file (default: $%s)" % ENV)
    args = parser.parse_args(argv)
    if args.action == "emit":
        return emit(args.file or None, args.reason)
    return 2


if __name__ == "__main__":
    sys.exit(main())
