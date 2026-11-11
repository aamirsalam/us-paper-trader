#!/usr/bin/env bash
# Start one phase on Fargate right now (same task the schedules run) and tail its logs.
#   ./scripts/run-phase.sh premarket|trade|postclose|report
set -euo pipefail

PHASE="${1:?usage: run-phase.sh premarket|trade|postclose|report}"
REGION="${AWS_REGION:-ap-southeast-2}"
NAME="us-paper-trader"
cd "$(dirname "$0")/../infra/envs/prod"

SUBNETS=$(terraform output -json tenant | jq -r '.subnet_ids | join(",")')
SG=$(terraform output -json tenant | jq -r '.task_security_group_id')

TASK_ARN=$(aws ecs run-task --region "$REGION" --cluster "$NAME" --task-definition "$NAME" \
  --launch-type FARGATE --count 1 \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --overrides "{\"containerOverrides\":[{\"name\":\"trader\",\"command\":[\"$PHASE\"]}]}" \
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
