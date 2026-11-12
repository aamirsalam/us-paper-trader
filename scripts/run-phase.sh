#!/usr/bin/env bash
# Start one command on Fargate right now (same task the schedules run) and print its logs.
#   ./scripts/run-phase.sh premarket|trade|postclose|report
#   ./scripts/run-phase.sh resume --mode aggressive
set -euo pipefail

[[ $# -ge 1 ]] || { echo "usage: run-phase.sh <command> [args...]" >&2; exit 2; }
COMMAND_JSON=$(printf '%s\n' "$@" | jq -R . | jq -sc .)
REGION="${AWS_REGION:-ap-southeast-2}"
NAME="us-paper-trader"
cd "$(dirname "$0")/../infra/envs/prod"

SUBNETS=$(terraform output -json tenant | jq -r '.subnet_ids | join(",")')
SG=$(terraform output -json tenant | jq -r '.task_security_group_id')

TASK_ARN=$(aws ecs run-task --region "$REGION" --cluster "$NAME" --task-definition "$NAME" \
  --launch-type FARGATE --count 1 \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --overrides "{\"containerOverrides\":[{\"name\":\"trader\",\"command\":$COMMAND_JSON}]}" \
  --query 'tasks[0].taskArn' --output text)

echo "started $TASK_ARN"
aws ecs wait tasks-stopped --region "$REGION" --cluster "$NAME" --tasks "$TASK_ARN"
EXIT=$(aws ecs describe-tasks --region "$REGION" --cluster "$NAME" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)
TASK_ID="${TASK_ARN##*/}"
aws logs get-log-events --region "$REGION" --log-group-name "/ecs/$NAME" \
  --log-stream-name "trader/trader/$TASK_ID" --query 'events[].message' --output text | tr '\t' '\n'
echo "exit code: $EXIT"
exit "$EXIT"
