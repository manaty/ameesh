# SPDX-License-Identifier: AGPL-3.0-only
"""The client half of the contract witness: standard library only.

A consumer reaches ameesh through the installed executables, the HTTP API
and the named SQL read views (docs/api/module-contract-v1.md). Each
executable is a command prefix supplied by the consumer's manifest (for
example `["ameesh"]` or `[python, "-m", "ameesh"]`), never an import.
"""
import http.client
import json
import re
import subprocess

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


class ContractError(AssertionError):
    """A published element is missing or has changed."""


def run(command, args, *, env=None, timeout=120):
    done = subprocess.run(list(command) + list(args), env=env, capture_output=True,
                          text=True, timeout=timeout)
    return done.returncode, done.stdout, done.stderr


def run_json(command, args, *, env=None):
    code, out, err = run(command, args, env=env)
    try:
        return code, json.loads(out)
    except ValueError:
        raise ContractError("%s %s: no JSON document (exit %d): %s"
                            % (" ".join(command), " ".join(args), code, err.strip()[-300:]))


def help_options(command, args, *, env=None):
    """Long options documented by `--help` (the CLI surface of a release)."""
    code, out, err = run(command, list(args) + ["--help"], env=env)
    if code != 0:
        raise ContractError("%s %s --help: exit %d: %s"
                            % (" ".join(command), " ".join(args), code, err.strip()[-300:]))
    return set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", out)), out


def require_options(command, args, options, *, env=None):
    found, _ = help_options(command, args, env=env)
    missing = sorted(set(options) - found)
    if missing:
        raise ContractError("%s %s: undocumented option(s) %s"
                            % (" ".join(command), " ".join(args), ", ".join(missing)))
    return found


def require_fields(document, fields, where):
    missing = sorted(set(fields) - set(document))
    if missing:
        raise ContractError("%s: missing field(s) %s" % (where, ", ".join(missing)))


def http_request(port, path, *, method="GET", token=None, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        sent = dict(headers or {})
        if token:
            sent["Authorization"] = "Bearer %s" % token
        conn.request(method, path, body=body, headers=sent)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def http_json(port, path, **kwargs):
    status, headers, body = http_request(port, path, **kwargs)
    return status, headers, (json.loads(body) if body else None)


def discover_api(port):
    """The two unauthenticated documents: discovery, then OpenAPI."""
    status, _, discovery = http_json(port, "/.well-known/ameesh")
    if status != 200:
        raise ContractError("/.well-known/ameesh: HTTP %d" % status)
    require_fields(discovery, ("openapi",), "/.well-known/ameesh")
    status, _, document = http_json(port, discovery["openapi"])
    if status != 200:
        raise ContractError("%s: HTTP %d" % (discovery["openapi"], status))
    return discovery, document


def operations(document):
    """{(METHOD, path)} of an OpenAPI document."""
    verbs = {"get", "put", "post", "delete", "patch", "head", "options"}
    return {(verb.upper(), path) for path, item in document.get("paths", {}).items()
            for verb in item if verb in verbs}


def view_select(view, columns):
    """`SELECT` of explicit admitted columns, never `*`."""
    for name in (view,) + tuple(columns):
        if not _IDENT.match(name):
            raise ValueError("not a plain SQL identifier: %r" % name)
    return "SELECT %s FROM %s" % (", ".join(columns), view)


def read_view(query, view, columns):
    """Rows of a named view, through the consumer's own `query(sql)`."""
    rows = query(view_select(view, columns))
    for row in rows:
        require_fields(row, columns, view)
    return rows


def _resolve(document, schema):
    while isinstance(schema, dict) and "$ref" in schema:
        node = document
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        schema = node
    return schema if isinstance(schema, dict) else {}


def _object(document, schema):
    schema = _resolve(document, schema)
    return sorted(schema.get("required", [])), sorted(schema.get("properties", {}))


def openapi_surface(document):
    """What a consumer relies on per operation: inputs it must send, outputs it reads."""
    surface = {}
    for method, path in sorted(operations(document)):
        op = document["paths"][path][method.lower()]
        params = [_resolve(document, p) for p in op.get("parameters", [])]
        body = _resolve(document, op.get("requestBody", {}))
        body_schema = body.get("content", {}).get("application/json", {}).get("schema", {})
        ok = _resolve(document, op.get("responses", {}).get("200", {}))
        content = ok.get("content", {})
        ok_schema = content.get("application/json", {}).get("schema", {})
        body_required, body_props = _object(document, body_schema)
        ok_required, ok_props = _object(document, ok_schema)
        surface["%s %s" % (method, path)] = {
            "parameters": sorted("%s:%s" % (p.get("in"), p.get("name")) for p in params),
            "required_parameters": sorted("%s:%s" % (p.get("in"), p.get("name"))
                                          for p in params if p.get("required")),
            "body_required": body_required, "body_properties": body_props,
            "response_types": sorted(content),
            "response_required": ok_required, "response_properties": ok_props,
        }
    return surface


def openapi_breaks(baseline, candidate):
    """Incompatible changes from a baseline surface to a candidate one.

    Additive changes are compatible: a new operation, an optional input, a
    response field. Removing an operation, parameter, output field or media
    type, or newly requiring an input, is a break."""
    breaks = []
    for key, old in sorted(baseline.items()):
        new = candidate.get(key)
        if new is None:
            breaks.append("%s: operation removed" % key)
            continue
        checks = (("parameters", "parameter removed", True),
                  ("body_properties", "request field removed", True),
                  ("response_types", "response media type removed", True),
                  ("response_required", "response field no longer required", True),
                  ("response_properties", "response field removed", True),
                  ("required_parameters", "parameter newly required", False),
                  ("body_required", "request field newly required", False))
        for name, label, kept in checks:
            before, after = set(old[name]), set(new[name])
            for item in sorted(before - after if kept else after - before):
                breaks.append("%s: %s: %s" % (key, label, item))
    return breaks
