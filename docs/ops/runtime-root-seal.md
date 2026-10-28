# Runtime root du handoff : construction et scellement

L’entrée H8 est `/usr/bin/python3 -I -S -B
/usr/local/libexec/ameesh-runtime-seal.py`. Ce fichier est installé hors de
l’arbre mesuré, avec son empreinte dans le bundle de bootstrap signé. Avant
cet enrôlement, aucune unité H8 ne peut fonctionner. Le Python système, sa
stdlib et les bibliothèques système sont une précondition de confiance :
chemins de distribution enrôlés, fichiers root sans écriture groupe/autres,
paquets de distribution vérifiés par l’administrateur. Ils sont hors manifeste.
Le vérificateur initial n’importe que cette stdlib ; ni paquet ameesh, ni
compagnon de `/opt/ameesh-release`, ni dépendance de cet arbre n’est importé
avant la mesure complète.

## Construire hors root, depuis le SHA qui sera signé

Dans un répertoire privé sur disque, construire la roue depuis le checkout
exact et conserver les roues de dépendances et leurs empreintes pour la revue.
Utiliser un environnement de construction séparé ; jamais le venv partagé des
agents. Exemple de préparation (les variables désignent des chemins absolus
privés, `WHEEL` la roue ameesh revue et `TOOLS` le Python de construction) :

```sh
umask 022
/usr/bin/python3 -I -B -m venv --copies --without-pip "$STAGE/runtime"
# venv Linux crée ce lien même avec --copies : retirer seulement ce lien connu.
test -L "$STAGE/runtime/lib64"
rm "$STAGE/runtime/lib64"
"$TOOLS" -m pip --python "$STAGE/runtime/bin/python" install \
  --no-index --find-links "$WHEELHOUSE" --no-compile "$WHEEL"
```

Le wheelhouse fournit aussi les dépendances requises par le plan (dont crypto
si ce runtime vérifie des décisions). Le runtime ne contient ni pip, ni
setuptools, ni `.pth`, ni lien, même interne. Ne jamais retirer arbitrairement
un `.pth` fourni par une dépendance : choisir une distribution sans hook et
qualifier son fonctionnement. Copier le contenu de
`runtime/share/ameesh/mise-a-jour/` vers `$STAGE/lib`, fichiers indépendants,
pas de liens ni liens physiques. Le paquet et les compagnons proviennent de
la même roue du même SHA. Créer `$STAGE/store` ; son état entier, même vide,
entre dans le manifeste. `include-system-site-packages` doit être `false` et `home` le répertoire du Python système enrôlé (`/usr/bin`).

Qualifier les vrais imports nécessaires avec le Python concret du runtime :

```sh
"$STAGE/runtime/bin/python" -I -B -c \
  'import sys, subprocess; subprocess.run([sys.executable, "-I", "-B", "-c", "import ameesh.handoff_contract, psycopg, psutil"], check=True)'
```

Charger aussi les compagnons installés par `importlib.util` lors de la
qualification, sans appeler leurs effets. Le plan d’enrôlement conserve le
SHA, les versions Python/dépendances et le résultat de cette qualification.
Elle ne constitue pas un test du socket root ni un enrôlement staging.

## Lier le manifeste au bootstrap existant

L’objet JSON canonique (tri des clés, séparateurs `,` et `:`, aucun newline)
contient `version: 1`, `sha` Git complet, `machine_id` exact et `trees` avec
`runtime`, `lib`, `store`. Chaque arbre reprend le format d’inventaire de
`runtime-release.py` : `version`, `dirs` triés, `files` (taille, SHA-256, mode
0444 ou 0555), `bytes`, `digest` de l’objet sans `digest`. Pour un store vide :
`dirs: ["."]`, `files: {}`, `bytes: 0`, version 1, empreinte canonique calculée.
Aucun autre enfant de `/opt/ameesh-release` n’est admis. Un store non vide est
également intégralement couvert ; tout changement de son contenu impose un
nouveau pin autorisé avant de réouvrir H8. Le store de releases des runners,
qui utilise un lien mutable `current`, doit donc être enrôlé à une racine
distincte dans `execution.json` (par exemple sous `/var/lib/ameesh-handoff`),
pas ajouté ensuite à cet arbre immuable. Aucun store existant n’est déplacé
par le scellement.

Les inventaires normalisent les modes de publication : tous les répertoires
runtime/lib et les sous-répertoires du store sont 0555 ; fichiers 0444/0555 ;
`/opt/ameesh-release` est 0755, sa racine `store` 0711. Préparer ces modes avant
mesure et publication. Pas de fichiers pyc créés après le scellement (`-B`).

Passer cet objet à `entree-operateur.prepare(runtime_seal=...)` ;
`destinations(..., runtime_seal=...)` donne l’inventaire exact à observer au
préalable. Le renderer recoupe machine et empreintes runtime/lib avec le plan.
Il ajoute `/etc/ameesh-release/runtime-seal.json` root 0600 et le vérificateur
système root 0555 aux fichiers du bundle. Leur SHA-256 entre dans la question
compacte de bootstrap. `SignedBootstrapAuthority` et `verify_proof` relisent
la décision signée et la clé owner enrôlée/non révoquée à chaque étape. Aucun
nouveau geste de signature, clé ou autorité ES256 d’image n’est introduit.
La signature couvre notamment le SHA revendiqué et les octets de tout le
runtime, pas seulement un nom de version. Le pin installé est un résultat de
cette autorisation ; un inventaire produit sur l’hôte n’autorise jamais sa
propre installation.

L’administrateur crée préalablement le fichier de verrou permanent root 0600
`/etc/ameesh-release/runtime-seal.lock` (fichier vide, exclusif, sans lien) dans
un répertoire root protégé. Ne jamais remplacer/supprimer son inode, même au
retour arrière. Le bootstrap tient son verrou exclusif durant toutes les
étapes ; la validation recontrôle le contenu publié avant d’activer la clé.
Les remplacements opérateur et retours arrière d’arbres/pins utilisent ce même
verrou exclusif, socket et instances H8 arrêtés. Ne pas modifier ces arbres à
la main pendant leur utilisation. La création du verrou ne donne aucun droit
d’exécuter H8 ; sans pin signé installé, le lancement refuse.

## Vérification et refus

Chaque connexion Accept=yes lance une instance neuve : verrou partagé conservé
jusqu’à la fin de l’enfant, pin protégé lu à nouveau, mesure exhaustive sans
cache puis lancement du Python concret avec `-I -B`. Toute entrée ajoutée,
absente, altérée, liée, spéciale, `.pth`, mauvais mode/UID, configuration venv
élargie ou mutation pendant lecture refuse avant import. Un ancien pin sur
un nouvel arbre refuse. L’absence de pin/verrou refuse. Les enfants Python du
runtime se lancent avec `[sys.executable, '-I', '-B', ...]`.

Limites fixes : 30 000 entrées, 512 Mio de contenu, document 8 Mio, mesure 8 s,
attente verrou 8 s, alarme dispatcher 55 s, enfant requête 65 s (nettoyage H7 compris) ; systemd 85 s et arrêt 5 s couvrent
l’ensemble ; client local 90 s. Le journal reçoit catégorie fixe de refus ou SHA et durée d’une
mesure réussie, sans corps de requête ni secret. Mesurer le coût sur le runtime
réel de chaque enrôlement ; si le budget est dépassé, réduire le runtime ou
faire relire un changement de borne, jamais mettre un cache ou sauter une part.

## Retour arrière

Arrêter socket et instances, réconcilier les intents H7 conservés. Sous le
verrou exclusif, restaurer seulement les arbres et le pin précédents couverts
par le retour arrière signé, ou faire autoriser le nouveau bundle par le flux
existant si cette autorisation manque. Ne jamais générer un pin d’adoption à
partir d’un contenu inconnu. Revérifier intégralement les octets et le pin,
puis relancer. Les snapshots/reçus du bootstrap et le journal H7 restent
conservés. Le rollback des seules unités H8 passe par l’entrée vérifiée avec
`rollback` ; il ne remplace aucun arbre ni pin.
