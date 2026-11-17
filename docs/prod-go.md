# Autorisation de production signée

Une demande de décision de production porte une ligne exacte, couverte par la
signature de la réponse owner :

```text
prod-go/1 project=acme sha=<40 caractères hexadécimaux> version=release-11x deploy=a migrations=a,c merge=a until=<UTC YYYY-MM-DDTHH:MM:SSZ>
```

Chaque champ d’opération désigne les options autorisées, séparées par des
virgules ; `-` interdit l’opération. La demande impose une réponse signée.
La fenêtre doit respecter `prod_go_max_hours` du canon compilé ; sans ce
paramètre, la demande est refusée. Déployer le lecteur avant d’activer le
paramètre dans le canon.

`ameesh decide export <id> --json` produit une preuve
`ameesh-prod-go-proof/1` : document de réponse, charge signée exacte, signature
Ed25519 et informations de clé publique. Le consommateur épingle séparément
la clé owner : la clé incluse dans le JSON ne constitue pas une ancre de
confiance. Il vérifie signature, digest, projet, SHA, option et expiration.
Le runner de production ne nécessite aucun accès à la base du mesh.

Une décision répondue ne peut être retirée ou remplacée avant son échéance.
Pour interrompre l’opération, annuler le run et tenir le verrou de déploiement.
Cette fenêtre irrévocable fait partie du contrat proposé et doit être relue
avant activation.

Pour une fusion `release/*` vers la branche de production déclarée dans le
canon, le corps de PR contient exactement une ligne `Production decision: #123`.
La garde commune vérifie la tête, la source et la cible actuelles de la PR,
puis exige une preuve autorisant `merge` sur cette tête. Une exception de
paquet ne dispense pas de cette autorisation. Les projets déclarés sans
branche de production restent exempts. Les fusions hors outillage ne sont pas couvertes par cette garde.

La signature créée pour une prod-go couvre au moins son échéance `until`.
Une approbation pré-signée plus courte est refusée ; l’export et le consommateur
vérifient également l’échéance signée `expires`.

Une preuve exportée est une capacité bornée et non révocable : elle reste
utilisable jusqu’à `until` (au plus `expires` signé et la borne canon), même
si le registre de clés change après l’export. La clé épinglée dans le dépôt
est l’ancre du runner ; il ne consulte pas le registre du mesh. Toute révocation
de clé owner entraîne, dans le même tour, le retrait ou le remplacement de
l’épingle de chaque dépôt consommateur. Sans cette mise à jour, le runner
continue d’accepter la clé compromise. Annuler les runs déjà partis et tenir
le verrou pendant la rotation ; une révision déjà chargée conserve son ancienne
épingle. Les champs d’état du JSON, non signés, ne constituent pas une preuve
de non-révocation.
