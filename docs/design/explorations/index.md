---
type: Index
status: exploration
title: "Explorations"
---

# Explorations

Une exploration conserve une idée hors de la feuille de route. Un fichier par
idée, avec `type: Exploration` et `status: exploration`, décrit uniquement le
problème, les intuitions et les questions ouvertes. Les liens vers les études,
les décisions ou les documents existants apportent du contexte.

Ces fiches ne portent ni lots proposés, ni dates, ni estimations, ni critère
« fait ». Aucun agent n’en déduit un lot, un WorkPackage, une entrée de backlog
ou une ligne de roadmap. La promotion vers une [étude](../etudes/) puis vers
des lots exige une décision explicite de l’owner, consignée dans
[decisions/](../decisions/) ; l’exploration seule n’est pas une autorisation.
Une fiche de plan, de roadmap ou de lot référence alors l’étude décidée,
jamais cette réserve d’idées.

La règle commune est publiée dans le
[Processus de lot, §4](https://github.com/manaty/home/blob/main/processus/lots.md#4-files-de-travail),
après fusion de [home#52](https://github.com/manaty/home/pull/52).

- [Provisionnement d’ameesh-approve hébergé](ameesh-approve-heberge.md)
- [Évaluation des modèles](evaluation-modeles.md)
- [Descripteurs de harnais et conformité](descripteurs-harnais.md)
- [Signaux et boucle d’auto-réparation](auto-reparation.md)
- [Transports supplémentaires pour les fils](transports.md)
- [Porte exposée comme proxy MCP](proxy-mcp.md)
- [Pilote SQLite pour un profil personnel](sqlite.md)
- [Un utilisateur Unix par agent](uid-par-agent.md)
- [Installeur piloté par le harnais de l’utilisateur](installeur.md)

La garde `tests.test_l1124_explorations` contrôle les fiches et les références
locales depuis les plans, roadmaps et lots. Elle fait partie des lecteurs de
documentation en CI. Les lots et plans du canon externe ne sont pas présents
dans ce dépôt : leur validation appartient au canon.
