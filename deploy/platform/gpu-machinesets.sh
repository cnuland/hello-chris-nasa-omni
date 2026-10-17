#!/usr/bin/env bash
# GPU node pools for the POC (deploy spec AC-DEP-3):
#   gpu-t4   : 2 x Standard_NC64as_T4_v3 (4x T4 16 GB) -> perception (omni TP2 + agent + ASR) | decision (DiffusionGemma)
#   gpu-t4cv : 1 x Standard_NC16as_T4_v3 (1x T4)       -> perception fast path (OWLv2 + DINOv2)
#   (RHDP Azure policy allows only T4 SKUs; the A10 pool was denied, docs/QUESTIONS.md #15)
# Clones the first worker MachineSet. Upstream NFD (Helm) does not publish the RHCOS OSTREE_VERSION label that the GPU
# operator needs to use the OpenShift Driver Toolkit, so it is stamped from a worker node. Safe to re-run.
set -euo pipefail
OST="$(oc get nodes -l node-role.kubernetes.io/worker -o jsonpath='{.items[0].status.nodeInfo.osImage}' | grep -oE '[0-9]+\.[0-9]+\.[0-9]{8,}-[0-9]+')"
SRC="$(oc get machineset -n openshift-machine-api -o name | grep worker | head -1)"
BASE="$(oc get "$SRC" -n openshift-machine-api -o jsonpath='{.metadata.name}' | sed 's/-worker-.*//')"
mk() { # suffix vmSize replicas pool
  local NAME="$BASE-$1"
  oc get "$SRC" -n openshift-machine-api -o json | jq --arg n "$NAME" --arg vm "$2" --argjson r "$3" --arg ost "$OST" --arg pool "$4" '
    del(.status, .metadata.uid, .metadata.resourceVersion, .metadata.creationTimestamp, .metadata.generation, .metadata.annotations)
    | .metadata.name = $n | .spec.replicas = $r
    | .spec.selector.matchLabels["machine.openshift.io/cluster-api-machineset"] = $n
    | .spec.template.metadata.labels["machine.openshift.io/cluster-api-machineset"] = $n
    | .spec.template.spec.metadata.labels = {"workload":"gpu","gpu-pool":$pool,"nvidia.com/gpu.present":"true","feature.node.kubernetes.io/system-os_release.OSTREE_VERSION":$ost}
    | .spec.template.spec.taints = [{"key":"nvidia.com/gpu","value":"true","effect":"NoSchedule"}]
    | .spec.template.spec.providerSpec.value.vmSize = $vm
    | .spec.template.spec.providerSpec.value.osDisk.diskSizeGB = 300' | oc apply -f -
}
mk gpu-t4  "${T4_VM:-Standard_NC64as_T4_v3}"   "${T4_REPLICAS:-2}" t4
# 1x T4 for the perception fast path (OWLv2 + DINOv2): CPU measured ~14 s/frame, ~20x slower than real time
mk gpu-t4cv "${T4CV_VM:-Standard_NC16as_T4_v3}" "${T4CV_REPLICAS:-1}" t4cv
oc get machineset -n openshift-machine-api
