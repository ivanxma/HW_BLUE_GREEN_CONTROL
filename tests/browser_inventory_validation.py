"""Browser regression against synthetic resources; never uses operator credentials."""
import json
from pathlib import Path
import sys
import tempfile
import threading
sys.path[:0]=[str(Path(__file__).resolve().parent),str(Path(__file__).resolve().parents[1])]
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server
from app import create_app
from fakes import FakeAuth
from sqlite_store import Store
from modules.demo import DemoService, DemoObserver

OUT=Path(__file__).resolve().parents[1]/'validation/inventory-update-20261014'
OUT.mkdir(parents=True,exist_ok=True)
class InventoryDemo(DemoService):
    def __init__(self):
        super().__init__()
        self.systems.append({**self.systems[2],'id':'deleted-db','display_name':'Deleted tombstone','lifecycle_state':'DELETED'})
        self.replica_list=[dict(id='demo-replica',display_name='Reporting read replica',db_system_id='demo-standalone',
                                lifecycle_state='ACTIVE',ip_address='10.0.1.31',port=3306,mysql_version='8.4.8',shape_name='MySQL.8')]
        self.channel_list.append({**self.channel_list[-1],'id':'deleted-channel','display_name':'Deleted channel tombstone','lifecycle_state':'DELETED'})
    def inventory(self,compartment):
        if compartment=='forbidden-comp':
            return {'systems':[],'deployments':[],'channels':[],'read_replicas':[],'work_requests':[],
                    'access_warning':'Access to the selected compartment is not granted.',
                    'registration_scope_verified':False,
                    'registration_diagnostics':[{'operation':'ListDbSystems','compartment_id':compartment,
                                                'compartment_name':'Restricted lab','error':'OCI 404 NotAuthorizedOrNotFound'}],
                    'discovery_scope':{'outside_compartment_discovery':False,'information':'Only this selected compartment is inspected.'}}
        return {**super().inventory(compartment),'read_replicas':self.replica_list}
    def compartments(self):return super().compartments()+[{'id':'forbidden-comp','name':'Restricted lab'}]
    def read_replicas(self,db_id,compartment):return [r for r in self.replica_list if r['db_system_id']==db_id]
    def channels(self,db_id,compartment):return [c for c in super().channels(db_id,compartment) if c['lifecycle_state']!='DELETED']

checks=[]
def check(name,condition):
    assert condition,name
    checks.append({'name':name,'status':'PASS'})

with tempfile.TemporaryDirectory(prefix='bg-browser-') as folder:
    service=InventoryDemo();store=Store(Path(folder)/'test.sqlite')
    app=create_app({'TESTING':True,'DATA_DIR':folder},service,DemoObserver(service),store,FakeAuth(store))
    server=make_server('127.0.0.1',0,app,threaded=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1540,'height':1100})
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}/login')
            page.get_by_label('Admin username').fill('admin');page.get_by_label('Password',exact=False).fill('test-private-password')
            page.get_by_role('button',name='Sign in',exact=True).click()
            page.locator('.system-choice').first.wait_for()
            check('All non-deleted states are the default',page.locator('#state-filter').input_value()=='')
            check('Deleted DB systems are invisible',page.get_by_text('Deleted tombstone',exact=True).count()==0)
            page.locator('[data-inspect="demo-standalone"]').click()
            page.locator('#system-detail h2').filter(has_text='Reporting').wait_for()
            check('Selected DB appears in right detail panel',page.locator('#system-detail h2').inner_text()=='Reporting · standalone')
            left=page.locator('.inventory-sidebar').bounding_box();right=page.locator('#system-detail').bounding_box()
            check('DB list on left and details on right',left['x']+left['width']<=right['x'])
            check('Read replica name and endpoint visible',page.get_by_text('Reporting read replica',exact=True).is_visible() and '10.0.1.31' in page.locator('#system-detail').inner_text())
            check('Read replicas disable blue-green creation',page.locator('#system-detail [data-create]').is_disabled() and 'Read replicas present' in page.locator('.creation-blockers').inner_text())
            check('Deleted replication channels are invisible','Deleted channel tombstone' not in page.locator('#system-detail').inner_text())
            for section in ['Connection & configuration','Read replicas (1)','Replication channels (1)','GTIDs & SQL replication','Work requests (0)']:
                check('Detail section: '+section,page.get_by_role('heading',name=section,exact=True).is_visible())
            page.screenshot(path=str(OUT/'inventory-read-replica.png'),full_page=True)
            page.locator('#replica-filter').select_option('present')
            check('Read replica presence filter',page.locator('.system-choice').count()==1)
            page.locator('#replica-filter').select_option('none')
            check('Read replica absence filter',page.locator('[data-inspect="demo-standalone"]').count()==0)
            page.locator('#replica-filter').select_option('')
            page.locator('#channel-filter').select_option('DISABLED')
            check('Channel state filter',page.locator('.system-choice').count()==2)
            page.locator('#channel-filter').select_option('')
            page.get_by_role('button',name='High availability',exact=False).click()
            check('HA list includes updating by default',page.locator('[data-inspect="demo-ha"]').count()==1)
            page.locator('#state-filter').select_option('ACTIVE')
            check('DB system state filter hides updating',page.locator('[data-inspect="demo-ha"]').count()==0)
            page.locator('#state-filter').select_option('')
            page.locator('[data-inspect="demo-blue"]').click()
            page.locator('#system-detail h2.blue').wait_for()
            check('Blue name has white text on blue',page.locator('#system-detail h2.blue').evaluate('e=>getComputedStyle(e).color')=='rgb(255, 255, 255)' and page.locator('#system-detail h2.blue').evaluate('e=>getComputedStyle(e).backgroundColor')=='rgb(53, 100, 155)')
            page.screenshot(path=str(OUT/'inventory-blue.png'),full_page=True)
            page.get_by_role('button',name='Standalone',exact=False).click()
            page.locator('[data-inspect="demo-green"]').click();page.locator('#system-detail h2.green').wait_for()
            check('Green name has white text on green',page.locator('#system-detail h2.green').evaluate('e=>getComputedStyle(e).color')=='rgb(255, 255, 255)' and page.locator('#system-detail h2.green').evaluate('e=>getComputedStyle(e).backgroundColor')=='rgb(40, 113, 68)')
            check('Normal ACTIVE status is green text',page.locator('#system-detail .panel-title .badge.green').is_visible())
            page.screenshot(path=str(OUT/'inventory-green.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            check('Mobile page has no horizontal overflow',page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
            page.screenshot(path=str(OUT/'inventory-mobile.png'),full_page=True)
            page.set_viewport_size({'width':1540,'height':1100})
            page.get_by_role('button',name='Change compartment',exact=True).click()
            page.locator('[name=compartment_id]').select_option('forbidden-comp')
            page.get_by_role('button',name='Use compartment',exact=True).click()
            page.locator('#compartment-id').filter(has_text='forbidden-comp').wait_for()
            check('Denied compartment shows warning rather than error',page.locator('#message.orange').is_visible() and page.locator('#message.red').count()==0)
            page.locator('#discovery-diagnostics summary').click()
            policy=page.locator('#discovery-details pre').first.inner_text()
            check('IAM policy targets exactly the newly selected compartment','in compartment id forbidden-comp' in policy and 'in tenancy' not in policy)
            check('Prior compartment inventory is cleared',page.locator('.system-choice').count()==0)
            page.screenshot(path=str(OUT/'inventory-iam-warning.png'),full_page=True)
            check('No browser JavaScript errors',not errors)
            browser.close()
    finally:server.shutdown()
(OUT/'browser-inventory.json').write_text(json.dumps({'scope':'Synthetic OCI inventory in production Flask routes and UI; test-only authentication and store','checks':checks},indent=2))
print(f'Browser inventory checks passed: {len(checks)}')
