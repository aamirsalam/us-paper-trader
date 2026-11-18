# Fill this tenant's SSM SecureStrings. Values are typed hidden and never touch Terraform state,
# the screen or PowerShell history. Run from anywhere after `aws login` and `terraform apply`:
#   .\scripts\put-secrets.ps1            # every parameter
#   .\scripts\put-secrets.ps1 balanced   # only those whose name contains "balanced"
param([string]$Filter = "", [string]$Region = "ap-southeast-2")
$ErrorActionPreference = "Stop"

$names = aws ssm get-parameters-by-path --region $Region --path "/us-paper-trader" --recursive `
    --query "Parameters[].Name" --output json | ConvertFrom-Json | Sort-Object
if (-not $names) { throw "No parameters under /us-paper-trader in $Region. Run terraform apply first." }

foreach ($name in $names) {
    if ($Filter -and $name -notlike "*$Filter*") { continue }
    $secure = Read-Host -AsSecureString "$name (Enter to skip)"
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    if (-not $plain) { continue }
    # Pass the value through a temp JSON file so it never appears on a command line.
    $tmp = New-TemporaryFile
    try {
        @{ Name = $name; Value = $plain; Type = "SecureString"; Overwrite = $true } |
            ConvertTo-Json -Compress | Set-Content $tmp -Encoding ascii
        aws ssm put-parameter --region $Region --cli-input-json "file://$tmp" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "failed to set $name" }
        Write-Host "  set" -ForegroundColor Green
    } finally {
        Remove-Item $tmp -ErrorAction SilentlyContinue
        $plain = $null
    }
}
