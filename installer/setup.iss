#define MyAppName "StreamSaver"
#define MyAppVersion "1.0.0"
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
OutputBaseFilename=StreamSaver_Setup
SetupIconFile=assets\icon.ico
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
var
  EnvPage: TWizardPage;
  RelayURLEdit: TEdit;
  RelaySecretEdit: TEdit;

procedure InitializeWizard;
var
  lbl1, lbl2: TLabel;
begin
  EnvPage := CreateCustomPage(wpSelectDir, '서버 연결 정보', '릴레이 서버 정보를 확인하세요. (기본값 그대로 사용하시면 됩니다)');

  lbl1 := TLabel.Create(EnvPage);
  lbl1.Parent := EnvPage.Surface;
  lbl1.Caption := '릴레이 서버 주소:';
  lbl1.Top := 8;
  lbl1.Left := 0;

  RelayURLEdit := TEdit.Create(EnvPage);
  RelayURLEdit.Parent := EnvPage.Surface;
  RelayURLEdit.Top := 28;
  RelayURLEdit.Left := 0;
  RelayURLEdit.Width := EnvPage.SurfaceWidth;
  RelayURLEdit.Text := '{#RelayURL}';

  lbl2 := TLabel.Create(EnvPage);
  lbl2.Parent := EnvPage.Surface;
  lbl2.Caption := '비밀 키:';
  lbl2.Top := 68;
  lbl2.Left := 0;

  RelaySecretEdit := TEdit.Create(EnvPage);
  RelaySecretEdit.Parent := EnvPage.Surface;
  RelaySecretEdit.Top := 88;
  RelaySecretEdit.Left := 0;
  RelaySecretEdit.Width := EnvPage.SurfaceWidth;
  RelaySecretEdit.Text := '{#RelaySecret}';
  RelaySecretEdit.PasswordChar := '*';
end;

procedure CreateEnvFile;
var
  DataDir, EnvPath: String;
begin
  DataDir := ExpandConstant('{localappdata}\StreamSaver');
  ForceDirectories(DataDir);
  EnvPath := DataDir + '\.env';

  SaveStringToFile(EnvPath, 'RELAY_SERVER_URL=' + RelayURLEdit.Text + #13#10, False);
  SaveStringToFile(EnvPath, 'RELAY_SECRET=' + RelaySecretEdit.Text + #13#10, True);
  SaveStringToFile(EnvPath, 'RELAY_PAIR_CODE=' + #13#10, True);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    CreateEnvFile;
end;

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "StreamSaver 지금 실행하기"; Flags: nowait postinstall skipifsilent
