#define MyAppName "Vyntra"
#define MyAppVersion "1.5.0"
#define MyAppExeName "Vyntra.exe"
#define PublicServerUrl "https://prefeitura-remote.onrender.com"

[Setup]
AppId={{E5197E7E-B820-4DD9-853E-55A8CDAA26CF}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Vyntra
SetupIconFile=Vyntra.ico
WizardStyle=modern
WizardImageFile=vyntra-wizard.bmp
WizardSmallImageFile=vyntra-wizard-small.bmp
DefaultDirName={autopf}\PrefeituraRemoteManaged
DefaultGroupName=Vyntra
OutputDir=..\..\dist
OutputBaseFilename=Vyntra-Setup
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
AppMutex=Global\PrefeituraRemote.ManagedAgent
CloseApplications=no
RestartApplications=no
DisableProgramGroupPage=yes
DisableDirPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
VersionInfoVersion=1.5.0.0
VersionInfoProductName=Vyntra
VersionInfoProductVersion=1.5.0

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "autostart"; Description: "Iniciar o Vyntra automaticamente ao entrar no Windows"; Flags: unchecked

[Files]
Source: "..\..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion; AfterInstall: ProvisionManagedProfile

[Registry]
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "PrefeituraRemoteManaged"; Flags: deletevalue
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Vyntra"; ValueData: """{app}\{#MyAppExeName}"" --managed"; Flags: uninsdeletevalue; Tasks: autostart
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "Vyntra"; Flags: deletevalue; Tasks: not autostart

[Icons]
Name: "{autoprograms}\Vyntra"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--managed --show-startup-errors"

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--managed --show-startup-errors"; Description: "Abrir o Vyntra"; Flags: postinstall nowait skipifsilent runasoriginaluser

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
  if not Exec(ExpandConstant('{app}\{#MyAppExeName}'), Params, '', SW_SHOWNORMAL, ewWaitUntilTerminated, ResultCode) then
    RaiseException('Não foi possível executar a verificação/provisão do perfil.');
  if ResultCode = 2 then
    RaiseException('Ativação cancelada. Abra o instalador novamente quando tiver um código da TI.');
  if ExistingProfile and (ResultCode <> 0) then
    RaiseException('Perfil gerenciado reprovado. Agente não iniciado; instalação exige revisão administrativa.');
  if ResultCode <> 0 then
    RaiseException('A ativação não foi concluída. Confira a mensagem de ativação e tente novamente.');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then begin
    DeleteFile(ExpandConstant('{commonprograms}\Prefeitura Remote - Cliente.lnk'));
    DeleteFile(ExpandConstant('{app}\PrefeituraRemoteCliente.exe'));
  end;
end;

function InitializeUninstall: Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if not FileExists(ProfileFile) then
    Exit;
  Result := False;
  if UninstallSilent then
    Exit;
  if MsgBox('Antes de desinstalar, encerre o Vyntra e peça à TI para revogar a credencial deste computador no Console. O perfil local será arquivado em uma pasta protegida.' + #13#10 + #13#10 + 'A credencial já foi revogada e o Vyntra está fechado?', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) <> IDYES then
    Exit;
  if Exec(ExpandConstant('{app}\{#MyAppExeName}'), '--deactivate-managed-install', '', SW_SHOWNORMAL, ewWaitUntilTerminated, ResultCode) then
    Result := ResultCode = 0;
  if not Result then
    MsgBox('Não foi possível arquivar o perfil. Feche o aplicativo e tente novamente ou consulte a TI.', mbError, MB_OK);
end;
