"""OCI control plane adapter, authenticated solely by the Compute instance principal."""
import base64
import json
import os
import re
import time
import urllib.request

import oci

M = oci.mysql.models


def db_update_mode(params):
    if params.get('db_update_mode', 'STANDARD') != 'STANDARD':
        raise ValueError('Only Standard OCI updates are supported. Refresh the page and review the operation again.')
    return 'STANDARD'


def visible_resources(resources):
    return [r for r in resources if str(r.get('lifecycle_state', '')).upper() != 'DELETED']


def public_error(exc):
    # SDK/SQL exception text may include credentials or submitted request bodies.
    if isinstance(exc, oci.exceptions.ServiceError):
        return f'OCI {exc.status} {exc.code}; request ID {exc.request_id or "unavailable"}'
    if isinstance(exc, (ValueError, RuntimeError, TimeoutError)):
        return str(exc)[:600]
    return f'{type(exc).__name__}: operation failed; check IAM, network connectivity and service state'


def rows(client_method, *args, **kwargs):
    result = []
    while True:
        response = client_method(*args, **kwargs)
        data = response.data
        result.extend(oci.util.to_dict(data if isinstance(data, list) else data.items))
        if not response.headers.get('opc-next-page'):
            return result
        kwargs['page'] = response.headers['opc-next-page']


class OCIService:
    demo = False

    def __init__(self):
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        config = {'region': signer.region}
        options = dict(config=config, signer=signer, timeout=(5, 30))
        self.bg = oci.mysql.BlueGreenDeploymentsClient(**options)
        self.db = oci.mysql.DbSystemClient(**options)
        self.mysql = oci.mysql.MysqlaasClient(**options)
        self.ch = oci.mysql.ChannelsClient(**options)
        self.rep = oci.mysql.ReplicasClient(**options)
        self.wr = oci.mysql.WorkRequestsClient(**options)
        self.identity = oci.identity.IdentityClient(**options)
        self.vault = oci.vault.VaultsClient(**options)
        self.secrets = oci.secrets.SecretsClient(**options)
        self.kms = oci.key_management.KmsVaultClient(**options)
        self.config = config
        self.signer = signer
        self.tenancy_id = signer.tenancy_id
        self.region = signer.region
        compartment = os.environ.get('BLUEGREEN_COMPARTMENT_ID')
        if not compartment:
            request = urllib.request.Request('http://169.254.169.254/opc/v2/instance/',
                                             headers={'Authorization': 'Bearer Oracle'})
            with urllib.request.urlopen(request, timeout=5) as response:
                compartment = json.load(response)['compartmentId']
        self.default_compartment = compartment

    def compartments(self):
        entries = rows(self.identity.list_compartments, self.tenancy_id,
                       compartment_id_in_subtree=True, access_level='ACCESSIBLE', lifecycle_state='ACTIVE')
        # Offer actual compartments. A synthetic tenancy-root option would
        # suggest broader access than this compartment-scoped console needs.
        return entries

    def db_system(self, ocid):
        return oci.util.to_dict(self.db.get_db_system(ocid).data)

    def configuration(self, db):
        configuration_id = db.get('configuration_id')
        if not configuration_id:
            return {}
        config = oci.util.to_dict(self.mysql.get_configuration(configuration_id).data)
        if config.get('type') != 'DEFAULT' and config.get('compartment_id') != db['compartment_id']:
            raise ValueError('Configuration is outside the selected DB compartment')
        return {key: config.get(key) for key in ['id', 'display_name']}

    def deployment(self, ocid):
        return oci.util.to_dict(self.bg.get_blue_green_deployment(ocid).data)

    def channels(self, db_id, compartment):
        channels = visible_resources(rows(self.ch.list_channels, compartment, db_system_id=db_id))
        known = {c['id'] for c in channels}
        # Some API versions omit the embedded channel list from GetDbSystem.
        for channel in self.db_system(db_id).get('channels') or []:
            if channel['id'] not in known and channel.get('lifecycle_state') != 'DELETED':
                channels.append(self.channel(channel['id']))
        return visible_resources(channels)

    def read_replicas(self, db_id, compartment):
        return visible_resources(rows(self.rep.list_replicas, compartment, db_system_id=db_id))

    def ha_configuration(self, db, configuration_id):
        original = None
        seen = set()
        while configuration_id and configuration_id not in seen and len(seen) < 12:
            seen.add(configuration_id)
            config = oci.util.to_dict(self.mysql.get_configuration(configuration_id).data)
            if config.get('shape_name') != db.get('shape_name'):
                raise ValueError('HA configuration must match the DB-system shape: ' + str(db.get('shape_name')))
            if config.get('lifecycle_state') != 'ACTIVE':
                raise ValueError('Choose an ACTIVE HA configuration')
            if config.get('compartment_id') not in {db['compartment_id'], None} or (config.get('type') != 'DEFAULT' and config.get('compartment_id') is None):
                raise ValueError('Custom HA configuration must belong to the selected DB compartment')
            original = original or config
            if config.get('type') == 'DEFAULT':
                if not str(config.get('display_name','')).upper().endswith('.HA'):
                    raise ValueError('Choose a configuration compatible with high availability')
                return {k:original.get(k) for k in ['id','display_name','shape_name','type','compartment_id']}
            configuration_id = config.get('parent_configuration_id')
        raise ValueError('HA configuration compatibility could not be verified from its parent configuration')

    def ha_configurations(self, db, configurations=None):
        result, information = [], []
        if configurations is None:
            configurations = rows(self.mysql.list_configurations, db['compartment_id'],
                                  shape_name=db['shape_name'], lifecycle_state='ACTIVE')
        for config in configurations:
            if config.get('shape_name') != db['shape_name'] or config.get('lifecycle_state') != 'ACTIVE':
                continue
            if config.get('type') != 'DEFAULT' and config.get('compartment_id') != db['compartment_id']:
                continue
            try:
                result.append(self.ha_configuration(db, config['id']))
            except ValueError:
                continue
            except Exception as exc:
                information.append('A configuration could not be verified in the selected compartment: '+public_error(exc))
        return {'configurations': sorted(result,key=lambda c:(c['display_name'] or '',c['id'])),
                'information': information, 'shape_name':db['shape_name'],
                'current_configuration_id':db.get('configuration_id'), 'compartment_id':db['compartment_id']}

    def creation_options(self, source, shape_name=None):
        shapes = rows(self.mysql.list_shapes, source['compartment_id'], is_supported_for=['DBSYSTEM'])
        shapes = [s for s in shapes if 'DBSYSTEM' in (s.get('is_supported_for') or [])]
        shape_name = shape_name or source['shape_name']
        if shape_name not in {s['name'] for s in shapes}:
            raise ValueError('Choose an available DB-system target shape')
        versions = []
        version_key = lambda value: tuple(int(n) for n in re.findall(r'\d+', value)[:3])
        for family in rows(self.mysql.list_versions, source['compartment_id']):
            for version in family.get('versions') or []:
                value = version.get('version','')
                if re.fullmatch(r'\d+\.\d+\.\d+',value) and version_key(value) >= version_key(source['mysql_version']):
                    versions.append({**version,'version_family':family.get('version_family')})
        configs = rows(self.mysql.list_configurations, source['compartment_id'],
                       shape_name=shape_name, lifecycle_state='ACTIVE')
        configs = [c for c in configs if c.get('shape_name') == shape_name and c.get('lifecycle_state') == 'ACTIVE'
                   and (c.get('type') == 'DEFAULT' or c.get('compartment_id') == source['compartment_id'])]
        ha = self.ha_configurations({**source,'shape_name':shape_name}, configs)
        return {'shapes':sorted(shapes,key=lambda s:s['name']),
                'versions':sorted(versions,key=lambda v:version_key(v['version']),reverse=True),
                'configurations':sorted(configs,key=lambda c:(c.get('display_name') or '',c['id'])),
                'ha_configurations':ha['configurations'],'information':ha['information'],
                'shape_name':shape_name,'source_shape_name':source['shape_name'],
                'source_configuration_id':source.get('configuration_id'),'compartment_id':source['compartment_id']}

    def validate_creation_target(self, source, params):
        shape = params.get('shape_name') or source['shape_name']
        options = self.creation_options(source, shape)
        version = params.get('mysql_version') or source['mysql_version']
        configuration = params.get('configuration_id') or source.get('configuration_id')
        if version not in {v['version'] for v in options['versions']}:
            raise ValueError('Choose an available target MySQL version at or above the source version')
        if configuration not in {c['id'] for c in options['configurations']}:
            raise ValueError('Target configuration must be ACTIVE and match the selected target shape and compartment')
        promoted = params.get('promoted_ha_configuration_id')
        if shape != source['shape_name'] and not promoted:
            raise ValueError('Choose a promoted HA configuration for the different target shape')
        if promoted and promoted not in {c['id'] for c in options['ha_configurations']}:
            raise ValueError('Promoted HA configuration must match the target shape and support HA')
        return options


    def channel(self, ocid):
        return oci.util.to_dict(self.ch.get_channel(ocid).data)

    def channel_health(self, ocid, refresh=False, wait=None):
        if refresh:
            response = self.ch.generate_channel_status(ocid)
            if wait:
                wait(response)
        data = oci.util.to_dict(self.ch.get_channel_status(ocid).data)
        return data

    def list_deployments(self, compartment):
        # Registration visibility is deliberately confined to the selected
        # compartment. Instance principals can inspect compartment names even
        # when they have no MySQL permissions there.
        return visible_resources(rows(self.bg.list_blue_green_deployments, compartment))

    def inventory(self, compartment):
        warnings = []
        def read(operation, call, default, *args, **kwargs):
            try:
                return call(*args, **kwargs), True
            except Exception as exc:
                warnings.append({'operation':operation, 'compartment_id':compartment,
                                 'compartment_name':'Selected compartment', 'error':public_error(exc)})
                return default, False

        summaries, db_scope = read('ListDbSystems', rows, [], self.db.list_db_systems, compartment)
        systems = []
        for summary in visible_resources(summaries):
            db, allowed = read('GetDbSystem', self.db_system, None, summary['id'])
            db_scope = db_scope and allowed
            if db and db.get('lifecycle_state') != 'DELETED':
                systems.append(db)
        deployments, registration_scope = read('ListBlueGreenDeployments', self.list_deployments, [], compartment)
        replicas, replica_scope = read('ListReplicas', rows, [], self.rep.list_replicas, compartment)
        work_requests, work_scope = read('ListWorkRequests', self.work_requests, [], compartment)
        channels, known, channel_scope = [], set(), True
        for db in systems:
            found, allowed = read('ListChannels', self.channels, [], db['id'], compartment)
            channel_scope = channel_scope and allowed
            for channel in found:
                if channel['id'] not in known:
                    channels.append(channel)
                    known.add(channel['id'])
        for channel in channels:
            try:
                channel['health'] = self.channel_health(channel['id'])
            except Exception as exc:
                channel['health'] = {'error':public_error(exc)}
        def warning_for(operation):
            return next((f"{w['operation']} could not be verified in the selected compartment: {w['error']}"
                         for w in warnings if w['operation'] == operation), None)
        return {'systems':systems, 'deployments':deployments, 'channels':visible_resources(channels),
                'read_replicas':visible_resources(replicas), 'work_requests':work_requests,
                'db_scope_verified':db_scope, 'channel_scope_verified':channel_scope,
                'work_request_scope_verified':work_scope, 'replica_scope_verified':replica_scope,
                'registration_scope_verified':registration_scope,
                'registration_warning':warning_for('ListBlueGreenDeployments'),
                'replica_warning':warning_for('ListReplicas'),
                'access_warning':(f"Some resources in selected compartment {compartment} cannot be inspected. "
                                  'Review the compartment-specific IAM examples below.') if warnings else None,
                'registration_diagnostics':warnings,
                'discovery_scope':{'mode':'COMPARTMENT', 'compartment_id':compartment,
                                   'outside_compartment_discovery':False,
                                   'information':'Only the selected compartment is inspected. Registrations moved to other compartments are outside this view; OCI validates source conflicts during creation.'}}

    def secret_catalog(self, compartment):
        return rows(self.vault.list_secrets, compartment, lifecycle_state='ACTIVE')

    def vault_catalog(self, compartment):
        return [v for v in rows(self.kms.list_vaults, compartment) if v['lifecycle_state'] == 'ACTIVE']

    def key_catalog(self, vault_id, compartment):
        vault = self.kms.get_vault(vault_id).data
        client = oci.key_management.KmsManagementClient(self.config, vault.management_endpoint, signer=self.signer)
        return [k for k in rows(client.list_keys, compartment) if k['lifecycle_state'] == 'ENABLED']

    def secret_value(self, ocid):
        bundle = self.secrets.get_secret_bundle(ocid, stage='CURRENT').data
        content = bundle.secret_bundle_content
        if content.content_type != 'BASE64':
            raise ValueError('Secret must use BASE64 content')
        return base64.b64decode(content.content).decode('utf-8')

    def assert_secret_compartment(self, secret_id, compartment):
        value = self.vault.get_secret(secret_id).data
        if value.compartment_id != compartment or value.lifecycle_state != 'ACTIVE':
            raise ValueError('Select an active secret from the specified compartment')

    def create_secret(self, compartment, vault_id, key_id, name, password):
        vault = self.kms.get_vault(vault_id).data
        if vault.compartment_id != compartment:
            raise ValueError('Vault must belong to the selected compartment')
        if key_id not in {k['id'] for k in self.key_catalog(vault_id, compartment)}:
            raise ValueError('Select an enabled key from this vault and compartment')
        value = oci.vault.models.CreateSecretDetails(
            compartment_id=compartment, vault_id=vault_id, key_id=key_id, secret_name=name,
            secret_content=oci.vault.models.Base64SecretContentDetails(
                content=base64.b64encode(password.encode()).decode(), stage='CURRENT'))
        return oci.util.to_dict(self.vault.create_secret(value).data)

    def work_request(self, ocid, expected_compartment=None):
        # Status is authoritative; error details must not hide it. Optional OCI
        # progress logs are deliberately not fetched: the application journal
        # records workflow steps/timings without that additional dependency.
        request = oci.util.to_dict(self.wr.get_work_request(ocid).data)
        if expected_compartment is not None and request.get('compartment_id') != expected_compartment:
            raise ValueError('Choose a work request in the selected compartment')
        result = {'request': request, 'warnings': []}
        try:
            result['errors'] = rows(self.wr.list_work_request_errors, ocid)
            result['errors_available'] = True
        except Exception as exc:
            result['errors'] = []
            result['errors_available'] = False
            result['warnings'].append({'operation': 'ListWorkRequestErrors', 'section': 'errors',
                'compartment_id': request.get('compartment_id'), 'error': public_error(exc),
                'information': 'Error details are unavailable. This does not change the OCI work-request status; '
                               'an unavailable error list must not be interpreted as an empty verified error list.'})
        return result

    def work_requests(self, compartment):
        summaries = rows(self.wr.list_work_requests, compartment)
        cache = getattr(self, '_work_resource_cache', None)
        if cache is None:
            cache = self._work_resource_cache = {}
        for summary in summaries:
            # WorkRequestSummary has no resources field in the real SDK.
            key = (compartment, summary['id'])
            signature = (summary.get('status'), summary.get('time_finished'))
            previous = cache.get(key)
            active = summary.get('status') in {'ACCEPTED', 'IN_PROGRESS', 'CANCELING'}
            if previous and previous['signature'] == signature and (not active or time.monotonic() - previous['at'] < 15):
                summary['resources'] = previous['resources']
                continue
            try:
                detail = oci.util.to_dict(self.wr.get_work_request(summary['id']).data)
                if detail.get('compartment_id') != compartment:
                    raise ValueError('Work-request resource details are outside the selected compartment')
                summary['resources'] = detail.get('resources') or []
                cache[key] = {'signature': signature, 'at': time.monotonic(), 'resources': summary['resources']}
            except Exception as exc:
                summary['resources'] = []
                summary['resource_discovery_error'] = public_error(exc)
        while len(cache) > 512:
            cache.pop(next(iter(cache)))
        return summaries

    def wait(self, response, on_request, timeout=7200):
        request_id = response.headers.get('opc-work-request-id')
        if not request_id:
            return
        on_request(request_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.wr.get_work_request(request_id).data.status
            if state == 'SUCCEEDED':
                return
            if state in {'FAILED', 'CANCELED', 'CANCELLED'}:
                raise RuntimeError(f'Work request {request_id} ended with {state}; inspect its errors on the dashboard')
            time.sleep(5)
        raise TimeoutError(f'Work request {request_id} timed out; OCI operation may still be running')

    def update_db(self, db_id, *, db_update_mode='STANDARD', expected_compartment=None, **options):
        if db_update_mode != 'STANDARD':
            raise ValueError('Only Standard OCI updates are supported')
        if not expected_compartment:
            raise ValueError('Standard OCI updates require an explicit selected compartment')
        baseline = self.db_system(db_id)
        if baseline.get('compartment_id') != expected_compartment:
            raise ValueError('DB system is outside the selected compartment')
        if baseline.get('lifecycle_state') != 'ACTIVE':
            raise ValueError('Wait for the DB system to become active')
        if self.db_system(db_id) != baseline:
            raise ValueError('DB system details changed before the standard OCI update; refresh and review again')
        return self.db.update_db_system(db_id, M.UpdateDbSystemDetails(**options),
                                        retry_strategy=oci.retry.NoneRetryStrategy())

    def set_channel(self, channel_id, enabled, *, expected_compartment):
        if not expected_compartment:
            raise ValueError('Channel updates require an explicit selected compartment')
        baseline = self.channel(channel_id)
        if baseline.get('compartment_id') != expected_compartment:
            raise ValueError('Channel is outside the selected compartment')
        if baseline.get('lifecycle_state') not in {'ACTIVE', 'INACTIVE', 'NEEDS_ATTENTION', 'FAILED'}:
            raise ValueError('Wait for the channel to finish its current operation')
        if self.channel(channel_id) != baseline:
            raise ValueError('Channel details changed before the standard OCI update; refresh and review again')
        return self.ch.update_channel(channel_id, M.UpdateChannelDetails(is_enabled=enabled),
                                      retry_strategy=oci.retry.NoneRetryStrategy())

    def delete_channel(self, channel_id):
        return self.ch.delete_channel(channel_id)

    def create_deployment(self, params, request_token):
        options = {k: params[k] for k in ['mysql_version', 'shape_name', 'configuration_id', 'data_storage_size_in_gbs'] if params.get(k)}
        channel = M.CreateBlueGreenDeploymentChannelDetails(
            source_username=params['replication_username'],
            source_password=self.secret_value(params['replication_secret_id']), ssl_mode='REQUIRED')
        response = self.bg.create_blue_green_deployment(M.CreateBlueGreenDeploymentDetails(
            compartment_id=params['compartment_id'], display_name=params['display_name'],
            source_db_system_id=params['source_id'],
            target_db_system_details=M.CreateBlueGreenDeploymentTargetDbSystemDetails(**options),
            channel_details=channel), opc_retry_token=request_token)
        return response

    def copy_channel(self, channel, green_id, credential, channel_name, request_token):
        original = self.ch.get_channel(channel['id']).data
        source_options = {k: getattr(original.source, k) for k, v in channel['source'].items()
                          if k in M.CreateChannelSourceFromMysqlDetails().swagger_types and v is not None}
        source_options['password'] = self.secret_value(credential['secret_id'])
        source_options['username'] = credential['username']
        # Nested SDK models (CA, anonymous transaction handling, filters) are reconstructed below.
        source = M.CreateChannelSourceFromMysqlDetails(**source_options)
        target_options = {k: getattr(original.target, k) for k, v in channel['target'].items()
                          if k in M.CreateChannelTargetFromDbSystemDetails().swagger_types and v is not None}
        target_options.update(db_system_id=green_id, channel_name=channel_name)
        target = M.CreateChannelTargetFromDbSystemDetails(**target_options)
        return self.ch.create_channel(M.CreateChannelDetails(
            compartment_id=channel['compartment_id'], display_name=channel['display_name'] + ' · green',
            is_enabled=False, source=source, target=target,
            freeform_tags={'bluegreen-source-channel': channel['id']}), opc_retry_token=request_token)

    def switch(self, deployment_id, seconds):
        return self.bg.switchover_blue_green_deployment(
            deployment_id, M.SwitchoverBlueGreenDeploymentDetails(wait_time_in_seconds=seconds))
