# SPDX-License-Identifier: AGPL-3.0-only
"""L248b : packages whose release needs a local node_modules tree."""
import json
import os
import shlex


PACKAGERS = frozenset(("electron-builder", "electron-packager", "@electron/packager",
                       "pkg", "@yao-pkg/pkg", "nexe", "@vercel/ncc"))
COMMANDS = {"ncc": "@vercel/ncc"}
DEPENDENCIES = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")


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
        lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
        for index, word in enumerate(words):
            command = word.rsplit("/.bin/", 1)[-1]
            if command.startswith("./"):
                command = command[2:]
            if "@" in command[1:]:
                command = command.rsplit("@", 1)[0]
            # `npm pkg set` edits metadata; it is not the `pkg` packager.
            if command == "pkg" and index and words[index - 1] == "npm":
                continue
            command = COMMANDS.get(command, command)
            if command in PACKAGERS:
                found.add(command)
    return sorted(found)


def local_reason(tools):
    return ("%s needs a local node_modules for packaging; run `ameesh deps unlink` "
            "in this package before the release build" % ", ".join(tools))
