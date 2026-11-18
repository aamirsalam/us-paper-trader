# Start one trader command on Fargate now (the same task the schedules run) and print its logs.
#   .\scripts\run-phase.ps1 premarket            # or trade | postclose | report
#   .\scripts\run-phase.ps1 resume --mode aggressive
# Region comes from $env:AWS_REGION (default ap-southeast-2). Arguments pass straight through,
# so flags like --mode reach the trader untouched.
$Command = @($args)
if ($Command.Count -eq 0) { Write-Host "usage: .\scripts\run-phase.ps1 <command> [args...]"; exit 2 }
$Region = if ($env:AWS_REGION) { $env:AWS_REGION } else { "ap-southeast-2" }
$ErrorActionPreference = "Stop"
$name = "us-paper-trader"
$root = Split-Path -Parent $PSScriptRoot
$out = terraform -chdir="$root\infra\envs\prod" output -json tenant | ConvertFrom-Json

$net = New-TemporaryFile; $ovr = New-TemporaryFile
@{ awsvpcConfiguration = @{ subnets = @($out.subnet_ids); securityGroups = @($out.task_security_group_id);
   assignPublicIp = "ENABLED" } } | ConvertTo-Json -Depth 5 -Compress | Set-Content $net -Encoding ascii
@{ containerOverrides = @(@{ name = "trader"; command = @($Command) }) } |
    ConvertTo-Json -Depth 5 -Compress | Set-Content $ovr -Encoding ascii

$task = aws ecs run-task --region $Region --cluster $name --task-definition $name --launch-type FARGATE `
    --count 1 --network-configuration "file://$net" --overrides "file://$ovr" `
    --query "tasks[0].taskArn" --output text
Remove-Item $net, $ovr -ErrorAction SilentlyContinue
if ($LASTEXITCODE -ne 0 -or -not $task -or $task -eq "None") { throw "run-task failed" }

Write-Host "started $task, waiting for it to finish..."
aws ecs wait tasks-stopped --region $Region --cluster $name --tasks $task
$exit = aws ecs describe-tasks --region $Region --cluster $name --tasks $task `
    --query "tasks[0].containers[0].exitCode" --output text
$id = $task.Split("/")[-1]
aws logs get-log-events --region $Region --log-group-name "/ecs/$name" --log-stream-name "trader/trader/$id" `
    --query "events[].message" --output text | ForEach-Object { $_ -split "`t" }
Write-Host "exit code: $exit"
exit [int]($exit -replace "None", "1")
