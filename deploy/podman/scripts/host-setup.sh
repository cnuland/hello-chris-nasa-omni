#!/usr/bin/env bash
# Host layer for a POC node: Ubuntu + NVIDIA driver + NVIDIA Container Toolkit + Podman, then the CDI GPU spec.
# On NASA's GB10 the NVIDIA OS already provides the driver and toolkit; this script then only adds Podman and CDI.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -q
sudo apt-get install -y -q podman curl jq gnupg openssl
if ! command -v nvidia-smi >/dev/null; then
  sudo apt-get install -y -q ubuntu-drivers-common
  sudo ubuntu-drivers install --gpgpu
  DRV=$(dpkg -l | awk '/nvidia-compute-utils-[0-9]+-server/{print $2}' | grep -oE '[0-9]+' | head -1)
  sudo apt-get install -y -q "nvidia-utils-${DRV}-server"
  echo "NVIDIA driver installed: reboot, then run this script again"; exit 0
fi
if ! command -v nvidia-ctk >/dev/null; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo apt-get update -q && sudo apt-get install -y -q nvidia-container-toolkit
fi
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml >/dev/null 2>&1
# Podman 4.9 (Ubuntu 24.04) predates the 'additionalGids' field that newer toolkits write: drop it and pin cdiVersion.
if podman --version | grep -qE ' 4\.'; then
  sudo sed -i -E '/additionalGids:/d;/^ +- [0-9]+$/d;s/^cdiVersion: .*/cdiVersion: 0.6.0/' /etc/cdi/nvidia.yaml
fi
sudo podman run --rm --device nvidia.com/gpu=all docker.io/library/ubuntu:24.04 nvidia-smi -L
