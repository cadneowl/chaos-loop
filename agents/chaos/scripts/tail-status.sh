#!/usr/bin/env bash
# Tail the chaos-controller-manager logs.
kubectl -n chaos-mesh logs -f -l app.kubernetes.io/component=controller-manager
