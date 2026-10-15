"""Public HTTPS verification for the user-provided OL9 VM; OCI mutations stay blocked."""
import json
import os
import secrets
import shlex
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE=os.environ['BLUEGREEN_TEST_BASE_URL'].rstrip('/')
SSH_TARGET=os.environ['BLUEGREEN_TEST_SSH_TARGET']
SSH_KEY=Path(os.environ['BLUEGREEN_TEST_SSH_KEY']).expanduser()
KNOWN_HOSTS=Path(os.environ.get('BLUEGREEN_TEST_KNOWN_HOSTS',str(Path.home()/'.ssh/known_hosts'))).expanduser()
APP_DIR=os.environ.get('BLUEGREEN_TEST_APP_DIR','/home/opc/heatwave-bluegreen')
OUTPUT=Path(os.environ.get('BLUEGREEN_BROWSER_EVIDENCE',str(Path(__file__).resolve().parents[1]/'validation/ol9-remote')))
OUTPUT.mkdir(parents=True,exist_ok=True)
checks=[]
rotation={'password':None}


def check(name,value,detail=''):
    checks.append({'name':name,'status':'PASS' if value else 'FAIL','detail':detail})
    if not value:raise AssertionError(name+': '+detail)


def restore_fresh_account():
    if not rotation['password']:return
    code="""import sys,json,mysql.connector
from modules.store import Store
s=Store('.data/store.json');p=json.load(sys.stdin)['password']
c=mysql.connector.connect(unix_socket=s.connection_options['unix_socket'],user='admin',password=p)
x=c.cursor();x.execute("ALTER USER 'admin'@'localhost' IDENTIFIED BY %s",('ChangeMe123!',));x.close();c.close()
with s.db() as x:x.execute("UPDATE operators SET must_change_password=TRUE WHERE username='admin'")
print('Fresh admin account restored; first-login rotation required.')
"""
    subprocess.run(['ssh','-i',str(SSH_KEY),'-o','IdentitiesOnly=yes',
                    '-o','UserKnownHostsFile='+str(KNOWN_HOSTS),SSH_TARGET,
                    'cd '+shlex.quote(APP_DIR)+' && .venv/bin/python -c '+shlex.quote(code)],
                   input=json.dumps({'password':rotation['password']}),text=True,check=True,capture_output=True)
    rotation['password']=None


try:
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(ignore_https_errors=True,viewport={'width':1440,'height':1100})
        page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(BASE+'/login',wait_until='networkidle')
        check('External HTTPS login popup',page.locator('#auth-dialog').is_visible())
        page.screenshot(path=str(OUTPUT/'login.png'))
        check('Anonymous dashboard API blocked',context.request.get(BASE+'/api/dashboard').status==401)
        page.get_by_label('Admin username').fill('admin')
        page.get_by_label('Password',exact=False).fill('ChangeMe123!')
        page.get_by_role('button',name='Sign in',exact=True).click()
        page.get_by_role('heading',name='Change your password',exact=True).wait_for()
        check('Initial admin login requires password change',page.url.endswith('/change-password'))
        check('First-login API bypass blocked',context.request.get(BASE+'/api/dashboard').status==403)
        page.screenshot(path=str(OUTPUT/'mandatory-password-change.png'))
        password=secrets.token_urlsafe(32);rotation['password']=password
        page.get_by_label('New password *',exact=True).fill(password)
        page.get_by_label('Confirm new password *',exact=True).fill(password)
        page.get_by_role('button',name='Change password',exact=True).click()
        check('Password change logs user out','/login' in page.url)
        page.get_by_label('Password',exact=False).fill(password)
        page.get_by_role('button',name='Sign in',exact=True).click()
        page.get_by_role('heading',name='Blue Green Control Room',exact=True).wait_for()
        # Discovery is read-only and confined to the selected compartment.
        response=context.request.get(BASE+'/api/dashboard',timeout=120000)
        data=response.json();check('Live OCI dashboard discovery',response.status==200,str(data.get('error','')))
        check('Default scope is the VM compartment',data['compartment_id']==data['default_compartment_id'])
        checks.append({'name':'Live inventory counts','status':'PASS','detail':f"{len(data['systems'])} DB systems; {len(data['deployments'])} deployments; {len(data['channels'])} channels"})
        page.locator('#metrics .metric').first.wait_for(timeout=120000)
        check('Read-only OCI validation banner',page.get_by_text('READ-ONLY OCI VALIDATION',exact=False).is_visible())
        check('Real creation disabled during validation',page.locator('#new-deployment').is_disabled())
        csrf=page.locator('#app').get_attribute('data-csrf')
        response=context.request.post(BASE+'/api/create',headers={'X-CSRF-Token':csrf},data={})
        check('Server blocks real OCI mutation',response.status==400 and 'disabled' in response.json().get('error',''))
        cookies=context.cookies()
        cookie=next(c for c in cookies if c['name']=='heatwave_bluegreen_session')
        check('Secure HttpOnly Strict session cookie',cookie['secure'] and cookie['httpOnly'] and cookie['sameSite']=='Strict')
        check('Password absent from rendered dashboard',password not in page.content())
        page.screenshot(path=str(OUTPUT/'dashboard.png'),full_page=True)
        system_ids={s['id'] for s in data['systems']}
        selected=next((d for d in data['deployments'] if d.get('lifecycle_state')=='ACTIVE' and d.get('source_db_system_id') in system_ids and d.get('target_db_system_id') in system_ids),None)
        if selected:
            page.locator('[data-select="'+selected['id']+'"]').click()
            page.locator('#panels .panel').first.wait_for(timeout=30000)
            check('Blue and Green observation panels',page.locator('#panels .panel').count()==2)
            page.screenshot(path=str(OUTPUT/'blue-green-panels.png'),full_page=True)
        else:checks.append({'name':'Live Blue/Green observation panels','status':'NOT_RUN','detail':'No deployment with both members in the current DB-system inventory'})
        page.locator('[data-tab="db-systems"]').click()
        page.get_by_role('button',name='High availability',exact=False).click()
        check('Separate HA inventory tab',page.locator('[data-fleet="ha"]').get_attribute('aria-selected')=='true')
        page.set_viewport_size({'width':390,'height':844})
        page.screenshot(path=str(OUTPUT/'mobile.png'),full_page=True)
        check('Mobile layout avoids page overflow',page.evaluate('document.documentElement.scrollWidth <= innerWidth'),json.dumps(page.evaluate('[...document.querySelectorAll("body *")].filter(e=>e.getBoundingClientRect().right>innerWidth && e.tagName!=="TD" && e.tagName!=="TH").map(e=>({tag:e.tagName,cls:e.className,right:e.getBoundingClientRect().right})).slice(0,15)')))
        page.screenshot(path=str(OUTPUT/'mobile.png'),full_page=True)
        check('No browser JavaScript errors',not errors,str(errors))
        page.locator('details.user summary').click();page.get_by_role('button',name='Sign out',exact=True).click()
        check('Logout blocks authenticated API',context.request.get(BASE+'/api/dashboard').status==401)
        browser.close()
except Exception as exc:
    checks.append({'name':'Browser validation completion','status':'FAIL','detail':type(exc).__name__+': '+str(exc)[:500]})
    raise
finally:
    restore_fresh_account()
    (OUTPUT/'browser.json').write_text(json.dumps({'base_url':BASE,'certificate_validation':'Self-signed certificate accepted for this test only',
                                                  'checks':checks,'admin_restored_to_first_login':rotation['password'] is None},indent=2))
