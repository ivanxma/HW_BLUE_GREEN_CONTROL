#!/usr/bin/env python3
"""Read-only live inventory/API evidence using isolated test authentication."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parent/'tests'))
from app import create_app,ROOT
from fakes import FakeAuth
from sqlite_store import Store as TestStore
from modules.store import Store
from modules.oci_service import OCIService
from modules.observer import Observer

service=OCIService();store=Store(ROOT/'.data/store.json')
credential_file=ROOT/'.data/store.json'
def operator_state():
    with store.db() as cursor:
        cursor.execute('SELECT username,must_change_password FROM operators ORDER BY username')
        return cursor.fetchall()
before=operator_state();credential_hash=hashlib.sha256(credential_file.read_bytes()).digest()
calls=[]
for client,name in [(service.db,'list_db_systems'),(service.bg,'list_blue_green_deployments'),
                    (service.ch,'list_channels'),(service.rep,'list_replicas'),(service.wr,'list_work_requests')]:
    original=getattr(client,name)
    def traced(compartment,*args,_call=original,_name=name,**kwargs):
        assert compartment==service.default_compartment,'Unexpected out-of-compartment discovery'
        calls.append({'operation':_name,'compartment_id':compartment})
        return _call(compartment,*args,**kwargs)
    setattr(client,name,traced)
def no_compartment_enumeration(*args,**kwargs):raise AssertionError('Inventory must not enumerate other compartments')
service.identity.list_compartments=no_compartment_enumeration
checks=[]
def check(name,condition):
    assert condition,name
    checks.append({'name':name,'status':'PASS'})
with tempfile.TemporaryDirectory(prefix='bg-api-check-') as directory:
    test_store=TestStore(Path(directory)/'isolated.sqlite')
    app=create_app({'TESTING':True,'OCI_READ_ONLY':True,'DATA_DIR':directory},service,Observer(service,store),test_store,FakeAuth(test_store))
    client=app.test_client();client.get('/login')
    with client.session_transaction() as session:csrf=session['login_csrf']
    client.post('/login',data={'csrf':csrf,'username':'admin','password':'test-private-password'})
    response=client.get('/api/dashboard');check('Live inventory route returns HTTP 200',response.status_code==200)
    data=response.json
    check('No out-of-compartment discovery',data['discovery_scope']['outside_compartment_discovery'] is False)
    check('Selected compartment registrations verified',data['registration_scope_verified'])
    check('No regional IAM warning',not data['registration_warning'])
    for key in ['systems','channels','read_replicas','deployments']:
        check('Deleted resources excluded: '+key,all(x['lifecycle_state']!='DELETED' for x in data[key]))
    check('Creation excludes systems with read replicas',all(not s['eligible'] for s in data['systems'] if s['read_replicas']))
    selected=next((s for s in data['systems'] if s['lifecycle_state']=='ACTIVE'),None)
    detail={}
    if selected:
        response=client.get('/api/db/'+selected['id']);check('Live selected DB detail route returns HTTP 200',response.status_code==200)
        detail=response.json
        check('Detail contains connection, HA, replicas, channels and work requests',all(k in detail for k in ['db_system','channels','read_replicas','work_requests']) and bool(detail['db_system']['ip_address']))
    with client.session_transaction() as session:csrf=app.extensions['bluegreen']['sessions'][session['sid']]['csrf']
    response=client.post('/api/create',json={},headers={'X-CSRF-Token':csrf})
    check('Live OCI mutations remain blocked',response.status_code==400 and 'disabled' in response.json['error'])
check('Deployed operator state preserved',operator_state()==before)
check('Deployed service credentials preserved',hashlib.sha256(credential_file.read_bytes()).digest()==credential_hash)
report={'scope':'Real OCI reads through production Flask routes with isolated test-only authentication and journal; public operator login is not bypassed or changed.',
        'checks':checks,'compartment_id':service.default_compartment,'region':service.region,
        'counts':{key:len(data[key]) for key in ['systems','deployments','channels','read_replicas','work_requests']},
        'list_requests':calls,'admin_first_login_required':store.must_change_password('admin'),
        'selected_detail':{'display_name':detail.get('db_system',{}).get('display_name'),
                           'telemetry_error':detail.get('telemetry_error'), 'replica_error':detail.get('replica_error')}}
Path(sys.argv[1]).write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
