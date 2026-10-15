"""Read-only sample dashboard; never creates cloud resources."""
from copy import deepcopy
from .oci_service import OCIService

B = '11111111-1111-1111-1111-111111111111'
G = '22222222-2222-2222-2222-222222222222'


class DemoService:
    demo = True
    region = 'Demo region'
    default_compartment = 'demo-compartment'

    def __init__(self):
        self.systems = [dict(id='demo-blue', display_name='Orders · production', is_highly_available=True,
                             ip_address='10.0.1.20', hostname_label='orders-blue', lifecycle_state='ACTIVE',
                             compartment_id=self.default_compartment, mysql_version='8.4.8', port=3306,
                             shape_name='MySQL.8', configuration_id='demo-config', data_storage_size_in_gbs=200,
                             database_mode='READ_WRITE'),
                        dict(id='demo-green', display_name='Orders · upgrade', is_highly_available=False,
                             ip_address='10.0.1.21', hostname_label='orders-green', lifecycle_state='ACTIVE',
                             compartment_id=self.default_compartment, mysql_version='9.5.2', port=3306,
                             shape_name='MySQL.8', configuration_id='demo-config', data_storage_size_in_gbs=200,
                             database_mode='READ_ONLY'),
                        dict(id='demo-standalone', display_name='Reporting · standalone', is_highly_available=False,
                             lifecycle_state='ACTIVE', compartment_id=self.default_compartment, mysql_version='8.4.8',
                             ip_address='10.0.1.30', hostname_label='reporting', database_mode='READ_WRITE', port=3306),
                        dict(id='demo-ha', display_name='Billing · HA', is_highly_available=True,
                             lifecycle_state='UPDATING', compartment_id=self.default_compartment, mysql_version='8.4.8',
                             ip_address='10.0.1.40', hostname_label='billing', database_mode='READ_WRITE', port=3306)]
        self.dep = dict(id='demo-deployment', display_name='Orders upgrade', compartment_id=self.default_compartment,
                        source_db_system_id='demo-blue', target_db_system_id='demo-green', active_db_system_id='demo-blue',
                        replication_channel_id='demo-native', lifecycle_state='ACTIVE', switchover_status='READY_FOR_SWITCHOVER')
        self.channel_list = [dict(id='demo-native', display_name='Blue → Green', lifecycle_state='ACTIVE', is_enabled=True,
                                 compartment_id=self.default_compartment, target={'db_system_id': 'demo-green', 'channel_name': 'blue_green'},
                                 source={'hostname': '10.0.1.20', 'username': 'replication'},
                                 health={'channel_status_result': {'is_healthy': True, 'lag_duration': 'PT0S'}}),
                             dict(id='demo-copy', display_name='ERP inbound · green', lifecycle_state='INACTIVE', is_enabled=False,
                                  compartment_id=self.default_compartment, target={'db_system_id': 'demo-green', 'channel_name': 'erp'},
                                  source={'hostname': '10.0.2.10', 'username': 'erp_replication'}),
                             dict(id='demo-error', display_name='Legacy inbound · needs repair', lifecycle_state='INACTIVE', is_enabled=False,
                                  compartment_id=self.default_compartment, target={'db_system_id': 'demo-standalone', 'channel_name': 'legacy'},
                                  source={'hostname': '10.0.2.15'},
                                  health={'channel_status_result': {'is_healthy': False, 'errors': ['Source connection unavailable']}})]

    def compartments(self):
        return [{'id': self.default_compartment, 'name': 'HeatWave operations · demo'}]

    def inventory(self, compartment):
        return deepcopy({'systems': self.systems, 'deployments': [self.dep], 'channels': self.channel_list,
                         'work_requests': [{'id': 'demo-work-request', 'status': 'IN_PROGRESS', 'operation_type': 'UPDATE_DB_SYSTEM',
                                            'percent_complete': 65, 'resources': [{'identifier': 'demo-ha'}]}]})

    def list_deployments(self, compartment):
        return self.inventory(compartment)['deployments']

    def db_system(self, db_id):
        return deepcopy(next(x for x in self.systems if x['id'] == db_id))

    def configuration(self, db):
        return {'id': db.get('configuration_id'), 'display_name': db.get('shape_name', 'Demo configuration') + ('.HA' if db.get('is_highly_available') else '')}

    def deployment(self, db_id):
        if db_id != self.dep['id']:
            raise ValueError('Unknown deployment')
        return deepcopy(self.dep)

    def channels(self, db_id, compartment):
        return [deepcopy(c) for c in self.channel_list if c['target']['db_system_id'] == db_id]

    def channel(self, channel_id):
        return deepcopy(next(c for c in self.channel_list if c['id']==channel_id))

    def channel_health(self, channel_id, refresh=False, wait=None):
        return self.channel(channel_id).get('health', {})

    def read_replicas(self, db_id, compartment):
        return []

    def secret_catalog(self, compartment):
        return [{'id': 'demo-secret', 'secret_name': 'heatwave-replication'}]

    def vault_catalog(self, compartment):
        return []

    def key_catalog(self, *args):
        return []

    def ha_configuration(self, db, configuration_id):
        return {'id':configuration_id,'display_name':db['shape_name']+'.HA','shape_name':db['shape_name'],
                'type':'DEFAULT','compartment_id':None}

    def ha_configurations(self, db):
        return {'configurations':[self.ha_configuration(db,db['configuration_id'])],
                'current_configuration_id':db['configuration_id'],'shape_name':db['shape_name'],
                'compartment_id':db['compartment_id'],'information':[]}

    validate_creation_target = OCIService.validate_creation_target

    def creation_options(self, source, shape_name=None):
        shape = shape_name or source['shape_name']
        configs = [{'id':source['configuration_id'] if shape==source['shape_name'] else 'demo-config-16',
                    'display_name':shape,'shape_name':shape,'type':'DEFAULT','lifecycle_state':'ACTIVE'}]
        ha = [{'id':source['configuration_id'] if shape==source['shape_name'] else 'demo-config-16-ha',
               'display_name':shape+'.HA','shape_name':shape,'type':'DEFAULT','lifecycle_state':'ACTIVE'}]
        return {'shapes':[{'name':n,'cpu_core_count':8,'memory_size_in_gbs':64} for n in [source['shape_name'],'MySQL.16']],
                'versions':[{'version':v,'version_family':'Demo'} for v in [source['mysql_version'],'9.7.2']],
                'configurations':configs,'ha_configurations':ha,'information':[],
                'shape_name':shape,'source_shape_name':source['shape_name'],
                'source_configuration_id':source['configuration_id'],'compartment_id':source['compartment_id']}

    def work_request(self, *args):
        return {'request': self.inventory(self.default_compartment)['work_requests'][0], 'errors': [],
                'logs': [{'message': 'HA configuration update in progress'}]}

    def work_requests(self, compartment):
        return self.inventory(compartment)['work_requests']


class DemoObserver:
    def __init__(self, service):
        self.service = service

    def sample(self, db_id, **kwargs):
        green = db_id == 'demo-green'
        return {'gtid': B + ':1-248610' + (',' + G + ':1-4' if green else ''),
                'mysql_version': self.service.db_system(db_id).get('mysql_version'), 'version_comment': 'MySQL demo',
                'database_mode': 'READ_ONLY' if green else 'READ_WRITE', 'access_mode': 'ONLINE', 'offline_mode': 0,
                'server_uuid': G if green else B, 'hostname': 'orders-green' if green else 'orders-blue',
                'uptime': 32400 if green else 1824400, 'read_only': green, 'super_read_only': green,
                'replication': [{'channel_name': 'blue_green', 'service_state': 'ON', 'last_error_number': 0}] if green else [],
                'db_system': self.service.db_system(db_id)}

    def global_values(self, db_id, kind):
        sample = self.sample(db_id)
        if kind == 'variables':
            names = ['mysql_version', 'version_comment', 'read_only', 'super_read_only', 'offline_mode', 'hostname', 'server_uuid']
        elif kind == 'status':
            names = ['uptime']
        else:
            raise ValueError('Choose Variables or Status')
        return [{'name': name, 'value': str(sample[name])} for name in names]

    def events(self, db_id):
        return [dict(event_schema='orders', event_name='daily_rollup', status='REPLICA_SIDE_DISABLED' if db_id == 'demo-green' else 'ENABLED',
                     definer='event_admin@%', event_type='RECURRING', interval_value='1', interval_field='DAY',
                     time_zone='UTC', definition='CALL refresh_daily_rollup()', last_executed=None),
                dict(event_schema='orders', event_name='archive_old_orders', status='DISABLED', definer='event_admin@%',
                     event_type='RECURRING', interval_value='1', interval_field='WEEK', time_zone='UTC',
                     definition='CALL archive_orders()', last_executed=None)]
