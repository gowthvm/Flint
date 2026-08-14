# Signs dist\flint.exe with a Windows code-signing certificate (SHA-256 +
# RFC 3161 timestamp). Use this locally once you have a certificate; the
# GitHub release workflow signs automatically when the WINDOWS_SIGNING_PFX
# and WINDOWS_SIGNING_PASSWORD repository secrets are set.
#
# Examples:
#   .\scripts\sign.ps1 -PfxPath .\cert.pfx -PfxPassword 'secret'
#   .\scripts\sign.ps1 -PfxPath .\cert.pfx   (blank password)
param(
    [Parameter(Mandatory = $true)][string]$PfxPath,
    [string]$PfxPassword = "",
    [string]$TimestampUrl = "https://timestamp.digicert.com",
    [string]$ExePath = "dist\flint.exe"
)
$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $PfxPath)) {
    throw "certificate not found: $PfxPath"
}
if (-not (Test-Path -LiteralPath $ExePath)) {
    throw "executable not found: $ExePath (build it first: python -m PyInstaller --clean --noconfirm flint.spec)"
}

$signtool = Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe" `
    -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $signtool) {
    throw "signtool.exe not found - install the Windows SDK"
}

Write-Host "Signing $ExePath with $signtool"
& $signtool sign /fd SHA256 /tr $TimestampUrl /td SHA256 /f $PfxPath /p $PfxPassword $ExePath
if ($LASTEXITCODE -ne 0) {
    throw "signtool failed with exit code $LASTEXITCODE"
}

& $signtool verify /pa /v $ExePath | Select-Object -Last 5
Write-Host "Signed: $ExePath"
