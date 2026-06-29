; Inno Setup script for My Financial Life (ADR-104 / ADR-078).
;
; Produces a per-machine Windows installer (Program Files, admin) from the
; PyInstaller output folder. Driven by packaging\build_windows.ps1, which runs
; PyInstaller first and passes the version + source/output paths in as defines:
;
;     ISCC.exe /DMyAppVersion=1.0.0 ^
;              /DAppSourceDir="...\dist\My Financial Life" ^
;              /DOutputDir="...\dist" packaging\installer.iss
;
; Run standalone (defaults to dist\My Financial Life, version 1.0.0) once a
; PyInstaller build exists:  ISCC.exe packaging\installer.iss
;
; Code signing is NOT done here — build_windows.ps1 signs the bundled app exe
; before packaging and signs the finished installer afterwards when the signing
; env vars are set (ADR-078 K2). User data lives in %APPDATA% via QStandardPaths
; (ADR-050), NOT under {app}, so uninstall never touches the user's .mfl files.

#define MyAppName "My Financial Life"
#define MyAppPublisher "Garelochsoft"
#define MyAppURL "https://garelochsoft.com"
#define MyAppExeName "My Financial Life.exe"

; Overridable from the command line (build_windows.ps1 supplies the real values).
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#ifndef AppSourceDir
  #define AppSourceDir "..\dist\My Financial Life"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
; AppId uniquely identifies the app for upgrades/uninstall — KEEP CONSTANT
; across versions so 1.0.1 upgrades 1.0.0 in place rather than installing twice.
AppId={{8B4F2E1A-7C9D-4A53-B6E8-2F1D9C3A5E70}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
VersionInfoVersion={#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-machine install (Program Files) → requires elevation (owner's choice).
PrivilegesRequired=admin
; 64-bit only (PySide6 wheels are x64). "x64" works across all Inno 6.x
; (alias for x64os in 6.3+); avoids requiring a specific Inno version.
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=MyFinancialLife-{#MyAppVersion}-setup
SetupIconFile=..\assets\icons\mfl.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The entire PyInstaller folder (exe + Qt DLLs + bundled migrations/icons).
Source: "{#AppSourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
