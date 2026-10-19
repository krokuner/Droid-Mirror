; ---------------------------------------------------------------------------
;  DroidMirror installer (Inno Setup 6)
;
;  Build:
;    "C:\Users\Hardik\AppData\Local\Programs\Inno Setup 6\ISCC.exe" packaging\DroidMirror.iss
;  or:  powershell -ExecutionPolicy Bypass -File packaging\build-installer.ps1
;
;  Result: dist\DroidMirror-Setup.exe -- a wizard that installs to ProgramData
;  and drops a desktop shortcut. It needs dist\DroidMirror.exe to exist first
;  (packaging\build.ps1 makes that).
;
;  Why ProgramData and not Program Files: DroidMirror is a portable-shaped app.
;  It keeps settings.json, profiles\, icons\ and the cached scrcpy server jar
;  *beside its own executable* (see droidmirror.config.project_root). Program
;  Files is read-only to normal users, so the app would silently fail to save
;  profiles there. ProgramData has exactly one copy per machine, is on the
;  user's PATH-free side of the fence, and an explicit ACL is all that is needed
;  to make it work -- which is what the [Dirs] entry below does.
; ---------------------------------------------------------------------------

#define AppName        "DroidMirror"
#define AppVersion     "0.1.0"
#define AppExe         "DroidMirror.exe"
#define SourceExe      "..\dist\DroidMirror.exe"

[Setup]
AppId={{8E5C1D42-3B77-4C0E-9A17-2F5D6C4B9E31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion=0.1.0.0
AppPublisher={#AppName}
DefaultDirName={code:GetDefaultDir}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
OutputDir=..\dist
OutputBaseFilename={#AppName}-Setup
SetupIconFile=DroidMirror.ico
InfoAfterFile=Install-notes.txt
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; ProgramData needs an administrator, and a normal install is worth a UAC prompt.
; /CURRENTUSER on the command line still installs per-user into %LOCALAPPDATA%
; for anyone who cannot elevate.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
AllowNoIcons=yes
DisableDirPage=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Dirs]
; Everything the app writes lands in here (profiles, settings.json, icons\,
; vendor\scrcpy-server-*, crash.log), so Users need Modify -- not just Read.
; Inheritance is applied to subdirectories, which is what makes it stick for
; folders the app creates later.
Name: "{app}"; Permissions: users-modify
Name: "{app}\profiles"; Permissions: users-modify
Name: "{app}\vendor"; Permissions: users-modify
Name: "{app}\icons"; Permissions: users-modify

[Files]
Source: {#SourceExe}; DestDir: "{app}"; DestName: {#AppExe}; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\settings.json.bak"

[Code]
var
  KeepState: Boolean;

{ ProgramData for a machine-wide install; %LOCALAPPDATA% when /CURRENTUSER is
  used, where writing to ProgramData would fail for lack of rights. }
function GetDefaultDir(Param: String): String;
begin
  if IsAdminInstallMode then
    Result := ExpandConstant('{commonappdata}\DroidMirror')
  else
    Result := ExpandConstant('{localappdata}\Programs\DroidMirror');
end;

function InitializeUninstall(): Boolean;
var
  Answer: Integer;
begin
  Result := True;
  { Nothing promptable in a silent uninstall: keep the user's data, which is
    the answer that cannot destroy anything. }
  KeepState := True;

  if not UninstallSilent then
  begin
    Answer := SuppressibleMsgBox(
      'Keep your keybind profiles, settings and the cached device server?' + #13#10 + #13#10 +
      'Yes  - keep them, so a reinstall picks up where you left off' + #13#10 +
      'No   - delete everything DroidMirror saved',
      mbConfirmation, MB_YESNO, IDYES);
    KeepState := (Answer = IDYES);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and (not KeepState) then
  begin
    DelTree(ExpandConstant('{app}\profiles'), True, True, True);
    DelTree(ExpandConstant('{app}\icons'), True, True, True);
    DelTree(ExpandConstant('{app}\vendor'), True, True, True);
    DeleteFile(ExpandConstant('{app}\settings.json'));
    DeleteFile(ExpandConstant('{app}\crash.log'));
  end;
end;

{ The app is a single ~98 MB file; a locked copy kills the install with a
  confusing "file in use" error, so say something clearer first. }
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if Exec(ExpandConstant('{cmd}'), '/C tasklist /FI "IMAGENAME eq {#AppExe}" | find /I "{#AppExe}" >NUL', '',
          SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0) then
  begin
    if MsgBox('DroidMirror is running. Close it first, then click OK to continue.',
              mbInformation, MB_OKCANCEL) = IDCANCEL then
      Result := False;
  end;
end;
