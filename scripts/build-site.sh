#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Construit le site public dans _site/ : la page d'accueil (site/landing/) à la
# racine, la documentation MkDocs (site/docs/, mkdocs.yml) sous _site/docs/.
#
#   scripts/build-site.sh            # MkDocs pris dans .venv-site/ (créé au besoin)
#   MKDOCS=mkdocs scripts/build-site.sh   # un mkdocs déjà installé (CI)
#
# Aperçu local : python3 -m http.server -d _site 8000  →  http://127.0.0.1:8000/
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${MKDOCS:-}" ]; then
  if [ ! -x .venv-site/bin/mkdocs ]; then
    python3 -m venv .venv-site
    .venv-site/bin/pip install --quiet -r site/requirements.txt
  fi
  MKDOCS=.venv-site/bin/mkdocs
fi

rm -rf _site
"$MKDOCS" build --strict --site-dir _site/docs
# site/landing/CNAME arrive ainsi à la racine : il documente le domaine
# personnalisé (https://ameesh.manaty.net) ; avec un déploiement par Actions,
# c'est le réglage Pages du dépôt qui fait foi.
cp -R site/landing/. _site/
# GitHub Pages : servir les fichiers tels quels (pas de Jekyll)
touch _site/.nojekyll
echo "site construit dans _site/ (accueil : _site/index.html, documentation : _site/docs/)"
