# Ativação do cliente Windows

## Preparação pela TI

1. Aplique a migração com `python -m server.migrate --apply` e publique o servidor e o painel do mesmo conjunto de alterações. Em PostgreSQL com papéis separados, aplique ao usuário da aplicação também o grant de `DELETE` limitado à tabela `public.activation_codes`, conforme [o modelo de permissões](deploy/debian/roles.sql.example).
2. Entre no painel como administrador e clique em **Códigos de instalação → Gerar código de uso único**. Entregue o código ao usuário por um canal privado antes de fechar a janela. Cada código é aleatório, expira em 30 minutos e só cadastra um computador. A TI também pode emitir um código pelo terminal do servidor com `python -m server.provision_agent activation-code`.

## Instalação pelo usuário

Abra o instalador na área de trabalho da conta Windows que usará o cliente. Siga o assistente, confirme a elevação de instalação do Windows e digite o código fornecido pela TI. Após a conclusão, abra **Prefeitura Remote - Cliente** pelo assistente ou pelo menu Iniciar.

O cliente identifica a conta da área de trabalho, gera o UUID e usa o servidor HTTPS configurado no instalador. O perfil gerenciado fica em `%ProgramData%\PrefeituraRemoteManaged\config.json` com gravação reservada a administradores e SYSTEM e leitura permitida à conta vinculada.

A TI pode localizar o computador no painel pelo nome informado no inventário e autorizá-lo quando estiver **Pendente de aprovação**. Visualização e controle precisam de confirmações locais separadas. A aprovação do computador permite que o agente entre no canal, e as sessões remotas continuam dependendo dessas confirmações.

## Publicação no Render

1. Abra **Settings** do serviço existente e confirme o repositório e a branch conectados, o modo de deploy e o banco ou disco usados pelo serviço. Preserve os comandos de build e start, a configuração de persistência e as variáveis de ambiente atuais.
2. Faça backup do banco antes de publicar. Se a tabela `activation_codes` já existe, a interface administrativa usa o esquema da versão 1.4.0. Se a tabela estiver ausente e a migração automática estiver desativada, aplique `python -B -m server.migrate --apply` com a identidade migradora no banco existente e verifique com `python -B -m server.migrate`. Em PostgreSQL externo, um pre-deploy já habilitado pode executar a migração. Para SQLite em disco persistente, execute-a em um ambiente que acesse o mesmo disco, pois o pre-deploy do Render roda em instância separada.
3. Publique o backend e o dashboard juntos na branch conectada. Acompanhe o deploy automático existente ou selecione **Manual Deploy → Deploy latest commit** se o deploy automático estiver desligado. Confira os logs de startup e, após atualizar o painel, confirme que a seção aparece com login administrativo e fica oculta com login de técnico.
4. Emita um código para o computador de teste, instale o cliente 1.4.0 e confirme que o registro aparece como pendente. Aprove o computador e teste a visualização e o controle com as confirmações locais.

## Reinstalação, falhas e remoção

Uma reinstalação valida e reaproveita o perfil existente. Se a conexão cair durante a ativação, confira no painel se o código chegou a ser consumido. Se o servidor registrou a máquina e o perfil local não foi criado, revogue a credencial do registro órfão e emita outro código.

Para remover uma instalação ativa, revogue a credencial no servidor com `python -m server.provision_agent revoke <UUID>`. Encerre o agente e execute o cliente elevado com `--deactivate-managed-install` para arquivar o perfil antes de desinstalar. A rotação administrativa de credencial mantém a identidade existente.
