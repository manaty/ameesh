# Pré-geste : prototype #7761

La commande `ameesh gate pre-act --lot ID --kind kms|key|prod --since ISO`
relit le journal et le courrier puis écrit un reçu sur le lot si aucun motif
de refus n'est trouvé. `--since` est un horodatage ISO avec fuseau, capturé
avant la planification ou l'attente, puis conservé jusqu'au contrôle. Une
date future, une identité absente, une base illisible ou une écriture de reçu
impossible refusent le passage. La commande ne remplace pas les autorisations
requises pour le geste.

Le contrôle détecte les notes `hold:`, les mots d'arrêt dans les notes et les
messages associés au lot, les messages non livrés à l'agent dont le lot est
référencé, et les décisions retirées pendant la fenêtre. Un hold ancien non
levé est également bloquant. Le texte libre est reconnu de façon prudente,
mais une formulation imprévue peut échapper à cette détection ; une référence
prose doit contenir `#ID` si le courrier ne porte pas le champ lot structuré.

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
commande, sans interpréteur ajouté. La borne `--since` reste celle capturée
avant l'attente. Il faut placer l'attente avant cet appel : une attente ou un
autre travail à l'intérieur de la commande relance la même faille. Un refus
n'exécute pas la commande.

Le reçu écrit une preuve structurée `pre-act/1` (lot, acteur, type de geste,
cible) et utilise l'horloge de la base pour sa date. `--target` est obligatoire
pour un geste prod. La convention de cible permettant la corrélation avec un
run GitHub et la correspondance entre identités mesh et GitHub restent à
définir : le contrôle détectif n'est pas encore intégré. Les audits KMS/IAM
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
