#define MyAppName "Prefeitura Remote - Cliente"
#define MyAppVersion "1.4.1"
#define MyAppExeName "PrefeituraRemoteCliente.exe"
#define PublicServerUrl "https://prefeitura-remote.onrender.com"

[Setup]
AppId={{E5197E7E-B820-4DD9-853E-55A8CDAA26CF}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Prefeitura
SetupIconFile=PrefeituraRemote.ico
DefaultDirName={autopf}\PrefeituraRemoteManaged
DefaultGroupName=Prefeitura Remote
OutputDir=..\..\dist
OutputBaseFilename=PrefeituraRemote-Cliente-Setup
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
AppMutex=Global\PrefeituraRemote.ManagedAgent
CloseApplications=no
RestartApplications=no
DisableProgramGroupPage=yes
DisableDirPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\..\dist\PrefeituraRemoteCliente.exe"; DestDir: "{app}"; Flags: ignoreversion; AfterInstall: ProvisionManagedProfile

[Registry]
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "PrefeituraRemoteManaged"; ValueData: """{app}\{#MyAppExeName}"" --managed"; Flags: uninsdeletevalue

[Icons]
Name: "{autoprograms}\Prefeitura Remote - Cliente"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--managed"

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--managed"; Description: "Abrir Prefeitura Remote - Cliente"; Flags: postinstall nowait skipifsilent runasoriginaluser

[Code]
function ProfileFile: String;
begin
  Result := ExpandConstant('{commonappdata}\PrefeituraRemoteManaged\config.json');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if CompareText(ExpandConstant('{app}'), ExpandConstant('{autopf}\PrefeituraRemoteManaged')) <> 0 then begin
    Result := 'O executável gerenciado deve permanecer no diretório protegido padrão de Program Files.';
    Exit;
  end;
  if RegValueExists(HKLM, 'Software\Microsoft\Windows\CurrentVersion\Run', 'PrefeituraRemote') or
     FileExists(ExpandConstant('{autopf}\PrefeituraRemote\PrefeituraRemoteCliente.exe')) or
     RegKeyExists(HKLM, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8F4D8B7C-7C1A-4C3A-9E5A-PREFEITURAREMOTE}_is1') then begin
    Result := 'Instalação legada detectada. Encerre/revogue e migre a identidade conforme o roteiro, sem apagar os dados existentes.';
    Exit;
  end;
  if WizardSilent and not FileExists(ProfileFile) then
    Result := 'Abra o assistente para ativar este computador com o código fornecido pela TI.';
end;

procedure ProvisionManagedProfile;
var
  Params: String;
  ResultCode: Integer;
  ExistingProfile: Boolean;
begin
  ExistingProfile := FileExists(ProfileFile);
  if ExistingProfile then
    Params := '--verify-managed-install'
  else
    Params := '--activate-managed "{#PublicServerUrl}"';
  if not Exec(ExpandConstant('{app}\{#MyAppExeName}'), Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    RaiseException('Não foi possível executar a verificação/provisão do perfil.');
  if ResultCode = 2 then
    RaiseException('Ativação cancelada. Abra o instalador novamente quando tiver um código da TI.');
  if ExistingProfile and (ResultCode <> 0) then
    RaiseException('Perfil gerenciado reprovado. Agente não iniciado; instalação exige revisão administrativa.');
  if ResultCode <> 0 then
    RaiseException('A ativação não foi concluída. Confira a mensagem de ativação e tente novamente.');
end;

function InitializeUninstall: Boolean;
begin
  Result := not FileExists(ProfileFile);
  if not Result then
    MsgBox('Antes da remoção: revogue a credencial no servidor, encerre o agente e execute o cliente elevado com --deactivate-managed-install. O perfil será arquivado sem leitura do usuário. Nenhum perfil foi apagado.', mbError, MB_OK);
end;
