from copy import deepcopy
from types import SimpleNamespace

import oci

from modules.demo import DemoService, DemoObserver, B, G
from modules.events import fingerprint
from modules.oci_service import OCIService
from sqlite_store import Store
from modules.workflow import Workflow


class SimulatedOCI(DemoService):
    """Stateful fake, with the same response/work-request boundary as the SDK."""
    demo=False

    def __init__(self):
        super().__init__()
        self.created=False
        self.switched=False
        self.copied=[]
        self.channel_list=[dict(id='existing-inbound',display_name='ERP',is_enabled=True,
                               compartment_id=self.default_compartment,lifecycle_state='ACTIVE',
                               source={'username':'erp','hostname':'10.0.2.10'},
                               target={'db_system_id':'demo-blue','channel_name':'blue_green'})]
        self.systems[0]['endpoints']=[{'ip_address':'10.0.1.20','hostname':'blue.internal', 'modes':['READ','WRITE']}]

    def inventory(self, compartment):
        payload=super().inventory(compartment)
        payload['deployments']=[deepcopy(self.dep)] if self.created else []
        return payload

    def channel(self, channel_id):
        return deepcopy(next(c for c in self.channel_list if c['id']==channel_id))

    def assert_secret_compartment(self,*args):pass

    def channel_health(self,*args):
        return {'channel_status_result':{'is_healthy':True,'errors':[]}}

    def response(self, data=None):
        return SimpleNamespace(data=data,headers={'opc-work-request-id':'test-work'})

    def wait(self,response,on_request,**kwargs):on_request('test-work')

    def update_db(self, db_id, **options):
        next(db for db in self.systems if db['id']==db_id).update(options)
        return self.response()

    def set_channel(self, channel_id, enabled):
        next(c for c in self.channel_list if c['id']==channel_id)['is_enabled']=enabled
        return self.response()

    def create_deployment(self,p,token):
        assert not self.db_system(p['source_id'])['is_highly_available']
        self.created=True
        self.channel_list.append(dict(id='demo-native',display_name='Native',is_enabled=True,
                                     lifecycle_state='ACTIVE',compartment_id=self.default_compartment,
                                     source={'username':p['replication_username']},
                                     target={'db_system_id':'demo-green','channel_name':'blue_green'}))
        return self.response(SimpleNamespace(id=self.dep['id']))

    def copy_channel(self, channel, green_id, credential, channel_name, token):
        assert channel_name != 'blue_green'
        copy=deepcopy(channel)
        copy.update(id='copied-inbound',is_enabled=False)
        copy['target'].update(db_system_id=green_id,channel_name=channel_name)
        self.channel_list.append(copy);self.copied.append(copy)
        return self.response(SimpleNamespace(id=copy['id']))

    def switch(self, deployment_id,seconds):
        assert not self.db_system('demo-blue')['is_highly_available']
        assert self.db_system('demo-green')['database_mode']=='READ_ONLY'
        self.switched=True
        self.dep.update(active_db_system_id='demo-green',switchover_status='SWITCHOVER_COMPLETED')
        self.update_db('demo-green',database_mode='READ_WRITE')
        self.channel_list=[c for c in self.channel_list if c['id']!='demo-native']
        return self.response()


class SimulatedSQL(DemoObserver):
    def __init__(self,service):
        super().__init__(service)
        self.states={db:super().events(db) for db in ['demo-blue','demo-green']}
        self.transitions=[]

    def events(self, db_id):return deepcopy(self.states[db_id])

    def set_event(self,db_id,schema,name,enabled):
        e=next(e for e in self.states[db_id] if e['event_schema']==schema and e['event_name']==name)
        e['status']='ENABLED' if enabled else 'DISABLED'
        self.transitions.append((db_id,name,e['status']))
        return {'status':e['status'],'event_name':name}

    def sample(self,db_id,**kwargs):
        sample=super().sample(db_id)
        sample['read_only']=sample['super_read_only']=self.service.db_system(db_id)['database_mode']=='READ_ONLY'
        if self.service.switched:
            sample['gtid']=B+':1-248611'+(','+G+':1-4' if db_id=='demo-green' else '')
        return sample


def review(events,after='RESTORE'):
    return {'fingerprint':fingerprint(events), 'choices':[{'event_schema':e['event_schema'],'event_name':e['event_name'],
            'disable_before':e['status']=='ENABLED', 'after':after} for e in events]}


def test_full_ha_creation_event_restoration_and_switch(tmp_path):
    service=SimulatedOCI();observer=SimulatedSQL(service);store=Store(tmp_path/'state.db')
    workflow=Workflow(service,observer,store)
    original=observer.events('demo-blue')
    p={'source_id':'demo-blue','compartment_id':'demo-compartment','secret_compartment_id':'demo-compartment',
       'replication_username':'replication','replication_secret_id':'secret-ref','display_name':'Deployment',
       'restore_blue_ha':True,'blue_ha_configuration_id':'demo-config',
       'channel_credentials':{'existing-inbound':{'username':'erp','secret_id':'erp-secret-ref'}},
       'event_review':review(original)}
    job=store.start('demo-blue','create',p)
    workflow.run(job,'create','demo-blue',p)
    assert store.history()[0]['status']=='SUCCEEDED',store.history()
    assert observer.events('demo-blue')==original
    assert service.db_system('demo-blue')['is_highly_available']
    assert not service.db_system('demo-green')['is_highly_available']
    assert not service.copied[0]['is_enabled']
    assert store.get('plan:demo-deployment')['original_events']==original
    assert observer.transitions==[('demo-blue','daily_rollup','DISABLED'),('demo-blue','daily_rollup','ENABLED')]
    target=review(observer.events('demo-green'),'DISABLE')
    target['choices'][0]['after']='ENABLE'
    source=review(observer.events('demo-blue'),'KEEP')
    params={'wait_seconds':300,'enable_ha':True,'ha_configuration_id':'demo-config','activate_channels':True,
            'source_event_review':source,'target_event_review':target}
    job=store.start('demo-deployment','switchover',params)
    workflow.run(job,'switchover','demo-deployment',params)
    assert store.history()[0]['status']=='SUCCEEDED',store.history()
    assert service.switched
    assert service.db_system('demo-green')['is_highly_available']
    assert service.db_system('demo-green')['database_mode']=='READ_WRITE'
    assert service.channel('copied-inbound')['is_enabled']
    assert observer.events('demo-green')[0]['status']=='ENABLED'
    assert observer.events('demo-green')[1]['status']=='DISABLED'
    assert observer.events('demo-blue')[0]['status']=='DISABLED'
    verification=store.get('verification:demo-deployment')
    assert verification['ok'] and not verification['missing_on_green']
    assert verification['extra_green']==G+':1-4'
    steps=[e['step'] for e in store.history()[0]['events']]
    assert steps.index('Verify VIP, hostname, GTID and read-write')<steps.index('Enable HA on promoted Green')
    assert steps.index('Enable HA on promoted Green')<steps.index('Apply selected event state')


def test_creation_stale_event_review_changes_nothing(tmp_path):
    service=SimulatedOCI();observer=SimulatedSQL(service);store=Store(tmp_path/'state.db');workflow=Workflow(service,observer,store)
    p={'source_id':'demo-blue','compartment_id':'demo-compartment','secret_compartment_id':'demo-compartment',
       'replication_secret_id':'secret-ref','channel_credentials':{'existing-inbound':{'username':'erp','secret_id':'ref'}},
       'event_review':review(observer.events('demo-blue'))}
    observer.states['demo-blue'][0]['status']='DISABLED'
    job=store.start('demo-blue','create',p);workflow.run(job,'create','demo-blue',p)
    assert store.history()[0]['status']=='FAILED'
    assert not service.created and service.db_system('demo-blue')['is_highly_available']
    assert not observer.transitions


def test_sdk_payloads_use_supported_models_and_keep_password_server_side():
    from unittest.mock import Mock
    service=OCIService.__new__(OCIService)
    service.bg=Mock();service.secret_value=Mock(return_value='vault-password-only-server-side')
    p={'compartment_id':'comp','source_id':'db','display_name':'test','replication_username':'repl',
       'replication_secret_id':'ref','configuration_id':'config','data_storage_size_in_gbs':200}
    service.create_deployment(p,'request-token')
    payload=service.bg.create_blue_green_deployment.call_args.args[0]
    assert isinstance(payload,oci.mysql.models.CreateBlueGreenDeploymentDetails)
    assert payload.channel_details.source_password=='vault-password-only-server-side'
    assert payload.channel_details.ssl_mode=='REQUIRED'
    assert payload.target_db_system_details.data_storage_size_in_gbs==200
    assert 'vault-password-only-server-side' not in str(p)
    service.db=Mock()
    service.db.base_client.session.headers={}
    service.db.get_db_system.return_value=SimpleNamespace(data=oci.mysql.models.DbSystem(id='db'),headers={'etag':'test-etag'})
    service.update_db('db',database_mode='READ_ONLY')
    assert service.db.update_db_system.call_args.args[1].database_mode=='READ_ONLY'


def test_vault_catalog_uses_actual_sdk_argument_validation():
    from unittest.mock import Mock
    service=OCIService.__new__(OCIService)
    service.kms=oci.key_management.KmsVaultClient({'region':'uk-london-1'},signer=Mock(spec=oci.auth.signers.InstancePrincipalsSecurityTokenSigner))
    service.kms.base_client.call_api=Mock(return_value=SimpleNamespace(data=[
        oci.key_management.models.VaultSummary(id='active',lifecycle_state='ACTIVE'),
        oci.key_management.models.VaultSummary(id='deleting',lifecycle_state='DELETING')],headers={}))
    assert [v['id'] for v in service.vault_catalog('compartment')]==['active']
