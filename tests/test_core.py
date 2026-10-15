import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fakes import FakeAuth

from app import create_app
from modules.demo import DemoService, DemoObserver
from modules.events import fingerprint, validate_plan
from modules.gtid import compare, parse, subtract
from modules.mysql_util import ConnectionCache, build_tls_kwargs, escape_sql_literal
from sqlite_store import Store
from modules.workflow import Workflow

B='11111111-1111-1111-1111-111111111111'
G='22222222-2222-2222-2222-222222222222'
X='33333333-3333-3333-3333-333333333333'


def test_gtid_rules_and_large_intervals():
    assert compare(B+':1-999999999999', B+':1-9', G)['ok']
    assert compare(B+':1-10', B+':1-10', G)['exact']
    assert compare(B+':1-10', B+':1-10,'+G+':1-3', G)['extra_green']==G+':1-3'
    assert not compare(B+':1-10', B+':1-11', G)['ok']
    assert not compare(B+':1-10', B+':1-10,'+X+':1', G)['ok']
    assert compare(B+':1-10', B+':1-9', G)['missing_on_green']==B+':10'
    assert subtract(parse(B+':1-10'),parse(B+':2-3:6-8'))=={B:[(1,1),(4,5),(9,10)]}
    with pytest.raises(ValueError): parse(B+':10-1')


def test_event_review_detects_state_or_definition_change():
    events=[{'event_schema':'db','event_name':'e','status':'ENABLED','definition':'SELECT 1','last_executed':None}]
    review={'fingerprint':fingerprint(events),'choices':[{'event_schema':'db','event_name':'e','after':'RESTORE'}]}
    assert validate_plan(events,review)
    events[0]['last_executed']='later'
    assert validate_plan(events,review)
    events[0]['status']='DISABLED'
    with pytest.raises(ValueError): validate_plan(events,review)


def test_creation_restore_and_promotion_choices(tmp_path):
    store=Store(tmp_path/'journal.db')
    observer=Mock()
    workflow=Workflow(Mock(),observer,store)
    job=store.start('db','create',{})
    original=[{'event_schema':'db','event_name':'on','status':'ENABLED'},
              {'event_schema':'db','event_name':'off','status':'DISABLED'}]
    observer.events.return_value = original
    def change(db_id, schema, name, enabled):
        next(e for e in original if e['event_name'] == name)['status'] = 'ENABLED' if enabled else 'DISABLED'
    observer.set_event.side_effect = change
    snapshot = [dict(e) for e in original]
    choices=[{**e,'disable_before':e['status']=='ENABLED','after':'RESTORE'} for e in original]
    workflow.event_before(job,'db',choices)
    workflow.event_after(job,'db',choices,snapshot)
    assert observer.set_event.call_args_list[0].args==('db','db','on',False)
    assert observer.set_event.call_args_list[1].args==('db','db','on',True)
    assert observer.set_event.call_count == 2
    observer.reset_mock()
    choices[0]['after']='DISABLE'; choices[1]['after']='ENABLE'
    workflow.event_after(job,'green',choices,original)
    assert observer.set_event.call_args_list[0].args==('green','db','on',False)
    assert observer.set_event.call_args_list[1].args==('green','db','off',True)


def test_journal_exclusion_and_restart(tmp_path):
    store=Store(tmp_path/'journal.db')
    job=store.start('db','create',{'secret_id':'ocid-reference'})
    event=store.event(job,'step')
    store.update_event(event,work_request='work-reference')
    with pytest.raises(ValueError):store.start('db','create',{})
    store.recover()
    assert store.history()[0]['status']=='INTERRUPTED'
    assert store.history()[0]['events'][0]['status']=='INTERRUPTED'
    assert store.history()[0]['events'][0]['work_request']=='work-reference'


@pytest.fixture
def client(tmp_path):
    service=DemoService(); observer=DemoObserver(service)
    store=Store(tmp_path/'journal.db')
    app=create_app({'TESTING':True,'DATA_DIR':str(tmp_path)},service,observer,store,FakeAuth(store))
    return app,app.test_client()


def login(client):
    client.get('/login')
    with client.session_transaction() as session:csrf=session['login_csrf']
    return client.post('/login',data={'csrf':csrf,'username':'admin','password':'test-private-password'})


def test_auth_csrf_cookie_and_server_session_loss(client):
    app,c=client
    assert c.get('/api/dashboard').status_code==401
    assert c.post('/login',data={'username':'admin'}).status_code==400
    assert login(c).status_code==302
    with c.session_transaction() as s:
        assert set(s)=={'sid'}
        assert 'test-private-password' not in json.dumps(dict(s))
        csrf=app.extensions['bluegreen']['sessions'][s['sid']]['csrf']
    assert c.post('/api/compartment',json={'compartment_id':'demo-compartment'}).status_code==400
    assert c.post('/api/compartment',json={'compartment_id':'demo-compartment'},headers={'X-CSRF-Token':csrf}).status_code==200
    app.extensions['bluegreen']['sessions'].clear()
    assert c.get('/').status_code==302
    assert c.get('/api/dashboard').status_code==401


def test_dashboard_demo_read_only_and_event_listing(client):
    app,c=client;login(c)
    payload=c.get('/api/dashboard').json
    assert not next(s for s in payload['systems'] if s['id']=='demo-blue')['eligible']
    assert next(s for s in payload['systems'] if s['id']=='demo-standalone')['tone']=='red'
    assert next(s for s in payload['systems'] if s['id']=='demo-ha')['tone']=='orange'
    assert c.get('/api/events/demo-blue').json['events'][0]['status']=='ENABLED'
    assert c.get('/api/deployments/demo-deployment').status_code==200
    with c.session_transaction() as s:token=app.extensions['bluegreen']['sessions'][s['sid']]['csrf']
    assert c.post('/api/create',json={},headers={'X-CSRF-Token':token}).status_code==400
    assert c.get('/').status_code==200


def test_connector_tls_cache_and_literal_escaping(monkeypatch):
    import modules.mysql_util as mysql
    connection=Mock();cursor=Mock();connection.cursor.return_value=cursor
    opener=Mock(return_value=(connection,None));monkeypatch.setattr(mysql,'open_connection',opener)
    cache=ConnectionCache();profile={'host':'localhost','ssl_mode':'REQUIRED'}
    assert cache.borrow('id',profile,'u','p') is connection
    assert cache.borrow('id',profile,'u','p') is connection
    assert opener.call_count==1
    assert cursor.execute.call_args_list[0].args==('SELECT 1',)
    assert cursor.execute.call_count==2
    assert build_tls_kwargs({'ssl_mode':'REQUIRED'})['ssl_disabled'] is False
    assert build_tls_kwargs({'ssl_mode':'VERIFY_CA'})['ssl_verify_cert']
    assert build_tls_kwargs({'ssl_mode':'VERIFY_IDENTITY'})['ssl_verify_identity']
    assert build_tls_kwargs({'ssl_mode':'DISABLED'})=={'ssl_disabled':True}
    assert escape_sql_literal(None)=='NULL'
    assert escape_sql_literal("'\\\0\n\r\x1a") == "'\\'\\\\\\0\\n\\r\\Z'"


def test_precheck_rejects_unknown_health_extra_gtid_and_enabled_channels(tmp_path):
    service=DemoService(); observer=DemoObserver(service)
    service.channel_health=lambda *args: {'channel_status_result':{'is_healthy':True}}
    workflow=Workflow(service,observer,Store(tmp_path/'journal.db'))
    assert workflow.precheck('demo-deployment',lambda x:x)['gtid']['extra_green']==G+':1-4'
    service.channel_health=lambda *args: {'channel_status_result':{}}
    with pytest.raises(ValueError,match='not healthy'):workflow.precheck('demo-deployment',lambda x:x)
    service.channel_list[1]['is_enabled']=True
    with pytest.raises(ValueError,match='must be disabled'):workflow.precheck('demo-deployment',lambda x:x)


def test_green_reuses_blue_observer_profile_with_explicit_override_and_scope_guards(tmp_path):
    from modules.observer import resolve_profile
    service=DemoService();store=Store(tmp_path/'journal.db')
    profile={'username':'sql-observer','secret_id':'secret-reference','secret_compartment_id':service.default_compartment}
    store.put('observer:demo-blue',profile)
    assert resolve_profile(service,store,'demo-green')=={**profile,'inherited_from':'demo-blue'}
    assert store.get('observer:demo-green') is None
    explicit={**profile,'username':'green-observer'}
    store.put('observer:demo-green',explicit)
    assert resolve_profile(service,store,'demo-green')==explicit
    store.put('observer:demo-green',{})
    service.dep['compartment_id']='another-compartment'
    assert resolve_profile(service,store,'demo-green')=={}
    service.dep['compartment_id']=service.default_compartment
    service.dep['lifecycle_state']='DELETED'
    assert resolve_profile(service,store,'demo-green')=={}


def test_precheck_rejects_non_source_primary_and_inactive_deployment(tmp_path):
    service=DemoService();observer=DemoObserver(service)
    workflow=Workflow(service,observer,Store(tmp_path/'journal.db'))
    service.dep.update(switchover_status='NOT_STARTED',lifecycle_state='UPDATING')
    with pytest.raises(ValueError,match='UPDATING / NOT_STARTED'):workflow.precheck('demo-deployment',lambda x:x)
    service.dep.update(lifecycle_state='ACTIVE',active_db_system_id='demo-green')
    with pytest.raises(ValueError,match='Blue must still'):workflow.precheck('demo-deployment',lambda x:x)


def test_unresolved_oci_work_request_blocks_another_submission(tmp_path):
    store=Store(tmp_path/'journal.db');service=Mock();observer=Mock()
    previous=store.start('db','create',{})
    event=store.event(previous,'Create')
    store.update_event(event,work_request='pending-request',status='FAILED')
    store.finish(previous,'FAILED')
    service.work_request.return_value={'request':{'status':'IN_PROGRESS'}}
    workflow=Workflow(service,observer,store)
    with pytest.raises(ValueError,match='still running'):
        workflow.submit('update_db','another-db',{'database_mode':'READ_ONLY'})
    assert len(store.history())==1


def test_event_datetime_snapshot_is_durable(tmp_path):
    from datetime import datetime
    store=Store(tmp_path/'journal.db')
    event={'event_schema':'db','event_name':'daily','status':'ENABLED','starts':datetime(2026,10,14,12,0)}
    store.put('registered-events:db',[event])
    assert store.get('registered-events:db')[0]['starts']=='2026-10-14 12:00:00'


def test_initial_password_forces_rotation_and_cannot_bypass_api(tmp_path):
    store=Store(tmp_path/'journal.db')
    auth=FakeAuth(store,password='ChangeMe123!',first_login=True)
    service=DemoService();observer=DemoObserver(service)
    app=create_app({'TESTING':True,'DATA_DIR':str(tmp_path)},service,observer,store,auth)
    c=app.test_client();c.get('/')
    page=c.get('/login')
    assert b'id="auth-dialog"' in page.data
    assert b'ChangeMe123!' not in page.data
    with c.session_transaction() as s:csrf=s['login_csrf']
    response=c.post('/login',data={'csrf':csrf,'username':'admin','password':'ChangeMe123!'})
    assert response.location.endswith('/change-password')
    assert c.get('/').location.endswith('/change-password')
    assert c.get('/api/dashboard').status_code==403
    with c.session_transaction() as s:
        assert set(s)=={'sid'}
        csrf=app.extensions['bluegreen']['sessions'][s['sid']]['csrf']
    assert c.post('/api/create',json={},headers={'X-CSRF-Token':csrf}).status_code==403
    response=c.post('/change-password',data={'csrf':csrf,'new_password':'ChangeMe123!','confirm_password':'ChangeMe123!'})
    assert b'Choose a different password' in response.data
    assert auth.must_change('admin')
    response=c.post('/change-password',data={'csrf':csrf,'new_password':'new-private-password!','confirm_password':'new-private-password!'})
    assert response.status_code==302 and '/login' in response.location
    assert not auth.must_change('admin')
    assert c.get('/api/dashboard').status_code==401
    c.get('/login')
    with c.session_transaction() as s:csrf=s['login_csrf']
    assert c.post('/login',data={'csrf':csrf,'username':'admin','password':'ChangeMe123!'}).status_code==200
    assert c.post('/login',data={'csrf':csrf,'username':'admin','password':'new-private-password!'}).status_code==302
    assert c.get('/api/dashboard').status_code==200


def test_logged_in_api_never_returns_local_password(client):
    app,c=client;login(c)
    for path in ['/','/api/dashboard','/api/events/demo-blue','/api/deployments/demo-deployment']:
        assert b'test-private-password' not in c.get(path).data
