# Mode médié (a2) sur une VM — L159i

Ce lot fournit la **recette de lancement**, le superviseur rootless et le
service système à raccorder au garde de capacité. Aucune VM n'a été modifiée
et aucune image n'a été construite par ce lot. Les contrôles hors ligne ne
remplacent pas la recette d'acceptation sur la VM pilote.

Base de travail : `main`, après reconstruction de la branche (la voie B,
`lot/100-voie-b`, est fusionnée). Contrats : [image](IMAGE-EXECUTEUR.md),
[API médiée](EXECUTEUR-MEDIEE.md),
[conception L159](design/etudes/l159-capacite-des-hotes.md).

## Préparer et examiner la recette

Le propriétaire copie `deploy/guest/mediated-recipe.example.json`, remplace
le digest fictif par celui de la release vérifiée, puis indique le serveur
HTTPS, le mesh, la fiche Host et les plafonds convenus pour cette VM.
Le volume est dédié au couple VM/mesh ; les quatre répertoires de montage
sont distincts. Le code d'enrôlement reste dans `enroll_dir/code`, jamais
dans le JSON.

Pour obtenir la commande à examiner, sans lancement ni accès au réseau :

```sh
python -m ameesh.mediated_executor.vm_recipe /chemin/recette.json
```

Le module rend des arguments Podman : image épinglée et déjà présente
(`--pull=never`), racine en lecture seule, aucune capacité, uid/gid 10001,
volume inscriptible, état de porte en lecture seule, acquittement inscriptible,
code en lecture seule, `/tmp` en tmpfs, plafonds CPU, mémoire et processus
(512, la borne de la recette PROV-3), arrêt par SIGTERM avec 100 secondes. Il
refuse les champs supplémentaires, notamment les secrets et les modifications
arbitraires de l'environnement de l'image.

La recette emploie Podman **rootless** sous l'identité dédiée `ameesh-guest`.
`keep-id:uid=10001,gid=10001` traduit cette identité vers celle de l'image ;
le volume et l'acquittement appartiennent donc à `ameesh-guest` sur l'hôte.
Référence des options : [documentation officielle Podman](https://docs.podman.io/en/latest/markdown/podman-run.1.html).
Le gestionnaire cgroupfs est explicite pour permettre un placement sous le
cgroup délégué du service système ; cette hiérarchie devra être vérifiée
sur la VM avant activation.

## Déploiement à effectuer par le propriétaire

1. Attendre la fusion de la voie B et disposer d'un serveur du mesh en HTTPS
   avec un certificat reconnu. Préparer la fiche Host, ses agents admis et
   l'offre de capacité. Les harnais avec abonnement restent sur les hôtes
   classiques ; l'image utilise le relais de modèles.
2. Préparer l'identité `ameesh-guest`, sans sudo ni groupe docker, ses plages
   subuid/subgid, Podman rootless et un volume séparé du disque de
   l'application. Activer la persistance de son gestionnaire
   (`loginctl enable-linger ameesh-guest`) : Podman rootless a besoin d'un
   `XDG_RUNTIME_DIR`, donc de `/run/user/<uid>`, que la persistance crée au
   démarrage. Vérifier cgroups v2, délégation, horloge et disponibilité
   locale de l'image dont le digest et la signature ont été contrôlés.
3. Préparer le volume en `0700`, appartenant à `ameesh-guest`. Le garde
   contrôle `state_dir`, non inscriptible par l'invité ; `ack_dir` est
   inscriptible par l'invité. Monter les **répertoires**, car les fichiers
   sont remplacés par renommage atomique. Vérifier les chemins réels et
   refuser les liens symboliques ou chevauchements de montages.
4. Installer une restriction réseau qui n'autorise depuis le conteneur que
   le serveur du mesh sur 443, y compris pour le relais et les archives git.
   La commande générée ne configure pas ce filtrage. Aucun port entrant
   n'est publié. Aucun DSN, compte fournisseur, token de forge ou socket
   Docker ne doit être transmis.
5. Installer le garde et `deploy/guest/ameesh-guest-mediated.service` dans
   `ameesh-guest.slice`, avec les plafonds de l'offre, poids CPU/I/O bas et
   délégation adaptée au Podman rootless. Vérifier que les processus du
   conteneur restent sous cette slice et que le garde peut effectivement
   réduire, geler et arrêter l'invité. Le garde observateur seul ne suffit pas.
6. Le propriétaire émet un code avec `ameesh host enroll` et le dépose dans
   `enroll_dir/code`, lisible par l'invité, hors du volume persistant.
   Examiner la recette rendue avant d'autoriser l'activation.

## Supervision et raccordement au garde

`python -m ameesh.mediated_executor.vm_supervisor /etc/ameesh/mediated-recipe.json`
applique les codes 0 à 6 de l'interface image :
relance du seul code 1 avec délai croissant de 30 à 600 secondes ; arrêt
pour les codes 2 à 5 ; effacement du volume dédié et attente d'un nouveau
code après le code 6. Un échec du moteur Podman (125 à 127) est distinct
d'une sortie de l'exécuteur et doit être signalé.

Le superviseur exige **Python 3.11 ou plus** : l'effacement de révocation
(code 6) emploie `shutil.rmtree(dir_fd=)`, absent avant. Sur 3.9/3.10 il
refuse au démarrage, avant tout lancement, au lieu d'échouer au moment
d'effacer le volume et de le laisser en place.

Le code d'enrôlement est effacé au premier acquittement **du lancement
courant** après enrôlement, au plus tard à sa fin, y compris sur erreur.
Seule exception : le `SIGKILL` du garde au niveau 4 tue le superviseur sans
lui laisser ce nettoyage (#1402). Le code reste alors dans `enroll_dir` (lisible
par l'invité et le propriétaire seulement), mais seulement si l'unité est tuée
avant que le superviseur observe le premier acquittement : le code est
retiré à cette observation, à la prochaine lecture (chaque seconde).
Le lancement suivant l'utilise et l'efface comme d'habitude. Le
propriétaire qui ne veut pas d'un code en attente le retire lui-même
(`rm <enroll_dir>/code`) avant de relancer l'unité.
Avant chaque lancement, l'ancien `ack.json` est retiré. Le répertoire
d'acquittement est exclusif à ce conteneur ; le propriétaire arrête tout
ancien lanceur avant activation. Un verrou dans `enroll_dir` refuse deux
superviseurs simultanés utilisant le même répertoire. Le superviseur
conserve le volume sur sortie 5 et après une fenêtre normale ; sur sortie 6,
il efface son contenu en gardant sa racine de montage, sans suivre les liens
symboliques. Les répertoires sont ouverts sans suivre les liens, y compris
dans leurs parents, et leurs descripteurs restent ouverts pour le nettoyage.

Une porte absente, malformée, sans battement `expires_ts` ou dont ce battement
est périmé interdit un lancement. Le battement est celui du garde, renouvelé
régulièrement et borné à 300 secondes selon le contrat ; `until_ts` est la
fin facultative d'une fenêtre de prêt, pas une preuve de vie du garde.
`until_ts: null` permet une offre sans fenêtres lorsque le battement est frais.
La recette impose `AMEESH_HOST_GATE_LENDING=1` : la porte fichier de l'image
vérifie elle aussi ce battement et se replie sur `stopped`, même si le
superviseur est gelé ou disparaît. Une fin de fenêtre passée bloque aussi.
Si elle devient `stopped` ou périmée pendant l'exécution, ou si le
superviseur reçoit SIGTERM/SIGINT, `podman stop --time=100` transmet SIGTERM
puis borne l'arrêt. Le drainage normal est appliqué par le runner de l'image.
La suite dépend de la cause (#1402) :

- `stopped` d'un garde vivant (battement frais), ou fin de fenêtre
  (`until_ts` passé) : le superviseur **ne sort pas**. Il attend la prochaine
  porte `available` et relance alors lui-même le conteneur. L'unité reste
  active : un arrêt du garde (niveau 4) la trouve, la note dans
  `guard.stopped` et la redémarre après `restart_after_seconds`. Une sortie du
  superviseur avant le `stop_guest` du garde la lui aurait cachée.
- porte périmée (garde absent ou arrêté) ou signal : le superviseur sort 0,
  et seul l'opérateur le redémarre.

Le journal donne le code brut de l'exécuteur et, pour une fin par signal, le
nom du signal (`-9 (SIGKILL)`). Une telle fin reste traitée comme une erreur
(sortie 2). [Référence Podman stop](https://docs.podman.io/en/latest/markdown/podman-stop.1.html).

Le propriétaire dépose la recette sans secret dans
`/etc/ameesh/mediated-recipe.json` et prépare `HOME` et `TMPDIR` de l'unité
(`0700`, invité), le stockage local Podman et les quatre répertoires de la
recette. `enroll_dir` est dédié, appartient à l'invité, et n'est inscriptible
que par lui et le propriétaire ; le conteneur le monte en lecture seule.
Le service utilise `User=ameesh-guest`, `Slice=ameesh-guest.slice`,
`Delegate=yes`, `KillMode=mixed`, `TimeoutStopSec=125` et `Restart=no`.
Systemd ne pose pas `XDG_RUNTIME_DIR` pour un service système : la
persistance de l'étape 2 crée `/run/user/<uid>`, et une extension de l'unité
pose le chemin réel
(`Environment=XDG_RUNTIME_DIR=/run/user/<uid>` ; `%U` vaut `0` dans le
gestionnaire système, il ne suit pas `User=`). Sans cela, Podman rootless
refuse de démarrer faute de répertoire d'exécution.
Il ne reçoit aucun fichier d'environnement du mode classique. La délégation
et les helpers subuid/subgid Podman doivent fonctionner : ajouter
`NoNewPrivileges` au service peut empêcher `newuidmap`/`newgidmap`.
La racine du conteneur reste durcie par la recette.

Configurer le garde avec les chemins de la recette et cette unité précise :

```json
{
  "agent_units": "ameesh-guest-mediated.service",
  "slice": "ameesh-guest.slice",
  "state_path": "/run/ameesh-gate/state/state.json",
  "ack_path": "/run/ameesh-gate/ack/ack.json",
  "work_path": "/srv/ameesh-guest/executor/example-mesh"
}
```

Ce fragment complète la configuration `host`/`lend` du garde ; il n'est pas
une offre complète. Lancer `ameesh guard run --config /etc/ameesh/guard.json`
sans `--observe`. Installer également `ameesh-guest.slice` et
`ameesh.slice.d/guest-weight.conf`. Avant activation, vérifier le cgroup
du conteneur sous celui de l'unité et de la slice, les plafonds effectifs,
le gel/dégel et l'arrêt par le garde. Après les sorties 2 à 6 ou une erreur
Podman, corriger la cause et redémarrer explicitement le service ; pour 3,
4 ou 6, le propriétaire doit fournir un nouveau code. Aucun redémarrage
automatique systemd ne contourne cette règle.

## Recette d'acceptation

Les vérifications hors ligne couvrent les codes de sortie, les délais bornés,
les acquittements anciens/courants, le retrait du code, le verrou, l'arrêt
sur signal/porte périmée, l'attente puis la relance après un `stopped` frais
ou une fin de fenêtre, et le nettoyage sans suivi de liens. Le propriétaire
effectue ensuite sur la VM pilote : enrôlement,
tour relayé, drainage et acquittement, reprise, retrait sous charge,
arrêt du garde (porte périmée), SIGTERM et révocation. L'existence d'une
recette générée ne vaut pas validation de ces essais sur la VM.
