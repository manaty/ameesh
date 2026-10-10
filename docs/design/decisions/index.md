# Décisions du propriétaire

* [Nom : ameesh, logo carriole, dépôt manaty/ameesh](0001-nom-ameesh.md) - Le paquet agent-mesh devient ameesh ; renommage complet immédiat ; dépôt privé puis public à la première release.
* [Portée : coordonner des équipes mixtes humains + agents pour tout travail de bureau](0002-portee-equipes-mixtes.md) - ameesh ne sert pas qu'au développement logiciel : il coordonne humains et agents, sur appareils et serveurs, pour tout métier de bureau.
* [ameesh est un produit autonome, utilisé par Nexlink et intégré nativement](0003-ameesh-et-nexlink.md) - Partage des rôles entre Nexlink (identités, pages, messages, signatures humaines) et ameesh (exécution).
* [Alternatives open source et connecteurs vers les outils courants](0004-open-source-et-outils-courants.md) - ameesh doit tourner sur une pile open source (quitte à créer la brique manquante) et se connecter aux outils courants comme Slack.
* [Le canon est en OKF dans git, fédéré selon OKF Federation](0005-canon-okf.md) - Équipes, responsabilités, structure des projets sont décrites en OKF ; Nexlink et les outils n'en sont que des vues ou des interfaces de proposition.
* [Tous les échanges sont lisibles et auditables par les humains](0006-lisibilite-humaine.md) - Les agents se parlent en langage humain, dans des canaux que l'équipe peut lire ; aucun canal caché.
* [Les intégrations reprennent les standards des outils d'agents](0007-standards-des-harnais.md) - Pas de format de connecteur propre à ameesh : MCP, skills, plugins des harnais.
* [Les agents ont une fiche dans le canon OKF ; leur état reste dans ameesh](0008-agents-dans-le-canon.md) - Déclaration de l'agent (parrain, rôle, capacités, harnais, budget, outils) dans le canon ; état d'exécution dans ameesh.
* [Toute parole est signée ; pas de procédure provisoire avant ameesh v1](0009-paroles-signees.md) - Les messages des humains et des agents doivent être signés pour faire preuve ; nos sessions actuelles restent sans signature.
* [Responsabilité humaine, projets vivants, orchestrateurs multiples](0010-responsabilite-et-orchestrateurs.md) - Tout agent a un humain responsable ; un projet peut se scinder ; un humain peut avoir plusieurs orchestrateurs et un projet plusieurs humains. mesh-design devient l'orchestrateur d'ameesh.
* [L'hébergement est une configuration, préparée par le harnais de l'utilisateur](0011-hebergement-configurable.md) - Local, page Nexlink ou VPS provisionné ; installation conduite par le harnais que l'utilisateur a déjà.
* [Abandon de la cérémonie Ed25519 ; autorité par ameesh-approve](0012-autorite-par-ameesh-approve.md) - Passkey, application Nexlink ou application native ; un seul format de reçu.
* [Licence d'ameesh : AGPL-3.0-only, avec accord de contribution](0013-licence-agpl.md) - Termes figés à la v3 ; CLA pour les contributions externes.
* [Orchestrateurs à tours ; placement décidé par les humains responsables](0014-orchestrateurs-a-tours-et-placement.md) - Attachement interactif ; politique par hôte ; clé d'API ou forfait choisi par le responsable.
* [Aucun client nommé ; interfaces publiques pour bâtir les façades](0015-interfaces-des-facades.md) - Applications tierces, dont mobiles natives, sans imposer Nexlink.
* [Stockage : Postgres en v1, SQL derrière une interface, SQLite plus tard](0016-stockage.md) - Postgres installé par l'installateur en local ; pilote SQLite pour le mode perso ensuite.
* [ameesh-approve joignable via une page Nexlink](0017-approbation-via-page-nexlink.md) - Exposition HTTPS du service d'approbation pour la bascule.
* [Vitesse des agents](0018-vitesse-des-agents.md) - Revue par classe de risque, interruption, rotation de session, délais mesurés.
* [Budgets et routage](0019-budgets-et-routage.md) - Modèle et effort par tâche, plafonds, jauges de forfait lues à la source.
* [Catalogue et évaluation des modèles](0020-catalogue-et-evaluation-des-modeles.md) - Lister, découvrir, évaluer les modèles pour guider le routage modèle/effort.
* [Catalogue des harnais](0021-catalogue-des-harnais.md) - Harnais décrits, certifiés et évalués par un service central au lieu d'être codés en dur.
* [Auto-réparation des bugs](0022-auto-reparation.md) - Signaux, triage au canon, réparation, vérification, livraison, surveillance.
* [Critère d'adoption des réglages](0023-critere-d-adoption-des-reglages.md) - Gain significatif de délai ou de coût, sans perte de qualité globale après revue et recette.
* [Vue temps réel de l'avancement](0024-vue-temps-reel.md) - Frise des lots, agents, jalons et budget, alimentée par les événements d'ameesh.
* [Une session neuve par lot](0025-une-session-par-lot.md) - Construction et relecture : session neuve par lot, gardée jusqu'à la fusion ; orchestrateurs tournés avec résumé.
* [Hébergement d'ameesh-approve par équipe](0026-hebergement-d-ameesh-approve-par-equipe.md) - PC + relais, page Nexlink ou serveur propre ; RP ID exactement égal à l'hôte de l'équipe.
* [Bascule automatique entre comptes](0027-bascule-automatique-entre-comptes.md) - Compte secondaire quand le primaire approche sa limite, retour après la remise à zéro.
* [Ressources des hôtes et répartition](0028-ressources-des-hotes-et-repartition.md) - Mesure, contre-pression, ressources orphelines, déplacement des agents entre hôtes admis.
* [Persona et session](0029-persona-et-session.md) - Identité durable et mémoire séparées des exécutions ; double numérique des humains ; le canon déclare des règles de placement, pas la position.
* [Pas de travail sans réveil possible](0030-pas-de-travail-sans-reveil-possible.md) - Attribution gardée, adoption et reprise par ameesh, alertes de vivacité poussées, délégation à échéance, identité jamais tirée du dossier.
* [Plusieurs canons sur un même hôte](0031-plusieurs-canons.md) - Liste de canons, identité par fédération, synchronisation bornée au canon, noms au premier déclarant ; lots L42–L45, L47.
* [Consommer d'abord ce qui expire](0034-consommer-d-abord-ce-qui-expire.md) - Les comptes au forfait forment un réservoir : chaque tour prend le compte dont la capacité inutilisée expire le plus tôt, sous le rythme permis ; amende 0027.
