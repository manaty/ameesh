# SPDX-License-Identifier: AGPL-3.0-only
"""agent-mesh — boîte aux lettres durable et exécuteur multi-harnais.

v1 (points 1–2) : `agent_mailbox` + `agent_registry` sur Postgres, migrations
versionnées, LISTEN/NOTIFY, CLI `agent-mail` compatible v0, un `agent-runner`
par machine avec baux et adaptateurs claude/codex/deepseek.
"""
__version__ = "0.1.0"


def version() -> str:
    """La version du paquet ameesh installé (`ameesh --version`, `ameesh
    doctor`), lue par `importlib.metadata`. Repli, pour un arbre source lancé
    sans installation (`PYTHONPATH=src`, la CI) : le `pyproject.toml` voisin.
    `inconnue` si ni l'un ni l'autre ne la donne.

    `__version__` ci-dessus est un numéro de protocole figé (client ACP), pas
    la version du paquet."""
    from importlib import metadata
    try:
        return metadata.version("ameesh")
    except metadata.PackageNotFoundError:
        pass
    import os
    import re
    chemin = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          os.pardir, os.pardir, "pyproject.toml")
    try:
        with open(chemin, encoding="utf-8") as fh:
            texte = fh.read()
    except OSError:
        return "inconnue"
    trouve = re.search(r'^version\s*=\s*"([^"]+)"', texte, re.M)
    return trouve.group(1) if trouve else "inconnue"
