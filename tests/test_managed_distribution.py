from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from agent.agent import run
from agent.managed import (ADMIN,SYSTEM,FULL,READ,READ_EXECUTE,SCHEMA,ManagedContextDenied,
                           validate_acl,validate_profile,profile_sddl,runtime_profile,provision,deactivate)
from agent.managed import write_provision_source
from agent.windows_security import WindowsSecurity,SecurityAttributes

SID='S-1-5-21-1-2-3-1001'
OTHER='S-1-5-21-1-2-3-1002'


def profile(**changes):
    return dict(dict(schema=SCHEMA,authorized_sid=SID,agent_id='11111111-2222-4333-8444-555555555555',
                     server_url='https://synthetic.invalid',credential='synthetic_'+'x'*40,interval_seconds=300),**changes)


def acl(directory=False):
    flags=3 if directory else 0
    return dict(owner=ADMIN,protected=True,reparse=False,
                aces=[(SYSTEM,FULL,flags,0),(ADMIN,FULL,flags,0),(SID,READ_EXECUTE if directory else READ,flags,0)])


class FakeSecurity:
    """Filesystem content is synthetic; ACL/token effects are deliberately simulated."""
    def __init__(self,base,elevated=False,sid=SID,session=1):
        self.base=Path(base)/'managed'
        self.identity=dict(sid=sid,elevated=elevated,session=session)
        self.calls=[]
        self.busy=False

    def root(self): return self.base
    def context(self): return self.identity
    def running(self): return self.busy
    def require_user_sid(self,sid): self.calls.append('resolve_user')
    def check_directory(self,path): self.calls.append('check_directory'); return SID
    def create_directory(self,path,sid): self.calls.append('secure_directory'); path.mkdir()
    def read(self,path,**kwargs): self.calls.append(('read',kwargs)); return path.read_bytes()
    def write_profile(self,path,data,sid,replace=False,source=False): self.calls.append('secure_source_write' if source else 'secure_write'); path.write_bytes(data)
    def prepare_source_directory(self,path): self.calls.append('secure_source_directory'); path.mkdir(exist_ok=True)
    def retire_profile(self,path,sid): self.calls.append('retire_admin_only'); path.rename(path.with_name('retired.json'))
    @contextmanager
    def mutex(self,sid):
        if self.busy: raise RuntimeError('already running')
        self.calls.append('mutex')
        yield


class ManagedPolicyTests(unittest.TestCase):
    def test_admin_source_is_prepared_before_secret_and_duplicate_keys_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            sec=FakeSecurity(directory,elevated=True)
            target=Path(directory)/'source'/'profile.json'
            write_provision_source(target,json.dumps(profile()).encode(),sec)
            self.assertLess(sec.calls.index('secure_source_directory'),sec.calls.index('secure_source_write'))
            duplicate=json.dumps(profile()).replace('"schema":', '"schema":"duplicate","schema":',1).encode()
            with self.assertRaises(ValueError): write_provision_source(target,duplicate,sec)

    def test_managed_once_never_enrolls_or_loads_legacy_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            sec=FakeSecurity(directory); sec.base.mkdir()
            (sec.base/'config.json').write_text(json.dumps(profile()))
            original=runtime_profile
            with patch('agent.managed.runtime_profile',side_effect=lambda:original(sec)), \
                 patch('agent.agent.load_config',side_effect=AssertionError('legacy config accessed')), \
                 patch('agent.agent.get_agent_id',side_effect=AssertionError('identity written')), \
                 patch('agent.agent.enroll_agent',side_effect=AssertionError('enrollment attempted')), \
                 patch('agent.agent.save_individual_credential',side_effect=AssertionError('credential written')), \
                 patch('agent.agent.collect_inventory',return_value={'system':{'hostname':'SYNTHETIC'}}), \
                 patch('agent.agent.send_inventory') as send,patch('agent.agent.ClientApp'):
                self.assertEqual(run(once=True,managed=True),0)
                self.assertEqual(send.call_args.args[:2],('https://synthetic.invalid',profile()['credential']))

    def test_profile_rejects_ambiguous_identity_and_secret_in_url(self):
        for changes in ({'enrollment_token':'forbidden'},{'authorized_sid':ADMIN},{'credential':'bad\nvalue'},
                        {'server_url':'https://user:secret@synthetic.invalid'},
                        {'server_url':'https://synthetic.invalid/?token=secret'},
                        {'server_url':'http://public.invalid'},{'interval_seconds':True}):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                validate_profile(profile(**changes))
        self.assertEqual(validate_profile(profile())['authorized_sid'],SID)

    def test_acl_rejects_other_users_write_inheritance_and_reparse(self):
        self.assertEqual(validate_acl(acl()),SID)
        self.assertEqual(validate_acl(acl(True),directory=True),SID)
        for changed in [dict(protected=False),dict(reparse=True),dict(owner=SID),
                        dict(aces=acl()['aces']+[(OTHER,READ,0,0)]),
                        dict(aces=[(SYSTEM,FULL,0,0),(ADMIN,FULL,0,0),(SID,FULL,0,0)]),
                        dict(aces=[(SYSTEM,FULL,0,0),(ADMIN,FULL,0,0),(SID,READ,0x10,0)])]:
            bad=acl(); bad.update(changed)
            with self.subTest(changed=changed), self.assertRaises(PermissionError): validate_acl(bad)
        self.assertNotIn('WD',profile_sddl(SID))

    def test_source_acl_does_not_allow_target_user_to_read_bootstrap(self):
        with self.assertRaises(PermissionError): validate_acl(acl(),source=True,caller_sid=OTHER)
        source=acl(); source['aces']=source['aces'][:2]
        self.assertIsNone(validate_acl(source,source=True,caller_sid=OTHER))

    def test_runtime_ignores_environment_and_does_not_write_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            sec=FakeSecurity(directory)
            sec.base.mkdir(); (sec.base/'config.json').write_text(json.dumps(profile()))
            with patch.dict(os.environ,{'PREFEITURA_AGENT_CREDENTIAL':'untrusted','PREFEITURA_SERVER_URL':'http://untrusted.invalid'}):
                with runtime_profile(sec) as config:
                    self.assertEqual(config.token,profile()['credential'])
                    self.assertEqual(config.server_url,'https://synthetic.invalid')
                    self.assertEqual(config.provisioned_agent_id,profile()['agent_id'])
            self.assertEqual(sorted(p.name for p in sec.base.iterdir()),['config.json'])

    def test_wrong_user_elevation_and_session_zero_never_start_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            sec=FakeSecurity(directory); sec.base.mkdir()
            (sec.base/'config.json').write_text(json.dumps(profile()))
            for changes in ({'sid':OTHER},{'elevated':True},{'session':0}):
                sec.identity=dict(sid=SID,elevated=False,session=1); sec.identity.update(changes)
                with self.assertRaises(ManagedContextDenied):
                    with runtime_profile(sec): self.fail('must not start')
        with patch('agent.managed.runtime_profile',side_effect=ManagedContextDenied()), \
             patch('agent.agent.collect_inventory') as collect,patch('agent.agent.ClientApp') as ui, \
             patch('agent.agent.enroll_agent') as enroll:
            self.assertEqual(run(managed=True),0)
            collect.assert_not_called(); ui.assert_not_called(); enroll.assert_not_called()

    def test_provision_upgrade_and_rotation_preserve_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            sec=FakeSecurity(directory,elevated=True)
            source=Path(directory)/'source.json'; source.write_text(json.dumps(profile()))
            provision(source,security=sec)
            old=(sec.base/'config.json').read_bytes()
            self.assertLess(sec.calls.index('secure_directory'),sec.calls.index('secure_write'))
            with self.assertRaises(FileExistsError): provision(source,security=sec)
            self.assertEqual((sec.base/'config.json').read_bytes(),old)
            source.write_text(json.dumps(profile(agent_id='22222222-2222-4333-8444-555555555555')))
            with self.assertRaises(ValueError): provision(source,replace=True,security=sec)
            self.assertEqual((sec.base/'config.json').read_bytes(),old)
            source.write_text(json.dumps(profile(credential='replacement_'+'y'*40)))
            provision(source,replace=True,security=sec)
            self.assertEqual(json.loads((sec.base/'config.json').read_bytes())['agent_id'],profile()['agent_id'])

    def test_provision_refuses_running_agent_and_non_admin(self):
        with tempfile.TemporaryDirectory() as directory:
            sec=FakeSecurity(directory)
            with self.assertRaises(PermissionError): provision('unused',security=sec)
            sec.identity['elevated']=True; sec.busy=True
            with self.assertRaises(RuntimeError): provision('unused',security=sec)
            self.assertFalse(sec.base.exists())

    def test_deactivation_archives_without_deleting_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            sec=FakeSecurity(directory,elevated=True); sec.base.mkdir()
            original=json.dumps(profile()).encode(); (sec.base/'config.json').write_bytes(original)
            deactivate(sec)
            self.assertFalse((sec.base/'config.json').exists())
            self.assertEqual((sec.base/'retired.json').read_bytes(),original)
            self.assertIn('retire_admin_only',sec.calls)


class WindowsBoundaryTests(unittest.TestCase):
    def test_mismatched_file_reader_is_rejected_before_content_read(self):
        sec=object.__new__(WindowsSecurity); sec.k=MagicMock()
        sec.summary=MagicMock(return_value=acl())
        @contextmanager
        def opened(*args,**kwargs): yield 42
        sec.opened=opened
        with self.assertRaises(PermissionError): sec.read('synthetic',expected_sid=OTHER)
        sec.k.ReadFile.assert_not_called()

    @unittest.skipUnless(os.name=='nt','Native boundary only available on Windows')
    def test_native_token_query_is_read_only(self):
        context=WindowsSecurity().context()
        self.assertTrue(context['sid'].startswith('S-1-'))
        self.assertIsInstance(context['elevated'],bool)
        self.assertIsInstance(context['session'],int)

    @unittest.skipUnless(os.name=='nt','Native boundary only available on Windows')
    def test_native_common_data_path_ignores_programdata_environment(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'PROGRAMDATA':directory}):
            self.assertNotEqual(WindowsSecurity().root(),Path(directory)/'PrefeituraRemoteManaged')

    def test_bad_temporary_acl_aborts_before_writing_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            sec=object.__new__(WindowsSecurity)
            sec.k=MagicMock(); sec.k.CreateFileW.return_value=42
            sec.summary=MagicMock(return_value=dict(acl(),protected=False))
            @contextmanager
            def attributes(sddl): yield SecurityAttributes()
            sec.attributes=attributes
            with self.assertRaises(PermissionError):
                sec.write_profile(Path(directory)/'profile.json',b'must-not-be-written',SID)
            sec.k.WriteFile.assert_not_called()
            sec.k.CloseHandle.assert_called_once_with(42)

    @unittest.skipUnless(os.name=='nt','Native boundary only available on Windows')
    def test_native_read_rejects_unprotected_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'synthetic.json'; path.write_bytes(b'no credential')
            sec=WindowsSecurity()
            with self.assertRaises(PermissionError): sec.read(path)
