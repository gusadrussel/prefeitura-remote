"""Interactive first-time enrollment for the Windows managed installer."""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request
import uuid

from agent.http_transport import open_agent_request
from agent.managed import SCHEMA, verify_install, validate_profile


def redeem_code(server_url, agent_id, code):
    parsed = urlsplit(server_url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError('O instalador exige uma URL HTTPS válida.')
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,64}', code):
        raise ValueError('Confira o código de ativação fornecido pela TI.')
    request = Request(
        server_url.rstrip('/') + '/api/v1/agents/activate', data=b'', method='POST',
        headers={'X-Prefeitura-Activation': code, 'X-Prefeitura-Agent-Id': agent_id},
    )
    try:
        with open_agent_request(request, timeout=20) as response:
            payload = response.read(4097)
            if len(payload) > 4096:
                raise ValueError()
            result = json.loads(payload)
            if response.status != 201 or not isinstance(result, dict) or result.get('agent_id') != agent_id:
                raise ValueError()
            credential = result.get('credential')
            if not isinstance(credential, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,512}', credential):
                raise ValueError()
            return credential
    except HTTPError as error:
        error.close()
        messages = {
            401: 'Código inválido, expirado ou já utilizado. Solicite outro código à TI.',
            404: 'O servidor ainda não disponibiliza a ativação deste cliente. Consulte a TI.',
            429: 'Muitas tentativas. Aguarde e tente novamente.',
        }
        raise RuntimeError(messages.get(error.code, 'O servidor recusou a ativação. Consulte a TI.')) from None
    except (URLError, OSError):
        raise RuntimeError('Falha na conexão HTTPS. Verifique a rede e consulte a TI antes de tentar novamente.') from None
    except ValueError:
        raise RuntimeError('Resposta de ativação inválida. Consulte a TI.') from None


def activate_install(server_url, ask_code, security):
    context = security.context()
    if not context['elevated'] or not context['session'] or security.running():
        raise PermissionError('Abra o instalador na conta que usará o cliente e encerre o agente antes de continuar.')
    root = security.root()
    target = root / 'config.json'
    if target.exists():
        verify_install(security)
        return True
    sid = security.interactive_user_sid(context['session'])
    security.require_user_sid(sid)
    code = ask_code()
    if not code:
        return False
    if root.exists():
        if security.check_directory(root) != sid:
            raise PermissionError('O diretório existente está vinculado a outra conta Windows. Consulte a TI.')
    else:
        security.create_directory(root, sid)
    agent_id = str(uuid.uuid4())
    credential = redeem_code(server_url, agent_id, code.strip())
    profile = validate_profile(dict(schema=SCHEMA, authorized_sid=sid, agent_id=agent_id,
                                    server_url=server_url, credential=credential, interval_seconds=300))
    security.write_profile(target, json.dumps(profile).encode('utf-8'), sid)
    verify_install(security)
    return True


def run_activation(server_url):
    import tkinter as tk
    from tkinter import messagebox, simpledialog
    from agent.windows_security import WindowsSecurity

    window = tk.Tk()
    window.withdraw()
    title = 'Ativação do Prefeitura Remote'
    try:
        activated = activate_install(
            server_url, lambda: simpledialog.askstring(title, 'Digite o código de uso único fornecido pela TI.',
                                                       show='*', parent=window), WindowsSecurity(),
        )
        return 0 if activated else 2
    except (RuntimeError, ValueError) as error:
        messagebox.showerror(title, str(error), parent=window)
        return 1
    except OSError:
        messagebox.showerror(title, 'Não foi possível proteger o perfil da conta Windows. Consulte a TI.',
                             parent=window)
        return 1
    finally:
        window.destroy()
