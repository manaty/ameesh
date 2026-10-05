# SPDX-License-Identifier: AGPL-3.0-only
"""L'assistant du site public d'ameesh (L33).

Une fonction serverless qui répond aux questions sur ameesh à partir de la
documentation publique : recherche lexicale dans un index de passages construit
au déploiement (`build_index.py`), consigne système stricte, modèle compatible
OpenAI (DeepSeek « flash » par défaut), plafond de dépense, limites de débit,
aucune conversation conservée ni journalisée.

Aucune dépendance hors de la bibliothèque standard, et aucune dépendance au
paquet `ameesh` : la fonction se déploie seule.
"""
