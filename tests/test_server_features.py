import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from server.config import Settings
from server.main import create_app

class SupportFeaturesTests(unittest.TestCase):
    def test_support_text_rejects_nul_without_persisting_and_recovers(self):
        cid = self.seed()
        self.assertEqual(self.client.patch(f'/api/v1/admin/computers/{cid}/name',
            params={'name':'bad\x00name'},headers=self.d).status_code,400)
        for field in ('username','display_name'):
            payload = {'username':'synthetic','display_name':'Synthetic','password':'synthetic-password',field:'bad\x00text'}
            self.assertEqual(self.client.post('/api/v1/admin/technicians',json=payload,headers=self.d).status_code,422)
        self.assertEqual(self.client.post(f'/api/v1/admin/computers/{cid}/messages',
            params={'message':'bad\x00message'},headers=self.d).status_code,400)
        for field in ('title','description'):
            payload = {'title':'Synthetic','description':'Synthetic',field:'bad\x00text'}
            self.assertEqual(self.client.post('/api/v1/admin/tickets',params=payload,headers=self.d).status_code,400)
        self.assertEqual(self.client.get(f'/api/v1/admin/computers/{cid}/messages',headers=self.d).json(),[])
        self.assertEqual(self.client.get('/api/v1/admin/tickets',headers=self.d).json(),[])
        self.assertEqual(self.client.post('/api/v1/admin/tickets',
            params={'title':'Valid','description':'Valid'},headers=self.d).status_code,200)

    def test_non_ascii_bearer_is_rejected_without_server_error(self):
        with TestClient(self.app, raise_server_exceptions=False) as client:
            response = client.get('/api/v1/admin/tickets',headers=[(b'Authorization', b'Bearer \xff')])
        self.assertEqual(response.status_code,401)

    def test_concurrent_account_creation_returns_conflicts_without_server_errors(self):
        payload = {'username':'same-tech','display_name':'Test','password':'only-test-password'}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _:self.client.post('/api/v1/admin/technicians',json=payload,headers=self.d), range(4)))
        self.assertEqual(sorted(response.status_code for response in results), [200,409,409,409])

    def test_support_endpoints_validate_references_and_bound_results(self):
        self.assertEqual(self.client.post('/api/v1/admin/tickets',params={'title':'Test','description':'Test','computer_id':99999},headers=self.d).status_code,404)
        self.assertEqual(self.client.get('/api/v1/admin/computers/99999/messages',headers=self.d).status_code,404)
        self.assertEqual(self.client.post('/api/v1/admin/tickets',params={'title':'x'*201,'description':'Test'},headers=self.d).status_code,400)
        cid = self.seed()
        for index in range(3):
            self.assertEqual(self.client.post(f'/api/v1/admin/computers/{cid}/messages',params={'message':str(index)},headers=self.d).status_code,200)
            self.assertEqual(self.client.post('/api/v1/admin/tickets',params={'title':str(index),'description':'Test'},headers=self.d).status_code,200)
        messages = self.client.get(f'/api/v1/admin/computers/{cid}/messages',params={'limit':2},headers=self.d).json()
        self.assertEqual([row['message'] for row in messages], ['1','2'])
        previous = self.client.get(f'/api/v1/admin/computers/{cid}/messages',params={'limit':2,'before_id':messages[0]['id']},headers=self.d).json()
        self.assertEqual([row['message'] for row in previous], ['0'])
        self.assertEqual(len(self.client.get('/api/v1/admin/tickets',params={'limit':2},headers=self.d).json()),2)
        self.assertEqual(len(self.client.get('/api/v1/admin/tickets',params={'limit':2,'offset':2},headers=self.d).json()),1)
        self.assertEqual(self.client.get('/api/v1/admin/tickets?limit=501',headers=self.d).status_code,422)
        self.assertEqual(self.client.get('/api/v1/admin/tickets').status_code,401)

    def test_logout_revokes_current_session_and_preserves_other_logins(self):
        payload = {'username':'logout-tech','display_name':'Test','password':'only-test-password'}
        self.assertEqual(self.client.post('/api/v1/admin/technicians',json=payload,headers=self.d).status_code,200)
        credentials = {'username':payload['username'],'password':payload['password']}
        first = self.client.post('/api/v1/auth/login',json=credentials).json()['token']
        second = self.client.post('/api/v1/auth/login',json=credentials).json()['token']
        first_headers = {'Authorization':f'Bearer {first}'}
        second_headers = {'Authorization':f'Bearer {second}'}
        self.assertEqual(self.client.post('/api/v1/auth/logout',headers=first_headers).status_code,200)
        self.assertEqual(self.client.get('/api/v1/admin/computers',headers=first_headers).status_code,401)
        self.assertEqual(self.client.get('/api/v1/admin/computers',headers=second_headers).status_code,200)

    def test_password_query_login_is_rejected_and_json_failures_are_limited(self):
        self.assertEqual(self.client.post('/api/v1/auth/login?username=test&password=not-a-real-secret').status_code, 422)
        payload = {'username':'unknown','password':'not-a-real-secret'}
        for _ in range(self.app.state.settings.auth_failures_per_window):
            self.assertEqual(self.client.post('/api/v1/auth/login', json=payload).status_code, 401)
        self.assertEqual(self.client.post('/api/v1/auth/login', json=payload).status_code, 429)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); db=Path(self.tmp.name)/'features.db'
        self.app=create_app(Settings(database_url=f'sqlite:///{db}',agent_token='a',dashboard_token='d',allow_legacy_agent_token=True))
        self.client=TestClient(self.app); self.d={'Authorization':'Bearer d'}
    def tearDown(self): self.client.close(); self.app.state.engine.dispose(); self.tmp.cleanup()
    def seed(self):
        r=self.client.post('/api/v1/inventory',json={'agent_id':'11111111-1111-4111-8111-111111111111','system':{'hostname':'PC-01'}},headers={'Authorization':'Bearer a'})
        return r.json()['computer_id']
    def test_technician_account_lifecycle(self):
        r=self.client.post('/api/v1/admin/technicians',json={'username':'joao','display_name':'Joao TI','password':'senha-super-segura'},headers=self.d); self.assertEqual(r.status_code,200)
        self.assertEqual(self.client.get('/api/v1/admin/technicians',headers=self.d).json()[0]['username'],'joao')
        login=self.client.post('/api/v1/auth/login',json={'username':'joao','password':'senha-super-segura'}); self.assertEqual(login.status_code,200); self.assertTrue(login.json()['token']); self.assertEqual(login.json()['technician']['role'],'technician')
        technician_headers={'Authorization': 'Bearer ' + login.json()['token']}
        forbidden=self.client.post('/api/v1/admin/technicians',json={'username':'maria','display_name':'Maria TI','password':'outra-senha-segura'},headers=technician_headers); self.assertEqual(forbidden.status_code,403)
    def test_computer_alias_is_local_only(self):
        cid=self.seed(); r=self.client.patch(f'/api/v1/admin/computers/{cid}/name?name=Secretaria%20Financeiro',headers=self.d); self.assertEqual(r.status_code,200); self.assertEqual(r.json()['display_name'],'Secretaria Financeiro')
        detail=self.client.get(f'/api/v1/admin/computers/{cid}',headers=self.d).json()['computer']; self.assertEqual(detail['hostname'],'PC-01'); self.assertEqual(detail['display_name'],'Secretaria Financeiro')
    def test_ticket_and_chat_lifecycle(self):
        cid=self.seed(); r=self.client.post(f'/api/v1/admin/tickets?title=Sem%20internet&description=Cliente%20sem%20rede&computer_id={cid}&priority=high',headers=self.d); self.assertEqual(r.status_code,200); tid=r.json()['id']
        self.assertEqual(self.client.patch(f'/api/v1/admin/tickets/{tid}?status=in_progress',headers=self.d).json()['status'],'in_progress')
        msg=self.client.post(f'/api/v1/admin/computers/{cid}/messages?message=Vou%20verificar%20agora',headers=self.d); self.assertEqual(msg.status_code,200)
        messages=self.client.get(f'/api/v1/admin/computers/{cid}/messages',headers=self.d).json(); self.assertEqual(messages[-1]['message'],'Vou verificar agora'); self.assertEqual(messages[-1]['sender'],'technician')

if __name__=='__main__': unittest.main()
