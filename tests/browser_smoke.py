"""Optional visual check against the read-only demo listening on port 5088."""
from pathlib import Path
import os
import secrets
import json
import sys
import atexit
import mysql.connector
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from modules.local_auth import BOOTSTRAP_PASSWORD
from playwright.sync_api import sync_playwright

rotation={'password':None}
def restore_bootstrap():
    if not rotation['password']:
        return
    root=Path(__file__).resolve().parents[1]
    options=json.loads((root/'.data/store.json').read_text())
    try:
        connection=mysql.connector.connect(unix_socket=options['unix_socket'],user='admin',password=rotation['password'])
    except mysql.connector.Error:
        return
    cursor=connection.cursor()
    cursor.execute("ALTER USER 'admin'@'localhost' IDENTIFIED BY %s",(BOOTSTRAP_PASSWORD,))
    cursor.close();connection.close()
    connection=mysql.connector.connect(**options)
    cursor=connection.cursor()
    cursor.execute("UPDATE operators SET must_change_password=TRUE WHERE username='admin'")
    connection.commit();cursor.close();connection.close()
    rotation['password']=None
atexit.register(restore_bootstrap)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1440,'height':1100})
    errors=[]
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto('http://127.0.0.1:5088')
    page.get_by_label('Admin username').fill('admin')
    page.get_by_label('Password',exact=False).fill(os.environ.get('BLUEGREEN_BROWSER_ADMIN_PASSWORD',BOOTSTRAP_PASSWORD))
    output=Path('/private/tmp/bluegreen-preview');output.mkdir(exist_ok=True)
    page.screenshot(path=str(output/'admin-login.png'))
    page.get_by_role('button',name='Sign in').click()
    rotated_password=None
    if page.locator('#password-title').count():
        page.screenshot(path=str(output/'first-password-change.png'))
        rotated_password=secrets.token_urlsafe(32)
        rotation['password']=rotated_password
        page.get_by_label('New password *',exact=True).fill(rotated_password)
        page.get_by_label('Confirm new password *',exact=True).fill(rotated_password)
        page.get_by_role('button',name='Change password',exact=True).click()
        page.get_by_label('Password',exact=False).fill(rotated_password)
        page.get_by_role('button',name='Sign in').click()
    page.get_by_role('button',name='Orders upgrade').click()
    page.get_by_text('EXECUTED GTID SET').first.wait_for()
    output=Path('/private/tmp/bluegreen-preview');output.mkdir(exist_ok=True)
    page.screenshot(path=str(output/'dashboard.png'),full_page=True)
    page.get_by_role('button',name='Events',exact=True).first.click()
    page.get_by_role('heading',name='Scheduled events · choose states').wait_for()
    page.screenshot(path=str(output/'events.png'))
    assert page.get_by_text('orders.daily_rollup',exact=True).is_visible()
    page.get_by_role('button',name='Close',exact=True).click()
    page.get_by_role('button',name='Details',exact=True).first.click()
    page.get_by_role('heading',name='Work request details').wait_for()
    page.get_by_role('button',name='Close',exact=True).click()
    page.locator('[data-create="demo-standalone"]').evaluate('e => e.disabled = false')
    page.locator('[data-create="demo-standalone"]').click()
    page.get_by_role('heading',name='Source scheduled events · review required').wait_for()
    page.get_by_role('button',name='Disable all enabled events before execution').click()
    assert page.locator('#creation-events input[type=checkbox]').first.is_checked()
    assert page.get_by_text('Restore original after creation',exact=True).count()==2
    page.screenshot(path=str(output/'creation.png'))
    page.get_by_role('button',name='Close',exact=True).click()
    page.locator('#switchover').evaluate('e => e.disabled = false')
    page.locator('#switchover').click()
    page.get_by_role('heading',name='Green final event states after switchover').wait_for()
    assert page.locator('#target-events select').first.input_value()=='ENABLE'
    assert page.locator('#target-events select').nth(1).input_value()=='DISABLE'
    page.screenshot(path=str(output/'switchover.png'))
    page.get_by_role('button',name='Close',exact=True).click()
    page.get_by_role('button',name='High availability',exact=False).click()
    assert page.get_by_text('Billing · HA',exact=True).is_visible()
    page.set_viewport_size({'width':390,'height':844})
    page.screenshot(path=str(output/'mobile.png'),full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), page.evaluate('[...document.querySelectorAll("body *")].filter(e=>e.getBoundingClientRect().right>innerWidth && getComputedStyle(e).position!=="absolute").map(e=>({tag:e.tagName,cls:e.className,right:e.getBoundingClientRect().right})).slice(0,20)')
    assert not errors,errors
    page.locator('details.user summary').click()
    page.get_by_role('button',name='Sign out',exact=True).click()
    print('Browser checks passed: admin popup, mandatory password change, dashboard, event reviews, work requests, mobile')
    browser.close()
    restore_bootstrap()
