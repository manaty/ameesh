# Contrat Nexlink à qualifier

`candidate.json` est une carte de sources, sans contrat exécutable ni
activation. Elle porte sur l'union sans conflit de :

- paquet-5 / develop `d73f3602898f878b40f1cd49ea69f770fe23a426` ;
- #1470 `6553d3ae11741f4163d6e65b450c97a40e9e2c17` ;
- arbre `a252cec5d7569d2a645b32a7f03c014f68363da8`.

La lecture porte sur les objets Git, sans checkout, branche, poussée ou CI.
Elle énumère vingt workflows de code et les jobs, conditions et matrices
déclarés, avec vingt-neuf blobs de workflows et de sélection. Les workflows
réutilisables locaux sont inclus. La fermeture de toutes les dépendances de
scripts et configurations de paquets reste à achever avant contrat exécutable.

La tête de #1470 seule ne contient pas `civil-crypto.yml` et `lot-plan.yml` ;
elle ne suffit donc pas à décrire les suites du paquet-5. Le sélecteur de
#1470 force bien `integration/**` et `train-*` en mode complet, y compris au
dispatch ; `related-tests.mjs` conserve ce mode sans lancer de sélection.

La liste de vingt suites de code, la portée des plateformes et le traitement
de Docs gate sont soumis au pilote. Docs gate possède la classification
documentation seule ; les jobs `lot-plan`, statut et reprise du plan ne
remplacent aucune suite exécutée. Les plateformes hébergées et le pont vers
un appareil ne sont pas activés par cette carte.

Les éléments auteur reçus le 15 octobre à 14:14 citent des exécutions réelles
sur paquet-5 ; ils ne donnent aucune exécution sur `integration/train-*`.
Aucun résultat de paquet-5 n'est transféré au futur train. Les blobs liés à
chaque futur manifeste devront correspondre à sa composition finale exacte.
