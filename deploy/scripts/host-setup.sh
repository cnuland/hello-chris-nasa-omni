#!/usr/bin/env bash
# Host layer for a POC node: Podman, the NVIDIA driver and Container Toolkit, and the CDI spec that lets a
# container claim a GPU. Supports the two distributions the POC targets:
#
#   Ubuntu 22.04 / 24.04      (NVIDIA's standard GB10 software stack)
#   RHEL 9 / 10, Fedora       (dnf; Podman is in the base repositories)
#
# On a GB10 the NVIDIA OS already provides the driver and toolkit, so this only adds Podman and the CDI spec.
# Run it again after the reboot if it installs a driver. Nothing here is needed on a node that runs only the
# data and application tiers (no GPU): install podman, curl, jq and openssl and stop.

# Ubuntu's /bin/sh is dash, which has no `set -o pipefail`: `sh thisscript.sh` would die with
# "Illegal option -o pipefail". Re-exec under bash so the script behaves the same however it is invoked
# (including when the executable bit was lost copying the bundle to a host).
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail

if command -v apt-get >/dev/null; then PKG=apt; elif command -v dnf >/dev/null; then PKG=dnf; else
  echo "unsupported distribution: need apt-get (Ubuntu/Debian) or dnf (RHEL/Fedora)" >&2; exit 1
fi

install_pkgs() {
  case $PKG in
    apt) sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$@";;
    dnf) sudo dnf install -y -q "$@";;
  esac
}

echo "==> base packages (podman, curl, jq, openssl)"
if [ "$PKG" = apt ]; then sudo DEBIAN_FRONTEND=noninteractive apt-get update -q; fi
install_pkgs podman curl jq gnupg2 openssl

# ------------------------------------------------------------------------------------------------ GPU driver
if ! command -v nvidia-smi >/dev/null; then
  echo "==> NVIDIA driver"
  case $PKG in
    apt)
      install_pkgs ubuntu-drivers-common
      sudo ubuntu-drivers install --gpgpu
      # `ubuntu-drivers install --gpgpu` installs no nvidia-smi: the utils package has to be named explicitly.
      DRV=$(dpkg -l | awk '/nvidia-compute-utils-[0-9]+-server/{print $2}' | grep -oE '[0-9]+' | head -1)
      install_pkgs "nvidia-utils-${DRV}-server";;
    dnf)
      # RHEL: the driver comes from the CUDA repository for this release.
      REL=$(. /etc/os-release && echo "rhel${VERSION_ID%%.*}")
      sudo dnf config-manager --add-repo \
        "https://developer.download.nvidia.com/compute/cuda/repos/${REL}/$(uname -m)/cuda-${REL}.repo"
      install_pkgs nvidia-driver-cuda nvidia-driver;;
  esac
  echo "NVIDIA driver installed: reboot, then run this script again"; exit 0
fi

# ------------------------------------------------------------------------------------------------ toolkit + CDI
if ! command -v nvidia-ctk >/dev/null; then
  echo "==> NVIDIA Container Toolkit"
  case $PKG in
    apt)
      curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
        | sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
      curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
      sudo apt-get update -q;;
    dnf)
      curl -fsSL https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
        | sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo >/dev/null;;
  esac
  install_pkgs nvidia-container-toolkit
fi

echo "==> CDI spec (/etc/cdi/nvidia.yaml)"
# Two warnings here are expected on a single-node machine and can be ignored:
#   WARN Could not locate nvidia-imex: executable file not found in $PATH
#   WARN Could not locate nvidia-imex-ctl: executable file not found in $PATH
# IMEX is NVIDIA's Internode Memory Exchange service, which shares GPU memory across NVLink-connected hosts in
# a multi-node system (GB200 NVL-class racks). A single GB10 has no IMEX domain and does not ship those
# binaries, so the toolkit notes their absence and writes the spec without them. Warnings are not suppressed:
# a real failure prints here too, and the device check below is what actually decides whether this step worked.
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml >/dev/null

# Podman 4.9 (Ubuntu 24.04) predates the 'additionalGids' field that toolkit 1.20+ writes, and rejects the spec
# with "unresolvable CDI devices". Drop the field and pin the spec version it does understand.
if podman --version | grep -qE ' 4\.'; then
  echo "    Podman 4.x: stripping additionalGids from the CDI spec"
  sudo sed -i -E '/additionalGids:/d;/^ +- [0-9]+$/d;s/^cdiVersion: .*/cdiVersion: 0.6.0/' /etc/cdi/nvidia.yaml
fi

# The spec is only useful if it actually declares devices. Checking this turns the ambiguous IMEX warnings
# above into a clear pass or fail, instead of leaving the operator to guess whether they mattered.
echo "==> CDI devices declared"
CDI_LIST="$(sudo nvidia-ctk cdi list 2>/dev/null || true)"
if printf '%s' "$CDI_LIST" | grep -q 'nvidia.com/gpu'; then
  printf '%s\n' "$CDI_LIST" | grep 'nvidia.com/gpu' | sed 's/^/    /'
elif sudo grep -q '^kind: nvidia.com/gpu' /etc/cdi/nvidia.yaml 2>/dev/null; then
  # Older toolkits have no `cdi list`; read the generated spec instead.
  sudo grep -E '^\s*name:' /etc/cdi/nvidia.yaml | sed 's/.*name: /    nvidia.com\/gpu=/'
else
  echo "no GPU devices in /etc/cdi/nvidia.yaml - the CDI spec was not generated correctly." >&2
  echo "Re-run: sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml   and read the warnings." >&2
  exit 1
fi

echo "==> verifying GPU passthrough"
sudo podman run --rm --device nvidia.com/gpu=all docker.io/library/ubuntu:24.04 nvidia-smi -L
