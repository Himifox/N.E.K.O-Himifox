#!/usr/bin/env bash
# Verify frontend build outputs that are required by packaged desktop builds.
set -euo pipefail

REQUIRED_MODEL_FILE="static/yui-origin/yui-origin.model3.json"

if [ ! -s "$REQUIRED_MODEL_FILE" ]; then
  echo "ERROR: missing or empty $REQUIRED_MODEL_FILE after build_frontend.sh" >&2
  exit 1
fi

for model in yui-lolita yui-origin yui-sister; do
  required_pngtuber_file="static/pngtuber/$model/model.json"
  if [ ! -s "$required_pngtuber_file" ]; then
    echo "ERROR: missing or empty $required_pngtuber_file after build_frontend.sh" >&2
    exit 1
  fi
done
