#!/bin/bash
# Point supagent at the mock company middleware (lab/mock_llm_gateway.py) or back to the lab LLM.
#   lab/use_mock_middleware.sh on  [DIR]     middleware mode, like the company: token URL with
#                                            mTLS + consumer key / secret, LLM at <base>/openai
#   lab/use_mock_middleware.sh off URL       no authentication, the LLM at URL (e.g. http://llm:8080/v1)
# Run as the owner of Superset's virtualenv, SUPERSET_CONFIG_PATH set. Secrets are read from
# DIR/consumer.env and never printed.
set -euo pipefail
SUPERSET=${SUPERSET:-superset}
case "${1:?on|off}" in
  on)
    D=$(cd "${2:-$HOME/supagent-lab/mw}" && pwd)
    # shellcheck disable=SC1091
    . "$D/consumer.env"
    "$SUPERSET" supagent settings \
      --set llm.base_url=https://127.0.0.1:9461/openai \
      --set llm.model=qwen3.6-27b \
      --set llm.auth=middleware \
      --set llm.middleware.token_url=https://127.0.0.1:9460/oauth2/token \
      --set "llm.middleware.consumer_key=$CONSUMER_KEY" \
      --set "llm.middleware.consumer_secret=$CONSUMER_SECRET" \
      --set "llm.middleware.cert_path=$D/client.pem" \
      --set "llm.middleware.key_path=$D/client.key" \
      --set "llm.ca_bundle=$D/ca.pem" | grep -E "^llm\.(base_url|model|auth|middleware\.token_url|middleware\.consumer_secret|ca_bundle) "
    ;;
  off)
    "$SUPERSET" supagent settings --set "llm.base_url=${2:?LLM URL}" --set llm.auth=none --set llm.model= \
      --unset llm.middleware.consumer_secret | grep -E "^llm\.(base_url|auth|model) "
    ;;
esac
