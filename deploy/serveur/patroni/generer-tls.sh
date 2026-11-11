#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Matériel TLS de la grappe etcd + Patroni (EP8.4b, lot #6870, étude
# docs/design/etudes/replique-base-mesh.md §4 et §8).
#
#   generer-tls.sh --sortie /root/grappe
#
# Produit, hors du dépôt et avec des permissions strictes :
#   ca.crt, ca.key                     autorité de la grappe (clé CA 0600)
#   secrets.env                        mots de passe REST et réplication (0600)
#   <nœud>/etcd-server.{crt,key}       pair etcd : serveur + client (0600 clé)
#   <nœud>/etcd-client.{crt,key}       client etcd pour Patroni/etcdctl
#   <nœud>/patroni-rest.{crt,key}      API REST Patroni (serveur)
#   empreintes.sha256                  contrôle d'intégrité après copie
#
# Aucun secret n'est affiché ni écrit dans le dépôt. La clé de la CA ne doit
# pas être copiée sur les nœuds : seuls ca.crt et le dossier du nœud le sont.
# Sans --force, un dossier de sortie non vide est refusé ; avec --force, il
# n'est remplacé qu'après une génération réussie.
set -euo pipefail
umask 077

ICI=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
GENERATEUR="$ICI/generer-configuration.py"
TOPOLOGIE="${TOPOLOGIE:-$ICI/topologie.json}"
SORTIE=""
JOURS=825
JOURS_CA=3650
FORCE=0

log() { printf 'generer-tls : %s\n' "$*" >&2; }
die() { log "$*"; exit 1; }

utiliser() {
  cat >&2 <<'TXT'
usage: generer-tls.sh --sortie DOSSIER [--topologie FICHIER]
                      [--jours N] [--jours-ca N] [--force]
TXT
  exit 2
}

while (($#)); do
  case "$1" in
    --sortie) [[ $# -ge 2 ]] || utiliser; SORTIE=$2; shift 2 ;;
    --topologie) [[ $# -ge 2 ]] || utiliser; TOPOLOGIE=$2; shift 2 ;;
    --jours) [[ $# -ge 2 ]] || utiliser; JOURS=$2; shift 2 ;;
    --jours-ca) [[ $# -ge 2 ]] || utiliser; JOURS_CA=$2; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help) utiliser ;;
    *) log "option inconnue : $1"; utiliser ;;
  esac
done

[[ -n $SORTIE ]] || utiliser
[[ $JOURS =~ ^[0-9]+$ ]] && ((JOURS > 0)) || die "--jours invalide"
[[ $JOURS_CA =~ ^[0-9]+$ ]] && ((JOURS_CA > 0)) || die "--jours-ca invalide"
[[ -r $TOPOLOGIE ]] || die "topologie illisible : $TOPOLOGIE"
command -v openssl >/dev/null || die "openssl requis"
command -v python3 >/dev/null || die "python3 requis"

SORTIE=$(realpath -m -- "$SORTIE") || die "sortie invalide"
case "$SORTIE" in
  /|.|..|"$HOME") die "sortie refusée : $SORTIE" ;;
esac
[[ -L $SORTIE ]] && die "sortie symbolique refusée : $SORTIE"
if [[ -e $SORTIE && ! -d $SORTIE ]]; then
  die "sortie existante et non répertoire : $SORTIE"
fi
if [[ -d $SORTIE && -n $(ls -A -- "$SORTIE") && $FORCE -ne 1 ]]; then
  die "sortie non vide : $SORTIE (utiliser --force pour la remplacer)"
fi
PARENT=$(dirname -- "$SORTIE")
mkdir -p -- "$PARENT"

TRAVAIL=$(mktemp -d -- "$PARENT/.patroni-tls.XXXXXX")
MATERIEL="$TRAVAIL/materiel"
LISTE="$TRAVAIL/noeuds"
mkdir -m 0700 -- "$MATERIEL"
nettoyer() { rm -rf -- "$TRAVAIL"; }
trap nettoyer EXIT

python3 "$GENERATEUR" --topologie "$TOPOLOGIE" --liste-noeuds > "$LISTE" \
  || die "topologie refusée par generer-configuration.py"

mot_de_passe() {
  local brut
  brut=$(head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')
  printf '%s' "${brut:0:32}"
}

REST=$(mot_de_passe)
REPL=$(mot_de_passe)
[[ ${#REST} -eq 32 && ${#REPL} -eq 32 && $REST != "$REPL" ]] \
  || die "génération de mot de passe incomplète"
cat > "$MATERIEL/secrets.env" <<EOF
PATRONI_RESTAPI_USERNAME=patroni
PATRONI_RESTAPI_PASSWORD=$REST
PATRONI_REPLICATION_USERNAME=replicator
PATRONI_REPLICATION_PASSWORD=$REPL
EOF
unset REST REPL
chmod 0600 "$MATERIEL/secrets.env"

openssl ecparam -name prime256v1 -genkey -noout -out "$MATERIEL/ca.key" >/dev/null 2>&1
chmod 0600 "$MATERIEL/ca.key"
openssl req -x509 -new -key "$MATERIEL/ca.key" -sha256 -days "$JOURS_CA" \
  -subj "/CN=ameesh-db-ca" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash" \
  -out "$MATERIEL/ca.crt" >/dev/null 2>&1
chmod 0644 "$MATERIEL/ca.crt"
openssl verify -CAfile "$MATERIEL/ca.crt" "$MATERIEL/ca.crt" >/dev/null 2>&1 \
  || die "CA auto-signée invalide"

generer_certificat() {
  local nom=$1 adresse=$2 base=$3 eku=$4 usage=$5
  local dossier="$MATERIEL/$nom"
  local cle="$dossier/$base.key" cert="$dossier/$base.crt"
  local csr ext cn
  mkdir -p -m 0700 -- "$dossier"
  csr=$(mktemp -- "$TRAVAIL/csr.XXXXXX")
  ext=$(mktemp -- "$TRAVAIL/ext.XXXXXX")
  cn=$nom
  [[ $base == etcd-client ]] && cn="$nom-etcd-client"
  openssl ecparam -name prime256v1 -genkey -noout -out "$cle" >/dev/null 2>&1
  chmod 0600 "$cle"
  openssl req -new -key "$cle" -subj "/CN=$cn" -out "$csr" >/dev/null 2>&1
  {
    echo "basicConstraints=critical,CA:FALSE"
    if [[ $usage == serveur ]]; then
      echo "keyUsage=critical,digitalSignature,keyEncipherment"
    else
      echo "keyUsage=critical,digitalSignature"
    fi
    echo "extendedKeyUsage=$eku"
    echo "subjectAltName=DNS:$nom,IP:$adresse"
    echo "authorityKeyIdentifier=keyid,issuer"
    echo "subjectKeyIdentifier=hash"
  } > "$ext"
  openssl x509 -req -in "$csr" -CA "$MATERIEL/ca.crt" -CAkey "$MATERIEL/ca.key" \
    -CAcreateserial -CAserial "$MATERIEL/ca.srl" \
    -days "$JOURS" -sha256 -extfile "$ext" -out "$cert" >/dev/null 2>&1
  chmod 0644 "$cert"
  openssl verify -CAfile "$MATERIEL/ca.crt" "$cert" >/dev/null 2>&1 \
    || die "certificat invalide pour $nom/$base"
  rm -f -- "$csr" "$ext"
}

while IFS=$'\t' read -r nom adresse; do
  [[ -n $nom && -n $adresse ]] || continue
  generer_certificat "$nom" "$adresse" "etcd-server" "serverAuth,clientAuth" "serveur"
  generer_certificat "$nom" "$adresse" "etcd-client" "clientAuth" "client"
  generer_certificat "$nom" "$adresse" "patroni-rest" "serverAuth" "serveur"
done < "$LISTE"

rm -f -- "$MATERIEL/ca.srl"
(
  cd "$MATERIEL"
  find . -type f ! -name empreintes.sha256 -print0 \
    | sort -z | xargs -0 sha256sum > empreintes.sha256
)
chmod 0644 "$MATERIEL/empreintes.sha256"

# Remplacement seulement après une génération complète et vérifiée.
if [[ -d $SORTIE ]]; then
  if [[ $FORCE -eq 1 ]]; then
    rm -rf -- "$SORTIE"
  else
    rmdir -- "$SORTIE" 2>/dev/null || die "sortie non vide : $SORTIE"
  fi
fi
mv -- "$MATERIEL" "$SORTIE"
chmod 0700 "$SORTIE"

log "matériel TLS écrit dans $SORTIE (clés 0600, secrets 0600)"
while IFS=$'\t' read -r nom adresse; do
  [[ -n $nom ]] || continue
  log "  $nom ($adresse)"
done < "$LISTE"
log "copier ca.crt et le dossier de chaque nœud, jamais ca.key"
