"""Read-only previews for the operator's final confirmation; never executes changes."""
import re
from urllib.parse import unquote
from .oci_service import db_update_mode
from .ha_configuration import promotion_configuration
from .resource_actions import (DB_LIFECYCLE_ACTIONS, confirmation_snapshot, db_lifecycle_blocker,
                               deployment_delete_blocker, require_allowed, switchover_blocker)


def build_plan(service, store, compartment, path, params, review):
    plan={'title':'Review operation','compartment_id':compartment,'changes':[],
          'db_update_mode':db_update_mode(params),
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
        if action in DB_LIFECYCLE_ACTIONS:
            require_allowed(db_lifecycle_blocker(target,action))
            plan['confirmation_resource']=confirmation_snapshot(target)
            resource=name(target)+' ['+target['id']+']'
            policy=target.get('deletion_policy') or {}
            if action=='start':
                plan['title']='Start DB system'
                add(resource,'Start DB system','Resume service on this DB system; normal service billing resumes.')
            elif action=='stop':
                plan['title']='Stop DB system'
                add(resource,'Stop DB system','FAST shutdown. Database connections and replication are interrupted until the DB system is started again.')
            elif action=='disable_delete_protection':
                plan['title']='Disable deletion protection'
                add(resource,'Disable deletion protection','The DB system becomes eligible for deletion. Its backup deletion policy is preserved. This action does not delete the DB system.')
            else:
                plan['title']='Delete DB system permanently'
                add(resource,'Delete DB system and its data permanently','OCI also removes attached replication channels and other associated DB resources. This action cannot be undone.')
                add(resource,'Apply existing backup deletion policy','Automatic backups: '+str(policy.get('automatic_backup_retention') or 'OCI current policy')+
                    '; final backup: '+str(policy.get('final_backup') or 'OCI current policy')+'. Manual backups retain their defined retention period.')
        elif action=='configuration':
            config=service.validate_configuration_apply(target,params.get('configuration_id'))
            plan['confirmation_resource']=confirmation_snapshot(target)
            plan['title']='Apply DB configuration'
            add(name(target)+' ['+target['id']+']','Apply '+name(config)+' ['+config['id']+']',
                'OCI may restart the DB system and interrupt connections. The configuration shape and HA compatibility are checked. '
                'Omitted variables do not reset already persisted values; inherited/default values may still apply.')
        elif action=='events':
            plan['title']='Apply scheduled event choices'
            final_events(target['id'],params.get('event_review'))
        elif action=='ha_on':ha_changes(target,params.get('configuration_id'),'Enable HA')
        elif action in labels:
            add(name(target),*labels[action])
            if action=='read_write':
                plan['conversion_guidance']={
                    'db_system_id':target['id'],
                    'message':'Planning schema changes on Green? Review replica_type_conversions before changing column types or widths while replication continues. Adding an index alone does not require type conversion.',
                    'options':[
                        ['Empty set','Keep strict type matching; no attribute promotion or demotion.'],
                        ['ALL_NON_LOSSY','Allow supported conversions to a type with sufficient range, such as widening an integer.'],
                        ['ALL_LOSSY','Allow supported narrowing conversions that may truncate data. Use only after reviewing the data impact.'],
                        ['ALL_SIGNED','Interpret promoted integer values as signed.'],
                        ['ALL_UNSIGNED','Interpret promoted integer values as unsigned.']],
                    'note':'Signedness options accompany a conversion mode. Selecting both ALL_SIGNED and ALL_UNSIGNED uses signed values where possible, otherwise unsigned. For INT to BIGINT UNSIGNED, review ALL_NON_LOSSY,ALL_UNSIGNED against your actual values. These settings do not permit arbitrary incompatible schema changes. Nothing is changed automatically.',
                    'source':'https://dev.mysql.com/doc/refman/9.7/en/replication-features-differing-tables.html#replication-features-attribute-promotion'}
        else:raise ValueError('Unsupported DB action')
    elif match:=re.fullmatch(r'/api/db/([^/]+)/configuration-versions',path):
        target=db(unquote(match[1]))
        editor,submitted,removed=service.configuration_draft(target,params)
        plan['confirmation_resource']=confirmation_snapshot(target)
        plan['title']='Save configuration '+editor['suggested_name']
        add(editor['suggested_name'],'Create a new HeatWave configuration',
            'Shape: '+target['shape_name']+'. Save only; the DB system is unchanged until a separate Apply confirmation.')
        for key,value in submitted.items():
            if value!=editor['variables'].get(key):
                add(key,'Set configuration variable',value)
        for key in removed:
            add(key,'Omit optional variable from new configuration',editor['removal_warning'])
    elif match:=re.fullmatch(r'/api/channels/([^/]+)/action',path):
        target=channel(unquote(match[1]));action=params.get('action');plan['title']='Update replication channel'
        labels={'disable_channel':'Disable replication channel','enable_channel':'Enable replication channel','delete_channel':'Delete replication channel permanently'}
        if action not in labels:raise ValueError('Unsupported channel action')
        add(name(target),labels[action],'Target DB system: '+target['target']['db_system_id'])
        if action=='enable_channel':add(name(target),'Verify channel health; disable again if unhealthy')
    elif match:=re.fullmatch(r'/api/deployments/([^/]+)/action',path):
        deployment=service.deployment(unquote(match[1]))
        if deployment['compartment_id']!=compartment:raise ValueError('Choose a deployment in the selected compartment')
        action=params.get('action')
        if action=='delete':
            require_allowed(deployment_delete_blocker(deployment))
            plan['title']='Delete Blue/Green deployment only'
            plan['confirmation_resource']=confirmation_snapshot(deployment)
            add(name(deployment)+' ['+deployment['id']+']','Delete deployment only',
                'Both DB systems, their data, backups and any existing replication channel are retained. '
                'A deployment deleted before switchover can no longer be switched over.')
        else:
            blue=db(deployment['source_db_system_id']);green=db(deployment['target_db_system_id'])
        if action=='delete':pass
        elif action=='precheck':
            plan['title']='Run switchover precheck'
            add(name(deployment),'Request fresh native replication channel status','Generate and monitor the channel-status work request')
            plan['checks']+=['Read Blue/Green GTIDs and server identities.','Check Green standalone mode, channel health and event states. No DB configuration or event changes.']
        elif action=='activate_copies':
            plan['title']='Activate copied channels';copied_channels(green)
        elif action=='switchover':
            require_allowed(switchover_blocker(deployment))
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
    if plan['will_mutate_oci']:
        plan['checks'].append('Standard OCI requests. DB-system and channel updates recheck the selected compartment, resource state and unchanged details before sending each update once.')
    return plan
