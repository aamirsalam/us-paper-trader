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

# Docker Desktop on Windows stores logins in Windows Credential Manager, which rejects ECR's
# long tokens ("The stub received bad data"). So this push uses a throwaway Docker config: a
# copy of yours minus the credential store, with your contexts and builders carried over.
# The ECR login lands in that temp folder and is deleted at the end.
$realConfig = if ($env:DOCKER_CONFIG) { $env:DOCKER_CONFIG } else { Join-Path $HOME ".docker" }
$tempConfig = Join-Path ([IO.Path]::GetTempPath()) ("usptrader-docker-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tempConfig | Out-Null
$cfgPath = Join-Path $realConfig "config.json"
$cfg = if (Test-Path $cfgPath) { Get-Content $cfgPath -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
foreach ($p in "credsStore", "credHelpers", "auths") { $cfg.PSObject.Properties.Remove($p) }
$cfg | Add-Member -Force -NotePropertyName auths -NotePropertyValue ([pscustomobject]@{})
$cfg | ConvertTo-Json -Depth 20 | Set-Content (Join-Path $tempConfig "config.json") -Encoding ascii
foreach ($sub in "contexts", "buildx", "cli-plugins") {
    $src = Join-Path $realConfig $sub
    if (Test-Path $src) { Copy-Item $src (Join-Path $tempConfig $sub) -Recurse }
}
$previousDockerConfig = $env:DOCKER_CONFIG
$env:DOCKER_CONFIG = $tempConfig
try {

# Fetch the ECR token into a variable first. Piping it straight into docker breaks on Windows
# PowerShell 5.1, which re-encodes text on the way into native programs (ECR answers 400).
$token = aws ecr get-login-password --region $Region
if ($LASTEXITCODE -ne 0 -or -not $token) {
    throw "Could not get an ECR login token. Your AWS sign-in has probably expired: run '. .\scripts\aws-session.ps1' and try again."
}
$token = ($token | Out-String).Trim()
# Docker warns that --password is less safe than stdin; the token lasts 12 hours and never hits disk or history.
# Windows PowerShell 5.1 turns anything a native program writes to stderr into a terminating
# error when the script captures it and ErrorActionPreference is Stop, so relax it for this call.
$ErrorActionPreference = "Continue"
$loginOut = & docker login --username AWS --password $token $registry 2>&1
$loginExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
$loginOut | ForEach-Object { "$_" } | Where-Object { $_ -notmatch "password via the CLI is insecure" } | Write-Host
Remove-Variable token
if ($loginExit -ne 0) { throw "docker login to ECR failed (is Docker Desktop running?)" }

# arm64 because the tasks run on Graviton; Docker Desktop emulates it on an Intel/AMD laptop.
$ErrorActionPreference = "Continue"
docker buildx build --platform linux/arm64 --provenance=false `
    -t "${repoUrl}:live" -t "${repoUrl}:$sha" --push $root
$buildExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($buildExit -ne 0) { throw "image build or push failed" }

} finally {
    if ($previousDockerConfig) { $env:DOCKER_CONFIG = $previousDockerConfig } else { Remove-Item Env:DOCKER_CONFIG -ErrorAction SilentlyContinue }
    Remove-Item $tempConfig -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host "Pushed ${repoUrl}:live ($sha). The next scheduled or manual run uses it." -ForegroundColor Green
