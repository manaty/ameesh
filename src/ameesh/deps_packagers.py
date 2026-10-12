# SPDX-License-Identifier: AGPL-3.0-only
"""L248b : packages whose release needs a local node_modules tree."""
import json
import os
import shlex


PACKAGERS = frozenset(("electron-builder", "electron-packager", "@electron/packager",
                       "pkg", "@yao-pkg/pkg", "nexe", "@vercel/ncc"))
COMMANDS = {"ncc": "@vercel/ncc"}
BUILDER_CONFIGS = ("electron-builder.yml", "electron-builder.yaml",
                   "electron-builder.json", "electron-builder.json5",
                   "electron-builder.js", "electron-builder.cjs",
                   "electron-builder.ts")
DEPENDENCIES = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")


def _script_commands(script):
    """Command positions in simple shell pipelines and npm/npx invocations.

    Keep quotes until after splitting: an echoed ';' is not a separator.
    This is declaration detection, not an interpreter for shell programs.
    """
    lexer = shlex.shlex(script, posix=False, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    segments = [[]]
    for word in lexer:
        if word and all(char in ";&|()\n" for char in word):
            segments.append([])
        else:
            segments[-1].append(word)
    for segment in segments:
        words = [shlex.split(word)[0] if word[:1] in ("'", '"') else word
                 for word in segment]
        while words and "=" in words[0] and not words[0].startswith("-"):
            words.pop(0)  # leading environment assignments
        if not words:
            continue
        command = words.pop(0)
        if command == "npm":
            if not words or words.pop(0) not in ("exec", "x"):
                continue
        elif command != "npx":
            yield command
            continue
        # npm exec/npx: flags precede the command, not arbitrary arguments.
        while words and words[0].startswith("-"):
            option = words.pop(0)
            if option == "--":
                break
            if option in ("-p", "--package", "--cache", "--userconfig") and words:
                words.pop(0)
        if words:
            yield words[0]


def packagers(pkg_dir):
    """Read declarations and script commands, without running package code.

    A broken manifest is an error, rather than permission to create a link.
    Scripts hidden behind JS or external shell files cannot be inferred.
    """
    with open(os.path.join(pkg_dir, "package.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    if not isinstance(manifest, dict):
        raise ValueError("package.json must be an object")
    found = set()
    if "build" in manifest or any(os.path.isfile(os.path.join(pkg_dir, name))
                                  for name in BUILDER_CONFIGS):
        found.add("electron-builder")
    if "pkg" in manifest:
        found.add("pkg")
    if "nexe" in manifest:
        found.add("nexe")
    for field in DEPENDENCIES:
        dependencies = manifest.get(field, {})
        if not isinstance(dependencies, dict):
            raise ValueError("package.json %s must be an object" % field)
        for name, spec in dependencies.items():
            if name in PACKAGERS:
                found.add(name)
            if isinstance(spec, str) and spec.startswith("npm:"):
                target = spec[4:].rsplit("@", 1)[0] if "@" in spec[5:] else spec[4:]
                if target in PACKAGERS:
                    found.add(target)
    scripts = manifest.get("scripts", {})
    if not isinstance(scripts, dict):
        raise ValueError("package.json scripts must be an object")
    for script in scripts.values():
        if not isinstance(script, str):
            raise ValueError("package.json script must be a string")
        for word in _script_commands(script):
            command = word.rsplit("/.bin/", 1)[-1]
            if command.startswith("./"):
                command = command[2:]
            if "@" in command[1:]:
                command = command.rsplit("@", 1)[0]
            command = COMMANDS.get(command, command)
            if command in PACKAGERS:
                found.add(command)
    return sorted(found)


def local_reason(tools):
    return ("%s needs a local node_modules for packaging; run `ameesh deps unlink` "
            "in this package before the release build" % ", ".join(tools))
