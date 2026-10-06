# Prefeitura Remote

Plataforma interna para inventário autorizado de computadores e visualização/controle assistido de tela. O agente coleta dados locais e os envia à API central; o painel permite visualizar e, mediante início explícito, controlar mouse e teclado. O projeto não executa comandos arbitrários, transfere arquivos, eleva privilégios ou contorna proteções do Windows.

## Requisitos

- Python 3.10 ou superior.
- Uma virtualenv por ambiente, ou a `.venv` já existente na raiz.
- PostgreSQL para instalações compartilhadas. SQLite é usado por padrão apenas para desenvolvimento e testes locais.
- No Windows, PowerShell/CIM é usado para hardware. No Linux, `psutil`, `/sys`, `lsblk` e ferramentas opcionais são consultados sem `sudo`.

## Desenvolvimento local

Na raiz do projeto:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r agent/requirements.txt -r server/requirements.txt
```

Configure um token forte para o painel. O token global de agente abaixo é apenas para a janela temporária de migração:

```bash
export PREFEITURA_AGENT_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export PREFEITURA_DASHBOARD_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
# Somente durante a migração dos agentes antigos:
export PREFEITURA_ALLOW_LEGACY_AGENT_TOKEN=true
```

Inicie o servidor, que por padrão fica acessível apenas na própria máquina:

```bash
python -m uvicorn server.main:app --host 127.0.0.1 --port 8000
```

Abra <http://127.0.0.1:8000/> e informe o token do painel. A documentação OpenAPI fica em `/docs`; o endpoint de saúde é `/api/v1/health`.

O SQLite cria `prefeitura_remote.db` na raiz. Para PostgreSQL, instale/execute o banco e configure, por exemplo:

```bash
export PREFEITURA_DATABASE_URL="postgresql+psycopg://usuario:senha@localhost:5432/prefeitura_remote"
```

O SQLAlchemy cria a tabela inicial automaticamente. Em produção, use migrações antes de alterar o esquema.

## Agente

O agente pode ser executado no Linux ou Windows. Sem URL do servidor, imprime uma coleta JSON e termina:

```bash
python agent/agent.py --once --json
```

Para enviá-la ao servidor local durante a migração legada:

```bash
export PREFEITURA_SERVER_URL="http://127.0.0.1:8000"
export PREFEITURA_AGENT_TOKEN="o-mesmo-token-configurado-no-servidor"
python agent/agent.py --once
```

Agentes novos devem usar credencial individual provisionada pelo operador; `PREFEITURA_AGENT_TOKEN` não é necessário para eles. Para o fluxo individual, configure `PREFEITURA_AGENT_CREDENTIAL` em vez do token global.

Para execução contínua, remova `--once`; o agente envia inventário periodicamente e mantém o canal WebSocket aberto. O intervalo do inventário é cinco minutos por padrão; configure com `PREFEITURA_AGENT_INTERVAL` (mínimo de 30 segundos). `--once` envia somente o inventário e termina, sem abrir o canal persistente.

## Canal persistente

- Endpoint: `GET /ws/agents/{agent_id}` por WebSocket. O `agent_id` precisa existir e estar `authorized`.
- Agentes provisionados autenticam inventário e WebSocket com uma credencial aleatória individual; o servidor mantém somente SHA-256 da credencial. O dashboard token não é aceito pelo agente.
- Durante a migração, `PREFEITURA_ALLOW_LEGACY_AGENT_TOKEN=true` permite temporariamente o token global: inventários sem credencial individual e registros não provisionados podem usar o token, e o WebSocket legado ainda deriva HMAC pelo `agent_id`. O padrão é desligado; hash individual presente ou credencial revogada nunca recorre ao modo global.
- O agente envia `hello` com seu ID e versão; o servidor responde `hello_ack`.
- O agente envia `heartbeat` periodicamente e recebe `heartbeat_ack`; o servidor fecha conexões sem heartbeat dentro do timeout configurado.
- Ao encerrar normalmente, o agente envia `disconnect` e recebe `disconnect_ack`. Desconexões de rede são detectadas e o cliente tenta reconectar.
- O servidor anuncia heartbeat de 30 segundos e encerra após 90 segundos sem heartbeat por padrão; ajuste com `PREFEITURA_AGENT_HEARTBEAT_INTERVAL`, `PREFEITURA_AGENT_HEARTBEAT_TIMEOUT` e `PREFEITURA_AGENT_HELLO_TIMEOUT`.
- O agente reconecta após falha e continua enviando inventário no intervalo definido por `PREFEITURA_AGENT_INTERVAL`; execute `python agent/agent.py` sem `--once` para manter o canal aberto.
- O canal aceita heartbeat e comandos tipados de visualização/controle descritos abaixo. Comandos desconhecidos ou campos fora do protocolo são recusados.
- Limites em memória usam janela fixa configurável por `PREFEITURA_RATE_LIMIT_WINDOW_SECONDS` (60 s padrão): `PREFEITURA_AUTH_FAILURE_LIMIT` (10 por origem/serviço), `PREFEITURA_INVENTORY_UPDATE_LIMIT` (30 por origem/agente), `PREFEITURA_VIEWER_TICKET_LIMIT` (20 por origem/computador), `PREFEITURA_AUTHORIZATION_CHANGE_LIMIT` (20 por origem/computador) e `PREFEITURA_WEBSOCKET_CONNECTION_LIMIT` (60 por origem/agente ou viewer/computador). Respostas REST limitadas usam HTTP 429 e `Retry-After`; novos handshakes WebSocket são fechados com código 1013.
- As chaves de limite usam o endereço peer observado pelo socket; `X-Forwarded-For` não é confiado. Atrás de proxy, todas as conexões podem compartilhar o IP do proxy; configure um proxy confiável antes de usar limites por cliente.
- O limitador é local ao processo: reinicializações zeram os contadores e múltiplos workers mantêm limites independentes. Nesta etapa, execute um único worker ou use limitação compartilhada no proxy antes de escalar.
- Localmente é permitido `ws://localhost`/loopback. Para qualquer host externo, configure o agente com `https://...` (ele converte para `wss://`) e sirva o FastAPI atrás de TLS/WSS; o servidor rejeita `ws://` externo.
- O dashboard mostra o estado do canal e o último heartbeat. O registro ativo fica em memória no processo Uvicorn; use um único worker nesta etapa. Multiworker exigirá um registro compartilhado antes de escalar.

## Visualização de tela

Instale também as dependências Python de captura declaradas em `agent/requirements.txt` (`mss` e Pillow). O agente só captura depois que um técnico seleciona uma máquina `Authorized`/conectada e clica em **Iniciar visualização**; **Parar**, fechar o viewer ou perder o canal encerra o capturador.

- O protocolo transmite somente frames JPEG em Base64, sem persistência no servidor ou banco.
- Limites padrão: até 5 FPS, 1280×720, qualidade JPEG 55 e 750 KB por frame. Ajuste com `PREFEITURA_SCREEN_MAX_FPS`, `PREFEITURA_SCREEN_MAX_WIDTH`, `PREFEITURA_SCREEN_MAX_HEIGHT`, `PREFEITURA_SCREEN_JPEG_QUALITY` e `PREFEITURA_SCREEN_MAX_FRAME_BYTES`.
- Linux: execute o agente dentro de uma sessão gráfica com display acessível. Para validar encoding sem hardware, rode `python -m unittest tests.test_screen_capture -v`; em ambiente headless, `mss` não terá tela para capturar.
- Windows: instale `py -m pip install -r agent\requirements.txt` e execute o agente na sessão interativa do usuário que verá a tela. Serviços em Session 0 podem não ter acesso ao desktop interativo.
- Desenvolvimento local: suba o servidor em loopback, autorize o agente, execute `python agent/agent.py` sem `--once`, abra o dashboard e inicie a visualização explicitamente. Fora de localhost, configure TLS/WSS.

### Controle de mouse e teclado

- Primeiro inicie a visualização da tela e depois clique em **Iniciar controle**. O controle só é aceito enquanto a transmissão está ativa e a máquina continua autorizada; **Encerrar controle**, parar a transmissão, desconectar ou revogar a autorização encerra a sessão e libera teclas/botões pressionados.
- O viewer envia movimentos, cliques esquerdo/direito, rolagem e teclas de uma allowlist. Coordenadas são calculadas sobre a área real da imagem exibida e convertidas pelo agente para a resolução física da tela. A captura JPEG continua ativa durante o controle.
- O agente usa `PyAutoGUI` (`agent/requirements.txt`) somente no Windows e apenas após `control.start`; instala `PyAutoGUI` junto com as dependências do agente. O backend mantém o fail-safe padrão da biblioteca. O controle não eleva privilégios: aplicações Windows executadas em nível de integridade superior podem ignorar entrada sintetizada.
- **Tela cheia** usa a Fullscreen API do navegador, preserva a proporção da imagem e pode ser encerrada por `Esc` ou pelo botão.
- O servidor audita início e término com principal, computador, agente, sessão e motivo; não registra conteúdo das teclas. Como o painel atual usa um token compartilhado, os logs identificam o principal `shared-dashboard-token`, não a pessoa física que o utilizou.

O protocolo aceita somente as mensagens tipadas `screen.start`, `screen.start_ack`, `screen.frame`, `screen.stop`, `screen.stop_ack`, `control.start`, `control.start_ack`, `control.input`, `control.stop`, `control.stop_ack` e as mensagens existentes de heartbeat/desconexão. Execução arbitrária de comandos, transferência de arquivos e captura de credenciais não são implementadas.

No Windows, instale a dependência com `py -m pip install -r agent\requirements.txt`, configure `PREFEITURA_SERVER_URL` com uma URL HTTPS e `PREFEITURA_AGENT_CREDENTIAL` provisionada para aquele UUID, e execute `py agent\agent.py --once`. O UUID persistente fica por padrão em `%ProgramData%\PrefeituraRemote\agent_id`; conceda ao serviço/usuário de execução permissão de escrita na primeira inicialização. É possível sobrescrever o caminho com `PREFEITURA_AGENT_ID_FILE`. A coleta de módulos de RAM, discos, CPU, BIOS e placa-mãe usa CIM/PowerShell e pode retornar campos indisponíveis conforme o fabricante/permissões.

No Linux, o UUID fica em `~/.local/state/prefeitura-remote/agent_id` por padrão. `dmidecode` é opcional e chamado sem `sudo`; se a política/permissão do sistema não permitir, os módulos de RAM ficam vazios e a capacidade total continua disponível. `lspci` também é opcional para identificar GPU.

### Provisionamento de credenciais de agente

1. Obtenha o UUID persistido do agente no sistema de implantação e mantenha o registro/identidade do computador sob controle administrativo.
2. No servidor, com `PREFEITURA_DATABASE_URL` apontando para o mesmo banco da aplicação, execute `py -m server.provision_agent issue <agent_id>`. O comando cria o registro se necessário, mantém/define a máquina como `pending` e grava somente o hash; a credencial aleatória aparece uma vez no stdout. Autorize a máquina separadamente pelo fluxo normal do dashboard.
3. Entregue o valor ao host por MDM ou canal de segredos e configure `PREFEITURA_AGENT_CREDENTIAL` no ambiente protegido do agente. Não coloque o valor em linha de comando, imagem, repositório, ticket ou log.
4. Confirme inventário e canal individual; após todos os hosts migrarem, desligue `PREFEITURA_ALLOW_LEGACY_AGENT_TOKEN` e remova o token global antigo do ambiente do servidor.

`py -m server.provision_agent issue <agent_id>` também rotaciona a credencial; o valor anterior deixa de autenticar imediatamente, então coordene a atualização do agente. `py -m server.provision_agent revoke <agent_id>` revoga apenas aquele segredo sem mudar a aprovação administrativa da máquina. Canais existentes deixam de passar a revalidação de heartbeat/frame ou de uma ação do viewer. Os comandos são locais ao servidor e não existe endpoint público de emissão.

## API e segurança

- `POST /api/v1/inventory`: exige `Authorization: Bearer <PREFEITURA_AGENT_CREDENTIAL>` individual; o token global só é aceito para a janela legada explicitamente habilitada.
- Primeiro inventário de um `agent_id` novo cria uma máquina `pending`; atualizações preservam o estado atual.
- Máquinas legadas sem estado de autorização migram para `pending`; seus IDs e inventários são preservados e a aprovação deve ser explícita antes do acesso remoto. Bancos que já aplicaram a migração antiga com default `authorized` têm esses estados rebaixados para `pending` uma única vez, exigindo reaprovação.
- `GET /api/v1/admin/computers?search=hostname&authorization_status=pending`: lista máquinas e filtra por hostname/estado; exige `PREFEITURA_DASHBOARD_TOKEN`.
- `GET /api/v1/admin/computers/{id}`: retorna detalhes da máquina, autenticado como técnico.
- `POST /api/v1/admin/computers/{id}/authorize` e `/revoke`: alteram o estado de autorização, autenticados como técnico.
- Os endpoints anteriores `GET /api/v1/computers` e `/api/v1/computers/{id}` continuam disponíveis como aliases compatíveis.
- Uma máquina `revoked` não é considerada autorizada (`is_authorized`); nenhum recurso de suporte remoto é implementado nesta etapa.
- Online significa que houve contato nos últimos 900 segundos, configurável por `PREFEITURA_ONLINE_THRESHOLD`.
- Tokens ausentes desabilitam a respectiva operação. O padrão de rede é loopback; para acesso pela rede, configure TLS num proxy reverso, restrinja firewall/VPN e só então exponha o serviço. Nunca use HTTP fora de loopback.
- O token único do painel é uma proteção inicial, não substitui contas individuais, papéis, rotação centralizada de credenciais nem trilha de auditoria por técnico. Esses itens são necessários antes da implantação multiusuário.

## Testes

```bash
python -m unittest discover -v
```

## Limites e próximos passos

Este MVP ainda não oferece cadastro administrativo prévio de máquinas. A ingestão atual registra agentes com o token compartilhado; produção deve evoluir para credenciais individuais revogáveis e autenticação de técnicos para atribuição individual na auditoria. PostgreSQL está preparado via URL de conexão, mas não foi validado contra um servidor PostgreSQL neste ambiente. Coletores dependentes de CIM, módulos SMBIOS, GPU e detalhes físicos devem ser homologados em modelos reais de computadores Windows da prefeitura.