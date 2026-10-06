#define MyAppName "Prefeitura Remote - Cliente"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Prefeitura"
#define MyAppExeName "PrefeituraRemoteCliente.exe"

[Setup]
AppId={{8F4D8B7C-7C1A-4C3A-9E5A-PREFEITURAREMOTE}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}

DefaultDirName={autopf}\PrefeituraRemote
DefaultGroupName=Prefeitura Remote

OutputDir=..\..\dist
OutputBaseFilename=PrefeituraRemote-Cliente-Setup

ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin

DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\..\dist\PrefeituraRemoteCliente.exe"; \
    DestDir: "{app}"; \
    Flags: ignoreversion

[Registry]
Root: HKLM; \
    Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; \
    ValueName: "PrefeituraRemote"; \
    ValueData: """{app}\{#MyAppExeName}"""; \
    Flags: uninsdeletevalue

[Icons]
Name: "{autoprograms}\Prefeitura Remote - Cliente"; \
    Filename: "{app}\{#MyAppExeName}"

[UninstallDelete]
Type: filesandordirs; \
    Name: "{app}"

[Code]

var
  ConfigPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  ConfigPage := CreateInputQueryPage(
    wpSelectDir,
    'Configuração do Prefeitura Remote',
    'Conexão com o servidor',
    'Informe os dados fornecidos pelo técnico de TI.'
  );

  ConfigPage.Add('URL do servidor:', False);
  ConfigPage.Add('Credencial do agente:', True);

  ConfigPage.Values[0] := 'https://servidor.prefeitura.local';
end;

function JsonEscape(Value: string): string;
begin
  StringChangeEx(Value, '\', '\\', True);
  StringChangeEx(Value, '"', '\"', True);
  Result := Value;
end;

procedure CreateAgentConfig;
var
  ConfigDir: string;
  ConfigFile: string;
  Json: string;
begin
  ConfigDir := ExpandConstant('{commonappdata}\PrefeituraRemote');
  ConfigFile := ConfigDir + '\config.json';

  ForceDirectories(ConfigDir);

  Json :=
    '{' + #13#10 +
    '    "server_url": "' + JsonEscape(ConfigPage.Values[0]) + '",' + #13#10 +
    '    "credential": "' + JsonEscape(ConfigPage.Values[1]) + '",' + #13#10 +
    '    "interval_seconds": 300' + #13#10 +
    '}';

  SaveStringToFile(ConfigFile, Json, False);

  Exec(
    ExpandConstant('{sys}\icacls.exe'),
    '"' + ConfigFile + '" /inheritance:r /grant:r *S-1-5-18:F *S-1-5-32-544:F',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    CreateAgentConfig;
end;