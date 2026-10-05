# SPDX-License-Identifier: AGPL-3.0-only
# Image de l'exécuteur ameesh (profil « cluster », docs/profils/cluster.md).
#
#   docker build -t ameesh-runner:dev .
#   docker run --rm ameesh-runner:dev ameesh --help
#
# Contenu : Python slim, ameesh installé depuis ce dépôt (avec psycopg), git et
# ssh pour lire le canon, tini comme PID 1 (relaie SIGTERM à l'exécuteur et
# récupère les processus orphelins des harnais). Utilisateur non root
# `ameesh` (uid 10001), HOME=/home/ameesh — monté sur un volume persistant
# dans le cluster (sessions des harnais, état d'ameesh, fils lisibles).
#
# AUCUN SECRET dans l'image : DSN/mot de passe, clés d'API des fournisseurs et
# clé de déploiement du canon arrivent à l'exécution (Secret Kubernetes).
#
# HARNAIS. Par défaut, aucun harnais n'est installé (image de base : canon,
# migrations, sonde, outils). Deux façons d'en ajouter un :
#
#   1. argument de construction HARNESS_NPM — paquets npm installés
#      globalement avec Node.js de la distribution, par exemple :
#        docker build --build-arg HARNESS_NPM=@anthropic-ai/claude-code -t ameesh-runner:claude .
#        docker build --build-arg HARNESS_NPM=@openai/codex -t ameesh-runner:codex .
#      Le téléchargement de ces paquets est gratuit ; leur USAGE demande des
#      identifiants (clé d'API en Secret) et suit les conditions du fournisseur.
#      Épinglez une version (`paquet@x.y.z`) pour une image reproductible ;
#   2. image dérivée — pour un harnais distribué autrement (DeepSeek Harness
#      `dsh`, binaire maison…) :
#        FROM ameesh-runner:dev
#        COPY --chown=root:root dsh /usr/local/bin/dsh
#      ameesh trouve le binaire par AMEESH_<HARNAIS>_BIN, AMEESH_BIN_DIR ou le
#      PATH (src/ameesh/adapters.py).

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# --- construction de la roue (le dépôt ne va pas dans l'image finale) -------
FROM ${PYTHON_IMAGE} AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

# --- image d'exécution -------------------------------------------------------
FROM ${PYTHON_IMAGE}

ARG HARNESS_NPM=""

LABEL org.opencontainers.image.title="ameesh-runner" \
      org.opencontainers.image.description="Exécuteur ameesh (un agent persistant par pod)" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git openssh-client tini ca-certificates; \
    if [ -n "$HARNESS_NPM" ]; then \
        apt-get install -y --no-install-recommends nodejs npm; \
        npm install --global --omit=dev --no-fund --no-audit $HARNESS_NPM; \
        npm cache clean --force; \
    fi; \
    rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/ameesh-*.whl 'psycopg[binary]>=3.1' \
    && rm -rf /wheels

RUN useradd --uid 10001 --user-group --create-home --home-dir /home/ameesh \
        --shell /usr/sbin/nologin ameesh

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/ameesh \
    AMEESH_CONFIG=/etc/ameesh/config/config.json

USER 10001:10001
WORKDIR /home/ameesh

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["agent-runner", "--poll", "5"]
