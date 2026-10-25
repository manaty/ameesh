# Installer et qualifier le handoff en staging

La qualification du paquet et l'enrôlement des hôtes sont deux opérations
séparées. Un canari local n'atteste ni transport SSH réel, ni unité activée,
ni déplacement d'agent. Le pilote réel reste soumis aux décisions signées et
aux préalables L232d décrits dans [l'étude](design/etudes/handoff-reel-decoupage.md).

## Paquet installé

La roue contient les helpers épinglés sous
`<venv>/share/ameesh/mise-a-jour/`. Le chargeur utilise le préfixe de son
interpréteur ; aucun document, répertoire courant ou variable d'environnement
ne choisit la fabrique. Dans un checkout, elle reste sous `deploy/mise-a-jour/`.
Les imports relatifs de la fabrique, de l'exécuteur, des transports et du pont
secrets restent dans le même paquet. Les contrôles de propriété et de mode
refusent les fichiers modifiables par le groupe ou par tous.

L'opérateur installe la roue dans un nouvel environnement versionné privé
avec `umask 022`, sans modifier l'environnement utilisé par les runners.
Les sources des helpers doivent être `0644`, détenues par le contrôleur ou
root ; les parents ne doivent pas être modifiables par des agents non
habilités. Ne pas élargir les permissions pour faire passer un contrôle.

Depuis un répertoire extérieur au checkout, avec le Python de cet environnement :

```sh
<venv>/bin/python -I <venv>/share/ameesh/mise-a-jour/canari-handoff.py
```

Les sept booléens doivent être vrais. Ce canari crée uniquement des documents
jetables `0600` dans un dossier `0700`, sans secret, compte réel, DB, SSH ou
appel systemd. Il compose les vrais helpers installés, refuse une exécution
non enrôlée, refuse des permissions et documents invalides, restaure le
document précédent et vérifie les imports relatifs. Le rollback ainsi testé
est celui des documents du canari ; le retour à l'environnement précédent
sur un hôte doit être qualifié séparément par l'opérateur.

## Manifeste privé obligatoire avant effet réel

L'opérateur habilité renseigne les noms, chemins et permissions suivants,
sans valeur de secret ni token. Une case absente est un refus, jamais une
valeur déduite d'un exemple. Le manifeste reste dans le dossier protégé de
l'opérateur ; les preuves publiques ne contiennent que les résultats et liens
vers la version du code.

| Objet | Information à nommer et vérifier |
| --- | --- |
| Autorité | Opérateur, source datée de son autorisation, geste et hôtes couverts |
| Code | Dépôt, SHA complet, environnement précédent et candidat, chemins des helpers |
| Identités | Hôtes source/cible, agent canari, harness, compte exact, home privé |
| Exécution | Utilisateur Unix existant, scope et unité existante exacte, caps |
| Canaux | Canal opérateur déjà autorisé ; endpoint handoff admis ; transport secrets distinct |
| Admission | Inventaire authentifié frais, Requirements, outils et alias de références |
| Documents | Les cinq JSON de la fabrique, `execution.json`, propriétaires et modes |
| Secrets | Politique/helper L232c, références `fichier#CLE`, destinations autorisées, aucun secret réel au canari |
| Retour arrière | Environnement précédent, restauration des documents et preuve d'arrêt/quiescence avant rejeu |

La fabrique lit `/etc/ameesh-handoff/{enrollment,hosts,repositories,secrets,transports}.json`.
L'exécuteur lit aussi `execution.json`. Le dossier est protégé et non lié ;
les documents sont réguliers, non liés, détenus par le contrôleur ou root,
`0600` (ou `0640` dans la politique système admise), sans écriture groupe ni
accès aux autres. Les parents du montage font partie de la vérification de
l'opérateur. Le contrat exact est dans
[la fabrique](../deploy/mise-a-jour/fabrique-handoff.py) et
[l'exécuteur](../deploy/mise-a-jour/hote-handoff.py).

Le transport handoff exige actuellement l'utilisateur `ameesh-handoff`.
Ce nom dans le contrat n'autorise pas sa création et ne prouve pas son
existence. Un canal SSH existant sous un autre utilisateur ne satisfait pas
ce contrat. Toute absence est signalée au pilote sans créer de compte,
privilège, clé ou canal implicite. Le pont L232c conserve son propre canal
`ameesh-secrets` et ses contrôles ; le pont release n'est pas une délégation
de déploiement au handoff.

## Activation et retour à la version précédente

Après la qualification et les relectures requises, l'opérateur pose les
documents via le canal nommé. Un runner non enrôlé journalise un préalable
stable et retente une liaison vide au prochain passage lifecycle. Une liaison
active reste attachée à sa fabrique ; une modification des documents ou du
code demande un redémarrage contrôlé, hors opération incertaine. Les refus
de readiness conservent la source et ne diffusent pas le diagnostic privé.

Avant bascule, conserver l'environnement et les documents précédents.
Prouver installation, refus sans effet et restauration avec l'agent canari
nommé. Si un effet est incertain, réconcilier et quiescer l'époque avant
rejeu ; après switch, reprendre en avant sur la cible. Ne jamais redémarrer
aveuglément la source lors d'un retour au code précédent. La procédure et les
preuves de quiescence sont celles de [HOST-HANDOFFS](HOST-HANDOFFS.md).
