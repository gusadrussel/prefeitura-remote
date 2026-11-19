# PostgreSQL persistente para o próximo teste

**Destino sugerido para testes:** Neon Free anuncia PostgreSQL sem prazo fixo de expiração e suspende o compute após inatividade. Confirme as cotas e a política da organização antes de colocar inventário real no serviço. Supabase Free também pode servir ao teste, com pausa por inatividade. O PostgreSQL gratuito do próprio Render expira em 30 dias. [Neon Pricing](https://neon.com/pricing) · [Planos Neon](https://neon.com/docs/introduction/plans) · [Pausa no Supabase Free](https://supabase.com/docs/guides/platform/free-project-pausing) · [Render Free](https://render.com/docs/free)

## Preparação sem cortar o serviço

1. Crie um banco dedicado ao teste somente após autorização. Use um endpoint PostgreSQL direto e TLS com verificação do hostname. Configure a URL com o driver `postgresql+psycopg` e com `sslmode=verify-full`. Use a cadeia de confiança do provedor ou `sslrootcert=system` quando o cliente libpq oferecer suporte. Separe as credenciais da identidade migradora e da aplicação.
2. Obtenha um backup consistente do SQLite enquanto o serviço ainda estiver acessível. O Render Free não oferece Shell nem disco persistente. Este repositório não tem uma rota de exportação do SQLite em execução. Se a TI não tiver uma cópia verificável, a alternativa é começar vazio e reativar e aprovar os computadores, após autorização. [Render Free](https://render.com/docs/free)
3. Com a URL da identidade migradora injetada no ambiente local por um gerenciador de segredos, inicialize o esquema somente no PostgreSQL de teste. Use o mesmo código da versão que será publicada.

   ```sh
   python -B -m server.migrate --apply
   python -B -m server.migrate
   ```

4. Se houver um backup SQLite íntegro e o PostgreSQL estiver vazio, ensaie a importação primeiro em outro PostgreSQL descartável. Depois de autorizar o destino final e conferir o nome do banco, execute o mesmo comando com a identidade migradora.

   ```sh
   python -B -m tools.import_sqlite --sqlite-backup /caminho/privado/backup.db --target-database nome_do_banco --apply
   ```

   O importador lê a origem em modo somente leitura. Ele conserva IDs, aprovações, credenciais individuais em hash, contas, mensagens e chamados. Ele verifica contagens e ajusta as sequências para novos registros. Emita novos códigos de instalação no destino. Se houver dados no destino ou falha na importação, a transferência de linhas é interrompida. Guarde o backup fora do repositório.

5. Ajuste a role da aplicação com DML e as permissões de sequência. Em particular, a API precisa de `DELETE` em `activation_codes` para consumir ou limpar códigos. Revise o [modelo de grants](debian/roles.sql.example) para o banco dedicado. Valide o esquema com a mesma role da aplicação, sem `--apply`.

## Corte e reteste autorizados

1. No Render, mantenha a configuração de build e de execução vigente e um único worker ASGI. No ambiente do serviço, configure `PREFEITURA_DATABASE_URL` com a credencial de menor privilégio e `PREFEITURA_AUTO_MIGRATE=false` depois de inicializar o PostgreSQL. Use o gerenciador de segredos da plataforma. Preserve as variáveis de autenticação existentes.
2. Publique o backend e o dashboard 1.4.1 no mesmo commit. Emita um código, ative um cliente, aprove-o e confira o chat nos dois sentidos. Teste visualização e controle com aceites separados.
3. Reinicie o serviço e confira que o computador, sua aprovação e o histórico continuam presentes. A conexão WebSocket é restabelecida pelo cliente. Sessões, tickets de acesso e consentimentos em memória exigem nova autenticação ou novo aceite após o reinício.
4. Faça um backup lógico do PostgreSQL e teste a restauração em outro banco. Preserve os backups em armazenamento privado e verifique a restauração antes de mudanças futuras.
