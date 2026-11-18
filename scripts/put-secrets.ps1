# Fill this tenant's SSM SecureStrings. Values never touch Terraform state, the screen or
# PowerShell history. Run from anywhere after `aws login` and `terraform apply`:
#   .\scripts\put-secrets.ps1                  # every parameter, typed or pasted at a hidden prompt
#   .\scripts\put-secrets.ps1 gemini           # only names containing "gemini"
#   .\scripts\put-secrets.ps1 -FromClipboard   # copy each value, press Enter: no pasting needed
# Pasting into hidden prompts can deliver only the first character in VS Code's terminal: use
# -FromClipboard there, or a normal PowerShell window.
param([string]$Filter = "", [switch]$FromClipboard, [string]$Region = "ap-southeast-2")
$ErrorActionPreference = "Stop"

$names = aws ssm get-parameters-by-path --region $Region --path "/us-paper-trader" --recursive `
    --query "Parameters[].Name" --output json | ConvertFrom-Json | Sort-Object
if (-not $names) { throw "No parameters under /us-paper-trader in $Region. Run terraform apply first." }

foreach ($name in $names) {
    if ($Filter -and $name -notlike "*$Filter*") { continue }
    if ($FromClipboard) {
        $answer = Read-Host "Copy the value for $name, then press Enter (s + Enter to skip)"
        if ($answer -eq "s") { continue }
        $plain = (Get-Clipboard -Raw | Out-String).Trim()
    } else {
        $secure = Read-Host -AsSecureString "$name (Enter to skip)"
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)).Trim()
    }
    if (-not $plain) { continue }
    # Pass the value through a temp JSON file so it never appears on a command line.
    $tmp = New-TemporaryFile
    try {
        @{ Name = $name; Value = $plain; Type = "SecureString"; Overwrite = $true } |
            ConvertTo-Json -Compress | Set-Content $tmp -Encoding ascii
        aws ssm put-parameter --region $Region --cli-input-json "file://$tmp" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "failed to set $name" }
        Write-Host "  set ($($plain.Length) characters)" -ForegroundColor Green
    } finally {
        Remove-Item $tmp -ErrorAction SilentlyContinue
        $plain = $null
    }
}
if ($FromClipboard) { Set-Clipboard -Value " " }  # don't leave the last secret on the clipboard

# Lengths only, never values, so you can spot a truncated paste.
Write-Host ""
Write-Host "Stored lengths (values are never shown; NotSet = True means still the CHANGE_ME placeholder):"
aws ssm get-parameters-by-path --region $Region --path "/us-paper-trader" --recursive --with-decryption `
    --query "Parameters[].{Name: Name, Length: length(Value), NotSet: Value == 'CHANGE_ME'}" --output table
