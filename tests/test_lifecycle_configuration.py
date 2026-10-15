"""Lifecycle safety and versioned configuration contracts through the real OCI SDK."""
from copy import deepcopy
import pytest
from test_sdk_execution_path import harness, IDS, ids, route_ids
from modules.resource_actions import confirmation_snapshot, switchover_blocker
from modules.configuration_editor import VARIABLES, validate_values


def confirm(e,path,params):
    preview=e.post('/api/operation-plan',{'path':route_ids(path),'params':params})
    assert preview.status_code==200,preview.json
    assert preview.json['execution_allowed'] is True
    return {**params,'confirmation_token':preview.json['confirmation_token'],'confirmed':True},preview.json


def mutations(e):return [c for c in e.cloud.calls if c['method']!='GET']


@pytest.mark.parametrize('active,status,state,allowed',[
    ('blue','NOT_STARTED','ACTIVE',True),('blue','READY_FOR_SWITCHOVER','ACTIVE',True),
    ('green','NOT_STARTED','ACTIVE',False),('green','SWITCHOVER_COMPLETED','ACTIVE',False),
    (None,'NOT_STARTED','ACTIVE',False),('blue','SWITCHOVER_COMPLETED','ACTIVE',False),
    ('blue','NOT_STARTED','UPDATING',False)])
def test_switchover_identity_and_status_guards(harness,active,status,state,allowed):
    e=harness;e.cloud.created=True
    e.cloud.dep.update(activeDbSystemId=active,switchoverStatus=status,lifecycleState=state)
    e.cloud.systems['blue']['displayName']='Former name'
    e.cloud.systems['green']['displayName']='Contract Blue'
    detail=e.client.get('/api/deployments/deployment')
    assert detail.status_code==200,detail.json
    assert detail.json['actions']['switchover']['allowed'] is allowed
    if not allowed:
        params={'action':'switchover'}
        assert e.post('/api/deployments/deployment/action',params).status_code==400
        assert e.post('/api/operation-plan',{'path':route_ids('/api/deployments/deployment/action'),'params':params}).status_code==400
        with pytest.raises(ValueError):e.app.extensions['bluegreen']['runtime']()[2].precheck(IDS['deployment'],lambda x:x)
        assert not mutations(e)


def test_missing_member_ids_never_enable_switch():
    assert switchover_blocker({'lifecycle_state':'ACTIVE','switchover_status':'NOT_STARTED'})


@pytest.mark.parametrize('action,state', [('stop','ACTIVE'),('start','INACTIVE'),('delete','ACTIVE'),('disable_delete_protection','ACTIVE')])
def test_lifecycle_requires_bound_confirmation_and_records_work_request(harness,action,state):
    e=harness;e.cloud.systems['blue']['lifecycleState']=state
    e.cloud.systems['blue']['deletionPolicy']['isDeleteProtected']=action=='disable_delete_protection'
    path='/api/db/blue/action';params={'action':action}
    assert e.post(path,params).status_code==400
    assert not mutations(e)
    body,plan=confirm(e,path,params)
    assert IDS['blue'] in str(plan)
    assert not mutations(e)
    job=e.wait(e.post(path,body));assert job['status']=='SUCCEEDED',job
    calls=mutations(e);assert len(calls)==1 and calls[0]['if_match'] is None
    assert any(event.get('work_request') for event in job['events'])
    if action=='stop':assert calls[0]['body']=={'shutdownType':'FAST'}
    if action=='delete':
        assert calls[0]['method']=='DELETE' and calls[0]['path'].endswith('/dbSystems/'+IDS['blue'])
        assert not e.client.get('/api/dashboard').json['systems']
    if action=='disable_delete_protection':
        assert calls[0]['body']=={'deletionPolicy':{'isDeleteProtected':False}}
        assert e.cloud.systems['blue']['deletionPolicy']['finalBackup']=='REQUIRE_FINAL_BACKUP'
    assert e.post(path,body).status_code==400
    assert len(mutations(e))==1


@pytest.mark.parametrize('protected', [True,None])
def test_delete_protection_is_enforced_in_preview_api_and_adapter(harness,protected):
    e=harness;e.cloud.systems['blue']['deletionPolicy']['isDeleteProtected']=protected
    assert e.client.get('/api/db/blue').json['lifecycle_actions']['delete']['allowed'] is False
    assert e.post('/api/operation-plan',{'path':route_ids('/api/db/blue/action'),'params':{'action':'delete'}}).status_code==400
    assert e.post('/api/db/blue/action',{'action':'delete','confirmed':True}).status_code==400
    db=e.service.db_system(IDS['blue'])
    with pytest.raises(ValueError):e.service.lifecycle_db(IDS['blue'],'delete',expected_compartment=IDS['contract-comp'],expected_resource=confirmation_snapshot(db))
    assert not mutations(e)


@pytest.mark.parametrize('change', ['state','policy','name','compartment','parameters','expired'])
def test_confirmation_rejects_resource_or_request_changes(harness,change):
    e=harness;path='/api/db/blue/action';body,_=confirm(e,path,{'action':'delete'})
    if change=='state':e.cloud.systems['blue']['lifecycleState']='UPDATING'
    if change=='policy':e.cloud.systems['blue']['deletionPolicy']['finalBackup']='SKIP_FINAL_BACKUP'
    if change=='name':e.cloud.systems['blue']['displayName']='New name'
    if change=='compartment':e.cloud.systems['blue']['compartmentId']='denied-comp'
    if change=='parameters':body['action']='stop'
    if change=='expired':
        with e.client.session_transaction() as s:record=e.app.extensions['bluegreen']['sessions'][s['sid']]
        record['confirmations'][body['confirmation_token']]['created']-=601
    assert e.post(path,body).status_code==400
    assert not mutations(e)


def test_worker_rechecks_after_confirmation_and_does_not_retry(harness,monkeypatch):
    e=harness;db=e.service.db_system(IDS['blue']);original=e.service.db_system;count=0
    def changing(dbid):
        nonlocal count
        count+=1
        if count==2:e.cloud.systems['blue']['deletionPolicy']['isDeleteProtected']=True
        return original(dbid)
    monkeypatch.setattr(e.service,'db_system',changing)
    with pytest.raises(ValueError):e.service.lifecycle_db(IDS['blue'],'delete',expected_compartment=IDS['contract-comp'],expected_resource=confirmation_snapshot(db))
    assert not mutations(e)


@pytest.mark.parametrize('switched', [False,True])
def test_delete_deployment_deletes_only_wrapper(harness,switched):
    e=harness;e.cloud.created=True
    if switched:e.cloud.dep.update(activeDbSystemId='green',switchoverStatus='SWITCHOVER_COMPLETED')
    before=(deepcopy(e.cloud.systems),deepcopy(e.cloud.channels))
    body,plan=confirm(e,'/api/deployments/deployment/action',{'action':'delete'})
    assert 'Both DB systems' in str(plan)
    job=e.wait(e.post('/api/deployments/deployment/action',body));assert job['status']=='SUCCEEDED',job
    assert before==(e.cloud.systems,e.cloud.channels)
    assert len(mutations(e))==1 and mutations(e)[0]['path'].endswith('/blueGreenDeployments/'+IDS['deployment'])
    assert not e.client.get('/api/dashboard').json['deployments']
    assert len(e.client.get('/api/dashboard').json['systems'])==2


@pytest.mark.parametrize('state', ['CREATING','UPDATING','DELETING','DELETED'])
def test_deployment_deletion_blocks_busy_or_deleted(harness,state):
    e=harness;e.cloud.dep['lifecycleState']=state
    assert e.post('/api/operation-plan',{'path':route_ids('/api/deployments/deployment/action'),'params':{'action':'delete'}}).status_code==400
    assert not mutations(e)


def configure(e):
    e.cloud.configurations['ha-config']['options']=[{'name':'connect_timeout','value':'10'},{'name':'autocommit','value':'ON'},
        {'name':'replica_type_conversions','value':''},{'name':'max_connections','value':'500'}]
    data=e.client.get('/api/db/blue/configuration-editor')
    assert data.status_code==200,data.json
    return data.json


def draft(data,**changes):
    return {'source_configuration_id':data['source']['id'],'display_name':data['suggested_name'],
            'variables':{**data['variables'],**changes},'acknowledge_removal':False}


def test_configuration_save_version_then_separate_confirmed_apply(harness):
    e=harness;data=configure(e)
    assert set(data['variables'])=={'connect_timeout','autocommit','replica_type_conversions','max_connections'}
    assert 'server_uuid' not in data['catalog'] and 'lower_case_table_names' not in data['catalog']
    assert data['catalog']['autocommit']['choices']==['OFF','ON']
    params=draft(data,connect_timeout='20',replica_type_conversions='ALL_NON_LOSSY')
    path='/api/db/blue/configuration-versions'
    assert e.post(path,params).status_code==400
    body,plan=confirm(e,path,params)
    job=e.wait(e.post(path,body));assert job['status']=='SUCCEEDED',job
    assert len(mutations(e))==1
    call=mutations(e)[0]
    assert call['path'].endswith('/configurations') and call['method']=='POST'
    assert call['body']['displayName']=='MySQL.8.HA_v001'
    assert call['body']['parentConfigurationId']==IDS['ha-config']
    assert {v['name']:v['value'] for v in call['body']['options']}==params['variables']
    assert e.cloud.systems['blue']['configurationId']=='ha-config'
    result=e.client.get('/api/configuration-result/'+job['id']).json
    assert result['configuration']['id']==IDS['configuration-version']
    path='/api/db/blue/action';apply={'action':'configuration','configuration_id':'configuration-version'}
    assert e.post(path,apply).status_code==400
    body,plan=confirm(e,path,apply)
    assert 'restart' in str(plan)
    job=e.wait(e.post(path,body));assert job['status']=='SUCCEEDED',job
    assert mutations(e)[-1]['body']=={'configurationId':IDS['configuration-version']}
    assert e.cloud.systems['blue']['configurationId']=='configuration-version'
    again=e.client.get('/api/db/blue/configuration-editor').json
    assert again['suggested_name']=='MySQL.8.HA_v002'


def test_optional_variable_removal_requires_warning_acknowledgement(harness):
    e=harness;data=configure(e);params=draft(data);del params['variables']['connect_timeout']
    path='/api/db/blue/configuration-versions'
    assert e.post('/api/operation-plan',{'path':route_ids(path),'params':params}).status_code==400
    params['acknowledge_removal']=True
    body,plan=confirm(e,path,params)
    assert 'does not reset' in str(plan)
    job=e.wait(e.post(path,body));assert job['status']=='SUCCEEDED',job
    assert 'connect_timeout' not in {v['name'] for v in mutations(e)[0]['body']['options']}
    assert e.cloud.systems['blue']['configurationId']=='ha-config'


@pytest.mark.parametrize('representation', ['legacy', 'options', 'both'])
def test_configuration_preserves_initialization_without_mixing_api_fields(harness, representation):
    e=harness;configure(e)
    config=e.cloud.configurations['ha-config']
    if representation in {'legacy','both'}:
        config['initVariables']={'lowerCaseTableNames':'CASE_INSENSITIVE_LOWERCASE'}
    if representation in {'options','both'}:
        config['options'].append({'name':'lower_case_table_names','value':'CASE_INSENSITIVE_LOWERCASE'})
    data=e.client.get('/api/db/blue/configuration-editor').json
    assert data['initialization_variables']=={'lower_case_table_names':'CASE_INSENSITIVE_LOWERCASE'}
    assert 'lower_case_table_names' not in data['variables']
    path='/api/db/blue/configuration-versions'
    params=draft(data,replica_type_conversions='ALL_NON_LOSSY',max_connect_errors='18446744073709551615')
    body,_=confirm(e,path,params)
    job=e.wait(e.post(path,body));assert job['status']=='SUCCEEDED',job
    payload=mutations(e)[0]['body']
    assert 'initVariables' not in payload and 'variables' not in payload
    options={o['name']:o['value'] for o in payload['options']}
    assert options=={**params['variables'],'lower_case_table_names':'CASE_INSENSITIVE_LOWERCASE'}
    assert e.cloud.systems['blue']['configurationId']=='ha-config'
    # The new OCI response has initialization in options only. Applying that
    # unchanged setting must still pass the initialization compatibility check.
    apply={'action':'configuration','configuration_id':'configuration-version'}
    body,_=confirm(e,'/api/db/blue/action',apply)
    job=e.wait(e.post('/api/db/blue/action',body));assert job['status']=='SUCCEEDED',job


def test_custom_initialization_override_survives_configuration_version(harness):
    e=harness;configure(e)
    e.cloud.configurations['green-config']['initVariables']={'lowerCaseTableNames':'CASE_SENSITIVE'}
    current=e.cloud.configurations['ha-config']
    current.update(type='CUSTOM',compartmentId='contract-comp',parentConfigurationId='green-config')
    current['options'].append({'name':'lower_case_table_names','value':'CASE_INSENSITIVE_LOWERCASE'})
    data=e.client.get('/api/db/blue/configuration-editor').json
    assert data['initialization_variables']['lower_case_table_names']=='CASE_INSENSITIVE_LOWERCASE'
    path='/api/db/blue/configuration-versions';body,_=confirm(e,path,draft(data))
    job=e.wait(e.post(path,body));assert job['status']=='SUCCEEDED',job
    payload=mutations(e)[0]['body']
    assert payload['parentConfigurationId']==IDS['green-config']
    assert {o['name']:o['value'] for o in payload['options']}['lower_case_table_names']=='CASE_INSENSITIVE_LOWERCASE'


def test_known_configuration_validation_error_does_not_expose_request_body():
    import oci
    from modules.oci_service import public_error
    error=oci.exceptions.ServiceError(400,'InvalidParameter',{'opc-request-id':'example-request'},
        'attribute = {configurationDetails}, value = {secret-bearing request body} - cannot specify both options and initVariables simultaneously.')
    message=public_error(error)
    assert 'cannot be combined' in message and 'example-request' in message
    assert 'secret-bearing' not in message and 'configurationDetails' not in message


@pytest.mark.parametrize('values', [{'server_uuid':'forged'},{'autocommit':'MAYBE'},{'connect_timeout':'1'},
    {'connect_timeout':'10.5'},{'replica_type_conversions':'ALL_SIGNED,ALL_SIGNED'},
    {'replica_type_conversions':'INVENTED'},{'optimizer_switch':'invented=on'}])
def test_configuration_rejects_nonconfigurable_variables_and_invalid_values(harness,values):
    e=harness;data=configure(e);params=draft(data,**values)
    response=e.post('/api/operation-plan',{'path':route_ids('/api/db/blue/configuration-versions'),'params':params})
    assert response.status_code==400,response.json
    assert not mutations(e)


def test_mandatory_variables_cannot_be_removed():
    with pytest.raises(ValueError,match='requires'):validate_values({}, {'max_connections':'500'},True)
    result,_=validate_values({'max_connect_errors':'18446744073709551615'}, {})
    assert result['max_connect_errors']=='18446744073709551615'
    assert 'mysqlx_enable_hello_notice' not in VARIABLES


@pytest.mark.parametrize('change', ['shape','ha','compartment','initialization','initialization_options'])
def test_configuration_apply_checks_shape_ha_scope_initialization(harness,change):
    e=harness;configure(e)
    config=e.cloud.configurations['green-config']
    if change=='shape':config['shapeName']='MySQL.16'
    if change=='ha':config['displayName']='MySQL.8.Standalone'
    if change=='compartment':config.update(type='CUSTOM',compartmentId='denied-comp')
    if change=='initialization':config['initVariables']={'lowerCaseTableNames':'CASE_INSENSITIVE'}
    if change=='initialization_options':config['options']=[{'name':'lower_case_table_names','value':'CASE_INSENSITIVE'}]
    response=e.post('/api/operation-plan',{'path':route_ids('/api/db/blue/action'),
        'params':{'action':'configuration','configuration_id':'green-config'}})
    assert response.status_code==400,response.json
    assert not mutations(e)


def test_version_collision_requires_new_preview(harness):
    e=harness;data=configure(e);path='/api/db/blue/configuration-versions'
    body,_=confirm(e,path,draft(data))
    e.cloud.configurations['configuration-version']={**e.cloud.configurations['ha-config'],'id':'configuration-version',
        'type':'CUSTOM','compartmentId':'contract-comp','displayName':data['suggested_name'],'parentConfigurationId':'ha-config'}
    assert e.post(path,body).status_code==400
    assert not mutations(e)


def test_read_write_preview_offers_conversion_guidance_without_changes(harness):
    e=harness;path=route_ids('/api/db/green/action')
    result=e.post('/api/operation-plan',{'path':path,'params':{'action':'read_write'}})
    assert result.status_code==200,result.json
    guidance=result.json['conversion_guidance']
    assert guidance['db_system_id']==IDS['green']
    assert {r[0] for r in guidance['options']}=={'Empty set','ALL_NON_LOSSY','ALL_LOSSY','ALL_SIGNED','ALL_UNSIGNED'}
    assert 'Nothing is changed automatically' in guidance['note']
    assert not mutations(e)
    result,_=validate_values({'replica_type_conversions':'ALL_NON_LOSSY,ALL_SIGNED,ALL_UNSIGNED'}, {})
    assert result['replica_type_conversions']=='ALL_NON_LOSSY,ALL_SIGNED,ALL_UNSIGNED'
