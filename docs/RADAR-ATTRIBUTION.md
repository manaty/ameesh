# Attribution des conflits du radar

Une fusion brute de deux branches anciennes peut retrouver un conflit avec
l'histoire de leur cible, même si le second lot n'a pas changé ce contenu.
Le radar distingue ce cas avant de désigner le responsable d'une résolution.

Pour une paire en conflit dont les bases diffèrent, il effectue deux fusions
témoins, dans son espace d'objets jetable : il remplace successivement chaque tête par sa base commune
avec la cible. Retirer ainsi les changements propres du partenaire permet
de vérifier si le conflit existe déjà sans eux. Les attributs de fusion sont
ceux de la cible pour les trois fusions.
Avec une même base dans la cible, les deux côtés sont déjà leurs changements
propres : la fusion brute suffit.

La preuve d'un conflit hérité exige le même groupe de chemins en conflit
et les mêmes entrées d'index : modes, objets et stages. Un renommage conserve
ensemble tous les chemins que Git rattache au conflit, y compris un nouveau
nom absent de la liste des chemins communs. Un simple nom de fichier commun
ne prouve donc ni un conflit propre ni un conflit hérité.

Le rapport JSON garde `raw_files`, les deux `bases`, `target_head`, puis
sépare `files` (interaction propre prouvée), `inherited_files` et
`uncertain_files`. Les états visibles dans `ameesh conflicts` sont :

| État | Preuve et conséquence |
| --- | --- |
| `conflict` | Un groupe de conflit n'existe dans aucune fusion témoin. Un responsable est désigné si tous les groupes sont propres. |
| `target-conflict` | Tous les groupes sont hérités ; les conflits actuels avec la cible restent signalés à leurs auteurs respectifs. Aucune attribution à la paire. |
| `superseded` | Tous les groupes sont hérités et les deux branches se fusionnent proprement avec la cible actuelle : la divergence brute appartient à l'histoire déjà raccordée. Aucun courrier de résolution de paire. |
| `unclassified` | L'identité du conflit n'est pas prouvée, ou une fusion témoin est illisible. Le conflit reste visible et signalé sans responsable automatique. |
| `overlap` | Les branches touchent les mêmes chemins mais leur fusion brute est propre. |

Une paire mixte peut garder `conflict` pour ses groupes propres et afficher
les groupes hérités ou incertains à côté. Elle ne reçoit pas de responsable :
la commande de préparation fusionnerait encore les têtes brutes et ferait
résoudre aussi le raccord d'un lot tiers. Le courrier explique cette attente.
Une incertitude sur un même fichier reste volontairement visible, même si
le conflit semble hérité : une modification distante dans ce fichier change
les objets et empêche de conclure avec cette preuve.

Les préparations enregistrées sont conservées et leur replay reste affiché,
même quand le conflit brut est reclassé. Le radar ne réécrit aucune branche,
ne rebase aucune tête relue et ne détache aucun ancien transport du registre.
Le classement n'autorise aucune fusion : la file rejoue toujours l'arbre
intégré et traite une paire `unclassified` comme non préparée.

Les témoins sont mis en cache avec les deux têtes, leurs bases et la tête
cible ; un mouvement de la cible invalide ce diagnostic. Les objets des
fusions témoins disparaissent à la fin du passage.
