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
* [Licence d'ameesh : AGPL v3](0013-licence-agpl.md) - Publication sous GNU AGPL v3 ; double licence possible pour Manaty.
