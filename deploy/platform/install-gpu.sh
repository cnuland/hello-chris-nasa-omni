#!/usr/bin/env bash
# NFD + NVIDIA GPU operator from their public Helm charts (no Red Hat catalog / pull secret needed).
# Lessons (hello-chris-dd-kuberay, ARO 2026-10): every operator service account needs the privileged SCC; the node needs
# the OSTREE_VERSION label (stamped by gpu-machinesets.sh) so the driver builds with the Driver Toolkit.
# A10 nodes get GPU time-slicing (2 replicas) so agent-llm and ASR can share one A10.
set -euo pipefail
helm repo add nfd https://kubernetes-sigs.github.io/node-feature-discovery/charts >/dev/null 2>&1 || true
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia >/dev/null 2>&1 || true
helm repo update >/dev/null
oc create namespace node-feature-discovery --dry-run=client -o yaml | oc apply -f -
# every SA in the namespace (the chart's worker DaemonSet uses its own nfd-node-feature-discovery-worker SA)
oc adm policy add-scc-to-group privileged system:serviceaccounts:node-feature-discovery >/dev/null
# the GPU MachineSets taint nvidia.com/gpu: NFD workers must tolerate it or they never label the GPU nodes (pci-10de)
helm upgrade --install nfd nfd/node-feature-discovery -n node-feature-discovery --wait --timeout 10m \
  --set 'worker.tolerations[0].key=nvidia.com/gpu' --set 'worker.tolerations[0].operator=Exists' \
  --set 'worker.tolerations[0].effect=NoSchedule'
oc create namespace nvidia-gpu-operator --dry-run=client -o yaml | oc apply -f -
oc adm policy add-scc-to-group privileged system:serviceaccounts:nvidia-gpu-operator >/dev/null
for sa in gpu-operator nvidia-driver nvidia-container-toolkit nvidia-device-plugin nvidia-dcgm-exporter \
          nvidia-gpu-feature-discovery nvidia-operator-validator nvidia-mig-manager default; do
  oc adm policy add-scc-to-user privileged -z "$sa" -n nvidia-gpu-operator >/dev/null
done
oc apply -n nvidia-gpu-operator -f - <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata: {name: time-slicing-config}
data:
  a10: |-
    version: v1
    sharing:
      timeSlicing:
        resources:
        - name: nvidia.com/gpu
          replicas: 2
YAML
helm upgrade --install gpu-operator nvidia/gpu-operator -n nvidia-gpu-operator --wait --timeout 20m \
  --set platform.openshift=true --set nfd.enabled=false --set operator.defaultRuntime=crio \
  --set driver.use_ocp_driver_toolkit=true --set toolkit.enabled=true \
  --set devicePlugin.config.name=time-slicing-config --set devicePlugin.config.default=""
# A10 nodes use the time-slicing profile; T4 nodes stay whole (DiffusionGemma TP4 needs all four)
for n in $(oc get nodes -l gpu-pool=a10 -o name); do oc label "$n" nvidia.com/device-plugin.config=a10 --overwrite; done
echo "waiting for allocatable GPUs..."
for _ in $(seq 1 90); do
  TOTAL="$(oc get nodes -o json | jq '[.items[].status.allocatable["nvidia.com/gpu"] // "0" | tonumber] | add')"
  echo "  $(date +%H:%M:%S) allocatable nvidia.com/gpu=$TOTAL"; [ "$TOTAL" -ge "${EXPECT_GPUS:-8}" ] && break; sleep 20
done
oc get nodes -l nvidia.com/gpu.present=true -o custom-columns='NODE:.metadata.name,POOL:.metadata.labels.gpu-pool,GPUS:.status.allocatable.nvidia\.com/gpu'
