# Checks what this AWS account's guardrails allow BEFORE terraform apply, and says which
# terraform.tfvars switches to set. Safe to run any time:
#   * EC2 checks use --dry-run (nothing is created)
#   * the IAM check creates a throwaway policy + role and deletes them straight away
#   * everything else is read-only
# Run from the repo root after `aws login`:  .\scripts\preflight.ps1
param([string]$Region = "ap-southeast-2")

$ErrorActionPreference = "Continue"
$results = [ordered]@{}
$advice = New-Object System.Collections.Generic.List[string]

function Test-Aws([string]$Label, [scriptblock]$Call) {
    $out = & $Call 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0 -or $out -match "DryRunOperation") { $results[$Label] = "OK" ; return $true }
    if ($out -match "AccessDenied|UnauthorizedOperation|not authorized|explicit deny") {
        $results[$Label] = "DENIED" ; return $false
    }
    $results[$Label] = "ERROR: " + (($out -split "`n" | Where-Object { $_.Trim() })[0])
    return $false
}

Write-Host "Account:" -NoNewline
aws sts get-caller-identity --query "[Account,Arn]" --output text
if ($LASTEXITCODE -ne 0) { Write-Host "Not signed in. Run: aws login --region $Region"; exit 1 }

# --- networking ------------------------------------------------------------------------------
$vpcOk = Test-Aws "Create own VPC (dry run)" {
    aws ec2 create-vpc --cidr-block 10.42.0.0/16 --dry-run --region $Region
}
if (-not $vpcOk) { $advice.Add('use_default_vpc = true') }
$null = Test-Aws "Default VPC exists" {
    $id = aws ec2 describe-vpcs --filters Name=is-default,Values=true --query "Vpcs[0].VpcId" --output text --region $Region
    if ($id -eq "None" -or -not $id) { $global:LASTEXITCODE = 1; "no default VPC" } else { $id }
}
$null = Test-Aws "Create security group (dry run)" {
    $id = aws ec2 describe-vpcs --filters Name=is-default,Values=true --query "Vpcs[0].VpcId" --output text --region $Region
    aws ec2 create-security-group --group-name usptrader-preflight --description preflight --vpc-id $id --dry-run --region $Region
}

# --- IAM: managed policy + permissions boundary (created and deleted at once) ----------------
$policyName = "us-paper-trader-preflight"
$roleName = "us-paper-trader-preflight"
$doc = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"logs:CreateLogStream","Resource":"*"}]}'
$trust = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
$tmpDoc = New-TemporaryFile; Set-Content $tmpDoc $doc -Encoding ascii
$tmpTrust = New-TemporaryFile; Set-Content $tmpTrust $trust -Encoding ascii
$policyArn = $null
$policyOk = Test-Aws "Create managed IAM policy" {
    $script:policyArn = aws iam create-policy --policy-name $policyName --policy-document "file://$tmpDoc" --query Policy.Arn --output text
    $script:policyArn
}
if ($policyOk) {
    $boundaryOk = Test-Aws "Role with permissions boundary" {
        aws iam create-role --role-name $roleName --assume-role-policy-document "file://$tmpTrust" --permissions-boundary $script:policyArn
    }
    aws iam delete-role --role-name $roleName 2>$null | Out-Null
    aws iam delete-policy --policy-arn $script:policyArn 2>$null | Out-Null
    if (-not $boundaryOk) { $advice.Add('use_permissions_boundary = false') }
} else {
    $results["Role with permissions boundary"] = "skipped (needs a managed policy)"
    $advice.Add('use_permissions_boundary = false')
}
$null = Test-Aws "Create IAM role" {
    aws iam create-role --role-name "$roleName-2" --assume-role-policy-document "file://$tmpTrust"
}
aws iam delete-role --role-name "$roleName-2" 2>$null | Out-Null
Remove-Item $tmpDoc, $tmpTrust -ErrorAction SilentlyContinue

$null = Test-Aws "GitHub OIDC login (IAM OIDC providers)" { aws iam list-open-id-connect-providers }

# --- services the stack uses (read-only: a guardrail that blocks a service blocks reads too) --
$null = Test-Aws "ECS (Fargate)" { aws ecs list-clusters --region $Region }
$null = Test-Aws "ECR (image registry)" { aws ecr describe-repositories --region $Region }
$null = Test-Aws "DynamoDB (ledger)" { aws dynamodb list-tables --region $Region }
$null = Test-Aws "SSM Parameter Store (secrets)" { aws ssm describe-parameters --max-results 1 --region $Region }
$null = Test-Aws "CloudWatch Logs" { aws logs describe-log-groups --limit 1 --region $Region }
$null = Test-Aws "EventBridge Scheduler" { aws scheduler list-schedules --region $Region }
$null = Test-Aws "EventBridge rules (failure alerts)" { aws events list-rules --limit 1 --region $Region }
$null = Test-Aws "SNS (alert email)" { aws sns list-topics --region $Region }
$acct = aws sts get-caller-identity --query Account --output text
$budgetOk = Test-Aws "AWS Budgets" { aws budgets describe-budgets --account-id $acct --max-results 1 }
if (-not $budgetOk) { $advice.Add('create_budget = false') }

# --- report ------------------------------------------------------------------------------------
Write-Host ""
foreach ($k in $results.Keys) {
    $v = $results[$k]
    $color = if ($v -eq "OK") { "Green" } elseif ($v -eq "DENIED") { "Red" } else { "Yellow" }
    Write-Host ("{0,-42} {1}" -f $k, $v) -ForegroundColor $color
}
Write-Host ""
Write-Host "GitHub OIDC is not needed: leave github_oidc_provider_arn empty and create_github_oidc_provider = false,"
Write-Host "and push the image from this laptop with .\scripts\push-image.ps1"
if ($advice.Count) {
    Write-Host ""
    Write-Host "Add to infra\envs\prod\terraform.tfvars:" -ForegroundColor Cyan
    $advice | Sort-Object -Unique | ForEach-Object { Write-Host "  $_" }
} else {
    Write-Host "No extra switches needed." -ForegroundColor Green
}
$blocked = $results.GetEnumerator() | Where-Object { $_.Value -eq "DENIED" -and $_.Key -match "ECS|ECR|DynamoDB|SSM|Logs|Scheduler|Create IAM role" }
if ($blocked) {
    Write-Host ""
    Write-Host "Blocked services the trader can't run without:" -ForegroundColor Red
    $blocked | ForEach-Object { Write-Host "  $($_.Key)" }
}
