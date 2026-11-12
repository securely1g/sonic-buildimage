#!/bin/bash
set -euo pipefail

if [[ $# != 1 || -z $1 ]]; then
    echo 'Usage: azure_acr_login.sh REGISTRY_NAME' >&2
    exit 1
fi

if ! command -v az > /dev/null; then
    installer=$(mktemp)
    trap 'rm -f "$installer"' EXIT
    curl --fail --location --retry 3 --proto '=https' --tlsv1.2 \
        https://aka.ms/InstallAzureCLIDeb -o "$installer"
    sudo -n bash "$installer"
fi
az version --output none
az login --identity --output none
az acr login --name "$1" --output none
