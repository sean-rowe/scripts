# Install the Copilot CLI Bridge native messaging host on Windows (Chrome + Edge).
#
# Usage:  powershell -ExecutionPolicy Bypass -File .\install.ps1 -ExtensionId <ID>
# Get <ID> from edge://extensions or chrome://extensions after loading unpacked.

param([Parameter(Mandatory = $true)][string]$ExtensionId)

$ErrorActionPreference = "Stop"
$HostName = "com.pinyridgelabs.copilot_clibridge"
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path

$Node = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $Node) { Write-Error "node not found in PATH. Install Node.js first."; exit 1 }

# Batch wrapper so native messaging can launch the Node script.
$Wrapper = Join-Path $Dir "run-host.bat"
"@echo off`r`n`"$Node`" `"$Dir\host.js`" %*" | Set-Content -Encoding ASCII $Wrapper

# Manifest (path must point at the .bat wrapper; escape backslashes for JSON).
$WrapperJson = $Wrapper -replace '\\', '\\'
$Manifest = @"
{
  "name": "$HostName",
  "description": "Copilot CLI Bridge native host",
  "path": "$WrapperJson",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$ExtensionId/"]
}
"@

$ManifestPath = Join-Path $Dir "$HostName.json"
$Manifest | Set-Content -Encoding UTF8 $ManifestPath

# Register per-user for Chrome and Edge via the registry.
foreach ($browser in @("Google\Chrome", "Microsoft\Edge")) {
  $key = "HKCU:\Software\$browser\NativeMessagingHosts\$HostName"
  New-Item -Path $key -Force | Out-Null
  Set-ItemProperty -Path $key -Name "(default)" -Value $ManifestPath
  Write-Host "registered -> $key"
}

Write-Host ""
Write-Host "Done. Node: $Node"
Write-Host "Manifest: $ManifestPath"
Write-Host "Config created on first run at: $env:USERPROFILE\.copilot-cli-bridge\config.json"
Write-Host "Reload the extension if it was already running."
