# Build the trader image for Fargate (linux/amd64) and push it to this tenant's ECR as :live.
# Superseded by the Python command `usptrader-ops push-image`, which reads the platform from AWS.
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

# 1. ECR login token, fetched into a variable (piping it into docker breaks on Windows PowerShell 5.1).
$token = aws ecr get-login-password --region $Region
if ($LASTEXITCODE -ne 0 -or -not $token) {
    throw "Could not get an ECR login token. Your AWS sign-in has probably expired: run '. .\scripts\aws-session.ps1' and try again."
}
$token = ($token | Out-String).Trim()

# 2. A throwaway Docker config holding that login. We skip `docker login` on purpose: on Windows it
#    saves logins in Credential Manager, which rejects ECR's long tokens ("The stub received bad
#    data"). Writing the login into this temp config.json avoids any credential store, and because
#    the config lists a login, docker doesn't fall back to Credential Manager on its own either.
#    Your settings, contexts and builders are copied in; the folder is deleted at the end.
$realConfig = if ($env:DOCKER_CONFIG) { $env:DOCKER_CONFIG } else { Join-Path $HOME ".docker" }
$tempConfig = Join-Path ([IO.Path]::GetTempPath()) ("usptrader-docker-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tempConfig | Out-Null
$cfgPath = Join-Path $realConfig "config.json"
$cfg = if (Test-Path $cfgPath) { Get-Content $cfgPath -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
foreach ($p in "credsStore", "credHelpers", "auths") { $cfg.PSObject.Properties.Remove($p) }
$auth = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("AWS:$token"))
$auths = [pscustomobject]@{}
$auths | Add-Member -NotePropertyName $registry -NotePropertyValue ([pscustomobject]@{ auth = $auth })
$cfg | Add-Member -Force -NotePropertyName auths -NotePropertyValue $auths
$cfg | ConvertTo-Json -Depth 20 | Set-Content (Join-Path $tempConfig "config.json") -Encoding ascii
Remove-Variable token, auth
foreach ($sub in "contexts", "buildx", "cli-plugins") {
    $src = Join-Path $realConfig $sub
    if (Test-Path $src) { Copy-Item $src (Join-Path $tempConfig $sub) -Recurse }
}

$previousDockerConfig = $env:DOCKER_CONFIG
$env:DOCKER_CONFIG = $tempConfig
try {
    # 3. Build for amd64 (native on an Intel/AMD laptop) and push. buildx prints progress on stderr, which Windows PowerShell 5.1 would
    #    treat as an error under "Stop", so relax that for this call and check the exit code instead.
    $ErrorActionPreference = "Continue"
    docker buildx build --platform linux/amd64 --provenance=false `
        -t "${repoUrl}:live" -t "${repoUrl}:$sha" --push $root
    $buildExit = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($buildExit -ne 0) { throw "image build or push failed (is Docker Desktop running?)" }
} finally {
    if ($previousDockerConfig) { $env:DOCKER_CONFIG = $previousDockerConfig }
    else { Remove-Item Env:DOCKER_CONFIG -ErrorAction SilentlyContinue }
    Remove-Item $tempConfig -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host "Pushed ${repoUrl}:live ($sha). The next scheduled or manual run uses it." -ForegroundColor Green
