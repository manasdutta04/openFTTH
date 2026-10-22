; openFTTH installer (Inno Setup 6). Built by tools\package-windows.ps1 -Installer, which passes
; /DAppVersion and /DStage (the staged app folder). Installs for the current user only: no
; administrator rights, nothing written outside the user's profile.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Stage
  #define Stage "..\..\dist\openFTTH"
#endif

[Setup]
AppId={{7E3C2F1A-5B8D-4C6E-9A2F-0D1B3E4F5A6C}
AppName=openFTTH
AppVersion={#AppVersion}
AppVerName=openFTTH {#AppVersion}
AppPublisher=openFTTH contributors
AppPublisherURL=https://github.com/manasdutta04/openFTTH
AppSupportURL=https://github.com/manasdutta04/openFTTH/issues
AppUpdatesURL=https://github.com/manasdutta04/openFTTH/releases
DefaultDirName={localappdata}\Programs\openFTTH
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputBaseFilename=openFTTH-Setup-{#AppVersion}
SetupIconFile=..\..\desktop\crates\openftth-app\assets\openftth.ico
UninstallDisplayIcon={app}\openftth.exe
LicenseFile=..\..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; An upgrade replaces the bundled runtimes whole, so no stale file from an older release stays.
Type: filesandordirs; Name: "{app}\engine"
Type: filesandordirs; Name: "{app}\harness"
Type: filesandordirs; Name: "{app}\node"

[Files]
Source: "{#Stage}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\openFTTH"; Filename: "{app}\openftth.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\openFTTH"; Filename: "{app}\openftth.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\openftth.exe"; Description: "{cm:LaunchProgram,openFTTH}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
// Projects, settings and keys live in %USERPROFILE%\.openftth. Removing the app asks before
// deleting them, and keeps them unless the user says yes.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: string;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Data := ExpandConstant('{%USERPROFILE}\.openftth');
    if DirExists(Data) then
      if MsgBox('Also delete your openFTTH projects and settings in ' + Data + '?' + #13#10 +
                'Choose No to keep them for a later install.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Data, True, True, True);
  end;
end;
