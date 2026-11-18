# Get this PowerShell window ready for terraform: drop stale exported keys, sign in if needed,
# then export fresh temporary keys that Terraform can read. Dot-source it so the keys stay set:
#   . .\scripts\aws-session.ps1
param([string]$Region = "ap-southeast-2")

Remove-Item Env:AWS_ACCESS_KEY_ID, Env:AWS_SECRET_ACCESS_KEY, Env:AWS_SESSION_TOKEN,
    Env:AWS_CREDENTIAL_EXPIRATION -ErrorAction SilentlyContinue

aws sts get-caller-identity --region $Region *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Signing in to AWS (a browser window will open)..."
    aws login --region $Region
}
aws configure export-credentials --format powershell | Invoke-Expression
$who = aws sts get-caller-identity --query Account --output text 2>$null
if ($LASTEXITCODE -eq 0) {
    $until = if ($env:AWS_CREDENTIAL_EXPIRATION) { " until $(([datetime]$env:AWS_CREDENTIAL_EXPIRATION).ToLocalTime().ToString('HH:mm'))" } else { "" }
    Write-Host "Ready: account $who, keys valid$until. Run terraform now." -ForegroundColor Green
} else {
    Write-Host "Still not signed in. Run: aws login --region $Region, then dot-source this script again." -ForegroundColor Red
}
