#!/usr/bin/env bash
# Fill this tenant's SSM SecureStrings without the values touching Terraform state or shell history.
#   ./scripts/put-secrets.sh                 # every parameter
#   ./scripts/put-secrets.sh alpaca/baseline # only those matching a substring
set -euo pipefail

PREFIX="/us-paper-trader"
REGION="${AWS_REGION:-ap-southeast-2}"
FILTER="${1:-}"

mapfile -t PARAMS < <(aws ssm get-parameters-by-path --region "$REGION" --path "$PREFIX" --recursive \
  --query 'Parameters[].Name' --output text | tr '\t' '\n' | sort)

if [[ ${#PARAMS[@]} -eq 0 ]]; then
  echo "No parameters under $PREFIX in $REGION. Run terraform apply first." >&2
  exit 1
fi

for name in "${PARAMS[@]}"; do
  [[ -n "$FILTER" && "$name" != *"$FILTER"* ]] && continue
  read -r -s -p "$name (enter to skip): " value; echo
  [[ -z "$value" ]] && continue
  aws ssm put-parameter --region "$REGION" --name "$name" --type SecureString \
    --value "$value" --overwrite >/dev/null
  echo "  set"
done
