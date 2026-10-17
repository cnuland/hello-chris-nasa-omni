#!/usr/bin/env bash
# Put a sample feed on the perception PVC (RWO): scale the worker down, copy through a helper pod, scale back up.
# Default clip: NASA "Step Inside the International Space Station" (public domain, 2022, 15 min, with audio):
#   http://images-assets.nasa.gov/video/Step_Inside_the_Station-4K/Step_Inside_the_Station-4K~medium.mp4
set -euo pipefail
SRC="${1:?usage: load-sample-video.sh <local.mp4> [deployment]}"; DEP="${2:-perception-cam-handheld}"; NS=nasa-app
oc scale deploy/"$DEP" -n $NS --replicas=0
oc wait --for=delete pod -l app=perception -n $NS --timeout=2m || true
oc delete pod media-loader -n $NS --ignore-not-found
oc run media-loader -n $NS --image=registry.access.redhat.com/ubi9/ubi-minimal --restart=Never \
  --overrides='{"spec":{"volumes":[{"name":"m","persistentVolumeClaim":{"claimName":"sample-media"}}],"containers":[{"name":"media-loader","image":"registry.access.redhat.com/ubi9/ubi-minimal","command":["sleep","3600"],"volumeMounts":[{"name":"m","mountPath":"/media"}]}]}}'
oc wait --for=condition=Ready pod/media-loader -n $NS --timeout=3m
oc exec -i -n $NS media-loader -- sh -c "cat > /media/sample.mp4" < "$SRC"   # no tar in ubi-minimal (oc cp needs it)
oc exec -n $NS media-loader -- ls -la /media
oc delete pod media-loader -n $NS --wait=true
oc scale deploy/"$DEP" -n $NS --replicas=1
