# #6002 — pose AG3 v4.3 : N1 et N2 d'impl-03 (#200878) seulement (décision mesh-design #200888)

Bureau (BG), 08/11 ~13:50Z. **Rien d'autre ne change** par rapport à v4.2. AUCUN GESTE sur les VM.

## N2 (traité d'abord) : retour sans reçu `acl-parents.avant`

- **v4.2** : le retour affichait « aucune ACL à restaurer » et continuait. Si le reçu avait disparu
  alors que l'ACL était posée, `u:vm-operator:--x` restait sur les 4 parents.
- **v4.3** : `acl-retour-v43.sh verifier` est **toujours** appelé avant toute mutation. Il est
  exécuté depuis les octets relus de BG, sur l'entrée standard, même si le dossier de reçus manque.
  Sans reçu :
  - **STOP, rien n'est touché**, avec le motif ;
  - si une entrée `u:vm-operator` est présente : STOP, avec la liste des parents concernés ;
  - si aucune entrée n'est présente, STOP par défaut, avec la consigne « si la pose s'est arrêtée
    avant l'étape 6 bis, relancer avec `--sans-acl` ». `--sans-acl` n'est accepté que si **aucune**
    entrée `u:vm-operator` n'existe.

## N1 : dérive de mode avant `setfacl --restore`

- Le contrôle de non-dérive compare aussi `stat -c '%a %U:%G %n'` des 4 parents au reçu de modes
  (root 0600, exigé). Une dérive donne STOP **avant** toute restauration, et rien n'est effacé.
- Le contrôle des modes après restauration est conservé.

## Pose

`pose-AG3-vm-operator-v43.sh` = v4.2, à une seule différence près : il dépose `acl-retour-v43.sh`
dans les reçus, au lieu de `acl-retour-v42.sh`.

## Témoin réel en conteneur (`temoin-acl-v43.sh`) : 18/18 OK

- Les 10 cas de v4.2, inchangés.
- Ajouts :
  - **dérive de mode** (`chmod 755` après la pose) : STOP, ACL et modes inchangés ;
  - **reçu absent alors que l'ACL est posée** : STOP, rien touché ;
  - reçu remis : restauration OK ;
  - **reçu absent sans ACL** : STOP par défaut, puis `--sans-acl` accepté.
- Les cas sont isolés par une remise à zéro dans le conteneur seulement.

Essais à blanc sur AG3 (relevé) :
- pose : préalables OK ;
- retour sans reçus : STOP avec le motif ;
- `--sans-acl` (aucune ACL sur AG3) : accepté, à blanc.

Diff explicite : `delta-v42-v43.diff`, qui couvre `acl-retour`, `retour-AG3`, `pose-AG3` et `temoin-acl`.
