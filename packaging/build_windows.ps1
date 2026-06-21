# Build the Windows app (ADR-104): PyInstaller folder -> (optional sign) ->
# (optional Inno Setup installer).
#
# Usage:  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#
# Signing + the installer are OFF unless configured, so this produces a working
# *unsigned* app folder out of the box (SmartScreen will warn until signed —
# that needs a code-signing cert, ADR-078 K2):
#   $env:WINDOWS_SIGN_PFX       path to a .pfx code-signing cert
#   $env:WINDOWS_SIGN_PASSWORD  its password
# Inno Setup: if ISCC.exe is on PATH and packaging\installer.iss exists, an
# installer is produced; otherwise the app folder is the artifact.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$AppName = "My Financial Life"
$AppDir  = "dist\$AppName"

Write-Host "==> Stamping build metadata"
python packaging\stamp_build_info.py

Write-Host "==> Running PyInstaller"
pyinstaller --noconfirm --clean packaging\mfl.spec

$Exe = Join-Path $AppDir "$AppName.exe"
if (-not (Test-Path $Exe)) {
    Write-Error "ERROR: $Exe was not produced"
}

if ($env:WINDOWS_SIGN_PFX) {
    Write-Host "==> Code-signing"
    & signtool sign /fd SHA256 /f $env:WINDOWS_SIGN_PFX /p $env:WINDOWS_SIGN_PASSWORD `
        /tr http://timestamp.digicert.com /td SHA256 $Exe
} else {
    Write-Host "==> WINDOWS_SIGN_PFX not set — skipping signing (UNSIGNED build)."
}

$Iss = "packaging\installer.iss"
if ((Get-Command ISCC.exe -ErrorAction SilentlyContinue) -and (Test-Path $Iss)) {
    Write-Host "==> Building installer with Inno Setup"
    & ISCC.exe $Iss
} else {
    Write-Host "==> Inno Setup / installer.iss not found — app folder is the artifact: $AppDir"
}

Write-Host "==> Done."
