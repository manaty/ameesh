# SPDX-License-Identifier: AGPL-3.0-only
"""#7034: the restricted-tools policy of a spawned child (design #7031 §4).

A child is created by `ameesh agent spawn`; its policy is a subset of its
parent's: allowed tools and MCP server names, both optional. The policy is
stored with the child (`spawn_profiles`, written in the same transaction) and
applied by the **runner** at every turn, never by the child itself.

Application is declared by each harness descriptor under `ameesh.spawn`:

    "spawn": {
      "contract": "ameesh-spawn-tools/1",
      "tools": {"flag": ["--tools"]},
      "mcp":   {"flag": ["--mcp-config", "{path}", "--strict-mcp-config"],
                "source": ".mcp.json"}
    }

A harness without that block cannot host a restricted child
(`spawn-tools-unsupported`); the policy is refused at creation, and the
runner refuses the turn if a descriptor lost the block later. The child's
private settings file lives under its state directory (`0600`, `0700`
directory), never in the parent's account directory.

The policy is a guard against mistakes and injection, **not** an isolation
boundary: a child that shares its parent's Unix account can read the parent's
credentials. Isolation comes from the child's own placement (#5323/B5); until
it exists this module does not pretend otherwise.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from . import harnesses
from .persona_memory import SECRET_PATTERNS

CONTRACT = "ameesh-spawn-tools/1"
#: one settings file per child under its state directory
PROFILE_FILE = "spawn-tools.json"
MCP_FILE = "mcp-config.json"
CONTEXT_FILE = "spawn-context.md"
FILE_MODE = 0o600
DIR_MODE = 0o700
#: `--context` bound (design #7031 §4.3)
CONTEXT_LIMIT = 16 * 1024
#: one report body bound (design #7031 §4.2)
REPORT_LIMIT = 8 * 1024

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,79}")


class SpawnToolsError(ValueError):
    """The child's tool policy is invalid, unsupported or could not be applied."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Support:
    """How one harness applies a child policy (its descriptor's `ameesh.spawn`)."""

    harness: str
    tools_flag: tuple[str, ...]
    mcp_flag: tuple[str, ...]
    mcp_source: str | None

    @property
    def tools(self) -> bool:
        return bool(self.tools_flag)

    @property
    def mcp(self) -> bool:
        return bool(self.mcp_flag)


def support(harness: str) -> Support | None:
    """The declared application of the policy, or None (never pretend)."""
    descriptor = harnesses.get(harness) if harness else None
    raw = (descriptor.ameesh.get("spawn") if descriptor is not None else None)
    if not isinstance(raw, Mapping) or raw.get("contract") != CONTRACT:
        return None
    tools = raw.get("tools") if isinstance(raw.get("tools"), Mapping) else {}
    mcp = raw.get("mcp") if isinstance(raw.get("mcp"), Mapping) else {}
    tools_flag = tuple(tools.get("flag") or ())
    mcp_flag = tuple(mcp.get("flag") or ())
    source = mcp.get("source")
    return Support(harness=harness, tools_flag=tools_flag, mcp_flag=mcp_flag,
                   mcp_source=source if isinstance(source, str) and source else None)


def name(value: str, what: str) -> str:
    value = str(value or "").strip()
    if not _NAME_RE.fullmatch(value):
        raise SpawnToolsError("spawn-tool-name-invalid",
                              "%s name is invalid: %r (letters, digits, _.:/-; 80 max)"
                              % (what, value))
    return value


def normalize(tools: Iterable[str] | None, mcp: Iterable[str] | None) -> tuple[list[str], list[str]]:
    """Deduplicate, keep the caller's order; `mcp:` moves to the MCP list."""
    clean_tools: list[str] = []
    clean_mcp: list[str] = []
    # `tools: [git, "mcp:transport-readonly"]` is the canon vocabulary; the
    # same `mcp:<nom>` form is accepted on the command line, and `--mcp NAME`
    # is its sugar. One effective list, never two vocabularies.
    for raw in tools or ():
        value = name(raw, "tool")
        if value.startswith("mcp:"):
            value = value[4:]
            if value and value not in clean_mcp:
                clean_mcp.append(value)
        elif value not in clean_tools:
            clean_tools.append(value)
    for raw in mcp or ():
        value = name(raw, "MCP server")
        if value.startswith("mcp:"):
            value = value[4:]
        if value and value not in clean_mcp:
            clean_mcp.append(value)
    return clean_tools, clean_mcp


def parent_lists(card_tools: Iterable[str] | None,
                 profile: Mapping | None) -> tuple[list[str] | None, list[str] | None]:
    """The parent's effective lists; None means "no policy declared".

    A canonical parent reads its `tools` card field. An ephemeral parent has
    no card: its own `spawn_profiles` row is authoritative (transitivity). A
    parent whose policy is unknown (canonical, but its card could not be
    read) is **not** treated as unrestricted: it has empty lists, so a
    restricted child cannot appear under it.
    """
    if profile is not None:
        return list(profile.get("tools") or ()), list(profile.get("mcp") or ())
    if card_tools is None:
        return None, None
    tools, mcp = [], []
    for value in card_tools:
        value = str(value or "").strip()
        if not value:
            continue
        if value.startswith("mcp:"):
            mcp.append(value[4:])
        else:
            tools.append(value)
    return tools, mcp


def subset(parent_tools: list[str] | None, parent_mcp: list[str] | None,
           tools: Iterable[str], mcp: Iterable[str]) -> None:
    """Refuse a name the parent does not hold (`spawn-tool-not-in-parent`)."""
    if parent_tools is None and parent_mcp is None:
        return  # the parent declares no policy: a subset is not definable
    allowed_tools = set(parent_tools or ())
    allowed_mcp = set(parent_mcp or ())
    for value in tools:
        if value not in allowed_tools:
            raise SpawnToolsError(
                "spawn-tool-not-in-parent",
                "tool %r is not in the parent's tools (%s)"
                % (value, ", ".join(sorted(allowed_tools)) or "none"))
    for value in mcp:
        if value not in allowed_mcp:
            raise SpawnToolsError(
                "spawn-tool-not-in-parent",
                "MCP server %r is not in the parent's tools (%s)"
                % (value, ", ".join("mcp:" + n for n in sorted(allowed_mcp)) or "none"))


def policy_digest(tools: Iterable[str], mcp: Iterable[str]) -> str:
    """Stable digest of the effective policy document (audit, not a proof)."""
    document = {"contract": CONTRACT, "tools": list(tools), "mcp": list(mcp)}
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def supported(harness: str, tools: Iterable[str], mcp: Iterable[str]) -> Support | None:
    """The application block, or a refusal naming what the harness cannot prove."""
    tools, mcp = list(tools), list(mcp)
    if not tools and not mcp:
        return None
    block = support(harness)
    if block is None:
        raise SpawnToolsError(
            "spawn-tools-unsupported",
            "harness %r does not declare %s: a restricted child cannot be spawned"
            % (harness, CONTRACT))
    if tools and not block.tools:
        raise SpawnToolsError(
            "spawn-tools-unsupported",
            "harness %r does not apply a tool allow-list" % harness)
    if mcp and not block.mcp:
        raise SpawnToolsError(
            "spawn-tools-unsupported",
            "harness %r does not apply an MCP server list" % harness)
    return block


def mcp_servers(cwd: str | None, source: str | None,
                names: Iterable[str]) -> dict:
    """The allowed servers, read from the project's MCP file, filtered.

    `source` is declared by the harness descriptor (Claude: `.mcp.json` at the
    project root). Missing file with no allowed server is fine: the child gets
    an empty private config, under `--strict-mcp-config`, so it inherits no
    MCP server. A named server that cannot be resolved is refused at creation.
    """
    names = list(names)
    path = os.path.join(cwd or os.getcwd(), source) if source else None
    document: dict = {}
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError) as exc:
            raise SpawnToolsError("spawn-mcp-source-invalid",
                                  "MCP config %s is unreadable: %s" % (path, exc)) from None
        if not isinstance(document, dict):
            raise SpawnToolsError("spawn-mcp-source-invalid",
                                  "MCP config %s: an object is expected" % path)
    servers = document.get("mcpServers")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise SpawnToolsError("spawn-mcp-source-invalid",
                              "MCP config %s: `mcpServers` must be an object" % path)
    out = {}
    for value in names:
        if value not in servers:
            raise SpawnToolsError(
                "spawn-mcp-not-found",
                "MCP server %r is not defined in %s" % (value, path or "the project"))
        if _has_secret(json.dumps(servers[value], ensure_ascii=False)):
            raise SpawnToolsError(
                "spawn-context-secret",
                "MCP server %r carries a literal secret: refusing to copy it" % value)
        out[value] = servers[value]
    return out


def _has_secret(text: str) -> bool:
    """Known patterns AND the exact values of secret environment variables.

    Design #7031 §4.3: `turn_context.environment_secrets` is part of the
    filter, not only the generic patterns. A context or an MCP server that
    carries a declared credential value is refused before it can cross hosts.
    """
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        return True
    try:
        from . import turn_context
        values = turn_context.environment_secrets()
    except Exception:  # pragma: no cover — a filter failure never widens
        return True
    return any(value and value in text for value in values)


def check_text(text: str, what: str, limit: int) -> str:
    """Bound UTF-8 text and refuse a likely secret; never echo the value."""
    if not isinstance(text, str):
        raise SpawnToolsError("spawn-text-invalid", "%s: text expected" % what)
    size = len(text.encode("utf-8"))
    if size > limit:
        raise SpawnToolsError("spawn-text-too-long",
                              "%s is %d bytes (limit %d)" % (what, size, limit))
    if _has_secret(text):
        raise SpawnToolsError("spawn-context-secret",
                              "%s contains a likely secret; refusing it" % what)
    return text


def read_context(path: str) -> tuple[str, str]:
    """Read `--context`: one regular file, bounded, secret-free, digest."""
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isdir(path):
        raise SpawnToolsError("spawn-context-invalid",
                              "--context takes one file, not a directory: %s" % path)
    try:
        with open(path, "rb") as handle:
            raw = handle.read(CONTEXT_LIMIT + 1)
    except OSError as exc:
        raise SpawnToolsError("spawn-context-invalid",
                              "--context file unreadable: %s" % exc) from None
    if len(raw) > CONTEXT_LIMIT:
        raise SpawnToolsError("spawn-text-too-long",
                              "--context is larger than %d bytes" % CONTEXT_LIMIT)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SpawnToolsError("spawn-context-invalid",
                              "--context must be UTF-8 text") from None
    check_text(text, "--context", CONTEXT_LIMIT)
    return text, hashlib.sha256(raw).hexdigest()


def _write_private(path: str, text: str) -> str:
    """Atomically write one `0600` file under a `0700` directory."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=DIR_MODE, exist_ok=True)
    temporary = path + ".%d.tmp" % os.getpid()
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0), FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), FILE_MODE)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path


def write_profile(state_dir: str, profile: Mapping, cwd: str | None) -> tuple[str, str]:
    """Write the child's private settings; return (json path, mcp path or "")."""
    block = support(str(profile.get("harness") or ""))
    if block is None:
        raise SpawnToolsError(
            "spawn-tools-unsupported",
            "harness %r lost %s: refusing the turn" % (profile.get("harness"), CONTRACT))
    tools = list(profile.get("tools") or ())
    mcp = list(profile.get("mcp") or ())
    if tools and not block.tools:
        raise SpawnToolsError("spawn-tools-unsupported",
                              "harness %r no longer applies a tool allow-list"
                              % profile.get("harness"))
    if mcp and not block.mcp:
        raise SpawnToolsError("spawn-tools-unsupported",
                              "harness %r no longer applies an MCP server list"
                              % profile.get("harness"))
    # #7034 × #7107 : le profil créé au spawn porte les serveurs retenus, déjà
    # filtrés depuis le `.mcp.json` du parent. Un enfant placé sur un autre
    # hôte ne peut pas relire ce fichier : on ne retombe sur la lecture du
    # projet que pour un profil antérieur (colonne vide) ou une politique sans
    # MCP. La liste des noms reste l'autorité : un serveur stocké hors liste
    # n'est jamais écrit.
    stored = profile.get("mcp_servers")
    if stored is None or stored == {}:
        servers = mcp_servers(cwd, block.mcp_source, mcp)
    elif not isinstance(stored, Mapping):
        raise SpawnToolsError("spawn-mcp-source-invalid",
                              "stored MCP servers are not an object")
    else:
        servers = {}
        for name in mcp:
            if name not in stored:
                raise SpawnToolsError(
                    "spawn-mcp-not-found",
                    "MCP server %r is not in the child's stored profile" % name)
            servers[name] = stored[name]
    mcp_path = ""
    if block.mcp:
        mcp_path = _write_private(
            os.path.join(state_dir, MCP_FILE),
            json.dumps({"mcpServers": servers}, ensure_ascii=False,
                       sort_keys=True, indent=1) + "\n")
    document = {
        "contract": CONTRACT,
        "child": profile.get("child"),
        "parent": profile.get("parent"),
        "harness": profile.get("harness"),
        "tools": tools,
        "mcp": mcp,
        "mcp_config": mcp_path or None,
    }
    path = _write_private(
        os.path.join(state_dir, PROFILE_FILE),
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1) + "\n")
    return path, mcp_path


def argv(profile: Mapping, path: str) -> list[str]:
    """The harness arguments that apply the policy (descriptor templates)."""
    block = support(str(profile.get("harness") or ""))
    if block is None:
        return []
    tools = list(profile.get("tools") or ())
    out: list[str] = []
    if block.tools_flag:
        out += list(block.tools_flag)
        out += tools or [""]
    if block.mcp_flag:
        mcp_path = os.path.join(os.path.dirname(path), MCP_FILE)
        out += [value.format(path=mcp_path) for value in block.mcp_flag]
    return out


def context_path(state_dir: str) -> str:
    return os.path.join(os.path.abspath(state_dir), CONTEXT_FILE)


def write_context(state_dir: str, text: str, *, digest: str | None = None) -> str:
    """Write the bounded context file `0600`; empty text removes any leftover.

    `digest` is the SHA-256 stored with the child: verify the text before
    writing, so a database or transfer corruption never reaches the harness.
    """
    target = context_path(state_dir)
    if not text:
        try:
            os.unlink(target)
        except FileNotFoundError:
            pass
        return ""
    actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest is not None and actual != digest:
        raise SpawnToolsError(
            "spawn-context-digest",
            "the stored spawn context does not match its recorded digest")
    return _write_private(target, text)
