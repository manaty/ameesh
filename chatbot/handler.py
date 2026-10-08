# SPDX-License-Identifier: AGPL-3.0-only
"""Point d'entrée Scaleway Functions (runtime Python) : `handler.handle`.

L'application est construite une fois par instance (index chargé, compteur lu
au premier appel). Une configuration incomplète rend 503 : la bulle du site
affiche alors son lien vers la documentation.
"""
from __future__ import annotations

import os

from ameesh_chat.app import App, Config, load_index, log_event, store_from_env

_APP = None
_HERE = os.path.dirname(os.path.abspath(__file__))


def _build() -> App:
    config = Config.from_env()
    if not config.api_key:
        raise KeyError("DEEPSEEK_API_KEY")
    index = load_index(os.environ.get("INDEX_PATH") or os.path.join(_HERE, "index.json"))
    return App(config, index, store_from_env())


def handle(event, context):  # noqa: ARG001 - signature imposée par le runtime
    global _APP
    if _APP is None:
        try:
            _APP = _build()
        except (KeyError, ValueError, OSError) as error:
            log_event(evt="chat", status=503, code="config_" + type(error).__name__)
            return {"statusCode": 503, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
                    "body": '{"error": "unavailable", "docs": "https://ameesh.org/docs/"}'}
    return _APP.handle(event)
