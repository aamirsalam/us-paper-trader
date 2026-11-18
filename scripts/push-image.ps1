# Build the trader image for Fargate (linux/arm64) and push it to this tenant's ECR as :live.
# Use this instead of the GitHub deploy workflow when the account can't use GitHub's OIDC login.
# Needs Docker Desktop running. Run from the repo root after `aws login` and `terraform apply`:
#   .\scripts\push-image.ps1
param([string]$Region = "ap-southeast-2")
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$repoUrl = (terraform -chdir="$root\infra\envs\prod" output -json tenant | ConvertFrom-Json).ecr_repository_url
if (-not $repoUrl) { throw "No ECR repository in terraform output. Run terraform apply first." }
$registry = $repoUrl.Split("/")[0]
$sha = (git -C $root rev-parse --short HEAD)

# Fetch the ECR token into a variable first. Piping it straight into docker breaks on Windows
# PowerShell 5.1, which re-encodes text on the way into native programs (ECR answers 400).
$token = aws ecr get-login-password --region $Region
if ($LASTEXITCODE -ne 0 -or -not $token) {
    throw "Could not get an ECR login token. Your AWS sign-in has probably expired: run '. .\scripts\aws-session.ps1' and try again."
}
$token = ($token | Out-String).Trim()
# Docker warns that --password is less safe than stdin; the token lasts 12 hours and never hits disk or history.
docker login --username AWS --password $token $registry 2>&1 | Where-Object { $_ -notmatch "Using --password via the CLI is insecure" }
$loginExit = $LASTEXITCODE
Remove-Variable token
if ($loginExit -ne 0) { throw "docker login to ECR failed (is Docker Desktop running?)" }

# arm64 because the tasks run on Graviton; Docker Desktop emulates it on an Intel/AMD laptop.
docker buildx build --platform linux/arm64 --provenance=false `
    -t "${repoUrl}:live" -t "${repoUrl}:$sha" --push $root
if ($LASTEXITCODE -ne 0) { throw "image build or push failed" }
Write-Host "Pushed ${repoUrl}:live ($sha). The next scheduled or manual run uses it." -ForegroundColor Green
