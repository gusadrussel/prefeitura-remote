"""Narrow Win32 boundary for managed distribution; instantiated only explicitly."""
from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import uuid


class SecurityAttributes(C.Structure):
    _fields_ = [('length',W.DWORD),('descriptor',C.c_void_p),('inherit',W.BOOL)]


class AclHeader(C.Structure):
    _fields_ = [('revision',W.BYTE),('reserved',W.BYTE),('size',W.WORD),('count',W.WORD),('reserved2',W.WORD)]


class WindowsSecurity:
    def __init__(self):
        if os.name != 'nt':
            raise OSError('Perfil gerenciado disponível somente no Windows')
        self.k = C.WinDLL('kernel32.dll',use_last_error=True,winmode=0x800)
        self.a = C.WinDLL('advapi32.dll',use_last_error=True,winmode=0x800)
        self.shell = C.WinDLL('shell32.dll',use_last_error=True,winmode=0x800)
        self.user = C.WinDLL('user32.dll',use_last_error=True,winmode=0x800)
        p, u, b, s = C.c_void_p, W.DWORD, W.BOOL, W.LPCWSTR
        def bind(dll,name,args,result):
            f=getattr(dll,name); f.argtypes=args; f.restype=result
        bind(self.k,'LocalFree',[p],p)
        bind(self.k,'CloseHandle',[W.HANDLE],b)
        bind(self.k,'GetCurrentProcess',[],W.HANDLE)
        bind(self.k,'GetCurrentProcessId',[],u)
        bind(self.k,'OpenProcess',[u,b,u],W.HANDLE)
        bind(self.k,'ProcessIdToSessionId',[u,C.POINTER(u)],b)
        bind(self.user,'GetShellWindow',[],W.HWND)
        bind(self.user,'GetWindowThreadProcessId',[W.HWND,C.POINTER(u)],u)
        bind(self.a,'OpenProcessToken',[W.HANDLE,u,C.POINTER(W.HANDLE)],b)
        bind(self.a,'GetTokenInformation',[W.HANDLE,u,p,u,C.POINTER(u)],b)
        bind(self.a,'ConvertSidToStringSidW',[p,C.POINTER(W.LPWSTR)],b)
        bind(self.a,'ConvertStringSidToSidW',[s,C.POINTER(p)],b)
        bind(self.a,'LookupAccountSidW',[s,p,W.LPWSTR,C.POINTER(u),W.LPWSTR,C.POINTER(u),C.POINTER(u)],b)
        bind(self.a,'ConvertStringSecurityDescriptorToSecurityDescriptorW',[s,u,C.POINTER(p),C.POINTER(u)],b)
        bind(self.a,'GetSecurityInfo',[W.HANDLE,u,u,C.POINTER(p),C.POINTER(p),C.POINTER(p),C.POINTER(p),C.POINTER(p)],u)
        bind(self.a,'GetSecurityDescriptorControl',[p,C.POINTER(W.WORD),C.POINTER(u)],b)
        bind(self.a,'GetSecurityDescriptorDacl',[p,C.POINTER(b),C.POINTER(p),C.POINTER(b)],b)
        bind(self.a,'SetSecurityInfo',[W.HANDLE,u,u,p,p,p,p],u)
        bind(self.a,'GetAce',[p,u,C.POINTER(p)],b)
        bind(self.k,'CreateFileW',[s,u,u,C.POINTER(SecurityAttributes),u,u,W.HANDLE],W.HANDLE)
        bind(self.k,'CreateDirectoryW',[s,C.POINTER(SecurityAttributes)],b)
        bind(self.k,'GetFileInformationByHandleEx',[W.HANDLE,C.c_int,p,u],b)
        bind(self.k,'ReadFile',[W.HANDLE,p,u,C.POINTER(u),p],b)
        bind(self.k,'WriteFile',[W.HANDLE,p,u,C.POINTER(u),p],b)
        bind(self.k,'FlushFileBuffers',[W.HANDLE],b)
        bind(self.k,'MoveFileExW',[s,s,u],b)
        bind(self.k,'CreateMutexExW',[C.POINTER(SecurityAttributes),s,u,u],W.HANDLE)
        bind(self.k,'OpenMutexW',[u,b,s],W.HANDLE)
        bind(self.shell,'SHGetFolderPathW',[W.HWND,C.c_int,W.HANDLE,u,W.LPWSTR],C.c_long)

    def checked(self,result):
        if not result:
            raise C.WinError(C.get_last_error())
        return result

    def sid_string(self,sid):
        value=W.LPWSTR()
        self.checked(self.a.ConvertSidToStringSidW(sid,C.byref(value)))
        try:
            return value.value
        finally:
            self.k.LocalFree(C.cast(value,C.c_void_p))

    @contextmanager
    def process_token(self, process):
        token=W.HANDLE()
        self.checked(self.a.OpenProcessToken(process,8,C.byref(token)))
        try:
            yield token
        finally:
            self.k.CloseHandle(token)

    def token_sid(self, token):
        size=W.DWORD()
        self.a.GetTokenInformation(token,1,None,0,C.byref(size))
        buffer=C.create_string_buffer(size.value)
        self.checked(self.a.GetTokenInformation(token,1,buffer,size,C.byref(size)))
        return self.sid_string(C.cast(buffer,C.POINTER(C.c_void_p)).contents.value)

    def context(self):
        with self.process_token(self.k.GetCurrentProcess()) as token:
            size=W.DWORD()
            elevated=W.DWORD()
            self.checked(self.a.GetTokenInformation(token,20,C.byref(elevated),C.sizeof(elevated),C.byref(size)))
            session=W.DWORD()
            self.checked(self.k.ProcessIdToSessionId(self.k.GetCurrentProcessId(),C.byref(session)))
            return {'sid':self.token_sid(token),'elevated':bool(elevated.value),'session':session.value}

    def interactive_user_sid(self, session):
        window = self.checked(self.user.GetShellWindow())
        pid, shell_session = W.DWORD(), W.DWORD()
        self.checked(self.user.GetWindowThreadProcessId(window,C.byref(pid)))
        process = self.checked(self.k.OpenProcess(0x1000,False,pid))
        try:
            self.checked(self.k.ProcessIdToSessionId(pid,C.byref(shell_session)))
            if not session or shell_session.value != session:
                raise PermissionError('Abra a instalação na área de trabalho da conta do cliente')
            with self.process_token(process) as token:
                return self.token_sid(token)
        finally:
            self.k.CloseHandle(process)

    def root(self):
        buffer=C.create_unicode_buffer(260)
        if self.shell.SHGetFolderPathW(None,0x23,None,0,buffer) != 0:
            raise OSError('Diretório CommonApplicationData indisponível')
        return Path(buffer.value)/'PrefeituraRemoteManaged'

    def require_user_sid(self,sid):
        pointer=C.c_void_p()
        self.checked(self.a.ConvertStringSidToSidW(sid,C.byref(pointer)))
        try:
            n,d,kind=W.DWORD(),W.DWORD(),W.DWORD()
            self.a.LookupAccountSidW(None,pointer,None,C.byref(n),None,C.byref(d),C.byref(kind))
            name,domain=C.create_unicode_buffer(max(1,n.value)),C.create_unicode_buffer(max(1,d.value))
            self.checked(self.a.LookupAccountSidW(None,pointer,name,C.byref(n),domain,C.byref(d),C.byref(kind)))
            if kind.value != 1:
                raise PermissionError('O SID provisionado deve identificar um usuário, não um grupo')
        finally:
            self.k.LocalFree(pointer)

    @contextmanager
    def attributes(self,sddl):
        pointer=C.c_void_p()
        self.checked(self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,C.byref(pointer),None))
        try:
            yield SecurityAttributes(C.sizeof(SecurityAttributes),pointer,False)
        finally:
            self.k.LocalFree(pointer)

    @contextmanager
    def opened(self,path,directory=False):
        # Open the reparse point itself, not its target; deny concurrent write/delete.
        flags=0x00200000 | (0x02000000 if directory else 0x80)
        handle=self.k.CreateFileW(str(path),0x80000000,1,None,3,flags,None)
        if handle == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        try:
            yield handle
        finally:
            self.k.CloseHandle(handle)

    def summary(self,handle):
        owner,dacl,descriptor=C.c_void_p(),C.c_void_p(),C.c_void_p()
        code=self.a.GetSecurityInfo(handle,1,5,C.byref(owner),None,C.byref(dacl),None,C.byref(descriptor))
        if code:
            raise C.WinError(code)
        try:
            control,revision=W.WORD(),W.DWORD()
            self.checked(self.a.GetSecurityDescriptorControl(descriptor,C.byref(control),C.byref(revision)))
            attrs=(W.DWORD*2)()
            self.checked(self.k.GetFileInformationByHandleEx(handle,9,attrs,C.sizeof(attrs)))
            if not dacl.value:
                raise PermissionError('DACL ausente')
            count=C.cast(dacl,C.POINTER(AclHeader)).contents.count
            aces=[]
            for i in range(count):
                ace=C.c_void_p()
                self.checked(self.a.GetAce(dacl,i,C.byref(ace)))
                head=(W.BYTE*4).from_address(ace.value)
                if head[0] != 0:
                    raise PermissionError('Tipo de ACE inesperado')
                mask=W.DWORD.from_address(ace.value+4).value
                aces.append((self.sid_string(ace.value+8),mask,head[1],head[0]))
            return {'owner':self.sid_string(owner),'protected':bool(control.value&0x1000),
                    'reparse':bool(attrs[0]&0x400),'aces':aces}
        finally:
            self.k.LocalFree(descriptor)

    def check_directory(self,path):
        from agent.managed import validate_acl
        with self.opened(path,directory=True) as handle:
            return validate_acl(self.summary(handle),directory=True)

    def read(self,path,source=False,expected_sid=None):
        from agent.managed import validate_acl
        with self.opened(path) as handle:
            sid=validate_acl(self.summary(handle),source=source,caller_sid=self.context()['sid'] if source else None)
            if expected_sid is not None and sid != expected_sid:
                raise PermissionError('ACL do arquivo diverge do diretório protegido')
            buffer=C.create_string_buffer(65537)
            count=W.DWORD()
            self.checked(self.k.ReadFile(handle,buffer,len(buffer),C.byref(count),None))
            if count.value>65536:
                raise ValueError('Perfil excede o limite')
            return buffer.raw[:count.value]

    def create_directory(self,path,sid):
        from agent.managed import profile_sddl
        with self.attributes(profile_sddl(sid,directory=True)) as attrs:
            self.checked(self.k.CreateDirectoryW(str(path),C.byref(attrs)))
        if self.check_directory(path) != sid:
            raise PermissionError('ACL do diretório não confirmada')

    def write_profile(self,path,data,sid,replace=False,source=False):
        from agent.managed import profile_sddl
        temporary=path.with_name('profile-'+uuid.uuid4().hex+'.tmp')
        created=False
        try:
            sddl='O:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)' if source else profile_sddl(sid)
            with self.attributes(sddl) as attrs:
                handle=self.k.CreateFileW(str(temporary),0xc0000000,0,C.byref(attrs),1,0x80,None)
            if handle == C.c_void_p(-1).value:
                raise C.WinError(C.get_last_error())
            created=True
            try:
                from agent.managed import validate_acl
                actual=validate_acl(self.summary(handle),source=source,caller_sid=self.context()['sid'] if source else None)
                if not source and actual != sid:
                    raise PermissionError('ACL do temporário não confirmada antes da escrita')
                count=W.DWORD()
                buffer=C.create_string_buffer(data)
                self.checked(self.k.WriteFile(handle,buffer,len(data),C.byref(count),None))
                if count.value != len(data):
                    raise OSError('Gravação incompleta do perfil')
                self.checked(self.k.FlushFileBuffers(handle))
            finally:
                self.k.CloseHandle(handle)
            self.checked(self.k.MoveFileExW(str(temporary),str(path),8 | (1 if replace else 0)))
            created=False
        finally:
            if created: temporary.unlink(missing_ok=True)

    def prepare_source_directory(self,path):
        from agent.managed import validate_acl
        if not path.exists():
            with self.attributes('O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)') as attrs:
                self.checked(self.k.CreateDirectoryW(str(path),C.byref(attrs)))
        with self.opened(path,directory=True) as handle:
            validate_acl(self.summary(handle),directory=True,source=True,caller_sid=self.context()['sid'])

    def running(self):
        from agent.managed import MUTEX
        handle=self.k.OpenMutexW(0x100000,False,MUTEX)
        if handle:
            self.k.CloseHandle(handle)
            return True
        if C.get_last_error() == 2:
            return False
        raise PermissionError('Não foi possível verificar agente em execução')

    def retire_profile(self,path,sid):
        from agent.managed import validate_acl
        handle=self.k.CreateFileW(str(path),0x80040000,0,None,3,0x00200080,None)
        if handle == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        try:
            if validate_acl(self.summary(handle)) != sid:
                raise PermissionError('ACL do perfil não confirmada')
            with self.attributes('O:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)') as attrs:
                present,defaulted=W.BOOL(),W.BOOL()
                dacl=C.c_void_p()
                self.checked(self.a.GetSecurityDescriptorDacl(attrs.descriptor,C.byref(present),C.byref(dacl),C.byref(defaulted)))
                code=self.a.SetSecurityInfo(handle,1,0x80000004,None,None,dacl,None)
                if code:
                    raise C.WinError(code)
            validate_acl(self.summary(handle),source=True,caller_sid=self.context()['sid'])
        finally:
            self.k.CloseHandle(handle)
        archive=path.with_name('retired-profile-'+uuid.uuid4().hex+'.json')
        self.checked(self.k.MoveFileExW(str(path),str(archive),8))

    @contextmanager
    def mutex(self,sid):
        from agent.managed import MUTEX
        sddl=f'O:{sid}D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x00100001;;;{sid})'
        with self.attributes(sddl) as attrs:
            handle=self.checked(self.k.CreateMutexExW(C.byref(attrs),MUTEX,0,0x100001))
            exists=C.get_last_error()==183
        try:
            if exists:
                raise RuntimeError('Agente gerenciado já está em execução')
            yield
        finally:
            self.k.CloseHandle(handle)
