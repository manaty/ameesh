# Pré-geste : prototype #7761

Chaque reçu comporte un `pregeste_id` UUID. Pour un dispatch direct
`gh workflow run` de type prod, le préfixe ajoute l'entrée
`-f pregeste=<pregeste_id>` ; le workflow doit publier cet identifiant dans
son titre. La passe élue `github-watch` collecte les runs des workflows
inventoriés dans `projects.<projet>.production.workflows` du canon appliqué,
pour les dépôts déclarés par les agents de veille de ce projet. Aucun projet
ni fichier de workflow n'est inscrit dans le code du collecteur.

La commande `ameesh gate pre-act --lot ID --kind kms|key|prod --since ISO`
relit le journal et le courrier puis écrit un reçu sur le lot si aucun motif
de refus n'est trouvé. `--since` est un horodatage ISO avec fuseau, capturé
avant la planification ou l'attente, puis conservé jusqu'au contrôle. Une
date future, une identité absente, une base illisible ou une écriture de reçu
impossible refusent le passage. La commande ne remplace pas les autorisations
requises pour le geste.

Le contrôle détecte les holds explicites contenant un mot d'arrêt (par exemple
`hold: STOP`), les mots d'arrêt dans les autres notes et les
messages associés au lot, les messages non livrés à l'agent dont le lot est
référencé, et les décisions retirées pendant la fenêtre. Un hold d'arrêt ancien
non levé est également bloquant ; un hold d'attente ordinaire ne bloque pas.
Les notes et courriers de l'acteur lui-même sont ignorés. Le texte libre est
reconnu de façon prudente,
mais une formulation imprévue peut échapper à cette détection ; une référence
prose doit contenir `#ID` si le courrier ne porte pas le champ lot structuré.
Un courrier de session mentionnant seulement « MP-2 », tel que #225697, ne
sera pas détecté sans association structurée ou référence au lot : limite connue.

Le reçu constate une lecture ponctuelle : il ne verrouille ni les nouveaux
messages ni le service externe. Un STOP peut arriver entre la lecture et
l'effet. Une intégration doit effectuer la vérification immédiatement avant
l'effet, arrêter sur tout code non nul, et ne jamais remettre à zéro la borne
après une attente. Pour une garantie contre cette course, il faut un protocole
commun de sérialisation avec les producteurs d'arrêt et le service qui agit.

Ce prototype ne modifie pas les scripts du bureau ni les dispatchs de
production. Les runners de production vérifient `prod-go/1` hors ligne sans
accès à la base du mesh (voir `docs/prod-go.md`) ; l'appel de cette commande
doit donc être placé sur le poste de dispatch, avant l'envoi, et ses autres
chemins d'accès doivent être examinés. Une fois le run envoyé, un STOP exige
l'annulation du run et la tenue de `prod-deploy` pendant la fenêtre signée.

Un appel peut placer la commande d'effet après `--` : le processus de garde
enregistre le reçu, ferme la connexion puis exécute directement cette
commande, sans interpréteur ajouté. Pour toute attente programmée, utiliser
`--wait-until ISO -- commande` : le garde capture lui-même la borne avant
d'attendre, puis contrôle et exécute. `--since` reste réservé aux scripts qui
ont capturé la borne avant leur attente. Une attente ou un autre travail à
l'intérieur de la commande relance la même faille. Un refus
n'exécute pas la commande.

Le reçu écrit une preuve structurée `pre-act/1` (lot, acteur, type de geste,
cible) et utilise l'horloge de la base pour sa date. `--target` est obligatoire
pour un geste prod. La convention de cible permettant la corrélation avec un
run GitHub est l'UUID exact présent dans le titre `pregeste=<UUID>` : aucun
rapprochement par acteur ou proximité temporelle n'est effectué. Les audits KMS/IAM
restent hors de cette tranche (#225809), à traiter dans #7767.

Selon bureau (courrier #225756, 18/11), p2a et la sauvegarde sont jetables
sur bureau-goal ; les déploiements utilisent des appels directs à `gh` et les
migrations `scripts/migrate.sh` dans Nexlink. Le préfixe est donc volontaire :
il ne bloque pas les appels directs, ni un STOP entre contrôle et effet, ni
un STOP après le dispatch. Son adoption ne constitue pas une barrière imposée
aux identifiants KMS, à GitHub ou à la base. L'orchestrateur accepte cette
fenêtre pour la première tranche (#225799) ; l'accès imposé aux identifiants
relève du lot compagnon #7767, avec décision owner. La garde refuse de lancer
la commande si plus de 30 secondes se sont écoulées depuis le début du
contrôle, borne conservatrice par rapport à l'écriture du reçu.

Le premier passage enregistre l'activation en base, séparément pour chaque
projet, dépôt et workflow : aucune reprise historique. La lecture attend cinq
minutes pour l'indexation GitHub puis recommence au plus toutes les quinze
minutes (ou à la cadence de veille déclarée si elle est plus courte). Elle
relit depuis l'activation : une relance conserve la date de création initiale.
Les fenêtres saturées à 1 000 résultats sont divisées en sous-fenêtres. Une
réponse incomplète, périmée ou illisible conserve le curseur précédent.
Ce premier lecteur échange un coût de lecture croissant avec l'historique
contre la détection des relances ; les runs créés avant l'activation, y compris
leurs relances, restent hors de son périmètre sans reprise historique.

L'état partagé (`pre_act_audit_cursors`, `pre_act_audit_runs`, migration 0593)
survit aux redémarrages et changements d'hôte. Le contrôle traite séparément
chaque tentative d'un run, y compris les tentatives intermédiaires lorsque
plusieurs relances ont eu lieu entre deux passes. La consommation de l'UUID
est unique entre dépôts et workflows. Les reçus absents, invalides, inconnus,
ambigus ou déjà consommés déclenchent un courrier à l'agent portant le rôle
canonique `auditeur`. La consommation, le run, l'alerte et le curseur sont
validés dans une transaction ; un échec d'envoi annule cet ensemble.

Seuls les workflows déclarés dans `production.mode_marked_workflows`
(sous-ensemble de l'inventaire) peuvent être exclus selon les marqueurs du
titre : `environment=staging` ou `observe_only=true`, tous deux explicites et
non ambigus. Le mode mutateur est `environment=production observe_only=false`.
Un marqueur absent, inconnu ou dupliqué déclenche une alerte de mode. Les
workflows prod ordinaires sont tous contrôlés, quelle que soit leur branche.
Le lecteur compatible et la migration additive doivent être déployés avant
l'activation de la déclaration canonique (proposition home#304).
