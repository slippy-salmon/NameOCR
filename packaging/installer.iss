; Inno Setup script for NameOCR: per-user install, no admin rights needed.
; Build:  set APP_VERSION=1.0.0  then  iscc packaging\installer.iss
#define AppVersion GetEnv("APP_VERSION")
#if AppVersion == ""
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{395A8AEE-1CA7-4AFD-B835-679EA26CC9F7}
AppName=NameOCR
AppVersion={#AppVersion}
AppVerName=NameOCR {#AppVersion}
AppPublisher=Slippy
; {autopf} is %LOCALAPPDATA%\Programs for "just me" and Program Files for "everyone"
DefaultDirName={autopf}\NameOCR
; show the folder page on first install (Browse... to change it); upgrades reuse the same folder
DisableDirPage=auto
DisableProgramGroupPage=yes
; "Install for me only" (no admin) or "for all users" (asks for admin), like most installers
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=Output
OutputBaseFilename=NameOCR-Setup-{#AppVersion}
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\NameOCR.exe
UninstallDisplayName=NameOCR
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[InstallDelete]
; clear out the previous version's program files on upgrade (your settings live elsewhere)
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\NameOCR\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; AppUserModelID ties the running window to these shortcuts, so pinning works
Name: "{autoprograms}\NameOCR"; Filename: "{app}\NameOCR.exe"; AppUserModelID: "NameOCR"
Name: "{autodesktop}\NameOCR"; Filename: "{app}\NameOCR.exe"; Tasks: desktopicon; AppUserModelID: "NameOCR"

[Run]
Filename: "{app}\NameOCR.exe"; Description: "Open NameOCR now"; Flags: nowait postinstall skipifsilent
