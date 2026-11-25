#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# #8246 (TPK #2202 S6a) — qualification de bout en bout du chemin sandbox sur
# un cluster kind JETABLE. Ce n'est pas un geste de production ni de staging :
# le canon, la base, le relais, l'image et le cluster sont jetables.
#
# Le parcours exécuté par `tests/test_8246_sandbox_kind.py` :
#   canon de test (Agent/Host/Placement avec blocs sandbox)
#   -> admission S2 + réservations S3 (tranche + compte)
#   -> superviseur S4 réel (processus enfant) + KubernetesJobProvider réel
#   -> Job réel, jeton de relais lié au tour (S4d-3)
#   -> résultat `ameesh-sandbox-result/1` accepté une seule fois par S1
#   -> reçu relu par `ameesh sandbox show` et `/api/v1/sandbox/turns/{turn}`.
#
# Cas d'acceptation : annulation avant résultat (`revoked`), redémarrage du
# superviseur avec adoption du Job, bail perdu (`stale`), résultat en double
# (`duplicate`), Job orphelin (alerte `sandbox_orphan`).
#
# Usage :
#   ./preuve-sandbox-kind.sh                      # cluster neuf + relais depuis le Job
#   ./preuve-sandbox-kind.sh --no-host-relay     # rootless/slirp4netns : pas de route pod->hôte
#   ./preuve-sandbox-kind.sh --reuse              # réutilise le cluster nommé (dev)
#   ./preuve-sandbox-kind.sh --keep               # garde le cluster pour inspection
#
# Prérequis : kind (épinglé par le workflow), kubectl, docker, Python avec
# les dépendances du paquet (psycopg, PyYAML) et une base de test migrée
# (`AMEESH_TEST_DSN`, sinon le banc local de tests/support.py).
#
# Le Job est l'image publique `distroless/python3-debian12:nonroot` ÉPINGLÉE
# PAR DIGEST (voir SANDBOX_WITNESS_IMAGE ci-dessous) : elle tourne non-root,
# ce qu'exige le `runAsNonRoot` du Job ; le témoin embarqué est `turn-witness.py`,
# passé en `python -c`. Aucun kubeconfig de production, aucune clé de
# fournisseur, aucun secret réel n'entre dans le Job ni dans cette preuve.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$HERE/../../.." && pwd)
CLUSTER=${CLUSTER:-ameesh-sandbox-witness}
NS=${NS:-sandbox-witness}
#: Image publique épinglée par digest : ni tag mutable, ni build local. Le
#: workflow peut la remplacer par une autre empreinte publiée (même forme).
IMAGE=${SANDBOX_WITNESS_IMAGE:-gcr.io/distroless/python3-debian12@sha256:7d1042ce588ab97019fe95c24ffca7bc5a82ccdac572511d5e09bda4435c89c5}
PYTHON=${PYTHON:-python3}
RELAY=1
KEEP=0
REUSE=0
CREATED=0

usage() {
    sed -n '2,33p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1 ;;
        --reuse) REUSE=1 ;;
        --no-host-relay) RELAY=0 ;;
        --cluster) CLUSTER=${2:?}; shift ;;
        --namespace) NS=${2:?}; shift ;;
        --image) IMAGE=${2:?}; shift ;;
        --python) PYTHON=${2:?}; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "option inconnue: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

case "$IMAGE" in
    *@sha256:*) ;;
    *) echo "image non épinglée par digest : $IMAGE" >&2; exit 2 ;;
esac
case "$NS" in
    ''|-*|*[!a-z0-9-]*) echo "namespace DNS-1123 attendu : $NS" >&2; exit 2 ;;
esac

for outil in kind kubectl docker "$PYTHON"; do
    command -v "$outil" >/dev/null 2>&1 \
        || { echo "SKIP: $outil absent — preuve kind non exécutée (non-concluant)" >&2; exit 77; }
done

#: Un kubeconfig de canon doit être privé et HORS des racines partagées
#: (`/tmp`, `/var/tmp`, `/dev/shm`). RUNNER_TEMP (GitHub) ou ~/.cache.
BASE=${RUNNER_TEMP:-${HOME}/.cache}
mkdir -p "$BASE"
umask 077
WORK=$(mktemp -d "$BASE/ameesh-sandbox-kind.XXXXXX")
KUBECONFIG_FILE="$WORK/kubeconfig"

k() { kubectl --kubeconfig "$KUBECONFIG_FILE" -n "$NS" "$@"; }

diagnostic() {
    echo "--- diagnostic kind (cluster $CLUSTER, namespace $NS) ---" >&2
    kubectl --kubeconfig "$KUBECONFIG_FILE" get nodes -o wide >&2 || true
    k get jobs,pods -o wide >&2 || true
    k get events --sort-by=.lastTimestamp >&2 | tail -30 || true
    for job in $(k get jobs -o name 2>/dev/null || true); do
        k describe "$job" >&2 || true
        k logs "$job" --all-containers --tail=100 >&2 || true
    done
}

cleanup() {
    local code=$?
    trap - EXIT
    if [ "$code" -ne 0 ]; then
        diagnostic || true
    fi
    if [ "$CREATED" -eq 1 ] && [ "$KEEP" -eq 0 ]; then
        kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
    elif [ "$KEEP" -eq 1 ]; then
        echo "cluster conservé : kubectl --kubeconfig $KUBECONFIG_FILE -n $NS get jobs,pods" >&2
    fi
    if [ "$KEEP" -eq 0 ]; then
        rm -rf "$WORK"
    else
        echo "kubeconfig conservé : $KUBECONFIG_FILE" >&2
    fi
    exit "$code"
}
trap cleanup EXIT

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    if [ "$REUSE" -ne 1 ]; then
        echo "le cluster $CLUSTER existe déjà : --reuse pour l'utiliser, ou --cluster autre" >&2
        exit 2
    fi
    echo "trace: réutilisation du cluster $CLUSTER"
else
    #: Un cluster neuf par preuve : aucun état d'une exécution précédente.
    #: CREATED avant l'appel : un échec de création ne laisse pas un cluster
    #: à moitié démarré derrière lui.
    CREATED=1
    kind create cluster --name "$CLUSTER" --wait 180s
fi

kind get kubeconfig --name "$CLUSTER" > "$KUBECONFIG_FILE"
chmod 600 "$KUBECONFIG_FILE"
kubectl --kubeconfig "$KUBECONFIG_FILE" create namespace "$NS" \
    --dry-run=client -o yaml | kubectl --kubeconfig "$KUBECONFIG_FILE" apply -f - >/dev/null

GATEWAY=""
if [ "$RELAY" -eq 1 ]; then
    #: IPv4 du réseau Docker `kind` : le Job joint le relais de l'hôte. Un
    #: runner rootless (slirp4netns --disable-host-loopback) n'a pas cette
    #: route ; la preuve se lance alors avec --no-host-relay.
    GATEWAY=$(docker network inspect kind --format \
        '{{range .IPAM.Config}}{{.Gateway}}{{"\n"}}{{end}}' 2>/dev/null \
        | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -n1 || true)
    if [ -z "$GATEWAY" ]; then
        echo "passerelle IPv4 du réseau kind introuvable : utiliser --no-host-relay" >&2
        exit 2
    fi
fi

echo "trace: date=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "trace: sha=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo '?')"
echo "trace: kubernetes=$(kubectl --kubeconfig "$KUBECONFIG_FILE" get nodes -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}' 2>/dev/null || echo '?')"
echo "trace: cluster=$CLUSTER namespace=$NS image=$IMAGE"
echo "trace: relay_from_job=$RELAY host_gateway=${GATEWAY:-none}"

cd "$REPO_ROOT"
PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
AMEESH_SANDBOX_KIND=1 \
AMEESH_KIND_KUBECONFIG="$KUBECONFIG_FILE" \
AMEESH_KIND_IMAGE="$IMAGE" \
AMEESH_KIND_NAMESPACE="$NS" \
AMEESH_KIND_RELAY_HOST="$GATEWAY" \
AMEESH_KIND_RELAY="$RELAY" \
AMEESH_KIND_TURN_WITNESS="$HERE/turn-witness.py" \
"$PYTHON" -m unittest tests.test_8246_sandbox_kind -v
