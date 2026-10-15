# Raccord #1467 / #1616 — 15 octobre 2026

Préparation du consommateur, non gelée. Fournisseur exact :
`ddf1091d3a47c89ca8dcc1c445221af37d828b35`, branche
`lot/1616-process-params`. Il est injecté explicitement dans `Passage` ;
aucune copie de son module n'est ajoutée à #1467.

Le même objet compilé fournit le calendrier, la classification et le nombre
d'avis, les familles autorisées et leur indépendance. Le passage refuse
un document absent/invalide, un autre sujet, une CI autre que complète et
le mode historique par intervalle. Les validations Git du diff et les
preuves historiques exactes restent exigées. Le fournisseur #1465 et la CI
distante sont simulés ; Git, Postgres, les verrous et la promotion locale
sont réels et jetables.

`probe.py` charge uniquement le blob Python du fournisseur au commit
complet, dans un répertoire temporaire. Il exécute huit scénarios ciblés :
coupure modifiée dans le canon, fuseau modifié, fichier absent, famille
exclue, nombre d'avis modifié, parcours publication/feu/promotion avec
nouvelle connexion, concurrence de runners et provenance inconnue.

```sh
ameesh heavy -- env PYTHONPATH=src:. AMEESH_DRIVER=psycopg .venv/bin/python \
  reviews/1467-mechanics-port/probe.py --supplier . \
  --commit ddf1091d3a47c89ca8dcc1c445221af37d828b35
# Même commande avec AMEESH_DRIVER=psql pour le second pilote.
```

Pas de suite du fournisseur rejouée, ni CI distante lancée. Cette preuve
de port ne remplace pas ses avis indépendants, ne choisit pas un commit
canon de production et ne constitue pas une activation du train.
