#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Construit l'archive de la fonction : index frais + code, sans dépendance.
#
#   chatbot/package.sh            # → chatbot/dist/chatbot.zip
set -euo pipefail
cd "$(dirname "$0")"
rm -rf dist
mkdir -p dist/pkg/ameesh_chat
python3 build_index.py --out dist/pkg/index.json
cp handler.py dist/pkg/
cp ameesh_chat/*.py dist/pkg/ameesh_chat/
(cd dist/pkg && python3 -m zipfile -c ../chatbot.zip handler.py index.json ameesh_chat)
echo "archive : chatbot/dist/chatbot.zip"
