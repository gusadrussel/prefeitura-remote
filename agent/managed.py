"""Administrator-provisioned, read-only Windows profile. No work at import time."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid

from agent.config import AgentConfig
from agent.connection import validate_server_url

SCHEMA = 'prefeitura-managed-v1'
MUTEX = r'Global\PrefeituraRemote.ManagedAgent'
ADMIN = 'S-1-5-32-544'
SYSTEM = 'S-1-5-18'
FULL = 0x1f01ff
READ = 0x120089
READ_EXECUTE = 0x1200a9


class ManagedContextDenied(PermissionError):
    pass


def validate_profile(data):
    fields = {'schema','authorized_sid','agent_id','server_url','credential','interval_seconds'}
    if not isinstance(data,dict) or set(data) != fields or data.get('schema') != SCHEMA:
        raise ValueError('Perfil gerenciado inválido ou contém campos não permitidos')
    sid = data['authorized_sid']
    if not isinstance(sid,str) or not re.fullmatch(r'S-1-5-21-(?:\d+-){3}\d+',sid):
        raise ValueError('Perfil exige SID explícito de usuário local/domínio')
    try:
        identifier = str(uuid.UUID(data['agent_id']))
        url = data['server_url']
        parsed = urlsplit(url)
        if (not isinstance(url,str) or len(url)>2048 or parsed.username or parsed.password
                or parsed.query or parsed.fragment or any(ord(c)<32 for c in url)):
            raise ValueError()
        url = validate_server_url(url)
    except (ValueError,TypeError,AttributeError):
        raise ValueError('Identidade ou URL do perfil inválida') from None
    credential = data['credential']
    if not isinstance(credential,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,512}',credential):
        raise ValueError('Credencial individual do perfil inválida')
    interval = data['interval_seconds']
    if type(interval) is not int or not 30 <= interval <= 86400:
        raise ValueError('Intervalo do perfil inválido')
    return dict(data,agent_id=identifier,server_url=url)


def validate_acl(summary, *, directory=False, source=False, caller_sid=None):
    """Reject extra, inherited, denied or writable-by-user ACEs; no ACL guessing."""
    if not summary['protected'] or summary['reparse']:
        raise PermissionError('Objeto gerenciado deve ter DACL protegida e não ser reparse point')
    trusted = {ADMIN,SYSTEM}
    if source:
        trusted.add(caller_sid)
    if summary['owner'] not in trusted:
        raise PermissionError('Proprietário do objeto gerenciado não é confiável')
    readers = []
    seen = set()
    for ace in summary['aces']:
        sid,mask,flags,kind = ace
        if kind != 0 or flags & 0x10:
            raise PermissionError('ACE herdada ou não permitida no objeto gerenciado')
        expected_flags = 3 if directory else 0
        if flags != expected_flags:
            raise PermissionError('Propagação de ACL inesperada')
        if sid in seen:
            raise PermissionError('ACE duplicada no objeto gerenciado')
        seen.add(sid)
        if sid in trusted and mask == FULL:
            continue
        if not source and re.fullmatch(r'S-1-5-21-(?:\d+-){3}\d+',sid) and mask == (READ_EXECUTE if directory else READ):
            readers.append(sid)
            continue
        raise PermissionError('Permissão ampla ou escrita de usuário no objeto gerenciado')
    if not {ADMIN,SYSTEM} <= seen or (not source and len(readers)!=1):
        raise PermissionError('Conjunto de identidades incompleto no objeto gerenciado')
    return readers[0] if readers else None


def profile_sddl(sid, directory=False):
    if not re.fullmatch(r'S-1-5-21-(?:\d+-){3}\d+',sid):
        raise ValueError('SID inválido')
    flags, rights = ('OICI','FRFX') if directory else ('','FR')
    return f'O:BAD:P(A;{flags};FA;;;SY)(A;{flags};FA;;;BA)(A;{flags};{rights};;;{sid})'


def _decode(raw):
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:
                raise ValueError('Perfil contém chave duplicada')
            value[key]=item
        return value
    try:
        return validate_profile(json.loads(raw.decode('utf-8-sig'),object_pairs_hook=unique))
    except (UnicodeError,json.JSONDecodeError):
        raise ValueError('Arquivo de perfil inválido') from None


def verify_install(security=None):
    if security is None:
        from agent.windows_security import WindowsSecurity
        security = WindowsSecurity()
    root = security.root()
    sid = security.check_directory(root)
    profile = _decode(security.read(root/'config.json',expected_sid=sid))
    if profile['authorized_sid'] != sid:
        raise PermissionError('Identidade da configuração diverge da ACL')
    security.require_user_sid(sid)
    return profile


def provision(source, *, replace=False, security=None):
    if security is None:
        from agent.windows_security import WindowsSecurity
        security = WindowsSecurity()
    context = security.context()
    if not context['elevated']:
        raise PermissionError('Provisionamento exige administrador elevado')
    if security.running():
        raise RuntimeError('Encerre o agente antes de provisionar ou atualizar')
    profile = _decode(security.read(Path(source),source=True))
    sid = profile['authorized_sid']
    security.require_user_sid(sid)
    root = security.root()
    if root.exists():
        if security.check_directory(root) != sid:
            raise PermissionError('Diretório existente pertence a outra identidade')
    else:
        security.create_directory(root,sid)
    target = root/'config.json'
    if target.exists():
        previous = verify_install(security)
        if not replace:
            raise FileExistsError('Perfil existente preservado; rotação exige operação administrativa explícita')
        if previous['agent_id'] != profile['agent_id']:
            raise ValueError('Rotação não pode trocar a identidade do agente')
    security.write_profile(target,json.dumps(profile,ensure_ascii=True).encode('utf-8'),sid,replace=replace)
    verify_install(security)


def deactivate(security=None):
    if security is None:
        from agent.windows_security import WindowsSecurity
        security = WindowsSecurity()
    if not security.context()['elevated'] or security.running():
        raise PermissionError('Desativação exige administrador e agente encerrado')
    profile = verify_install(security)
    security.retire_profile(security.root()/'config.json',profile['authorized_sid'])


def write_provision_source(path,raw,security=None):
    if security is None:
        from agent.windows_security import WindowsSecurity
        security=WindowsSecurity()
    if not security.context()['elevated']:
        raise PermissionError('Criação da origem exige administrador')
    profile=_decode(raw)
    security.require_user_sid(profile['authorized_sid'])
    path=Path(path).absolute()
    security.prepare_source_directory(path.parent)
    security.write_profile(path,json.dumps(profile,ensure_ascii=True).encode('utf-8'),None,source=True)


@contextmanager
def runtime_profile(security=None):
    if security is None:
        from agent.windows_security import WindowsSecurity
        security = WindowsSecurity()
    context = security.context()
    if context['elevated'] or context['session'] == 0:
        raise ManagedContextDenied('Agente gerenciado exige sessão interativa sem elevação')
    try:
        profile = verify_install(security)
    except PermissionError:
        raise ManagedContextDenied('Perfil indisponível neste contexto de usuário') from None
    if profile['authorized_sid'] != context['sid']:
        raise ManagedContextDenied('Usuário não autorizado para este perfil')
    root = security.root()
    config = AgentConfig(server_url=profile['server_url'],token=profile['credential'],
                        individual_credential=True,interval_seconds=profile['interval_seconds'],
                        machine_id_file=root/'unused-agent-id',enrollment_token=None,
                        provisioned_agent_id=profile['agent_id'])
    with security.mutex(profile['authorized_sid']):
        yield config
