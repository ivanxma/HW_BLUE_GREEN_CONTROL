#!/usr/bin/env python3
"""Verify live-mode activation with GET-only OCI traffic and isolated test auth."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parent/'tests'))
from app import ROOT,create_app
from modules.oci_service import OCIService
from modules.store import Store
from modules.observer import Observer
from sqlite_store import Store as TestStore
from fakes import FakeAuth

settings={}
for line in (ROOT/'.runtime.env').read_text().splitlines():
    if line.strip() and not line.lstrip().startswith('#'):
        words=shlex.split(line)
        if words and '=' in words[0]:key,_,value=words[0].partition('=');settings[key]=value
checks=[]
def check(name,value):
    assert value,name
    checks.append({'name':name,'status':'PASS'})
check('Live OCI execution enabled in protected runtime settings',settings.get('BLUEGREEN_OCI_READ_ONLY')=='0')
pid=subprocess.check_output(['systemctl','show','heatwave-bluegreen-https.service','--property=MainPID','--value'],text=True).strip()
try:process_env=Path('/proc/'+pid+'/environ').read_bytes()
except PermissionError:process_env=subprocess.check_output(['sudo','cat','/proc/'+pid+'/environ'])
process_settings=dict(part.split(b'=',1) for part in process_env.split(b'\0') if b'=' in part)
check('Running web service inherited execution-enabled mode',process_settings.get(b'BLUEGREEN_OCI_READ_ONLY')==b'0')
service=OCIService();store=Store(ROOT/'.data/store.json');calls=[]
for client in [service.bg,service.db,service.ch,service.rep,service.wr,service.vault,service.secrets,service.kms]:
    original=client.base_client.session.request
    def get_only(method,url,_request=original,**kwargs):
        assert method=='GET','Activation verification must not submit an OCI mutation'
        calls.append({'method':method,'url':url.split('?')[0]})
        return _request(method,url,**kwargs)
    client.base_client.session.request=get_only
with tempfile.TemporaryDirectory(prefix='bg-live-check-') as directory:
    local=TestStore(Path(directory)/'test.sqlite')
    app=create_app({'TESTING':True,'DATA_DIR':directory,'OCI_READ_ONLY':False},service,Observer(service,store),local,FakeAuth(local))
    client=app.test_client();client.get('/login')
    with client.session_transaction() as session:csrf=session['login_csrf']
    client.post('/login',data={'csrf':csrf,'username':'admin','password':'test-private-password'})
    page=client.get('/').get_data(as_text=True)
    check('Dashboard identifies live operation mode','Live OCI operations enabled' in page and 'READ-ONLY OCI VALIDATION' not in page)
    check('Operation feedback is in the top banner before page content',page.index('id="message"')<page.index('<main>'))
    check('Dashboard provides failure notification and activity controls',all(s in page for s in ['id="notifications"','id="notification-dot"','id="message-history"']))
    response=client.get('/api/dashboard');check('Live selected-compartment discovery works',response.status_code==200)
    data=response.json
    check('Discovery remains confined to the selected compartment',data['discovery_scope']['outside_compartment_discovery'] is False)
    current=next(s for s in data['systems'] if s['lifecycle_state']=='ACTIVE')
    with client.session_transaction() as session:csrf=app.extensions['bluegreen']['sessions'][session['sid']]['csrf']
    response=client.post('/api/operation-plan',json={'path':'/api/db/'+current['id']+'/action','params':{'action':'ha_off'}},headers={'X-CSRF-Token':csrf})
    check('Live operation preview is available',response.status_code==200 and response.json['execution_allowed'])
    check('Preview lists OCI changes without submitting them',response.json['will_mutate_oci'] and bool(response.json['changes']))
    check('Default DB-system updates retain ETag protection',response.json['db_update_mode']=='ETAG')
    response=client.post('/api/operation-plan',json={'path':'/api/db/'+current['id']+'/action','params':{'action':'ha_off','db_update_mode':'STANDARD'}},headers={'X-CSRF-Token':csrf})
    check('Explicit standard OCI mode is available in the live preview',response.status_code==200 and response.json['execution_allowed'] and response.json['db_update_mode']=='STANDARD')
    check('Standard mode preview discloses the missing atomic ETag check',any('no atomic ETag version check' in c for c in response.json['checks']))
    check('Preview created no workflow or mutation job',not local.history())
check('Verification used GET-only OCI requests',all(c['method']=='GET' for c in calls))
report={'checks':checks,'mode':'LIVE','compartment_id':service.default_compartment,'region':service.region,
        'web_main_pid':int(pid),'admin_first_login_required':store.must_change_password('admin'),
        'live_inventory_counts':{k:len(data[k]) for k in ['systems','channels','read_replicas','deployments','work_requests']},
        'operation_preview':response.json,'oci_requests':calls,
        'scope_note':'Running-service environment verified. Dashboard and preview checks use production routes with isolated test authentication; public operator credentials remain unchanged. No live mutation is submitted.'}
Path(sys.argv[1]).write_text(json.dumps(report,indent=2))
print(json.dumps({'mode':report['mode'],'checks':len(checks),'compartment_id':report['compartment_id'],'oci_requests':len(calls)},indent=2))
