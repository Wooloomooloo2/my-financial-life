# Build the Windows app (ADR-104): PyInstaller folder -> (optional sign) ->
# (optional Inno Setup installer -> optional sign).
#
# Usage:  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#
# Signing is OFF unless configured, so this produces a working *unsigned* app +
# installer out of the box (SmartScreen warns until signed — needs a code-
# signing cert, ADR-078 K2). To sign, set:
#   $env:WINDOWS_SIGN_PFX       path to a .pfx code-signing cert
#   $env:WINDOWS_SIGN_PASSWORD  its password
# When set, both the bundled app exe AND the finished installer are signed.
#
# Installer: if ISCC.exe (Inno Setup) is on PATH, packaging\installer.iss is
# compiled into dist\MyFinancialLife-<version>-setup.exe; otherwise the app
# folder is the artifact.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$AppName = "My Financial Life"
$AppDir  = "dist\$AppName"

# Single source of truth for the version (drives the installer filename + metadata).
$Version = (python -c "from mfl_desktop.version import __version__; print(__version__)").Trim()
Write-Host "==> Building $AppName $Version"

Write-Host "==> Stamping build metadata"
python packaging\stamp_build_info.py

Write-Host "==> Running PyInstaller"
pyinstaller --noconfirm --clean packaging\mfl.spec

$Exe = Join-Path $AppDir "$AppName.exe"
if (-not (Test-Path $Exe)) {
    Write-Error "ERROR: $Exe was not produced"
}

function Invoke-Sign($Path) {
    & signtool sign /fd SHA256 /f $env:WINDOWS_SIGN_PFX /p $env:WINDOWS_SIGN_PASSWORD `
        /tr http://timestamp.digicert.com /td SHA256 $Path
    if ($LASTEXITCODE -ne 0) { Write-Error "signtool failed for $Path" }
}

if ($env:WINDOWS_SIGN_PFX) {
    Write-Host "==> Code-signing the app executable"
    Invoke-Sign $Exe
} else {
    Write-Host "==> WINDOWS_SIGN_PFX not set — skipping signing (UNSIGNED build)."
}

$Iss = "packaging\installer.iss"
if ((Get-Command ISCC.exe -ErrorAction SilentlyContinue) -and (Test-Path $Iss)) {
    Write-Host "==> Building installer with Inno Setup"
    $SrcAbs = (Resolve-Path $AppDir).Path
    $OutAbs = (Resolve-Path "dist").Path
    & ISCC.exe "/DMyAppVersion=$Version" "/DAppSourceDir=$SrcAbs" "/DOutputDir=$OutAbs" $Iss
    if ($LASTEXITCODE -ne 0) { Write-Error "ISCC failed" }

    $Installer = Join-Path "dist" "MyFinancialLife-$Version-setup.exe"
    if (Test-Path $Installer) {
        if ($env:WINDOWS_SIGN_PFX) {
            Write-Host "==> Code-signing the installer"
            Invoke-Sign $Installer
        }
        Write-Host "==> Installer: $Installer"
    } else {
        Write-Warning "Installer not found at $Installer"
    }
} else {
    Write-Host "==> Inno Setup (ISCC.exe) not found — app folder is the artifact: $AppDir"
    Write-Host "    Install Inno Setup 6 and re-run to produce an installer."
}

Write-Host "==> Done."
