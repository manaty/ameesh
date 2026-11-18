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


def help_section(text, name):
    """The block of a shared help page that documents `name` (`agent-mail send`):
    from its line to the next command of the same executable."""
    lines, block = text.splitlines(), []
    for line in lines:
        if block and line.startswith("  %s " % name.split()[0]):
            break
        if block or line.startswith("  %s " % name) or line.rstrip() == "  " + name:
            block.append(line)
    if not block:
        raise ContractError("%s: not documented by --help" % name)
    return "\n".join(block)


def help_options(command, args, *, env=None, section=None):
    """Long options documented by `--help` (the CLI surface of a release); with
    `section`, only those of that command's block of a shared help page."""
    code, out, err = run(command, list(args) + ["--help"], env=env)
    if code != 0:
        raise ContractError("%s %s --help: exit %d: %s"
                            % (" ".join(command), " ".join(args), code, err.strip()[-300:]))
    if section:
        out = help_section(out, section)
    return set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", out)), out


def require_options(command, args, options, *, env=None, section=None):
    found, _ = help_options(command, args, env=env, section=section)
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


#: The schema keywords that bound a value: what an input accepts, what an output yields.
_CONSTRAINTS = ("type", "const", "enum", "pattern", "format", "minimum", "maximum",
                "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength",
                "minItems", "maxItems", "additionalProperties", "required")


def schema_fields(document, schema, prefix="", out=None, refs=()):
    """{path: constraints} of a schema, flattened: `a.b`, `a[]`, `a{}`; "" is its root."""
    out = {} if out is None else out
    ref = schema.get("$ref") if isinstance(schema, dict) else None
    if ref in refs:
        return out  # a recursive schema: its fields are those of the first visit
    refs = refs + (ref,) if ref else refs
    schema = _resolve(document, schema)
    node = {key: schema[key] for key in _CONSTRAINTS
            if key in schema and not isinstance(schema[key], dict)}
    if "type" in node:
        node["type"] = sorted([node["type"]] if isinstance(node["type"], str) else node["type"])
    for key in ("enum", "required"):
        if key in node:
            node[key] = sorted(node[key], key=json.dumps)
    out[prefix] = node
    for name, sub in sorted(schema.get("properties", {}).items()):
        schema_fields(document, sub, "%s.%s" % (prefix, name) if prefix else name, out, refs)
    if isinstance(schema.get("items"), dict):
        schema_fields(document, schema["items"], prefix + "[]", out, refs)
    if isinstance(schema.get("additionalProperties"), dict):
        schema_fields(document, schema["additionalProperties"], prefix + "{}", out, refs)
    return out


def _content(document, content):
    """{media type: fields}; a stream's per-event document is under `@data`."""
    media = {}
    for name, item in sorted(content.items()):
        fields = schema_fields(document, item.get("schema", {}))
        if "x-ameesh-data" in item:
            schema_fields(document, item["x-ameesh-data"], "@data", fields)
        media[name] = fields
    return media


def openapi_surface(document):
    """What a consumer relies on per operation: the inputs it sends, with the values
    they accept, and every declared response, with the values it yields."""
    surface = {}
    for method, path in sorted(operations(document)):
        op = document["paths"][path][method.lower()]
        parameters = {}
        for parameter in op.get("parameters", []):
            parameter = _resolve(document, parameter)
            key = "%s:%s" % (parameter.get("in"), parameter.get("name"))
            parameters[key] = {"required": bool(parameter.get("required")),
                               "fields": schema_fields(document, parameter.get("schema", {}))}
        body = _resolve(document, op.get("requestBody", {}))
        responses = {}
        for status, response in sorted(op.get("responses", {}).items()):
            response = _resolve(document, response)
            responses[status] = {"headers": sorted(response.get("headers", {})),
                                 "content": _content(document, response.get("content", {}))}
        surface["%s %s" % (method, path)] = {
            "parameters": parameters,
            "request": {"required": bool(body.get("required")),
                        "content": _content(document, body.get("content", {}))},
            "responses": responses,
        }
    return surface


def _allowed(node):
    """The set of admitted values, or None when the schema does not enumerate them."""
    if "const" in node:
        return {json.dumps(node["const"])}
    if "enum" in node:
        return {json.dumps(value) for value in node["enum"]}
    return None


def _types(node):
    types = set(node.get("type", ())) or {"any"}
    return types | {"integer"} if "number" in types else types


def _wider(old, new, open_enum=False):
    """True when `new` admits at least the values `old` admits; with `open_enum`, a
    value added to an enumeration counts as admitted (an output's open vocabulary)."""
    if "any" not in _types(new) and not _types(old) <= _types(new):
        return False
    before, after = _allowed(old), _allowed(new)
    if after is not None and (before is None or not before <= after) \
            and not (open_enum and "enum" in old and "enum" in new):
        return False
    for key in ("minimum", "exclusiveMinimum", "minLength", "minItems"):
        if key in new and (key not in old or new[key] > old[key]):
            return False
    for key in ("maximum", "exclusiveMaximum", "maxLength", "maxItems"):
        if key in new and (key not in old or new[key] < old[key]):
            return False
    for key in ("pattern", "format"):
        if key in new and new.get(key) != old.get(key):
            return False
    return True


def _field_breaks(old, new, where, accepted):
    """Breaks between two flattened schemas. An input (`accepted`) may only widen:
    no field removed, no value refused that was accepted, no field newly required.
    An output may only narrow: no field removed or no longer required, no value
    yielded of another type, constant or bound; additional output fields and
    enumeration values are compatible, a consumer reads an unknown one as unknown."""
    breaks = []
    for path, before in sorted(old.items()):
        label = "%s %s" % (where, path or "(root)")
        after = new.get(path)
        if after is None:
            breaks.append("%s: field removed" % label)
            continue
        if not (_wider(before, after) if accepted else _wider(after, before, open_enum=True)):
            breaks.append("%s: %s changed: %s → %s" % (
                label, "accepted values" if accepted else "result values",
                json.dumps({k: v for k, v in before.items() if k != "required"},
                           sort_keys=True),
                json.dumps({k: v for k, v in after.items() if k != "required"},
                           sort_keys=True)))
        required = set(before.get("required", ())), set(after.get("required", ()))
        for name in sorted(required[1] - required[0] if accepted else required[0] - required[1]):
            breaks.append("%s: field %s: %s" % (label, "newly required" if accepted
                                                else "no longer required", name))
        if accepted and after.get("additionalProperties") is False \
                and before.get("additionalProperties") is not False:
            breaks.append("%s: additional fields newly refused" % label)
    return breaks


def openapi_breaks(baseline, candidate):
    """Incompatible changes from a baseline surface to a candidate one.

    Additive changes are compatible: a new operation, an optional input, a wider
    accepted input, a response field, a narrower result. Removing an operation,
    parameter, field, media type, declared response or header, newly requiring an
    input, refusing a value that was accepted, or yielding a value of another type,
    constant or bound is a break."""
    breaks = []
    for key, old in sorted(baseline.items()):
        new = candidate.get(key)
        if new is None:
            breaks.append("%s: operation removed" % key)
            continue
        out = []
        for name, before in sorted(old["parameters"].items()):
            after = new["parameters"].get(name)
            if after is None:
                out.append("parameter %s: removed" % name)
                continue
            if after["required"] and not before["required"]:
                out.append("parameter %s: newly required" % name)
            out += _field_breaks(before["fields"], after["fields"], "parameter " + name, True)
        out += ["parameter %s: newly required" % name for name, after
                in sorted(new["parameters"].items())
                if name not in old["parameters"] and after["required"]]
        if new["request"]["required"] and not old["request"]["required"]:
            out.append("request body: newly required")
        for media, before in sorted(old["request"]["content"].items()):
            after = new["request"]["content"].get(media)
            if after is None:
                out.append("request %s: media type removed" % media)
            else:
                out += _field_breaks(before, after, "request " + media, True)
        for status, before in sorted(old["responses"].items()):
            after = new["responses"].get(status)
            if after is None:
                out.append("response %s: removed" % status)
                continue
            out += ["response %s: header removed: %s" % (status, name)
                    for name in sorted(set(before["headers"]) - set(after["headers"]))]
            for media, fields in sorted(before["content"].items()):
                if media not in after["content"]:
                    out.append("response %s %s: media type removed" % (status, media))
                else:
                    out += _field_breaks(fields, after["content"][media],
                                         "response %s %s" % (status, media), False)
        breaks += ["%s: %s" % (key, item) for item in out]
    return breaks
