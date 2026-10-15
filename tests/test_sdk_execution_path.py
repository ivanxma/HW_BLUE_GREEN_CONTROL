"""Full application/adapter/SDK contract tests; OCI HTTP transport alone is intercepted.

These tests cannot mutate OCI DB systems. Live Compute runs optionally use the
real instance-principal signer and embedded MySQL as the workflow datastore.
Blue/Green SQL telemetry is supplied by explicit test stand-ins.
"""
import base64
import json
import os
import re
import secrets
import time
import uuid
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import Mock

import mysql.connector
import oci
import pytest
from oci._vendor import requests

from app import ROOT, create_app
from modules.demo import B, G
from modules.events import fingerprint
from modules.local_auth import BOOTSTRAP_PASSWORD, LocalAuth
from modules.oci_service import OCIService
from sqlite_store import Store as HarnessStore
from fakes import FakeAuth


ID_TYPES={'blue':'mysqldbsystem','green':'mysqldbsystem','deployment':'mysqlbluegreendeployment', 'replica':'mysqlreplica',
          'native':'mysqlchannel','inbound':'mysqlchannel','copied':'mysqlchannel','secret':'vaultsecret',
          'new-secret':'vaultsecret','vault':'vault','key':'key','contract-comp':'compartment',
          'contract-tenancy':'tenancy','ha-config':'mysqlconfiguration','green-config':'mysqlconfiguration',
          'promoted-ha-config':'mysqlconfiguration','denied-comp':'compartment'}
IDS={name:'ocid1.'+kind+'.oc1.'+('' if kind in {'compartment','tenancy'} else 'uk-london-1')+'.'+
     ('a'*48+chr(97+index)*16) for index,(name,kind) in enumerate(ID_TYPES.items())}
REVERSE={value:key for key,value in IDS.items()}

def ids(value,reverse=False):
    mapping=REVERSE if reverse else IDS
    if isinstance(value,dict):return {mapping.get(k,k):ids(v,reverse) for k,v in value.items()}
    if isinstance(value,list):return [ids(v,reverse) for v in value]
    if isinstance(value,str):return mapping.get(value,value)
    return value

def route_ids(path):
    for alias,ocid in IDS.items():
        path=path.replace('/'+alias+'/', '/'+ocid+'/')
        if path.endswith('/'+alias):path=path[:-len(alias)]+ocid
        path=path.replace('='+alias+'&','='+ocid+'&')
        if path.endswith('='+alias):path=path[:-len(alias)]+ocid
    return path


class SQLStandIn:
    def __init__(self, cloud):
        self.cloud=cloud
        self.states={'blue':[{'event_schema':'orders','event_name':'daily_rollup','status':'ENABLED','definition':'CALL daily_rollup()'},
                             {'event_schema':'orders','event_name':'archive','status':'DISABLED','definition':'CALL archive_orders()'}],
                     'green':[{'event_schema':'orders','event_name':'daily_rollup','status':'REPLICA_SIDE_DISABLED','definition':'CALL daily_rollup()'},
                              {'event_schema':'orders','event_name':'archive','status':'DISABLED','definition':'CALL archive_orders()'}]}
        self.transitions=[]
        self.extra_uuid=False
        self.endpoint_wrong=False
        self.no_rw=False
        self.green_ahead=False

    def events(self, db_id):return deepcopy(self.states[ids(db_id,True)])
    def set_event(self,db_id,schema,name,enabled):
        db_id=ids(db_id,True)
        event=next(e for e in self.states[db_id] if e['event_schema']==schema and e['event_name']==name)
        event['status']='ENABLED' if enabled else 'DISABLED'
        self.transitions.append({'db_system':db_id,'event':name,'status':event['status']})
        return deepcopy(event)
    def sample(self,db_id,host_override=None):
        db_id=ids(db_id,True)
        green=db_id=='green'
        final=self.cloud.switched
        number=101 if final else 100 if not green else 99
        if green and self.green_ahead:number=102
        gtid=B+':1-'+str(number)
        if green:gtid+=','+G+':1-2'
        if green and self.extra_uuid:gtid+=',33333333-3333-3333-3333-333333333333:1'
        rw=self.cloud.systems[db_id]['databaseMode']=='READ_WRITE'
        if green and self.no_rw:rw=False
        return {'gtid':gtid,'server_uuid':G if green else B,
                'hostname':'incorrect-endpoint' if host_override and self.endpoint_wrong else 'green-host' if green else 'blue-host',
                'read_only':not rw,'super_read_only':not rw,'uptime':1000,'replication':[],
                'db_system':self.cloud.service.db_system(ids(db_id))}


class OracleHTTP:
    def __init__(self):
        self.calls=[];self.created=False;self.switched=False;self.requests={};self.observer=None
        self.health=True;self.next_failure=False;self.green_ha=False;self.root_denied=False;self.registration_denied=False;self.replica_denied=False
        self.replicas=[]
        self.etag_conflicts=0;self.etag_drift=False;self.etag_missing=False;self.etag_reads=0;self.update_error=None
        self.allow_standard_updates=False;self.db_read_changes={}
        self.work_details_errors={}
        self.work_summaries=[]
        self.configurations={name:{'id':name,'displayName':'MySQL.8.HA','shapeName':'MySQL.8',
            'type':'DEFAULT','compartmentId':None,'lifecycleState':'ACTIVE'} for name in ['ha-config','green-config','promoted-ha-config']}
        self.shapes=[{'name':'MySQL.8','cpuCoreCount':8,'memorySizeInGBs':64,'isSupportedFor':['DBSYSTEM']},
                     {'name':'MySQL.16','cpuCoreCount':16,'memorySizeInGBs':128,'isSupportedFor':['DBSYSTEM']}]
        self.versions=[{'versionFamily':'8.4 LTS','versions':[{'version':'8.4.8'}]},
                       {'versionFamily':'9 Innovation','versions':[{'version':'9.5.2'},{'version':'9.7.2'}]}]
        self.secret=secrets.token_urlsafe(24)
        self.systems={'blue':{'id':'blue','displayName':'Contract Blue','compartmentId':'contract-comp','lifecycleState':'ACTIVE',
                   'isHighlyAvailable':True,'configurationId':'ha-config','shapeName':'MySQL.8','mysqlVersion':'8.4.8',
                   'dataStorageSizeInGBs':200,'ipAddress':'10.0.0.10','port':3306,'databaseMode':'READ_WRITE',
                   'endpoints':[{'ipAddress':'10.0.0.10','hostname':'blue.internal','modes':['READ','WRITE'],'status':'ACTIVE'}]},
          'green':{'id':'green','displayName':'Contract Green','compartmentId':'contract-comp','lifecycleState':'ACTIVE',
                   'isHighlyAvailable':False,'configurationId':'green-config','shapeName':'MySQL.8','mysqlVersion':'9.5.2',
                   'dataStorageSizeInGBs':200,'ipAddress':'10.0.0.11','port':3306,'databaseMode':'READ_ONLY',
                   'endpoints':[{'ipAddress':'10.0.0.11','hostname':'green.internal','modes':['READ'],'status':'ACTIVE'}]}}
        self.channels={'inbound':{'id':'inbound','displayName':'ERP inbound','compartmentId':'contract-comp',
                   'isEnabled':True,'lifecycleState':'ACTIVE','source':{'sourceType':'MYSQL','hostname':'10.0.1.10',
                   'port':3306,'username':'erp','sslMode':'REQUIRED'},'target':{'targetType':'DBSYSTEM',
                   'dbSystemId':'blue','channelName':'blue_green','applierUsername':'applier',
                   'filters':[{'type':'REPLICATE_DO_DB','value':'erp'}],'delayInSeconds':0}}}
        self.dep={'id':'deployment','displayName':'Contract deployment','compartmentId':'contract-comp',
                  'sourceDbSystemId':'blue','targetDbSystemId':'green','activeDbSystemId':'blue',
                  'replicationChannelId':'native','lifecycleState':'ACTIVE','switchoverStatus':'NOT_STARTED'}

    def db(self,db_id):
        value=deepcopy(self.systems[db_id])
        if db_id=='green' and self.green_ha:value['isHighlyAvailable']=True
        value['channels']=[deepcopy(c) for c in self.channels.values() if c['target']['dbSystemId']==db_id]
        return value

    def wr(self):
        key='ocid1.mysqlworkrequest.oc1.uk-london-1.'+'a'*48+str(len(self.requests)+1).zfill(16)
        self.requests[key]={'polls':0,'failure':self.next_failure}
        self.next_failure=False
        return key

    @staticmethod
    def redact(value):
        if isinstance(value,dict):
            return {k:'<redacted>' if any(x in k.lower() for x in ['password','content','token']) else OracleHTTP.redact(v) for k,v in value.items()}
        if isinstance(value,list):return [OracleHTTP.redact(v) for v in value]
        return value

    def request(self,method,url,auth=None,params=None,headers=None,data=None,**kwargs):
        path=urlparse(url).path;body=json.loads(data) if data else {};response_headers={'opc-request-id':str(uuid.uuid4()),'etag':'contract-etag'}
        signed=False
        if os.environ.get('BLUEGREEN_CONTRACT_INSTANCE_PRINCIPAL')=='1':
            prepared=requests.Request(method,url,params=params,headers=headers,data=data).prepare()
            auth(prepared)
            signed='authorization' in {k.lower() for k in prepared.headers}
            assert signed
        entry={'method':method,'path':path,'body':self.redact(body),'instance_principal_signed':signed,
               'if_match':(headers or {}).get('if-match'),'retry_token_present':bool((headers or {}).get('opc-retry-token'))}
        self.calls.append(entry)
        # Routing aliases are internal to the HTTP stand-in; requests carry valid-format OCIDs.
        body=ids(body,True)
        for ocid,alias in REVERSE.items():path=path.replace(ocid,alias)
        resource_update = '/dbSystems/' in path or ('/channels/' in path and not '/actions/' in path and not path.endswith('/channelStatus'))
        if resource_update and method == 'GET':
            self.etag_reads += 1
            if '/dbSystems/' in path and self.etag_reads in self.db_read_changes:
                self.systems['blue'].update(self.db_read_changes[self.etag_reads])
            response_headers['etag'] = 'contract-etag-'+str(self.etag_reads)
            if self.etag_missing:response_headers.pop('etag')
        def mutation(payload=None):
            wr=self.wr();response_headers['opc-work-request-id']=wr;entry['work_request']=wr
            return payload or {}
        def query(name):
            values=(params or {}).items() if isinstance(params,dict) else (params or [])
            return ids(next((v for k,v in values if k==name),None),True)
        entry['compartment_id']=ids(query('compartmentId'))
        status_code=200
        if resource_update and method=='PUT' and ((self.etag_conflicts and entry['if_match']) or self.update_error):
            assert entry['if_match']=='contract-etag-'+str(self.etag_reads) or (self.allow_standard_updates and entry['if_match'] is None)
            if self.update_error:
                status_code=self.update_error;payload={'code':'InternalError','message':'Injected service failure'}
            else:
                self.etag_conflicts-=1;status_code=412;payload={'code':'NoEtagMatch','message':'Injected validator conflict'}
                if self.etag_drift:
                    self.systems['blue']['description']='Concurrent operator edit'
                    self.channels['inbound']['description']='Concurrent operator edit'
        elif query('compartmentId')=='denied-comp':
            payload={'code':'NotAuthorizedOrNotFound','message':'Contract selected compartment is not authorized'};status_code=404
        elif (self.root_denied and query('compartmentId')=='contract-tenancy' or self.registration_denied) and method=='GET' and path.endswith('/blueGreenDeployments'):
            payload={'code':'NotAuthorizedOrNotFound','message':'Contract root compartment is outside IAM scope'};status_code=404
        elif path.endswith('/compartments/contract-tenancy'):payload={'id':'contract-tenancy','name':'Contract tenancy','lifecycleState':'ACTIVE'}
        elif path.endswith('/compartments'):payload=[{'id':'contract-comp','name':'Contract compartment','lifecycleState':'ACTIVE'},
                                                    {'id':'denied-comp','name':'Restricted lab','lifecycleState':'ACTIVE'}]
        elif path.endswith('/blueGreenDeployments') and method=='GET':payload={'items':[deepcopy(self.dep)] if self.created and query('compartmentId')=='contract-comp' else []}
        elif path.endswith('/blueGreenDeployments') and method=='POST':
            assert body['sourceDbSystemId']=='blue' and not self.systems['blue']['isHighlyAvailable']
            assert body['targetDbSystemDetails']['dataStorageSizeInGBs']==200
            assert body['channelDetails']['sourceUsername']=='replication'
            assert body['channelDetails']['sourcePassword']==self.secret
            assert body['channelDetails']['sslMode']=='REQUIRED' and entry['retry_token_present']
            assert all(e['status']!='ENABLED' for e in self.observer.states['blue'])
            self.created=True
            self.systems['green'].update(deepcopy(body['targetDbSystemDetails']))
            self.channels['native']={'id':'native','displayName':'Native Blue to Green','compartmentId':'contract-comp',
                'isEnabled':True,'lifecycleState':'ACTIVE','source':{'sourceType':'MYSQL','hostname':'10.0.0.10','username':'replication','port':3306,'sslMode':'REQUIRED'},
                'target':{'targetType':'DBSYSTEM','dbSystemId':'green','channelName':'blue_green'}}
            payload=mutation(deepcopy(self.dep))
        elif path.endswith('/blueGreenDeployments/deployment/actions/switchover'):
            assert method=='POST' and body['waitTimeInSeconds']==300
            assert not self.systems['blue']['isHighlyAvailable']
            assert not self.systems['green']['isHighlyAvailable']
            assert self.systems['green']['databaseMode']=='READ_ONLY'
            self.switched=True;self.dep['activeDbSystemId']='green';self.dep['switchoverStatus']='SWITCHOVER_COMPLETED'
            old_name=self.systems['blue']['displayName']
            self.systems['blue']['displayName']=old_name+'-old'
            self.systems['green']['displayName']=old_name
            self.systems['green']['databaseMode']='READ_WRITE';self.channels.pop('native')
            payload=mutation()
        elif path.endswith('/blueGreenDeployments/deployment'):payload=deepcopy(self.dep)
        elif path.endswith('/shapes'):
            payload=deepcopy(self.shapes)
        elif path.endswith('/versions'):
            payload=deepcopy(self.versions)
        elif path.endswith('/configurations'):
            payload=list(deepcopy(self.configurations).values())
        elif '/configurations/' in path:
            payload=deepcopy(self.configurations[path.split('/configurations/')[-1]])
        elif path.endswith('/dbSystems'):
            payload=[self.db('blue')]+([self.db('green')] if self.created else [])
        elif '/dbSystems/' in path:
            db_id=path.split('/dbSystems/')[-1]
            if method=='PUT':
                assert not (body.get('isHighlyAvailable') is True and 'configurationId' in body), 'HA toggle must be separate from a configuration change'
                assert entry['if_match']=='contract-etag-'+str(self.etag_reads) or (self.allow_standard_updates and entry['if_match'] is None)
                assert not any(k in body for k in ['accessMode','adminPassword'])
                self.systems[db_id].update(body);payload=mutation(self.db(db_id))
            else:payload=self.db(db_id)
        elif path.endswith('/replicas') and method=='GET':
            if self.replica_denied:
                payload={'code':'NotAuthorizedOrNotFound','message':'Contract replica discovery denied'};status_code=404
            else:
                payload=[deepcopy(r) for r in self.replicas if not query('dbSystemId') or r['dbSystemId']==query('dbSystemId')]
        elif path.endswith('/channels') and method=='GET':
            payload=[deepcopy(c) for c in self.channels.values() if not query('dbSystemId') or c['target']['dbSystemId']==query('dbSystemId')]
        elif path.endswith('/channels') and method=='POST':
            assert body['isEnabled'] is False
            assert body['source']['sourceType']=='MYSQL' and body['source']['password']==self.secret
            assert body['target']['targetType']=='DBSYSTEM' and body['target']['dbSystemId']=='green'
            assert body['target']['channelName']!='blue_green'
            assert body['target']['filters']==[{'type':'REPLICATE_DO_DB','value':'erp'}]
            assert entry['retry_token_present']
            self.channels['copied']={**deepcopy(body),'id':'copied','lifecycleState':'INACTIVE'}
            self.channels['copied']['source'].pop('password')
            payload=mutation(deepcopy(self.channels['copied']))
        elif '/channels/' in path and path.endswith('/actions/generateChannelStatus'):payload=mutation()
        elif '/channels/' in path and path.endswith('/channelStatus'):
            payload={'timeUpdated':'2026-10-14T12:00:00Z','channelStatusResult':{'isHealthy':self.health,'isReceivedGtidSetApplied':True,'errors':[],'lagDuration':'PT0S'}}
        elif '/channels/' in path:
            channel_id=path.split('/channels/')[-1]
            if method=='PUT':
                assert entry['if_match']=='contract-etag-'+str(self.etag_reads)
                self.channels[channel_id].update(body);payload=mutation(deepcopy(self.channels[channel_id]))
            elif method=='DELETE':self.channels.pop(channel_id);payload=mutation()
            else:payload=deepcopy(self.channels[channel_id])
        elif '/workRequests/' in path:
            suffix=path.split('/workRequests/')[-1];request_id=suffix.split('/')[0]
            section='errors' if suffix.endswith('/errors') else 'logs' if suffix.endswith('/logs') else 'request'
            if section in self.work_details_errors:
                status_code=self.work_details_errors[section]
                payload={'code':'NotAuthorizedOrNotFound' if status_code==404 else 'NotAuthorized' if status_code==403 else 'InternalError','message':'Injected optional diagnostics failure'}
            elif suffix.endswith('/errors'):payload=[{'code':'CONTRACT_FAILURE','message':'Injected work-request failure'}] if self.requests[request_id]['failure'] else []
            elif suffix.endswith('/logs'):raise AssertionError('Automatic work-request log fetching must remain disabled')
            else:
                state=self.requests[request_id];state['polls']+=1
                status='ACCEPTED' if state['polls']==1 else 'IN_PROGRESS' if state['polls']==2 else 'FAILED' if state['failure'] else 'SUCCEEDED'
                payload={'id':request_id,'status':status,'percentComplete':0 if status=='ACCEPTED' else 50 if status=='IN_PROGRESS' else 100,'operationType':'UPDATE_DB_SYSTEM','compartmentId':'contract-comp','resources':deepcopy(state.get('resources',[]))}
                entry['response_status']=status
        elif path.endswith('/workRequests'):payload=deepcopy(self.work_summaries)
        elif '/secretbundles/' in path:
            assert query('stage')=='CURRENT'
            payload={'secretId':'secret','secretBundleContent':{'contentType':'BASE64','content':base64.b64encode(self.secret.encode()).decode()}}
        elif path.endswith('/secrets') and method=='GET':payload=[{'id':'secret','secretName':'Contract secret','compartmentId':'contract-comp','lifecycleState':'ACTIVE'}]
        elif path.endswith('/secrets') and method=='POST':
            assert body['compartmentId']=='contract-comp' and body['vaultId']=='vault' and body['keyId']=='key'
            assert body['secretContent']['contentType']=='BASE64' and body['secretContent']['stage']=='CURRENT'
            payload={'id':'new-secret','secretName':body['secretName'],'compartmentId':'contract-comp','lifecycleState':'ACTIVE'}
        elif '/secrets/' in path:payload={'id':'secret','compartmentId':'contract-comp','lifecycleState':'ACTIVE'}
        elif path.endswith('/vaults'):payload=[{'id':'vault','displayName':'Contract Vault','compartmentId':'contract-comp','lifecycleState':'ACTIVE'}]
        elif '/vaults/' in path:payload={'id':'vault','compartmentId':'contract-comp','lifecycleState':'ACTIVE','managementEndpoint':'https://contract-management.kms.uk-london-1.oci.oraclecloud.com'}
        elif path.endswith('/keys'):payload=[{'id':'key','displayName':'Contract key','lifecycleState':'ENABLED','algorithm':{'algorithm':'AES','length':32}}]
        else:raise AssertionError('Uncovered SDK HTTP path '+method+' '+path)
        response=requests.Response();response.status_code=status_code;response._content=json.dumps(ids(payload)).encode()
        response.headers.update(response_headers);response.url=url;response.elapsed=timedelta(0)
        response.request=requests.Request(method,url).prepare()
        return response


@pytest.fixture
def harness(tmp_path,monkeypatch):
    cloud=OracleHTTP()
    if os.environ.get('BLUEGREEN_CONTRACT_INSTANCE_PRINCIPAL')=='1':
        service=OCIService()
    else:
        service=OCIService.__new__(OCIService)
        service.signer=Mock(spec=oci.auth.signers.InstancePrincipalsSecurityTokenSigner)
        service.config={'region':'uk-london-1'}
        for attr,cls in [('bg',oci.mysql.BlueGreenDeploymentsClient),('db',oci.mysql.DbSystemClient),('mysql',oci.mysql.MysqlaasClient),('ch',oci.mysql.ChannelsClient),('rep',oci.mysql.ReplicasClient),
                         ('wr',oci.mysql.WorkRequestsClient),('identity',oci.identity.IdentityClient),('vault',oci.vault.VaultsClient),
                         ('secrets',oci.secrets.SecretsClient),('kms',oci.key_management.KmsVaultClient)]:
            setattr(service,attr,cls(service.config,signer=service.signer))
    service.default_compartment=IDS['contract-comp'];service.tenancy_id=IDS['contract-tenancy'];service.region='uk-london-1'
    for attr in ['bg','db','mysql','ch','rep','wr','identity','vault','secrets','kms']:
        monkeypatch.setattr(getattr(service,attr).base_client.session,'request',cloud.request)
    original_kms=oci.key_management.KmsManagementClient
    def management(*args,**kwargs):
        client=original_kms(*args,**kwargs);monkeypatch.setattr(client.base_client.session,'request',cloud.request);return client
    monkeypatch.setattr(oci.key_management,'KmsManagementClient',management)
    import modules.oci_service as adapter
    monkeypatch.setattr(adapter,'time',SimpleNamespace(monotonic=time.monotonic,sleep=lambda seconds:None))
    sql=SQLStandIn(cloud);cloud.observer=sql;cloud.service=service
    schema=None;admin=None
    if os.environ.get('BLUEGREEN_MYSQL_INTEGRATION')=='1':
        from modules.store import Store
        options=json.loads(Path(os.environ.get('BLUEGREEN_TEST_STORE_CONFIG',str(ROOT/'.data/store.json'))).read_text())
        admin=mysql.connector.connect(unix_socket=options['unix_socket'],user='admin',password=BOOTSTRAP_PASSWORD)
        schema='bluegreen_contract_'+uuid.uuid4().hex
        cursor=admin.cursor();cursor.execute('CREATE DATABASE `'+schema+'` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin');cursor.close()
        conf=tmp_path/'mysql-store.json';conf.write_text(json.dumps({'unix_socket':options['unix_socket'],'user':'admin','password':BOOTSTRAP_PASSWORD,'database':schema}));conf.chmod(0o600)
        store=Store(conf)
        with store.db() as db:db.execute("INSERT INTO operators VALUES('admin',FALSE)")
        auth=LocalAuth(options['unix_socket'],store);login_password=BOOTSTRAP_PASSWORD
    else:
        store=HarnessStore(tmp_path/'test.sqlite');auth=FakeAuth(store);login_password='test-private-password'
    app=create_app({'TESTING':True,'DATA_DIR':str(tmp_path),'OCI_READ_ONLY':False},service,sql,store,auth)
    client=app.test_client()
    original_get=client.get
    client.get=lambda path,**kwargs:original_get(route_ids(path),**kwargs)
    client.get('/login')
    with client.session_transaction() as session:csrf=session['login_csrf']
    assert client.post('/login',data={'csrf':csrf,'username':'admin','password':login_password}).status_code==302
    with client.session_transaction() as session:csrf=app.extensions['bluegreen']['sessions'][session['sid']]['csrf']
    def post(path,body):return client.post(route_ids(path),json=ids(body),headers={'X-CSRF-Token':csrf})
    def wait_job(response):
        assert response.status_code==202,response.json
        jid=response.json['job_id'];deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            job=next(j for j in store.history() if j['id']==jid)
            if job['status']!='RUNNING':return job
            time.sleep(.01)
        raise AssertionError('Contract workflow timed out')
    env=SimpleNamespace(cloud=cloud,sql=sql,service=service,app=app,client=client,store=store,post=post,wait=wait_job)
    try:yield env
    finally:
        workflow=app.extensions['bluegreen']['runtime']()[2];workflow.executor.shutdown(wait=True)
        if hasattr(auth,'connections'):auth.connections.close_all()
        evidence=os.environ.get('BLUEGREEN_CONTRACT_EVIDENCE')
        if evidence:
            directory=Path(evidence);directory.mkdir(parents=True,exist_ok=True)
            name=os.environ.get('PYTEST_CURRENT_TEST','contract').split('::')[-1].split(' ')[0]
            name=re.sub(r'[^A-Za-z0-9_.\[\]-]', '_', name)
            (directory/(name+'.json')).write_text(json.dumps({'transport':'intercepted','sdk_version':oci.__version__,
                'embedded_mysql':schema is not None,'real_instance_principal_signing':os.environ.get('BLUEGREEN_CONTRACT_INSTANCE_PRINCIPAL')=='1',
                'http_calls':cloud.calls,'workflow_history':store.history(),'event_transitions':sql.transitions},default=str,indent=2))
        if admin:
            cursor=admin.cursor();cursor.execute('DROP DATABASE `'+schema+'`');cursor.close();admin.close()


def source_review(env,db='blue',target=False):
    data=env.client.get('/api/events/'+db).json
    return {'token':data['token'],'choices':[{'disable_before':not target and e['status']=='ENABLED',
            'after':'ENABLE' if target and i==0 else 'DISABLE' if target else 'KEEP'} for i,e in enumerate(data['events'])]}


def create(env,restore_ha=True,db_update_mode='ETAG'):
    return env.wait(env.post('/api/create',{'source_id':'blue','display_name':'Contract deployment',
        'replication_username':'replication','replication_secret_id':'secret','secret_compartment_id':'contract-comp',
        'mysql_version':'9.5.2','shape_name':'MySQL.8','configuration_id':'green-config','data_storage_size_in_gbs':200,
        'restore_blue_ha':restore_ha,'blue_ha_configuration_id':'ha-config','db_update_mode':db_update_mode,
        'channel_credentials':{'inbound':{'username':'erp','secret_id':'secret'}},'event_review':source_review(env)}))


@pytest.mark.parametrize('resource', ['db', 'channel'])
@pytest.mark.parametrize('failure', ['transient', 'persistent', 'changed', 'missing', 'other'])
def test_conditional_updates_keep_concurrency_protection(harness, resource, failure):
    e=harness
    e.cloud.etag_conflicts=1 if failure in ['transient','changed'] else 100 if failure=='persistent' else 0
    e.cloud.etag_drift=failure=='changed'
    e.cloud.etag_missing=failure=='missing'
    e.cloud.update_error=503 if failure=='other' else None
    action=(lambda:e.service.update_db(IDS['blue'],is_highly_available=False)) if resource=='db' else (lambda:e.service.set_channel(IDS['inbound'],False))
    limit=12 if resource=='db' else 3
    if failure=='transient':
        response=action()
        assert response.headers.get('opc-work-request-id')
        assert [a['status'] for a in response.etag_attempts]==[412,200]
    elif failure=='other':
        with pytest.raises(oci.exceptions.ServiceError) as caught:action()
        assert caught.value.status==503
    else:
        expected={'persistent':f'after {limit} fresh ETag attempts','changed':'resource details changed','missing':'no ETag'}[failure]
        with pytest.raises(RuntimeError,match=expected):action()
    puts=[c for c in e.cloud.calls if c['method']=='PUT']
    assert len(puts)=={'transient':2,'persistent':limit,'changed':1,'missing':0,'other':1}[failure]
    assert len({c['if_match'] for c in puts})==len(puts)
    assert all(c['if_match'] and c['if_match']!='*' for c in puts)
    assert len(e.cloud.requests)==(1 if failure=='transient' else 0)
    client=e.service.db if resource=='db' else e.service.ch
    assert client.base_client.session.headers['Accept-Encoding']=='identity'
    if failure!='transient':
        assert e.cloud.systems['blue']['isHighlyAvailable'] is True
        assert e.cloud.channels['inbound']['isEnabled'] is True


@pytest.mark.parametrize('conflicts', [1, 3, 11, 20])
def test_creation_handles_ha_etag_conflict_before_native_creation(harness, conflicts):
    e=harness;e.cloud.etag_conflicts=conflicts
    job=create(e,restore_ha=False)
    if conflicts<12:
        assert job['status']=='SUCCEEDED',job
        assert e.cloud.created
        assert e.sql.states['blue'][0]['status']=='ENABLED'
        detail=json.loads(next(event['detail'] for event in job['events'] if event['step']=='Disable Blue HA'))
        attempts=detail['etag_attempts']
        assert len(attempts)==conflicts+1 and attempts[-1]['work_request']
        assert all(a['status']==412 for a in attempts[:-1])
    else:
        assert job['status']=='FAILED'
        assert not e.cloud.created
        assert e.cloud.systems['blue']['isHighlyAvailable'] is True
        assert not e.cloud.requests
        assert any(event['step']=='Disable Blue HA' and 'after 12 fresh ETag attempts' in event['detail']
                   for event in job['events'])


def test_final_etag_rejection_checks_for_resource_drift(harness):
    e=harness;e.cloud.etag_conflicts=100
    e.cloud.db_read_changes[13]={'configurationId':'green-config'}
    with pytest.raises(RuntimeError,match='resource details changed') as caught:
        e.service.update_db(IDS['blue'],is_highly_available=False)
    assert len(caught.value.etag_attempts)==12
    puts=[c for c in e.cloud.calls if c['method']=='PUT']
    assert len(puts)==12 and all(c['if_match'] for c in puts)
    assert not e.cloud.requests and e.cloud.systems['blue']['isHighlyAvailable'] is True


@pytest.mark.parametrize('guard', ['scope','inactive','changed','moved','missing_scope','invalid_mode','service_error'])
def test_standard_db_updates_check_scope_state_and_never_replay(harness, guard):
    e=harness;e.cloud.allow_standard_updates=True
    compartment=IDS['denied-comp'] if guard=='scope' else None if guard=='missing_scope' else IDS['contract-comp']
    if guard=='inactive':e.cloud.systems['blue']['lifecycleState']='UPDATING'
    if guard=='changed':e.cloud.db_read_changes[2]={'configurationId':'green-config'}
    if guard=='moved':e.cloud.db_read_changes[2]={'compartmentId':'denied-comp'}
    if guard=='service_error':e.cloud.update_error=503
    expected=oci.exceptions.ServiceError if guard=='service_error' else ValueError
    with pytest.raises(expected):
        e.service.update_db(IDS['blue'],db_update_mode='invalid' if guard=='invalid_mode' else 'STANDARD',
                            expected_compartment=compartment,is_highly_available=False)
    puts=[c for c in e.cloud.calls if c['method']=='PUT']
    assert len(puts)==(1 if guard=='service_error' else 0)
    assert all(c['if_match'] is None for c in puts)
    assert e.cloud.systems['blue']['isHighlyAvailable'] is True


@pytest.mark.parametrize('state', ['running','failed'])
@pytest.mark.parametrize('unavailable', ['logs','errors','both','request','wrong_compartment'])
def test_work_request_diagnostics_preserve_authoritative_status(harness,state,unavailable):
    e=harness;wr=e.cloud.wr()
    if state=='failed':e.cloud.requests[wr].update(failure=True,polls=2)
    e.cloud.work_details_errors={key:404 for key in (['logs','errors'] if unavailable=='both' else [unavailable])}
    response=e.client.get('/api/work-requests/'+wr)
    if unavailable=='request':
        assert response.status_code==400
        assert not any(c['path'].endswith('/logs') or c['path'].endswith('/errors') for c in e.cloud.calls)
    elif unavailable=='wrong_compartment':
        # The selected-compartment check happens before fetching optional details.
        original=e.service.wr.get_work_request
        def outside(*args,**kwargs):
            r=original(*args,**kwargs);r.data.compartment_id=IDS['denied-comp'];return r
        e.service.wr.get_work_request=outside;e.cloud.calls.clear()
        assert e.client.get('/api/work-requests/'+wr).status_code==400
        assert not any(c['path'].endswith('/logs') or c['path'].endswith('/errors') for c in e.cloud.calls)
    else:
        assert response.status_code==200,response.json
        body=response.json
        assert body['request']['status']==('FAILED' if state=='failed' else 'ACCEPTED')
        assert body['errors_available']==('errors' not in e.cloud.work_details_errors)
        assert 'logs' not in body and 'logs_available' not in body
        assert len(body['warnings'])==int('errors' in e.cloud.work_details_errors)
        assert all(w['compartment_id']==IDS['contract-comp'] and '404' in w['error'] for w in body['warnings'])
    assert not any(c['method']!='GET' for c in e.cloud.calls)
    assert not any(c['path'].endswith('/logs') for c in e.cloud.calls)
    assert not e.store.history()


def test_selected_work_requests_use_detail_resources_and_blue_green_relations(harness):
    e=harness;e.cloud.created=True
    e.cloud.channels['native']={**deepcopy(e.cloud.channels['inbound']),'id':'native',
                               'target':{'targetType':'DBSYSTEM','dbSystemId':'green','channelName':'native'}}
    requests={}
    for name,identifier in [('blue','blue'),('green','green'),('deployment','deployment'),('channel','native'),('unrelated','unrelated-db'),('journal',None)]:
        wr=e.cloud.wr();requests[name]=wr
        e.cloud.requests[wr].update(polls=3,resources=[{'identifier':identifier}] if identifier else [])
        e.cloud.work_summaries.append({'id':wr,'status':'SUCCEEDED','operationType':'UPDATE_DBSYSTEM','compartmentId':'contract-comp','percentComplete':100})
    job=e.store.start(IDS['blue'],'create',{'source_id':IDS['blue']})
    event=e.store.event(job,'Native creation');e.store.update_event(event,'SUCCEEDED',work_request=requests['journal']);e.store.finish(job,'SUCCEEDED')
    expected={v for k,v in requests.items() if k!='unrelated'}
    for selected in ['blue','green']:
        detail=e.client.get('/api/db/'+selected).json
        assert {w['id'] for w in detail['work_requests']}==expected
        assert {m['role'] for m in detail['blue_green_context']['members']}=={'BLUE','GREEN'}
        assert next(m for m in detail['blue_green_context']['members'] if m['selected'])['id']==IDS[selected]
    deployment=e.client.get('/api/deployments/deployment')
    assert deployment.status_code==200,deployment.json
    for panel in deployment.json['panels'].values():
        assert {w['id'] for w in panel['work_requests']}==expected
        assert panel['read_replicas']==[]
    gets=[c for c in e.cloud.calls if c['method']=='GET' and '/workRequests/' in c['path']]
    assert len(gets)==len(requests),'Completed resource mappings should be cached across polls/selections'
    assert not any(c['path'].endswith('/logs') for c in e.cloud.calls)
    e.cloud.requests[requests['deployment']]['resources']=[{'identifier':'green'}]
    e.cloud.work_summaries[2]['status']='IN_PROGRESS'
    e.service.work_requests(IDS['contract-comp'])
    assert len([c for c in e.cloud.calls if '/workRequests/' in c['path']])==len(gets)+1


def test_deployment_panels_keep_telemetry_when_optional_discovery_is_denied(harness):
    e=harness;assert create(e,False)['status']=='SUCCEEDED'
    e.cloud.replica_denied=True
    e.service.work_requests=Mock(side_effect=oci.exceptions.ServiceError(404,'NotAuthorizedOrNotFound',{},'Denied'))
    before=len(e.cloud.calls)
    response=e.client.get('/api/deployments/deployment')
    assert response.status_code==200,response.json
    for panel in response.json['panels'].values():
        assert panel['gtid'] and panel['db_system']
        assert '404' in panel['replica_error'] and '404' in panel['work_request_warning']
        assert panel['work_requests']==[]
    assert all(c['method']=='GET' for c in e.cloud.calls[before:])


def test_status_routes_clear_sql_observation_on_connection_failure_and_recover(harness):
    e=harness;assert create(e,False)['status']=='SUCCEEDED'
    sample=e.sql.sample
    assert e.client.get('/api/db/green').json['telemetry_available'] is True
    def fail_green(db_id,**kwargs):
        if db_id==IDS['green']:raise ValueError('SQL connection unavailable')
        return sample(db_id,**kwargs)
    e.sql.sample=fail_green
    for panel in [e.client.get('/api/db/green').json,
                  e.client.get('/api/deployments/deployment').json['panels']['green']]:
        assert panel['telemetry_available'] is False and panel['telemetry_error']=='SQL connection unavailable'
        assert not any(key in panel for key in ['gtid','server_uuid','hostname','uptime','read_only','super_read_only','replication'])
        assert panel['db_system']['id']==IDS['green']
    assert e.client.get('/api/deployments/deployment').json['panels']['blue']['telemetry_available'] is True
    e.sql.sample=sample
    recovered=e.client.get('/api/db/green').json
    assert recovered['telemetry_available'] is True and recovered['gtid'] and 'telemetry_error' not in recovered


@pytest.mark.parametrize('mode',['STANDARD','ETAG'])
def test_ha_only_update_omits_unchanged_configuration(harness,mode):
    e=harness;assert create(e,False)['status']=='SUCCEEDED'
    e.cloud.allow_standard_updates=mode=='STANDARD';e.cloud.calls.clear()
    result=e.wait(e.post('/api/db/green/action',{'action':'ha_on','db_update_mode':mode}))
    assert result['status']=='FAILED' # Unpromoted Green remains protected.
    e.cloud.dep.update(activeDbSystemId='green',switchoverStatus='SWITCHOVER_COMPLETED')
    result=e.wait(e.post('/api/db/green/action',{'action':'ha_on','db_update_mode':mode}))
    assert result['status']=='SUCCEEDED',result
    updates=[c for c in e.cloud.calls if c['method']=='PUT']
    assert len(updates)==1 and updates[0]['body']=={'isHighlyAvailable':True}


def test_ha_parameter_rejection_keeps_safe_service_explanation(harness):
    harness.cloud.allow_standard_updates=True
    e=harness;assert create(e,False)['status']=='SUCCEEDED'
    e.cloud.dep.update(activeDbSystemId='green',switchoverStatus='SWITCHOVER_COMPLETED')
    e.cloud.update_error=400
    result=e.wait(e.post('/api/db/green/action',{'action':'ha_on'}))
    assert result['status']=='FAILED'
    assert 'Injected service failure' in result['events'][-1]['detail']
    assert 'OCI 400' in result['events'][-1]['detail']


def test_ha_catalog_filters_shape_scope_state_and_compatibility(harness):
    e=harness;assert create(e,False)['status']=='SUCCEEDED'
    config=deepcopy(e.cloud.configurations['ha-config'])
    e.cloud.configurations.update({
        'wrong-shape':{**config,'id':'wrong-shape','shapeName':'MySQL.64'},
        'standalone':{**config,'id':'standalone','displayName':'MySQL.8'},
        'deleted':{**config,'id':'deleted','lifecycleState':'DELETED'},
        'other-compartment':{**config,'id':'other-compartment','type':'CUSTOM','compartmentId':'denied-comp','parentConfigurationId':'ha-config'},
        'custom-ha':{**config,'id':'custom-ha','displayName':'Application configuration','type':'CUSTOM','compartmentId':'contract-comp','parentConfigurationId':'ha-config'}})
    e.cloud.calls.clear()
    response=e.client.get('/api/db/green/ha-configurations')
    assert response.status_code==200,response.json
    listed={c['id'] for c in response.json['configurations']}
    assert listed=={IDS['ha-config'],IDS['green-config'],IDS['promoted-ha-config'],'custom-ha'}
    calls=[c for c in e.cloud.calls if c['path'].endswith('/configurations')]
    assert len(calls)==1 and calls[0]['compartment_id']==IDS['contract-comp']
    assert not any(c['path'].endswith('/configurations/other-compartment') for c in e.cloud.calls)
    assert all(c['method']=='GET' for c in e.cloud.calls)


def test_ha_shape_mismatch_is_rejected_before_configuration_or_ha_update(harness):
    e=harness;assert create(e,False)['status']=='SUCCEEDED'
    e.cloud.dep.update(activeDbSystemId='green',switchoverStatus='SWITCHOVER_COMPLETED')
    e.cloud.configurations['ha-config']['shapeName']='MySQL.64'
    e.cloud.calls.clear()
    params={'action':'ha_on','configuration_id':'ha-config'}
    preview=e.post('/api/operation-plan',{'path':'/api/db/'+IDS['green']+'/action','params':params})
    assert preview.status_code==400 and 'shape' in preview.json['error']
    job=e.wait(e.post('/api/db/green/action',params))
    assert job['status']=='FAILED' and 'shape' in job['events'][-1]['detail']
    assert all(c['method']=='GET' for c in e.cloud.calls)


def test_creation_catalog_uses_db_shapes_versions_and_shape_matched_configurations(harness):
    e=harness
    e.cloud.shapes.append({'name':'HeatWave.only','isSupportedFor':['HEATWAVECLUSTER']})
    e.cloud.versions.append({'versionFamily':'Other','versions':[{'version':'8.0.40'},{'version':'26.7.0-mysql-preview-DbCluster'}]})
    e.cloud.configurations['promoted-ha-config']['shapeName']='MySQL.16'
    e.cloud.configurations['promoted-ha-config']['displayName']='MySQL.16.HA'
    response=e.client.get('/api/db/blue/creation-options?shape_name=MySQL.16')
    assert response.status_code==200,response.json
    assert {s['name'] for s in response.json['shapes']}=={'MySQL.8','MySQL.16'}
    assert {v['version'] for v in response.json['versions']}=={'8.4.8','9.5.2','9.7.2'}
    assert {c['id'] for c in response.json['configurations']}=={IDS['promoted-ha-config']}
    assert {c['id'] for c in response.json['ha_configurations']}=={IDS['promoted-ha-config']}
    assert all(c['method']=='GET' for c in e.cloud.calls)
    assert all(c.get('compartment_id') in {None,IDS['contract-comp']} for c in e.cloud.calls)


@pytest.mark.parametrize('case',['different_shape','unknown_shape','unknown_version','downgrade','target_mismatch','promoted_missing','promoted_mismatch','original_shape_optional'])
def test_creation_target_choices_checked_before_any_mutation(harness,case):
    harness.cloud.allow_standard_updates=True
    e=harness
    if case!='original_shape_optional':
        e.cloud.configurations['green-config']['shapeName']='MySQL.16'
        e.cloud.configurations['promoted-ha-config'].update(shapeName='MySQL.16',displayName='MySQL.16.HA')
    params={'source_id':'blue','display_name':'Shape selection','replication_username':'replication',
            'replication_secret_id':'secret','secret_compartment_id':'contract-comp','mysql_version':'9.5.2',
            'shape_name':'MySQL.16','configuration_id':'green-config','promoted_ha_configuration_id':'promoted-ha-config',
            'data_storage_size_in_gbs':200,'channel_credentials':{'inbound':{'username':'erp','secret_id':'secret'}},
            'event_review':source_review(e)}
    if case=='unknown_shape':params['shape_name']='MySQL.invalid'
    if case=='unknown_version':params['mysql_version']='99.0.1'
    if case=='downgrade':params['mysql_version']='8.0.40'
    if case=='target_mismatch':params['configuration_id']='ha-config'
    if case=='promoted_missing':params.pop('promoted_ha_configuration_id')
    if case=='promoted_mismatch':params['promoted_ha_configuration_id']='ha-config'
    if case=='original_shape_optional':
        params.update(shape_name='MySQL.8');params.pop('promoted_ha_configuration_id')
    valid=case in {'different_shape','original_shape_optional'}
    preview=e.post('/api/operation-plan',{'path':'/api/create','params':params})
    assert preview.status_code==(200 if valid else 400),preview.json
    job=e.wait(e.post('/api/create',params))
    assert job['status']==('SUCCEEDED' if valid else 'FAILED'),job
    if not valid:
        assert not any(c['method'] in {'POST','PUT','DELETE'} for c in e.cloud.calls)
        assert not e.sql.transitions
    else:
        assert e.cloud.systems['green']['shapeName']==params['shape_name']


def test_swapped_display_names_keep_original_blue_green_ocids_and_primary(harness):
    e=harness;assert create(e,False)['status']=='SUCCEEDED'
    result=e.wait(e.post('/api/deployments/deployment/action',{'action':'switchover','wait_seconds':300,
        'source_event_review':source_review(e),'target_event_review':source_review(e,'green',True)}))
    assert result['status']=='SUCCEEDED',result
    detail=e.client.get('/api/deployments/deployment').json
    assert detail['deployment']['source_db_system_id']==IDS['blue']
    assert detail['deployment']['target_db_system_id']==detail['deployment']['active_db_system_id']==IDS['green']
    assert detail['panels']['green']['db_system']['display_name']=='Contract Blue'
    assert detail['panels']['blue']['db_system']['display_name']=='Contract Blue-old'
    assert detail['panels']['blue']['server_uuid']==B and detail['panels']['green']['server_uuid']==G
    for selected in ['blue','green']:
        panel=e.client.get('/api/db/'+selected).json
        members={m['role']:m for m in panel['blue_green_context']['members']}
        assert members['BLUE']['id']==IDS['blue'] and members['GREEN']['id']==IDS['green']
        assert members['GREEN']['is_current_primary'] and not members['BLUE']['is_current_primary']


def test_standard_mode_is_explicit_in_preview_and_journal(harness):
    e=harness;e.cloud.allow_standard_updates=True
    params={'action':'ha_off','db_update_mode':'STANDARD'}
    preview=e.post('/api/operation-plan',{'path':'/api/db/'+IDS['blue']+'/action','params':params})
    assert preview.status_code==200 and preview.json['supports_db_update_mode']
    assert preview.json['db_update_mode']=='STANDARD'
    assert any('no atomic ETag version check' in c for c in preview.json['checks'])
    assert not e.store.history() and not any(c['method']=='PUT' for c in e.cloud.calls)
    job=e.wait(e.post('/api/db/blue/action',params))
    assert job['status']=='SUCCEEDED' and job['params']['db_update_mode']=='STANDARD'
    assert job['params']['compartment_id']==IDS['contract-comp']
    assert 'atomic_etag_check' in job['events'][0]['detail']
    assert e.post('/api/db/blue/action',{'action':'ha_off','db_update_mode':'invalid'}).status_code==400
    assert len(e.store.history())==1


@pytest.mark.parametrize('mode', ['ETAG', 'STANDARD'])
def test_full_creation_and_switchover_through_real_sdk(harness, mode):
    e=harness
    e.cloud.allow_standard_updates=mode=='STANDARD'
    # The opt-in standard path succeeds at the HTTP boundary even if every
    # conditional DB update would be rejected. Channel checks retain ETags.
    if mode=='STANDARD':e.cloud.etag_conflicts=100
    assert e.post('/api/observer/blue',{'username':'observer','secret_id':'secret','secret_compartment_id':'contract-comp'}).status_code==200
    catalog=e.client.get('/api/catalog').json;assert catalog['vaults'][0]['id']==IDS['vault']
    assert e.client.get('/api/keys?vault_id=vault').json[0]['id']==IDS['key']
    assert e.post('/api/secrets',{'compartment_id':'contract-comp','vault_id':'vault','key_id':'key','name':'new','password':'contract-value'}).status_code==200
    job=create(e,db_update_mode=mode);assert job['status']=='SUCCEEDED',job
    assert job['params']['db_update_mode']==mode
    if mode=='STANDARD':assert job['events'][0]['step']=='Operator selected standard OCI DB-system updates'
    e.cloud.etag_conflicts=0
    assert e.sql.events('blue')[0]['status']=='ENABLED'
    assert e.sql.events('blue')[1]['status']=='DISABLED'
    assert e.cloud.systems['blue']['isHighlyAvailable'] and not e.cloud.systems['green']['isHighlyAvailable']
    assert not e.cloud.channels['copied']['isEnabled']
    panels=e.client.get('/api/deployments/deployment').json['panels']
    assert set(panels)=={'blue','green'} and panels['blue']['gtid'] and panels['green']['gtid']
    assert not next(d for d in e.client.get('/api/dashboard').json['systems'] if d['id']==IDS['blue'])['eligible']
    fleet={s['id']:s for s in e.client.get('/api/dashboard').json['systems']}
    assert fleet[IDS['blue']]['blue_green_role']=='BLUE' and fleet[IDS['blue']]['is_current_primary']
    assert fleet[IDS['green']]['blue_green_role']=='GREEN'
    db_detail=e.client.get('/api/db/blue').json
    assert db_detail['db_system']['endpoints'] and db_detail['gtid']
    assert db_detail['channels'] and db_detail['work_requests'] is not None
    # Duplicate registration reaches the production exclusion guard and sends no second OCI create request.
    assert create(e)['status']=='FAILED'
    assert sum(c['method']=='POST' and c['path'].endswith('/blueGreenDeployments') for c in e.cloud.calls)==1
    # Operator temporarily opens Green for modifications; switchover returns it to RO through the SDK.
    assert e.wait(e.post('/api/db/green/action',{'action':'read_write','db_update_mode':mode}))['status']=='SUCCEEDED'
    result=e.wait(e.post('/api/deployments/deployment/action',{'action':'switchover','wait_seconds':300,
        'db_update_mode':mode,
        'enable_ha':True,'ha_configuration_id':'promoted-ha-config','activate_channels':True,
        'source_event_review':source_review(e),'target_event_review':source_review(e,'green',True)}))
    assert result['status']=='SUCCEEDED',result
    assert e.cloud.switched and e.cloud.systems['green']['databaseMode']=='READ_WRITE'
    assert e.cloud.systems['green']['isHighlyAvailable'] and e.cloud.channels['copied']['isEnabled']
    assert e.sql.events('green')[0]['status']=='ENABLED' and e.sql.events('green')[1]['status']=='DISABLED'
    assert e.sql.events('blue')[0]['status']=='DISABLED'
    assert e.store.get('verification:'+IDS['deployment'])['extra_green']==G+':1-2'
    assert any(c.get('response_status')=='ACCEPTED' for c in e.cloud.calls)
    assert any(c.get('response_status')=='IN_PROGRESS' for c in e.cloud.calls)
    assert any(c.get('response_status')=='SUCCEEDED' for c in e.cloud.calls)
    assert e.cloud.secret not in json.dumps(e.store.history())
    assert e.cloud.secret.encode() not in e.client.get('/api/dashboard').data
    db_updates=[c for c in e.cloud.calls if c['method']=='PUT' and '/dbSystems/' in c['path']]
    assert db_updates and all((c['if_match'] is None)==(mode=='STANDARD') for c in db_updates)
    assert all(c['if_match'] for c in e.cloud.calls if c['method']=='PUT' and '/channels/' in c['path'])
    for event in result['events']:
        assert event['ended']>=event['started']
        if event.get('work_request'):
            assert e.client.get('/api/work-requests/'+event['work_request']).status_code==200


@pytest.mark.parametrize('case',['unexpected_gtid','green_ahead','green_ha','extra_enabled_channel','unhealthy_native','enabled_green_event'])
def test_precheck_failure_matrix_sends_no_switchover(harness,case):
    e=harness;assert create(e,False)['status']=='SUCCEEDED'
    if case=='unexpected_gtid':e.sql.extra_uuid=True
    if case=='green_ahead':e.sql.green_ahead=True
    if case=='green_ha':e.cloud.green_ha=True
    if case=='extra_enabled_channel':e.cloud.channels['copied']['isEnabled']=True
    if case=='unhealthy_native':e.cloud.health=False
    if case=='enabled_green_event':e.sql.states['green'][0]['status']='ENABLED'
    result=e.wait(e.post('/api/deployments/deployment/action',{'action':'switchover','wait_seconds':300,
        'source_event_review':source_review(e),'target_event_review':source_review(e,'green',True)}))
    assert result['status']=='FAILED'
    assert not any(c['path'].endswith('/actions/switchover') for c in e.cloud.calls)


@pytest.mark.parametrize('status,allowed', [('NOT_STARTED',True),('READY_FOR_SWITCHOVER',True),
    ('VALIDATING',False),('REPLICATING_DATA',False),('SWITCHOVER_IN_PROGRESS',False),
    ('SWITCHOVER_FAILED',False),('SWITCHOVER_COMPLETED',False),('UNKNOWN_ENUM_VALUE',False)])
def test_initial_switchover_state_and_fail_closed_states(harness,status,allowed):
    e=harness;assert create(e,False)['status']=='SUCCEEDED'
    e.cloud.dep['switchoverStatus']=status
    result=e.wait(e.post('/api/deployments/deployment/action',{'action':'precheck'}))
    assert result['status']==('SUCCEEDED' if allowed else 'FAILED'),result
    assert not any(c['path'].endswith('/actions/switchover') for c in e.cloud.calls)
    if not allowed:
        assert status in result['events'][-1]['detail']


def test_detail_panels_read_actual_channel_health_without_generating_status(harness):
    e=harness;assert create(e,False)['status']=='SUCCEEDED'
    before=len(e.cloud.calls)
    panels=e.client.get('/api/deployments/deployment').json['panels']
    green=e.client.get('/api/db/green').json
    for channels in [panels['green']['channels'],green['channels']]:
        native=next(c for c in channels if c['id']==IDS['native'])
        assert native['health']['channel_status_result']['is_healthy'] is True
    assert all(c['method']=='GET' for c in e.cloud.calls[before:])
    e.cloud.health=False
    green=e.client.get('/api/db/green').json
    assert next(c for c in green['channels'] if c['id']==IDS['native'])['health']['channel_status_result']['is_healthy'] is False


def test_stale_event_review_and_work_request_failure_are_journaled(harness):
    e=harness
    review=source_review(e);e.sql.states['blue'][0]['definition']='CALL changed_definition()'
    response=e.post('/api/create',{'source_id':'blue','display_name':'stale','replication_username':'replication',
        'replication_secret_id':'secret','secret_compartment_id':'contract-comp',
        'channel_credentials':{'inbound':{'username':'erp','secret_id':'secret'}},'event_review':review})
    assert e.wait(response)['status']=='FAILED'
    assert not e.cloud.created
    e.cloud.next_failure=True
    result=create(e)
    assert result['status']=='FAILED' and any(x['work_request'] for x in result['events'])
    assert not e.cloud.created


@pytest.mark.parametrize('case',['wrong_endpoint','not_read_write','missing_final_blue_gtid'])
def test_post_switch_checks_block_leftover_processing(harness,monkeypatch,case):
    e=harness;assert create(e,False)['status']=='SUCCEEDED'
    if case=='wrong_endpoint':e.sql.endpoint_wrong=True
    if case=='not_read_write':e.sql.no_rw=True
    if case=='missing_final_blue_gtid':
        original=e.sql.sample
        def missing(db_id,**kwargs):
            sample=original(db_id,**kwargs)
            if e.cloud.switched and ids(db_id,True)=='green':sample['gtid']=B+':1-100,'+G+':1-2'
            return sample
        e.sql.sample=missing
    import modules.workflow as workflow_module
    clock={'value':0}
    def tick():clock['value']+=120;return clock['value']
    monkeypatch.setattr(workflow_module,'time',SimpleNamespace(monotonic=tick,sleep=lambda seconds:None))
    result=e.wait(e.post('/api/deployments/deployment/action',{'action':'switchover','wait_seconds':300,
        'enable_ha':True,'ha_configuration_id':'promoted-ha-config','activate_channels':True,
        'source_event_review':source_review(e),'target_event_review':source_review(e,'green',True)}))
    assert result['status']=='FAILED' and e.cloud.switched
    assert not e.cloud.systems['green']['isHighlyAvailable']
    assert not e.cloud.channels['copied']['isEnabled']
    assert e.store.get('verification:'+IDS['deployment']) is None
    assert not any(x['db_system']=='green' and x['status']=='ENABLED' for x in e.sql.transitions)


def test_standalone_creation_and_exact_gtid_confirmation(harness):
    e=harness;e.cloud.systems['blue']['isHighlyAvailable']=False
    assert create(e,False)['status']=='SUCCEEDED'
    assert not any(c['method']=='PUT' and 'isHighlyAvailable' in c['body'] for c in e.cloud.calls)
    original=e.sql.sample
    def exact(db_id,**kwargs):
        sample=original(db_id,**kwargs)
        if ids(db_id,True)=='green':sample['gtid']=sample['gtid'].split(',')[0]
        return sample
    e.sql.sample=exact
    result=e.wait(e.post('/api/deployments/deployment/action',{'action':'switchover','wait_seconds':300,
        'source_event_review':source_review(e),'target_event_review':source_review(e,'green',True)}))
    assert result['status']=='SUCCEEDED'
    assert e.store.get('verification:'+IDS['deployment'])['exact']


def test_unhealthy_inbound_channel_is_disabled_before_creation(harness):
    e=harness;e.cloud.health=False
    assert create(e,False)['status']=='SUCCEEDED'
    assert not e.cloud.channels['inbound']['isEnabled']
    assert not e.cloud.channels['copied']['isEnabled']
    assert any(c['method']=='PUT' and c['path'].endswith('/channels/'+IDS['inbound']) and c['body'].get('isEnabled') is False for c in e.cloud.calls)


@pytest.mark.parametrize('high_availability',[False,True])
def test_creation_without_inbound_channels(harness,high_availability):
    e=harness
    e.cloud.systems['blue']['isHighlyAvailable']=high_availability
    e.cloud.channels.clear()
    if not high_availability:
        # An empty event inventory still requires the operator's explicit review.
        e.sql.states={'blue':[],'green':[]}
    result=create(e,False)
    assert result['status']=='SUCCEEDED',result
    assert not e.cloud.systems['blue']['isHighlyAvailable']
    assert not e.cloud.systems['green']['isHighlyAvailable']
    assert set(e.cloud.channels)=={'native'}
    assert not any(c['method']=='POST' and c['path'].endswith('/channels') for c in e.cloud.calls)
    ha_updates=[c for c in e.cloud.calls if c['method']=='PUT' and 'isHighlyAvailable' in c['body']]
    assert len(ha_updates)==int(high_availability)
    if high_availability:
        assert e.sql.events('blue')[0]['status']=='ENABLED'
        assert e.sql.events('blue')[1]['status']=='DISABLED'
    else:
        assert e.sql.transitions==[]


def test_compartment_only_iam_never_queries_tenancy_and_allows_creation(harness):
    e=harness;e.cloud.root_denied=True
    response=e.client.get('/api/dashboard')
    assert response.status_code==200
    assert response.json['registration_scope_verified'] is True
    assert response.json['systems']
    assert any(s['eligible'] for s in response.json['systems'])
    assert response.json['discovery_scope']['outside_compartment_discovery'] is False
    assert create(e)['status']=='SUCCEEDED'
    assert not any(c['path'].endswith('/compartments') for c in e.cloud.calls)
    assert all(c.get('compartment_id') in {None,IDS['contract-comp']} for c in e.cloud.calls)
    assert not response.json['registration_warning']


def test_selected_compartment_registration_denial_is_warning_and_blocks_creation(harness):
    e=harness;e.cloud.registration_denied=True
    response=e.client.get('/api/dashboard')
    assert response.status_code==200 and not response.json['registration_scope_verified']
    assert response.json['systems'] and not any(s['eligible'] for s in response.json['systems'])
    assert create(e)['status']=='FAILED'
    assert not any(c['method'] in {'POST','PUT','DELETE'} for c in e.cloud.calls)
    assert response.json['registration_diagnostics'][0]['operation']=='ListBlueGreenDeployments'
    assert response.json['registration_diagnostics'][0]['compartment_id']==IDS['contract-comp']


@pytest.mark.parametrize('state',['ACTIVE','CREATING','DELETING','FAILED','DELETED'])
def test_read_replicas_block_creation_until_deleted(harness,state):
    e=harness
    e.cloud.replicas=[{'id':'replica','displayName':'Read replica','compartmentId':'contract-comp',
                       'dbSystemId':'blue','lifecycleState':state,'ipAddress':'10.0.0.12','mysqlVersion':'8.4.8'}]
    data=e.client.get('/api/dashboard').json
    source=next(s for s in data['systems'] if s['id']==IDS['blue'])
    assert source['eligible']==(state=='DELETED')
    assert len(source['read_replicas'])==int(state!='DELETED')
    result=create(e,False)
    assert result['status']==('SUCCEEDED' if state=='DELETED' else 'FAILED')
    if state!='DELETED':
        assert 'read replicas' in result['events'][-1]['detail']
        assert not any(c['method'] in {'POST','PUT','DELETE'} for c in e.cloud.calls)
        assert not e.sql.transitions


def test_unverified_replica_discovery_blocks_creation(harness):
    e=harness;e.cloud.replica_denied=True
    response=e.client.get('/api/dashboard')
    assert response.status_code==200 and not response.json['replica_scope_verified']
    assert not any(s['eligible'] for s in response.json['systems'])
    assert create(e)['status']=='FAILED'
    assert not any(c['method'] in {'POST','PUT','DELETE'} for c in e.cloud.calls)


def test_deleted_resources_hidden_without_fetching_tombstones(harness):
    e=harness;e.cloud.created=True;e.cloud.systems['green']['lifecycleState']='DELETED'
    e.cloud.channels['inbound']['lifecycleState']='DELETED'
    response=e.client.get('/api/dashboard')
    assert response.status_code==200
    assert [s['id'] for s in response.json['systems']]==[IDS['blue']]
    assert response.json['channels']==[]
    assert not any(c['path'].endswith('/dbSystems/'+IDS['green']) for c in e.cloud.calls)
    assert not any(c['path'].endswith('/channels/'+IDS['inbound']) or c['path'].endswith('/channelStatus') for c in e.cloud.calls)


def test_new_replica_after_inventory_is_rechecked_before_mutation(harness):
    e=harness;inventory=e.service.inventory
    def insert_after_read(compartment):
        result=inventory(compartment)
        e.cloud.replicas=[{'id':'replica','displayName':'New replica','compartmentId':'contract-comp',
                           'dbSystemId':'blue','lifecycleState':'CREATING'}]
        return result
    e.service.inventory=insert_after_read
    result=create(e)
    assert result['status']=='FAILED'
    assert not any(c['method'] in {'POST','PUT','DELETE'} for c in e.cloud.calls)
    assert not e.sql.transitions


def test_changing_to_denied_compartment_returns_scoped_warning_not_error(harness):
    e=harness
    assert e.post('/api/compartment',{'compartment_id':'denied-comp'}).status_code==200
    response=e.client.get('/api/dashboard')
    assert response.status_code==200
    data=response.json
    assert data['compartment_id']==IDS['denied-comp']
    assert data['systems']==[] and data['channels']==[] and data['read_replicas']==[]
    assert data['access_warning'] and data['discovery_scope']['outside_compartment_discovery'] is False
    assert all(w['compartment_id']==IDS['denied-comp'] for w in data['registration_diagnostics'])
    assert not any(c['method'] in {'POST','PUT','DELETE'} for c in e.cloud.calls)


def test_sql_observation_registration_allowed_without_enabling_oci_mutations(harness):
    e=harness;e.app.config['OCI_READ_ONLY']=True
    response=e.post('/api/observer/blue',{'username':'observer','secret_id':'secret','secret_compartment_id':'contract-comp'})
    assert response.status_code==200
    assert e.store.get('observer:'+IDS['blue'])['secret_id']==IDS['secret']
    assert e.client.get('/api/db/blue').json['gtid']
    assert e.post('/api/db/blue/action',{'action':'ha_off'}).status_code==400
    assert not any(c['method'] in {'POST','PUT','DELETE'} for c in e.cloud.calls)


def test_operation_preview_lists_ha_creation_channels_events_without_execution(harness):
    e=harness
    params={'source_id':'blue','display_name':'Reviewed environment','restore_blue_ha':True,
            'blue_ha_configuration_id':'ha-config','event_review':source_review(e)}
    response=e.post('/api/operation-plan',{'path':'/api/create','params':params})
    assert response.status_code==200,response.json
    actions=[c['action'] for c in response.json['changes']]
    assert 'Disable HA' in actions and 'Restore HA after Green creation' in actions
    assert 'Create native blue-green deployment' in actions and 'Create disabled copy on Green' in actions
    assert any('event' in a for a in actions)
    assert response.json['will_mutate_oci'] and response.json['will_mutate_sql']
    assert not e.store.history() and not e.sql.transitions
    assert not any(c['method'] in {'POST','PUT','DELETE'} for c in e.cloud.calls)


def test_operation_preview_secret_never_exposes_password(harness):
    e=harness
    response=e.post('/api/operation-plan',{'path':'/api/secrets','params':{'compartment_id':'contract-comp',
        'vault_id':'vault','key_id':'key','name':'Reviewed secret','password':'never-return-this-password'}})
    assert response.status_code==200
    assert 'never-return-this-password' not in response.get_data(as_text=True)
    assert response.json['changes'][0]['service']=='OCI Vault'
    assert not e.store.history()
    assert not any(c['method'] in {'POST','PUT','DELETE'} for c in e.cloud.calls)


def test_switchover_preview_lists_actual_ha_and_mode_changes_without_execution(harness):
    e=harness;assert create(e)['status']=='SUCCEEDED'
    e.cloud.systems['green']['databaseMode']='READ_WRITE'
    source=source_review(e);target=source_review(e,'green',True)
    e.cloud.calls.clear();e.sql.transitions.clear()
    response=e.post('/api/operation-plan',{'path':'/api/deployments/'+IDS['deployment']+'/action','params':{
        'action':'switchover','enable_ha':True,'ha_configuration_id':'promoted-ha-config',
        'activate_channels':True,'source_event_review':source,'target_event_review':target}})
    assert response.status_code==200,response.json
    actions=[c['action'] for c in response.json['changes']]
    for action in ['Disable HA before switchover','Set database mode to read-only before switchover',
                   'Execute native switchover','Enable HA after promotion','Enable copied replication channel']:
        assert action in actions
    assert not e.cloud.switched and not e.sql.transitions
    assert not any(c['method'] in {'POST','PUT','DELETE'} for c in e.cloud.calls)


@pytest.mark.parametrize('case', ['registered_blue','original_blue','current_blue','current_green','registered_override','explicit_override','no_ha'])
def test_optional_promotion_ha_configuration_preview_and_execution(harness,case):
    harness.cloud.allow_standard_updates=True
    e=harness;assert create(e,False)['status']=='SUCCEEDED'
    key='plan:'+IDS['deployment'];plan=e.store.get(key)
    expected=IDS['ha-config']
    if case in {'original_blue','current_blue','current_green'}:
        plan.pop('blue_ha_configuration_id',None)
    if case in {'current_blue','current_green'}:
        plan['original_ha']=False
    if case=='current_blue':e.cloud.systems['blue']['isHighlyAvailable']=True
    if case=='current_green':expected=e.cloud.service.db_system(IDS['green'])['configuration_id']
    if case=='registered_override':
        plan['promoted_ha_configuration_id']=IDS['promoted-ha-config'];expected=IDS['promoted-ha-config']
    e.store.put(key,plan)
    params={'action':'switchover','wait_seconds':300,'enable_ha':case!='no_ha',
            'ha_configuration_id':None,'source_event_review':source_review(e),
            'target_event_review':source_review(e,'green',True)}
    if case=='explicit_override':params['ha_configuration_id']=IDS['promoted-ha-config'];expected=IDS['promoted-ha-config']
    preview=e.post('/api/operation-plan',{'path':'/api/deployments/'+IDS['deployment']+'/action','params':params})
    assert preview.status_code==200,preview.json
    ha_changes=[c for c in preview.json['changes'] if c['action']=='Enable HA after promotion']
    if case=='no_ha':assert not ha_changes
    else:assert expected in ha_changes[0]['detail']
    e.cloud.calls.clear()
    job=e.wait(e.post('/api/deployments/deployment/action',params))
    assert job['status']=='SUCCEEDED',job
    updates=[c for c in e.cloud.calls if c['method']=='PUT' and c['path'].endswith('/dbSystems/'+IDS['green']) and c['body'].get('isHighlyAvailable') is True]
    if case=='no_ha':assert not updates
    else:
        assert len(updates)==1 and 'configurationId' not in updates[0]['body']
        assert e.cloud.service.db_system(IDS['green'])['configuration_id']==expected
        selected=next(event for event in job['events'] if event['step']=='Selected HA configuration for promotion')
        assert json.loads(selected['detail'])['configuration_id']==expected


@pytest.mark.parametrize('path',['/api/db/'+IDS['blue']+'/action','/api/channels/'+IDS['inbound']+'/action'])
def test_operation_preview_enforces_selected_compartment(harness,path):
    e=harness;assert e.post('/api/compartment',{'compartment_id':'denied-comp'}).status_code==200
    response=e.post('/api/operation-plan',{'path':path,'params':{'action':'ha_off'}})
    assert response.status_code==400 and 'selected compartment' in response.json['error']
    assert not any(c['method'] in {'POST','PUT','DELETE'} for c in e.cloud.calls)


@pytest.mark.parametrize('route,body',[('/api/db/blue/action',{'action':'ha_off'}),
                                     ('/api/channels/inbound/action',{'action':'disable_channel'}),
                                     ('/api/deployments/deployment/action',{'action':'precheck'})])
def test_mutations_cannot_target_another_compartment(harness,route,body):
    e=harness
    assert e.post('/api/compartment',{'compartment_id':'denied-comp'}).status_code==200
    response=e.post(route,body)
    assert response.status_code==400 and 'selected compartment' in response.json['error']
    assert not any(c['method'] in {'POST','PUT','DELETE'} for c in e.cloud.calls)
    assert not e.store.history()


def test_standard_defaults_match_preview_and_ha_create_switchover_execution(harness):
    e=harness;e.cloud.allow_standard_updates=True
    for action in ['ha_off','ha_on']:
        params={'action':action}
        preview=e.post('/api/operation-plan',{'path':'/api/db/'+IDS['blue']+'/action','params':params})
        assert preview.json['db_update_mode']=='STANDARD' and preview.json['default_db_update_mode']=='STANDARD'
        result=e.wait(e.post('/api/db/blue/action',params))
        assert result['status']=='SUCCEEDED' and result['params']['db_update_mode']=='STANDARD'
    params={'source_id':'blue','display_name':'Default mode','replication_username':'replication',
        'replication_secret_id':'secret','secret_compartment_id':'contract-comp','restore_blue_ha':True,
        'mysql_version':'9.5.2','shape_name':'MySQL.8','configuration_id':'green-config','data_storage_size_in_gbs':200,
        'blue_ha_configuration_id':'ha-config','channel_credentials':{'inbound':{'username':'erp','secret_id':'secret'}},
        'event_review':source_review(e)}
    preview=e.post('/api/operation-plan',{'path':'/api/create','params':params})
    assert preview.json['db_update_mode']=='STANDARD'
    result=e.wait(e.post('/api/create',params))
    assert result['status']=='SUCCEEDED' and result['params']['db_update_mode']=='STANDARD'
    params={'action':'switchover','wait_seconds':300,'enable_ha':True,'ha_configuration_id':'promoted-ha-config',
            'source_event_review':source_review(e),'target_event_review':source_review(e,'green',True)}
    preview=e.post('/api/operation-plan',{'path':'/api/deployments/'+IDS['deployment']+'/action','params':params})
    assert preview.json['db_update_mode']=='STANDARD'
    result=e.wait(e.post('/api/deployments/deployment/action',params))
    assert result['status']=='SUCCEEDED' and result['params']['db_update_mode']=='STANDARD'
    db_updates=[c for c in e.cloud.calls if c['method']=='PUT' and '/dbSystems/' in c['path']]
    assert db_updates and all(c['if_match'] is None for c in db_updates)
    assert all(c['if_match'] for c in e.cloud.calls if c['method']=='PUT' and '/channels/' in c['path'])
    for action in ['ha_off','read_only']:
        params={'action':action,'db_update_mode':'ETAG'}
        preview=e.post('/api/operation-plan',{'path':'/api/db/'+IDS['green']+'/action','params':params})
        assert preview.json['db_update_mode']=='ETAG'
