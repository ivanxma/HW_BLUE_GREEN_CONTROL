"""Explicit, journaled state transitions around the native OCI blue-green service."""
import hashlib
import json
import threading
import time
import oci
from concurrent.futures import ThreadPoolExecutor

from .gtid import compare
from .events import validate_plan
from .ha_configuration import promotion_configuration
from .oci_service import public_error, db_update_mode
from .resource_actions import require_allowed, switchover_blocker


class Workflow:
    def __init__(self, service, observer, store):
        self.service, self.observer, self.store = service, observer, store
        # A single Compute worker serializes all operations, including cross-resource actions.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='bluegreen')
        self.lock = threading.Lock()

    def submit(self, kind, resource, params):
        with self.lock:
            if any(j['status'] == 'RUNNING' for j in self.store.history()):
                raise ValueError('An operation is in progress; wait before starting another')
            for previous in self.store.history():
                if previous['status'] in {'FAILED', 'INTERRUPTED'}:
                    for event in previous['events']:
                        if event.get('work_request') and event['status'] in {'FAILED', 'INTERRUPTED'}:
                            status = self.service.work_request(event['work_request'])['request']['status']
                            if status not in {'SUCCEEDED', 'FAILED', 'CANCELED', 'CANCELLED'}:
                                raise ValueError('A previous OCI work request is still running; reconcile it before another operation')
            job = self.store.start(resource, kind, params)
            self.executor.submit(self.run, job, kind, resource, params)
        return job

    def run(self, job, kind, resource, params):
        try:
            db_update_mode(params)
            if kind in {'create', 'switchover', 'update_db'}:
                self.step(job, 'Standard OCI DB-system updates', lambda wait: {
                    'db_update_mode': 'STANDARD', 'compartment_id': params.get('compartment_id'),
                    'automatic_mutation_retries': False,
                    'checks': 'Fresh ACTIVE state, selected compartment and unchanged details before each update'})
            if kind == 'create':
                self.create(job, params)
            elif kind == 'switchover':
                self.switchover(job, resource, params)
            elif kind == 'precheck':
                self.step(job, 'Switchover precheck', lambda wait: self.precheck(resource, wait))
            elif kind in {'start_db', 'stop_db', 'delete_db', 'disable_delete_protection'}:
                action = params['action']
                label = {'start': 'Start DB system', 'stop': 'Stop DB system (FAST shutdown)',
                         'delete': 'Delete DB system permanently',
                         'disable_delete_protection': 'Disable DB-system deletion protection'}[action]
                self.step(job, label, lambda wait: wait(self.service.lifecycle_db(
                    resource, action, expected_compartment=params['compartment_id'],
                    expected_resource=params['expected_resource'])))
            elif kind == 'delete_deployment':
                self.step(job, 'Delete deployment only; retain DB systems and channels', lambda wait: wait(
                    self.service.delete_deployment(resource, expected_compartment=params['compartment_id'],
                                                   expected_resource=params['expected_resource'])))
            elif kind == 'create_configuration':
                def save_configuration(wait):
                    response = self.service.create_configuration_version(resource, params)
                    # Record the resource before waiting, so it remains discoverable
                    # if its subsequent work request needs attention.
                    config = oci.util.to_dict(response.data)
                    result = {k: config.get(k) for k in ['id', 'display_name', 'shape_name', 'compartment_id']}
                    self.store.put('configuration-result:' + job, result)
                    wait(response)
                    return result
                self.step(job, 'Save new configuration; leave DB unchanged', save_configuration)
            elif kind == 'apply_configuration':
                def apply_configuration(wait):
                    db = self.service.db_system(resource)
                    self.service._check_lifecycle_review(db, params['compartment_id'], params['expected_resource'])
                    self.service.validate_configuration_apply(db, params['configuration_id'])
                    return wait(self.update_db(params, resource, configuration_id=params['configuration_id']))
                self.step(job, 'Apply configuration to DB system', apply_configuration)
            elif kind == 'update_db':
                db = self.service.db_system(resource)
                if db['lifecycle_state'] != 'ACTIVE':
                    raise ValueError('Wait for the DB system to become active')
                if params.get('is_highly_available'):
                    for deployment in self.service.inventory(db['compartment_id'])['deployments']:
                        if deployment.get('target_db_system_id') == resource and deployment.get('active_db_system_id') != resource:
                            raise ValueError('Green must remain standalone until promotion')
                options = {k: v for k, v in params.items() if k not in {'db_update_mode', 'compartment_id'}}
                if options.get('is_highly_available') is True:
                    self.enable_ha(job, resource, params, options.get('configuration_id') or db['configuration_id'], 'Enable HA on DB system')
                else:
                    self.step(job, 'Update DB system', lambda wait: wait(self.update_db(params, resource, **options)))
            elif kind == 'quarantine_channel':
                def quarantine(wait):
                    channel = self.service.channel(resource)
                    if not channel.get('is_enabled'):
                        return {'disabled': True}
                    status = self.service.channel_health(resource, True, wait)
                    if (status.get('channel_status_result') or {}).get('is_healthy') is False:
                        wait(self.service.set_channel(resource, False, expected_compartment=params['compartment_id']))
                        return {'disabled': True, 'recommended_action': 'Fix or delete this unhealthy channel'}
                    return {'disabled': False, 'detail': 'Fresh status did not confirm unhealthy replication'}
                self.step(job, 'Quarantine unhealthy replication channel', quarantine)
            elif kind in {'disable_channel', 'enable_channel', 'delete_channel'}:
                def action(wait):
                    channel = self.service.channel(resource)
                    deployments = self.service.inventory(channel['compartment_id'])['deployments']
                    for deployment in deployments:
                        detail = self.service.deployment(deployment['id'])
                        if detail.get('replication_channel_id') == resource:
                            raise ValueError('The native blue-green channel is managed by OCI')
                        if kind == 'enable_channel' and channel['target']['db_system_id'] == detail.get('target_db_system_id'):
                            if detail.get('active_db_system_id') != detail.get('target_db_system_id'):
                                raise ValueError('Green inbound channels must remain disabled until switchover')
                    wait(self.service.delete_channel(resource) if kind == 'delete_channel'
                         else self.service.set_channel(resource, kind == 'enable_channel', expected_compartment=params['compartment_id']))
                    if kind == 'enable_channel':
                        health = self.service.channel_health(resource, True, wait)
                        if (health.get('channel_status_result') or {}).get('is_healthy') is not True:
                            wait(self.service.set_channel(resource, False, expected_compartment=params['compartment_id']))
                            raise ValueError('Enabled channel did not pass health verification and was disabled; fix or delete it')
                self.step(job, kind.replace('_', ' '), action)
            elif kind == 'event_states':
                original = self.observer.events(resource)
                choices = validate_plan(original, params.get('event_review'))
                self.store.put('event-snapshot:' + job, original)
                self.event_after(job, resource, choices, original)
            elif kind == 'activate_copies':
                deployment = self.service.deployment(resource)
                if deployment.get('active_db_system_id') != deployment.get('target_db_system_id') or deployment.get('switchover_status') != 'SWITCHOVER_COMPLETED':
                    raise ValueError('Copied channels can only be activated after successful switchover')
                self.activate(job, deployment['target_db_system_id'])
            else:
                raise ValueError('Unknown workflow')
        except Exception as exc:
            eid = self.store.event(job, 'Operation requires attention')
            self.store.update_event(eid, 'FAILED', public_error(exc))
            self.store.finish(job, 'FAILED')
        else:
            self.store.finish(job, 'SUCCEEDED')

    def step(self, job, label, operation):
        eid = self.store.event(job, label)
        def wait(response):
            self.service.wait(response, lambda wr: self.store.update_event(eid, work_request=wr))
            return response
        try:
            result = operation(wait)
        except Exception as exc:
            self.store.update_event(eid, 'FAILED', public_error(exc))
            raise
        detail = json.dumps(result, default=str) if isinstance(result, (dict, list)) else 'Completed'
        self.store.update_event(eid, 'SUCCEEDED', detail)
        return result

    def settle_db(self, db_id, **desired):
        db = self.service.db_system(db_id)
        if db['lifecycle_state'] != 'ACTIVE' or any(db.get(k) != v for k, v in desired.items()):
            raise ValueError('DB system has not reached the required active configuration')
        return db

    def update_db(self, params, db_id, **options):
        try:
            db_update_mode(params)
            return self.service.update_db(db_id, expected_compartment=params['compartment_id'], **options)
        except oci.exceptions.ServiceError as exc:
            # These update paths contain no credentials. Preserve the service's
            # parameter explanation without exposing bodies from other APIs.
            if exc.status == 400 and set(options) <= {'is_highly_available','configuration_id','database_mode'}:
                message = str(exc.message or '')
                if not any(word in message.lower() for word in ['password','secret','token','authorization','private key']):
                    raise RuntimeError(public_error(exc) + ': ' + message[:500]) from exc
            raise

    def enable_ha(self, job, db_id, params, configuration, label):
        current = self.service.db_system(db_id)
        self.service.ha_configuration(current, configuration)
        if configuration != current.get('configuration_id'):
            self.step(job, 'Apply HA configuration before enabling HA', lambda wait: wait(
                self.update_db(params, db_id, configuration_id=configuration)))
            self.settle_db(db_id, configuration_id=configuration)
        # An unchanged configuration must not be resubmitted as a config change.
        self.step(job, label, lambda wait: wait(self.update_db(params, db_id, is_highly_available=True)))
        self.settle_db(db_id, is_highly_available=True)

    def create(self, job, p):
        service = self.service
        inventory = service.inventory(p['compartment_id'])
        if any(not inventory.get(key, True) for key in ['db_scope_verified', 'registration_scope_verified',
                                                       'replica_scope_verified', 'channel_scope_verified',
                                                       'work_request_scope_verified']):
            raise ValueError('Required discovery in the selected compartment is unverified; review its IAM policy warning')
        if hasattr(service, 'list_deployments'):
            inventory['deployments'] = service.list_deployments(p['compartment_id'])
        occupied = {x.get(key) for x in inventory['deployments'] if x['lifecycle_state'] != 'DELETED'
                    for key in ['source_db_system_id', 'target_db_system_id', 'active_db_system_id']}
        if p['source_id'] in occupied:
            raise ValueError('This DB system already belongs to a blue-green deployment')
        blue = self.settle_db(p['source_id'])
        if blue['compartment_id'] != p['compartment_id']:
            raise ValueError('Source DB system must belong to the selected compartment')
        # Refresh immediately before any event, HA or cloud mutation. An API
        # caller cannot bypass the dashboard's eligibility check.
        if service.read_replicas(p['source_id'], p['compartment_id']):
            raise ValueError('Blue-green creation is not supported for a DB system with read replicas')
        service.validate_creation_target(blue, p)
        service.assert_secret_compartment(p['replication_secret_id'], p['secret_compartment_id'])
        channels = service.channels(p['source_id'], p['compartment_id'])
        credentials = p.get('channel_credentials', {})
        for channel in channels:
            credential = credentials.get(channel['id'])
            if not credential or not credential.get('username') or not credential.get('secret_id'):
                raise ValueError('Register a username and Vault secret for every inbound channel')
            service.assert_secret_compartment(credential['secret_id'], p['secret_compartment_id'])
        self.store.put('plan-source:' + p['source_id'], p)
        event_state = self.observer.events(p['source_id'])
        event_choices = validate_plan(event_state, p.get('event_review'))
        for choice in event_choices:
            choice['after'] = 'RESTORE'
        p = {**p, 'original_events': event_state}
        self.store.put('registered-events:' + p['source_id'], event_state)
        self.store.put('plan-source:' + p['source_id'], p)
        self.store.put('event-snapshot:' + job, event_state)
        self.event_before(job, p['source_id'], event_choices)
        original_ha = bool(blue.get('is_highly_available'))
        if original_ha:
            self.step(job, 'Disable Blue HA', lambda wait: wait(self.update_db(p, p['source_id'], is_highly_available=False)))
            self.settle_db(p['source_id'], is_highly_available=False)
        # Unhealthy channels are disabled before any creation; unknown health blocks instead of guessing.
        for channel in channels:
            if channel.get('is_enabled'):
                health = self.step(job, 'Check inbound channel health',
                                   lambda wait, c=channel: service.channel_health(c['id'], True, wait))
                result = health.get('channel_status_result') or {}
                if result.get('is_healthy') is False:
                    self.step(job, 'Disable unhealthy inbound channel', lambda wait, c=channel: wait(service.set_channel(c['id'], False, expected_compartment=p['compartment_id'])))
                elif result.get('is_healthy') is not True:
                    raise ValueError('Inbound channel health could not be established')
        def make(wait):
            response = service.create_deployment(p, job)
            dep = response.data
            self.store.put('plan:' + dep.id, {**p, 'original_ha': original_ha,
                           'original_configuration_id': blue['configuration_id']})
            self.store.put('job-deployment:' + job, dep.id)
            wait(response)
            return {'deployment_id': dep.id}
        created = self.step(job, 'Create native blue-green deployment', make)
        deployment_id = created['deployment_id']
        deployment = service.deployment(deployment_id)
        green_id = deployment['target_db_system_id']
        self.store.put('registered-events:' + green_id, event_state)
        profile = self.store.get('observer:' + p['source_id'])
        if profile and not self.store.get('observer:' + green_id):
            self.store.put('observer:' + green_id, profile)
        self.step(job, 'Ensure Green is read-only', lambda wait: wait(self.update_db(p, green_id, database_mode='READ_ONLY')))
        self.settle_db(green_id, is_highly_available=False, database_mode='READ_ONLY')
        used_names = {c.get('target', {}).get('channel_name') for c in service.channels(green_id, p['compartment_id'])}
        for channel in channels:
            channel_name = channel['target'].get('channel_name') or 'inbound'
            # The native channel name is chosen by OCI, so compare actual names after creation.
            if channel_name in used_names:
                channel_name = 'inbound_' + hashlib.sha256(channel['id'].encode()).hexdigest()[:20]
            if channel_name in used_names:
                raise ValueError('Replication channel name collision; resolve before copying')
            used_names.add(channel_name)
            token = hashlib.sha256((job + channel['id']).encode()).hexdigest()
            def copy(wait, c=channel, name=channel_name, token=token):
                response = service.copy_channel(c, green_id, credentials[c['id']], name, token)
                self.store.put('copy:' + response.data.id, {'deployment_id': deployment_id, 'source_id': c['id']})
                wait(response)
                return {'channel_id': response.data.id, 'enabled': False, 'channel_name': name}
            self.step(job, 'Copy inbound channel disabled', copy)
        if original_ha and p.get('restore_blue_ha'):
            self.enable_ha(job, p['source_id'], p, p.get('blue_ha_configuration_id') or blue['configuration_id'], 'Restore Blue HA')

        self.event_after(job, p['source_id'], event_choices, event_state)

    def precheck(self, deployment_id, wait):
        deployment = self.service.deployment(deployment_id)
        require_allowed(switchover_blocker(deployment))
        blue = self.observer.sample(deployment['source_db_system_id'])
        green = self.observer.sample(deployment['target_db_system_id'])
        if green['db_system'].get('is_highly_available'):
            raise ValueError('Green must be standalone')
        if blue['db_system']['lifecycle_state'] != 'ACTIVE' or green['db_system']['lifecycle_state'] != 'ACTIVE':
            raise ValueError('Both DB systems must be active')
        comparison = compare(blue['gtid'], green['gtid'], green['server_uuid'])
        if not comparison['ok']:
            raise ValueError('Unexpected Green GTIDs: ' + comparison['unexpected'])
        green_events = self.observer.events(deployment['target_db_system_id'])
        if any(e['status'] == 'ENABLED' for e in green_events):
            raise ValueError('Green has enabled scheduled events; disable them before switchover')
        channels = self.service.channels(deployment['target_db_system_id'], deployment['compartment_id'])
        native = deployment.get('replication_channel_id')
        if not native or native not in {c['id'] for c in channels}:
            raise ValueError('Native blue-green replication channel is missing')
        if not next(c for c in channels if c['id'] == native).get('is_enabled'):
            raise ValueError('Native blue-green replication channel must be enabled')
        for channel in channels:
            if channel['id'] != native and channel.get('is_enabled'):
                raise ValueError('All Green channels except Blue-to-Green must be disabled')
        health = self.service.channel_health(native, True, wait)
        result = health.get('channel_status_result') or {}
        if result.get('is_healthy') is not True or result.get('errors'):
            raise ValueError('Blue-to-Green replication is not healthy')
        return {'deployment': deployment, 'blue': blue, 'green': green,
                'gtid': comparison, 'replication_health': health}

    def switchover(self, job, deployment_id, p):
        service = self.service
        deployment = service.deployment(deployment_id)
        blue_id, green_id = deployment['source_db_system_id'], deployment['target_db_system_id']
        plan = self.store.get('plan:' + deployment_id, {})
        ha = promotion_configuration(p, plan, service.db_system(blue_id), service.db_system(green_id)) if p.get('enable_ha') else None
        self.step(job, 'Precheck GTID and replication health', lambda wait: self.precheck(deployment_id, wait))
        if ha:
            self.step(job, 'Selected HA configuration for promotion', lambda wait: ha)
        source_events = self.observer.events(blue_id)
        target_events = self.observer.events(green_id)
        source_choices = validate_plan(source_events, p.get('source_event_review'))
        target_choices = validate_plan(target_events, p.get('target_event_review'))
        self.store.put('event-snapshot:' + job, {'source': source_events, 'target': target_events})
        if not self.store.get('registered-events:' + green_id):
            self.store.put('registered-events:' + green_id, source_events)
        self.event_before(job, blue_id, source_choices)
        if service.db_system(blue_id).get('is_highly_available'):
            self.step(job, 'Disable Blue HA for switchover', lambda wait: wait(self.update_db(p, blue_id, is_highly_available=False)))
        self.settle_db(blue_id, is_highly_available=False)
        if service.db_system(green_id).get('database_mode') != 'READ_ONLY':
            self.step(job, 'Return Green to read-only', lambda wait: wait(self.update_db(p, green_id, database_mode='READ_ONLY')))
        self.settle_db(green_id, is_highly_available=False, database_mode='READ_ONLY')
        baseline = self.step(job, 'Final readiness check', lambda wait: self.precheck(deployment_id, wait))
        if not baseline['green']['read_only'] or not baseline['green']['super_read_only']:
            raise ValueError('SQL observation does not confirm Green is read-only')
        endpoints = baseline['blue']['db_system'].get('endpoints') or []
        endpoint = next((e for e in endpoints if 'WRITE' in (e.get('modes') or []) and e.get('ip_address')), None)
        if not endpoint:
            raise ValueError('Cannot identify the primary write endpoint for handoff verification')
        self.store.put('baseline:' + deployment_id, {'gtid': baseline['blue']['gtid'], 'endpoint': endpoint,
                       'blue_uuid': baseline['blue']['server_uuid'], 'green_uuid': baseline['green']['server_uuid'], 'green_hostname': baseline['green']['hostname']})
        self.step(job, 'Native OCI switchover and work request', lambda wait: wait(service.switch(deployment_id, p['wait_seconds'])))
        def verify(wait):
            deadline = time.monotonic() + 600
            last_detail = 'Waiting for endpoint handoff'
            while time.monotonic() < deadline:
                current = service.deployment(deployment_id)
                if current.get('switchover_status') == 'SWITCHOVER_FAILED':
                    raise ValueError('OCI reported switchover failure; reconcile deployment state')
                if current.get('active_db_system_id') == green_id and current.get('switchover_status') == 'SWITCHOVER_COMPLETED':
                    # Connect through original IP; compare SQL server UUID and hostname, not only DNS.
                    try:
                        sample = self.observer.sample(green_id, host_override=endpoint['ip_address'])
                        final_blue = self.observer.sample(blue_id)
                    except Exception as exc:
                        last_detail = public_error(exc)
                        time.sleep(5)
                        continue
                    if final_blue['server_uuid'] != baseline['blue']['server_uuid']:
                        last_detail = 'Retired Blue SQL endpoint has not reached the original Blue server'
                        time.sleep(5)
                        continue
                    cmp = compare(final_blue['gtid'], sample['gtid'], baseline['green']['server_uuid'])
                    if sample['server_uuid'] != baseline['green']['server_uuid'] or sample['hostname'] != baseline['green']['hostname']:
                        last_detail = 'Original endpoint has not reached the expected Green server'
                    elif not cmp['ok'] or cmp['missing_on_green']:
                        raise ValueError('Post-switch GTID verification failed: missing or unexpected transactions')
                    elif sample['read_only'] or sample['super_read_only'] or sample['db_system'].get('database_mode') != 'READ_WRITE':
                        last_detail = 'Promoted Green is not yet read-write'
                    else:
                        self.store.put('verification:' + deployment_id, cmp)
                        return {'endpoint': endpoint, 'hostname': sample['hostname'], 'server_uuid': sample['server_uuid'], 'gtid': cmp, 'read_write': True,
                                'gtid_scope': 'Final retired Blue GTID set, checked against the original Blue server UUID'}
                time.sleep(5)
            raise TimeoutError(last_detail + '; post-switch verification timed out')
        self.step(job, 'Verify VIP, hostname, GTID and read-write', verify)
        if p.get('enable_ha'):
            self.enable_ha(job, green_id, p, ha['configuration_id'], 'Enable HA on promoted Green')
        if p.get('activate_channels'):
            self.activate(job, green_id)
        self.event_after(job, blue_id, source_choices, source_events)
        self.event_after(job, green_id, target_choices, target_events)

    def activate(self, job, green_id):
        db = self.service.db_system(green_id)
        for channel in self.service.channels(green_id, db['compartment_id']):
            if self.store.get('copy:' + channel['id']):
                self.step(job, 'Enable copied inbound channel', lambda wait, c=channel: wait(self.service.set_channel(c['id'], True, expected_compartment=db['compartment_id'])))
                health = self.step(job, 'Verify enabled channel health', lambda wait, c=channel: self.service.channel_health(c['id'], True, wait))
                if (health.get('channel_status_result') or {}).get('is_healthy') is not True:
                    self.step(job, 'Disable unhealthy copied channel', lambda wait, c=channel: wait(self.service.set_channel(c['id'], False, expected_compartment=db['compartment_id'])))
                    raise ValueError('Copied channel was unhealthy and has been disabled; fix or delete it')

    def event_before(self, job, db_id, choices):
        for choice in choices:
            if choice.get('disable_before'):
                self.step(job, 'Disable selected source event', lambda wait, c=choice:
                          self.observer.set_event(db_id, c['event_schema'], c['event_name'], False))

    def event_after(self, job, db_id, choices, original):
        states = {(e['event_schema'], e['event_name']): e['status'] for e in original}
        current_states = {(e['event_schema'], e['event_name']): e['status'] for e in self.observer.events(db_id)}
        for choice in choices:
            action = choice['after']
            if action == 'KEEP':
                continue
            if action == 'RESTORE':
                previous = states[(choice['event_schema'], choice['event_name'])]
                if previous == 'REPLICA_SIDE_DISABLED':
                    # Preserve replica-side state by avoiding a local ALTER EVENT.
                    if choice.get('disable_before'):
                        raise ValueError('Cannot restore REPLICA_SIDE_DISABLED after local alteration')
                    continue
                enabled = previous == 'ENABLED'
            else:
                enabled = action == 'ENABLE'
            if current_states.get((choice['event_schema'], choice['event_name'])) == ('ENABLED' if enabled else 'DISABLED'):
                continue
            self.step(job, 'Apply selected event state', lambda wait, c=choice, enabled=enabled:
                      self.observer.set_event(db_id, c['event_schema'], c['event_name'], enabled))
