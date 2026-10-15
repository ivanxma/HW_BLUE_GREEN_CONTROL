"""Read-only previews for the operator's final confirmation; never executes changes."""
import re
from urllib.parse import unquote
from .oci_service import db_update_mode
from .ha_configuration import promotion_configuration


def build_plan(service, store, compartment, path, params, review):
    plan={'title':'Review operation','compartment_id':compartment,'changes':[],
          'checks':['The workflow rechecks current resource state before applying changes.'],
          'will_mutate_oci':False,'will_mutate_sql':False}
    def add(resource,action,detail='',kind='MySQL HeatWave'):
        plan['changes'].append({'service':kind,'resource':resource,'action':action,'detail':str(detail or '')})
        plan['will_mutate_oci'] |= kind in {'MySQL HeatWave','OCI Vault'}
        plan['will_mutate_sql'] |= kind=='MySQL SQL'
    def db(ocid):
        value=service.db_system(ocid)
        if value['compartment_id']!=compartment:raise ValueError('Choose a DB system in the selected compartment')
        return value
    def name(value):return value.get('display_name') or value['id']
    def ha_changes(target, configuration, action, source='Selected HA configuration'):
        configuration = configuration or target.get('configuration_id')
        selected = service.ha_configuration(target, configuration)
        if configuration != target.get('configuration_id'):
            add(name(target),'Apply HA configuration before enabling HA',str(configuration)+'; changing configuration may restart the DB system')
        add(name(target),action,source+': '+str(selected['display_name'])+' · '+str(configuration)+'; enable HA without resubmitting the configuration')
    def channel(ocid):
        value=service.channel(ocid)
        if value['compartment_id']!=compartment:raise ValueError('Choose a channel in the selected compartment')
        db(value['target']['db_system_id'])
        return value
    def event_before(db_id,value,mode):
        reviewed=review(value,db_id,mode)
        label=name(db(db_id)) if any(c.get('disable_before') for c in reviewed['choices']) else ''
        for c in reviewed['choices']:
            if c.get('disable_before'):
                add(c['event_schema']+'.'+c['event_name'],'Disable event before execution',label,'MySQL SQL')
        return reviewed
    def final_events(db_id,value):
        choices=review(value,db_id,'switch')['choices']
        label=name(db(db_id)) if any(c['after']!='KEEP' for c in choices) else ''
        for c in choices:
            if c['after']!='KEEP':
                add(c['event_schema']+'.'+c['event_name'],c['after'].title()+' event after promotion',label,'MySQL SQL')
    def copied_channels(green):
        for c in service.channels(green['id'],compartment):
            if store.get('copy:'+c['id']):
                add(name(c),'Enable copied replication channel',name(green))
                add(name(c),'Verify channel health; disable again if unhealthy')

    if path=='/api/create':
        blue=db(params['source_id']);plan['title']='Create blue-green environment'
        if service.read_replicas(blue['id'],compartment):raise ValueError('Blue-green creation is not supported with read replicas')
        service.validate_creation_target(blue,params)
        events=event_before(blue['id'],params.get('event_review'),'create')
        if blue.get('is_highly_available'):add(name(blue),'Disable HA','Convert Blue to standalone before native creation')
        source_channels=service.channels(blue['id'],compartment)
        for c in source_channels:
            add(name(c),'Disable source channel if fresh health confirms it is unhealthy')
        add(params.get('display_name','New deployment'),'Create native blue-green deployment',
            'Create Green DB system and Blue-to-Green replication channel. Target version: '+str(params.get('mysql_version') or blue.get('mysql_version'))+
            '; shape: '+str(params.get('shape_name') or blue.get('shape_name'))+'; configuration: '+str(params.get('configuration_id') or blue.get('configuration_id')))
        add('New Green DB system','Set database mode to read-only')
        for c in source_channels:
            add(name(c),'Create disabled copy on Green','Preserve source/target filters; choose a name distinct from the native channel')
        if blue.get('is_highly_available') and params.get('restore_blue_ha'):
            ha_changes(blue,params.get('blue_ha_configuration_id'),'Restore HA after Green creation')
        for c in events['choices']:
            if c.get('disable_before'):
                add(c['event_schema']+'.'+c['event_name'],'Restore original enabled state after successful creation',name(blue),'MySQL SQL')
        plan['checks']+=['Verify source eligibility and absence of read replicas.','Validate reviewed events and registered Vault secret references.']
    elif path=='/api/secrets':
        target=params.get('compartment_id')
        if target not in {c['id'] for c in service.compartments()}:raise ValueError('Choose an accessible secret compartment')
        plan['compartment_id']=target
        plan['title']='Create Vault secret'
        add(params.get('name','New secret'),'Create secret in OCI Vault','Vault: '+str(params.get('vault_id'))+'; encryption key: '+str(params.get('key_id')),'OCI Vault')
    elif match:=re.fullmatch(r'/api/observer/([^/]+)',path):
        blue=db(unquote(match[1]));plan['title']='Save SQL observation profile'
        add(name(blue),'Save local SQL username and Vault secret reference','Username: '+str(params.get('username'))+'; secret OCID: '+str(params.get('secret_id')),'Embedded registration database')
    elif match:=re.fullmatch(r'/api/db/([^/]+)/action',path):
        target=db(unquote(match[1]));action=params.get('action');plan['title']='Update '+name(target)
        labels={'ha_off':('Disable HA','Convert to standalone'),'ha_on':('Enable HA',params.get('configuration_id')),
                'read_only':('Set database mode to read-only','Writes will be blocked'),
                'read_write':('Set database mode to read-write','Allow temporary SQL modifications')}
        if action=='events':
            plan['title']='Apply scheduled event choices'
            final_events(target['id'],params.get('event_review'))
        elif action=='ha_on':ha_changes(target,params.get('configuration_id'),'Enable HA')
        elif action in labels:add(name(target),*labels[action])
        else:raise ValueError('Unsupported DB action')
    elif match:=re.fullmatch(r'/api/channels/([^/]+)/action',path):
        target=channel(unquote(match[1]));action=params.get('action');plan['title']='Update replication channel'
        labels={'disable_channel':'Disable replication channel','enable_channel':'Enable replication channel','delete_channel':'Delete replication channel permanently'}
        if action not in labels:raise ValueError('Unsupported channel action')
        add(name(target),labels[action],'Target DB system: '+target['target']['db_system_id'])
        if action=='enable_channel':add(name(target),'Verify channel health; disable again if unhealthy')
    elif match:=re.fullmatch(r'/api/deployments/([^/]+)/action',path):
        deployment=service.deployment(unquote(match[1]))
        if deployment['compartment_id']!=compartment:raise ValueError('Choose a deployment in the selected compartment')
        blue=db(deployment['source_db_system_id']);green=db(deployment['target_db_system_id']);action=params.get('action')
        if action=='precheck':
            plan['title']='Run switchover precheck'
            add(name(deployment),'Request fresh native replication channel status','Generate and monitor the channel-status work request')
            plan['checks']+=['Read Blue/Green GTIDs and server identities.','Check Green standalone mode, channel health and event states. No DB configuration or event changes.']
        elif action=='activate_copies':
            plan['title']='Activate copied channels';copied_channels(green)
        elif action=='switchover':
            plan['title']='Switch over blue-green deployment'
            event_before(blue['id'],params.get('source_event_review'),'switch')
            # Target choices select final states; prechecks require Green events disabled before handoff.
            review(params.get('target_event_review'),green['id'],'switch')
            if blue.get('is_highly_available'):add(name(blue),'Disable HA before switchover','Convert Blue to standalone')
            if green.get('database_mode')!='READ_ONLY':add(name(green),'Set database mode to read-only before switchover')
            add(name(deployment),'Execute native switchover','OCI drains replication and moves the original primary endpoint to Green; clients may need to reconnect')
            plan['checks']+=['Validate GTIDs, healthy native channel, standalone/read-only Green and other channels disabled.',
                              'Verify original endpoint identifies Green, final Blue GTIDs are retained, and promoted Green is read-write.']
            if params.get('enable_ha'):
                ha = promotion_configuration(params, store.get('plan:' + deployment['id'], {}), blue, green)
                ha_changes(green,ha['configuration_id'],'Enable HA after promotion',ha['source'])
            if params.get('activate_channels'):copied_channels(green)
            final_events(green['id'],params.get('target_event_review'))
        else:raise ValueError('Unsupported deployment action')
    else:raise ValueError('Unsupported operation preview')
    standard_default = path == '/api/create' or (params.get('action') in {'ha_on','ha_off'} and bool(re.fullmatch(r'/api/db/[^/]+/action', path))) or (params.get('action') == 'switchover' and bool(re.fullmatch(r'/api/deployments/[^/]+/action', path)))
    plan['default_db_update_mode'] = 'STANDARD' if standard_default else 'ETAG'
    mode = db_update_mode(params, default=plan['default_db_update_mode'])
    plan['supports_db_update_mode'] = (path == '/api/create' or
        bool(re.fullmatch(r'/api/db/[^/]+/action', path) and params.get('action') in {'ha_off','ha_on','read_only','read_write'}) or
        bool(re.fullmatch(r'/api/deployments/[^/]+/action', path) and params.get('action') == 'switchover'))
    plan['db_update_mode'] = mode
    if mode == 'STANDARD' and not plan['supports_db_update_mode']:
        raise ValueError('Standard OCI DB-system updates do not apply to this operation')
    if plan['supports_db_update_mode']:
        plan['checks'].append('DB-system updates: '+(
            'STANDARD OCI — omit optional If-Match; no atomic ETag version check. Verify ACTIVE state, selected compartment and unchanged details immediately before each update. Send each update once; record this choice in history.'
            if mode == 'STANDARD' else
            'ETAG PROTECTED — OCI checks the resource version atomically. No automatic fallback to standard updates.'))
    return plan
