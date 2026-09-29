# Prefeitura Remote

Plataforma interna inicial para inventário autorizado de computadores. O agente coleta dados locais e os envia à API central; o painel de técnicos consulta os equipamentos cadastrados. O projeto não implementa acesso remoto, captura de tela, teclado, mouse ou transferência de arquivos.

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

Configure tokens fortes e distintos para os agentes e os técnicos:

```bash
export PREFEITURA_AGENT_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export PREFEITURA_DASHBOARD_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
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

Para enviá-la ao servidor local:

```bash
export PREFEITURA_SERVER_URL="http://127.0.0.1:8000"
export PREFEITURA_AGENT_TOKEN="o-mesmo-token-configurado-no-servidor"
python agent/agent.py --once
```

Para execução contínua, remova `--once`; o agente envia inventário periodicamente e mantém o canal WebSocket aberto. O intervalo do inventário é cinco minutos por padrão; configure com `PREFEITURA_AGENT_INTERVAL` (mínimo de 30 segundos). `--once` envia somente o inventário e termina, sem abrir o canal persistente.

## Canal persistente

- Endpoint: `GET /ws/agents/{agent_id}` por WebSocket. O `agent_id` precisa existir e estar `authorized`.
- O agente deriva uma credencial HMAC específica do `agent_id` a partir de `PREFEITURA_AGENT_TOKEN` para o handshake. O segredo de inventário não é enviado no WebSocket e o dashboard token não é aceito.
- O agente envia `hello` com seu ID e versão; o servidor responde `hello_ack`.
- O agente envia `heartbeat` periodicamente e recebe `heartbeat_ack`; o servidor fecha conexões sem heartbeat dentro do timeout configurado.
- Ao encerrar normalmente, o agente envia `disconnect` e recebe `disconnect_ack`. Desconexões de rede são detectadas e o cliente tenta reconectar.
- O servidor anuncia heartbeat de 30 segundos e encerra após 90 segundos sem heartbeat por padrão; ajuste com `PREFEITURA_AGENT_HEARTBEAT_INTERVAL`, `PREFEITURA_AGENT_HEARTBEAT_TIMEOUT` e `PREFEITURA_AGENT_HELLO_TIMEOUT`.
- O agente reconecta após falha e continua enviando inventário no intervalo definido por `PREFEITURA_AGENT_INTERVAL`; execute `python agent/agent.py` sem `--once` para manter o canal aberto.
- Somente `hello`, `heartbeat` e `disconnect` são aceitos. Outros tipos são recusados; não há comandos de suporte remoto nesta etapa.
- Localmente é permitido `ws://localhost`/loopback. Para qualquer host externo, configure o agente com `https://...` (ele converte para `wss://`) e sirva o FastAPI atrás de TLS/WSS; o servidor rejeita `ws://` externo.
- O dashboard mostra o estado do canal e o último heartbeat. O registro ativo fica em memória no processo Uvicorn; use um único worker nesta etapa. Multiworker exigirá um registro compartilhado antes de escalar.

## Visualização de tela

Instale também as dependências Python de captura declaradas em `agent/requirements.txt` (`mss` e Pillow). O agente só captura depois que um técnico seleciona uma máquina `Authorized`/conectada e clica em **Iniciar visualização**; **Parar**, fechar o viewer ou perder o canal encerra o capturador.

- O protocolo transmite somente frames JPEG em Base64, sem persistência no servidor ou banco.
- Limites padrão: até 5 FPS, 1280×720, qualidade JPEG 55 e 750 KB por frame. Ajuste com `PREFEITURA_SCREEN_MAX_FPS`, `PREFEITURA_SCREEN_MAX_WIDTH`, `PREFEITURA_SCREEN_MAX_HEIGHT`, `PREFEITURA_SCREEN_JPEG_QUALITY` e `PREFEITURA_SCREEN_MAX_FRAME_BYTES`.
- Linux: execute o agente dentro de uma sessão gráfica com display acessível. Para validar encoding sem hardware, rode `python -m unittest tests.test_screen_capture -v`; em ambiente headless, `mss` não terá tela para capturar.
- Windows: instale `py -m pip install -r agent\requirements.txt` e execute o agente na sessão interativa do usuário que verá a tela. Serviços em Session 0 podem não ter acesso ao desktop interativo.
- Desenvolvimento local: suba o servidor em loopback, autorize o agente, execute `python agent/agent.py` sem `--once`, abra o dashboard e inicie a visualização explicitamente. Fora de localhost, configure TLS/WSS.

O protocolo desta etapa aceita apenas `screen.start`, `screen.start_ack`, `screen.frame`, `screen.stop` e `screen.stop_ack`, além das mensagens de heartbeat existentes. Mouse, teclado, comandos, transferência de arquivos e captura de credenciais não são implementados.

No Windows, instale a dependência com `py -m pip install -r agent/requirements.txt`, configure `PREFEITURA_SERVER_URL` com uma URL HTTPS e `PREFEITURA_AGENT_TOKEN`, e execute `py agent\agent.py --once`. O UUID persistente fica por padrão em `%ProgramData%\PrefeituraRemote\agent_id`; conceda ao serviço/usuário de execução permissão de escrita na primeira inicialização. É possível sobrescrever o caminho com `PREFEITURA_AGENT_ID_FILE`. A coleta de módulos de RAM, discos, CPU, BIOS e placa-mãe usa CIM/PowerShell e pode retornar campos indisponíveis conforme o fabricante/permissões.

No Linux, o UUID fica em `~/.local/state/prefeitura-remote/agent_id` por padrão. `dmidecode` é opcional e chamado sem `sudo`; se a política/permissão do sistema não permitir, os módulos de RAM ficam vazios e a capacidade total continua disponível. `lspci` também é opcional para identificar GPU.

## API e segurança

- `POST /api/v1/inventory`: exige `Authorization: Bearer <PREFEITURA_AGENT_TOKEN>`.
- Primeiro inventário de um `agent_id` novo cria uma máquina `pending`; atualizações preservam o estado atual.
- Máquinas existentes recebem `authorized` ao migrar o schema, preservando a compatibilidade com registros anteriores.
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

Este MVP não implementa controle remoto nem cadastro administrativo prévio de máquinas. A ingestão atual registra qualquer agente com o token compartilhado; produção deve evoluir para credenciais individuais revogáveis, autenticação de técnicos e autorização explícita por máquina/sessão. PostgreSQL está preparado via URL de conexão, mas não foi validado contra um servidor PostgreSQL neste ambiente. Coletores dependentes de CIM, módulos SMBIOS, GPU e detalhes físicos devem ser homologados em modelos reais de computadores Windows da prefeitura.