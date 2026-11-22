# Diagnostics des contrôles VM

Les argv de `enrollment.example.json` et de l’étape 6 du README restent
`/usr/bin/python3 -I /opt/ameesh-vm-adapter/checks/health.py` suivis de
`doctor` ou `version-mode`. Python peut être un lien root : chaque maillon
et chaque parent traversé doivent appartenir à root ; les répertoires et
la cible finale ne doivent être inscriptibles ni par le groupe ni par les
autres. Les bits de mode d’un lien sont ignorés, son propriétaire est vérifié.
Cela couvre aussi les liens intermédiaires de `/etc/alternatives`.

Le producteur publie ses échecs sous forme `étape:ClasseException` dans le
champ `reason` du reçu rouge et sur stderr, préfixé de `vm-health`.
Les étapes sont `setup`, `boot`, `verifier`, `inventory`, `runners-before`,
`venv`, `metadata`, `doctor`, `readiness`, `runners-after` et `publish`. La classe vient
d’une liste fixe ; une classe inconnue est ramenée à `Exception`. Aucun
texte d’exception, chemin privé, environnement ou DSN n’est ajouté au diagnostic.
Le reçu vert conserve son schéma sans champ supplémentaire.

Un échec de lecture ou validation de la carte (`setup`) ne permet pas de
faire confiance à un chemin de reçu : seul stderr reçoit alors la raison.
Un échec d’écriture (`publish`) se signale aussi sur stderr ; le précédent
reçu expire selon les règles habituelles. L’unité utilisateur conserve
stdout à `null` et envoie stderr au journal. Pour consulter ces raisons,
sous le compte ameesh :

```sh
journalctl --user -u ameesh-vm-health.service -n 20 --no-pager
```

`version_mode` et `doctor` sont prouvés séparément (#6334) : un échec de
`doctor` ou de `readiness` laisse `version_mode` vrai si les exécuteurs et
le venv n’ont pas bougé pendant la sonde (`runners-after`) ; le reçu garde
alors `doctor` faux et sa raison. `readiness` lit une fois le registre
(`ameesh list --json`, dans la même unité transitoire que `doctor`) : chaque
exécuteur lancé, identifié par le `--runner-id` de sa ligne de commande
réelle (drop-ins compris), doit tenir un bail vivant, sans blocage d’hôte
(`hôte non prêt`, `base injoignable`, `dossier absent`). Un exécuteur qui
boucle au démarrage sans bail n’est donc plus vert. Aucun secret ni droit
supplémentaire : le même fichier d’environnement que `doctor`, sans sudo.

Retour prises fermées (#8069) : en `returning` prises fermées, seule la
sonde (`return_probe`) peut être réclamée (#7874) ; les autres exécuteurs
ne tiennent aucun bail. Quand un exécuteur lancé n’a pas de bail,
`readiness` lit alors le contrôle de l’hôte (`ameesh host status <hôte>`,
même unité transitoire ; l’hôte est celui des lignes du registre de ces
exécuteurs). En retour prises fermées, seul le bail de la sonde est exigé,
tenu par un des exécuteurs lancés ; les autres restent prouvés par
`runners-before` et `runners-after` (actifs, mode `execute`, venv). Dans
tout autre état, chaque exécuteur doit tenir son bail.

Inventaire (#8069) : les unités attendues sont lues du gestionnaire
utilisateur, plus d’une carte. Toute unité `ameesh-runner-agent@` chargée
qui est activée (`enabled`) ou non `inactive` (active, `failed`, en
redémarrage) est attendue et doit tourner ; une unité désactivée et arrêtée
(agent retiré, essai fini) est ignorée. La clé `units` d’une ancienne carte
`health-producer.json` est obsolète et ignorée : ajouter ou retirer un agent
ne demande plus de pose root. Un exécuteur activé mais arrêté ou en échec
rend le reçu rouge (`runners-before:ValueError`).

Le lecteur SSH garde ses sorties supprimées. `version-mode` accepte un reçu
dont seul `doctor` est rouge (raison bornée présente) ; `doctor` refuse tout
reçu rouge.
Le producteur exécuté directement reste observable par son code de sortie
et son diagnostic stderr.
