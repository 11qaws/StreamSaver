#define MyAppName "StreamSaver"
#define MyAppVersion "1.1.18"
#define MyAppPublisher "StreamSaver"
#define MyAppExeName "StreamSaver.exe"
#define RelayURL "ws://217.142.229.237:8765"
#define RelaySecret "streamsaver2026"

[Setup]
AppId={{B4F7A2C1-3D8E-4F9A-B5C2-1E6D7F8A9B0C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=StreamSaver_Setup_v{#MyAppVersion}
SetupIconFile=assets\icon.ico
WizardImageFile=assets\wizard_large.bmp
WizardSmallImageFile=assets\wizard_small.bmp
Compression=lzma
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=120
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 바로가기 만들기"; GroupDescription: "추가 옵션:"; Flags: unchecked
Name: "startup"; Description: "Windows 시작 시 자동으로 실행"; GroupDescription: "추가 옵션:"

[Files]
Source: "..\dist\StreamSaver\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "yt-dlp.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "StreamSaver"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: startup

[Code]
procedure CreateEnvFile;
var
  DataDir, EnvPath: String;
begin
  DataDir := ExpandConstant('{localappdata}\StreamSaver');
  ForceDirectories(DataDir);
  EnvPath := DataDir + '\.env';

  if FileExists(EnvPath) then Exit;

  SaveStringToFile(EnvPath, 'RELAY_SERVER_URL={#RelayURL}' + #13#10, False);
  SaveStringToFile(EnvPath, 'RELAY_SECRET={#RelaySecret}' + #13#10, True);
  SaveStringToFile(EnvPath, 'RELAY_PAIR_CODE=' + #13#10, True);
end;

procedure KillRunningInstance;
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/f /im {#MyAppExeName}', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Sleep(1500);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    KillRunningInstance;
  if CurStep = ssPostInstall then
    CreateEnvFile;
end;

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "StreamSaver 지금 실행하기"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#MyAppExeName}"; Flags: nowait; Check: WizardSilent
