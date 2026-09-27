#!/usr/bin/env bash
# Print the Nemotron model IDs available to your Token Factory key. Copy them into .env (M_NANO/M_SUPER/M_ULTRA).
set -euo pipefail
: "${NEBIUS_API_KEY:?export NEBIUS_API_KEY first}"
curl -s https://api.tokenfactory.nebius.com/v1/models \
  -H "Authorization: Bearer ${NEBIUS_API_KEY}" | jq -r '.data[].id' | sort | grep -i nemotron \
  || echo "No Nemotron models found for this key; check the Token Factory console."
