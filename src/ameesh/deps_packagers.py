# SPDX-License-Identifier: AGPL-3.0-only
"""L248b : packages whose release needs a local node_modules tree.

The guard is per package, not per repository (#5417): a packager declared by
the desktop app of a monorepo must not force the backend, the website, the
mobile app or the shared libraries to keep a local tree.  A manifest only
guards the app it actually packages: itself, or the directory named by
electron-builder's `--projectDir` / `directories.app`, or the packages below
itself when the command names no target.
"""
import io
import json
import os
import re
import shlex


PACKAGERS = frozenset(("electron-builder", "electron-packager", "@electron/packager",
                       "pkg", "@yao-pkg/pkg", "nexe", "@vercel/ncc"))
COMMANDS = {"ncc": "@vercel/ncc"}
BUILDER_CONFIGS = ("electron-builder.yml", "electron-builder.yaml",
                   "electron-builder.json", "electron-builder.json5",
                   "electron-builder.js", "electron-builder.cjs",
                   "electron-builder.ts")
DEPENDENCIES = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
#: options of the command line that name the directory a packager reads
PROJECT_DIR_OPTIONS = ("--projectDir", "--project-dir", "--project")
APP_DIR_PREFIXES = ("-c.directories.app=", "--config.directories.app=",
                    "-c.directories.app", "--config.directories.app")


class _ScriptStream(io.StringIO):
    """Let shlex skip comments while preserving shell command newlines."""

    def __init__(self, script):
        super().__init__(script)
        self.source = script

    def readline(self, size=-1):
        # shlex calls readline after an unquoted/unescaped comment marker.
        start = self.tell() - 1
        line = super().readline(size)
        if line.endswith("\n"):
            self.seek(self.tell() - 1)
            line = line[:-1]
        end = self.tell()
        # Keep offsets stable, but exclude comment text from token spelling.
        self.source = self.source[:start] + " " * (end - start) + self.source[end:]
        return line


def _script_segments(script):
    """Word lists of the simple commands of a shell fragment.

    POSIX shlex handles quoting and escapes, including within assignments.
    Inspect each token's source spelling to distinguish operators from quoted
    or escaped arguments that shlex also returns as ';', '|', etc.
    This is declaration detection, not an interpreter for shell programs.
    """
    # shlex's punctuation_chars=True set, plus shell command newlines.
    stream = _ScriptStream(script)
    lexer = shlex.shlex(stream, posix=True,
                        punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    segments = [[]]
    start = 0
    for word in lexer:
        # Punctuation lookahead is held by shlex rather than consumed as part
        # of this token; subtract it to recover the original token spelling.
        end = lexer.instream.tell() - len(lexer._pushback_chars)
        spelling = stream.source[start:end].strip(" \t\r")
        start = end
        if spelling and all(char in ";&|()\n" for char in spelling):
            segments.append([])
        else:
            segments[-1].append(word)
    return segments


def _npx_words(words):
    """The command and its arguments after the flags of `npx`/`npm exec`."""
    words = list(words)
    while words and words[0].startswith("-"):
        option = words.pop(0)
        if option == "--":
            break
        if option in ("-p", "--package", "--cache", "--userconfig") and words:
            words.pop(0)
    if not words:
        return None, []
    return words[0], words[1:]


def _script_command(words):
    """(command word, arguments) of one simple command, or (None, [])."""
    words = list(words)
    while words and "=" in words[0] and not words[0].startswith("-"):
        words.pop(0)  # leading environment assignments
    if not words:
        return None, []
    command = words.pop(0)
    if command == "npm":
        if not words or words.pop(0) not in ("exec", "x"):
            return None, []
        return _npx_words(words)
    if command == "npx":
        return _npx_words(words)
    return command, words


def _normalized_command(word):
    command = word.rsplit("/.bin/", 1)[-1]
    if command.startswith("./"):
        command = command[2:]
    if "@" in command[1:]:
        command = command.rsplit("@", 1)[0]
    return COMMANDS.get(command, command)


def _script_commands(script):
    """Command names in simple shell pipelines and npm/npx invocations."""
    for words in _script_segments(script):
        command, _args = _script_command(words)
        if command is not None:
            yield _normalized_command(command)


def _script_invocations(manifest):
    """(tool, arguments) for each packager command in the manifest scripts."""
    scripts = manifest.get("scripts", {})
    if not isinstance(scripts, dict):
        raise ValueError("package.json scripts must be an object")
    for script in scripts.values():
        if not isinstance(script, str):
            raise ValueError("package.json script must be a string")
        for words in _script_segments(script):
            command, args = _script_command(words)
            if command is None:
                continue
            tool = _normalized_command(command)
            if tool in PACKAGERS:
                yield tool, args


def _manifest(pkg_dir):
    """The manifest of `pkg_dir`, validated enough to make the guard safe.

    A broken manifest is an error, rather than permission to create a link.
    Scripts hidden behind JS or external shell files cannot be inferred.
    """
    with open(os.path.join(pkg_dir, "package.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    if not isinstance(manifest, dict):
        raise ValueError("package.json must be an object")
    for field in DEPENDENCIES:
        dependencies = manifest.get(field, {})
        if not isinstance(dependencies, dict):
            raise ValueError("package.json %s must be an object" % field)
    scripts = manifest.get("scripts", {})
    if not isinstance(scripts, dict):
        raise ValueError("package.json scripts must be an object")
    for script in scripts.values():
        if not isinstance(script, str):
            raise ValueError("package.json script must be a string")
    return manifest


def _declared(manifest, pkg_dir):
    """Packagers declared by dependencies, configuration keys or files."""
    found = set()
    if "build" in manifest or any(os.path.isfile(os.path.join(pkg_dir, name))
                                  for name in BUILDER_CONFIGS):
        found.add("electron-builder")
    if "pkg" in manifest:
        found.add("pkg")
    if "nexe" in manifest:
        found.add("nexe")
    for field in DEPENDENCIES:
        for name, spec in manifest.get(field, {}).items():
            if name in PACKAGERS:
                found.add(name)
            if isinstance(spec, str) and spec.startswith("npm:"):
                target = spec[4:].rsplit("@", 1)[0] if "@" in spec[5:] else spec[4:]
                if target in PACKAGERS:
                    found.add(target)
    return found


def _app_option(args):
    """Directories named by electron-builder's project options or config overrides."""
    found = []
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        name, eq, value = arg.partition("=")
        if name in PROJECT_DIR_OPTIONS:
            if not eq and index < len(args):
                value = args[index]
                index += 1
            if value:
                found.append(value)
            continue
        for prefix in APP_DIR_PREFIXES:
            if arg.startswith(prefix):
                value = arg[len(prefix):]
                if not value and not arg.endswith("=") and index < len(args):
                    value = args[index]     # `-c.directories.app app`
                    index += 1
                if value:
                    found.append(value)
                break
    return found


def _target_args(tool, args):
    """Directories a packager command targets; None means its own package."""
    if tool == "electron-builder":
        return _app_option(args) or [None]
    # A package that invokes pkg, nexe or ncc builds from its own tree; their
    # entry arguments are not resolved here (documented in
    # docs/SHARED-DEPENDENCIES.md).
    return [None]


def _yaml_scalar(text):
    text = text.strip()
    if not text:
        return None
    if text[0] in "\"'":
        end = text.find(text[0], 1)
        return text[1:end] if end > 0 else None
    return text.split(" #", 1)[0].strip() or None


def _yaml_app_dir(text):
    """`directories.app` of a top-level electron-builder YAML config.

    Deliberately a small reader, not a YAML parser: only the one key the guard
    needs is read, and a construct it does not understand falls back to the
    package's own directory.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)directories\s*:\s*(.*?)\s*$", line)
        if not match:
            continue
        base = len(match.group(1))
        inline = match.group(2)
        if inline.startswith("{"):
            app = re.search(r"(?:^|[,{\s])app\s*:\s*([^,}]+)", inline)
            if app:
                return _yaml_scalar(app.group(1))
            continue
        for child in lines[index + 1:]:
            if not child.strip() or child.lstrip().startswith("#"):
                continue
            indent = len(child) - len(child.lstrip())
            if indent <= base:
                break
            app = re.match(r"^\s*app\s*:\s*(.+?)\s*$", child)
            if app:
                return _yaml_scalar(app.group(1))
    return None


def _json_app_dir(text):
    """`directories.app` of a JSON/JSON5 electron-builder config, best effort."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        directories = data.get("directories")
        if isinstance(directories, dict) and isinstance(directories.get("app"), str):
            return directories["app"]
    match = re.search(r'"directories"\s*:\s*\{[^{}]*"app"\s*:\s*"([^"]+)"', text)
    return match.group(1) if match else None


def _config_app_dirs(pkg_dir, manifest):
    """`directories.app` targets declared by the package's build configuration."""
    found = []
    build = manifest.get("build")
    if isinstance(build, dict):
        directories = build.get("directories")
        if isinstance(directories, dict) and isinstance(directories.get("app"), str):
            found.append(directories["app"])
    for name in BUILDER_CONFIGS:
        path = os.path.join(pkg_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read(1024 * 1024)
        except OSError:
            continue
        if name.endswith((".yml", ".yaml")):
            app = _yaml_app_dir(text)
        elif name.endswith((".json", ".json5")):
            app = _json_app_dir(text)
        else:
            app = None              # JS/TS config: dynamic, not inferred
        if app:
            found.append(app)
    return found


def packagers(pkg_dir):
    """Read declarations and script commands, without running package code.

    A broken manifest is an error, rather than permission to create a link.
    Scripts hidden behind JS or external shell files cannot be inferred.
    """
    manifest = _manifest(pkg_dir)
    found = _declared(manifest, pkg_dir)
    for tool, _args in _script_invocations(manifest):
        found.add(tool)
    return sorted(found)


def targets(pkg_dir):
    """What this manifest packages: `{tool: [target, ...]}`.

    A target is a directory as written in the manifest (relative or absolute),
    or None for the manifest's own package.  The caller resolves the relative
    ones (against the manifest and against the repository root, because an npm
    script may be run from either) and guards the packages they contain.
    """
    manifest = _manifest(pkg_dir)
    config_apps = _config_app_dirs(pkg_dir, manifest)
    out = {}
    for tool in _declared(manifest, pkg_dir):
        if tool == "electron-builder" and config_apps:
            out.setdefault(tool, []).extend(config_apps)
        else:
            out.setdefault(tool, []).append(None)
    for tool, args in _script_invocations(manifest):
        values = _target_args(tool, args)
        if values == [None] and tool == "electron-builder" and config_apps:
            values = list(config_apps)
        out.setdefault(tool, []).extend(values)
    for tool, values in out.items():
        seen = []
        for value in values:
            if value not in seen:
                seen.append(value)
        out[tool] = seen
    return out


def local_reason(tools):
    return ("%s needs a local node_modules for packaging; run `ameesh deps unlink` "
            "in this package before the release build" % ", ".join(tools))


def release_bundle_warning(pkg_dir):
    """Advise for declared Expo/Next tooling without executing Next config.

    Next configuration is arbitrary code: warn conditionally for standalone
    output. This advisory never restricts development links.
    """
    with open(os.path.join(pkg_dir, "package.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    tools = set()
    for field in DEPENDENCIES:
        for name, spec in manifest.get(field, {}).items():
            target = name
            if isinstance(spec, str) and spec.startswith("npm:"):
                target = spec[4:].rsplit("@", 1)[0] if "@" in spec[5:] else spec[4:]
            if target in ("expo", "next"):
                tools.add(target)
    for script in manifest.get("scripts", {}).values():
        tools.update(command for command in _script_commands(script)
                     if command in ("expo", "next"))
    builds = []
    if "expo" in tools:
        builds.append("expo export (release/OTA)")
    if "next" in tools:
        builds.append("next build with output: 'standalone'")
    if builds:
        return ("shared-cache node_modules can embed cache paths in %s; "
                "run `ameesh deps unlink` in this package before the release build"
                % " or ".join(builds))
    return None
