"""Shared action eligibility and confirmation snapshots using immutable resource IDs."""

DB_LIFECYCLE_ACTIONS = {'start', 'stop', 'delete', 'disable_delete_protection'}


def switchover_blocker(deployment):
    blue, green = deployment.get('source_db_system_id'), deployment.get('target_db_system_id')
    if not blue or not green or blue == green:
        return 'Both Blue and Green DB-system identities must be available before switchover.'
    if deployment.get('active_db_system_id') != blue:
        return 'Blue must still be the active DB system (current primary) before switchover. Green cannot be switched over again.'
    if deployment.get('lifecycle_state') != 'ACTIVE' or deployment.get('switchover_status') not in {'NOT_STARTED', 'READY_FOR_SWITCHOVER'}:
        return (f"OCI deployment is {deployment.get('lifecycle_state')} / {deployment.get('switchover_status')}. "
                'Switchover requires an ACTIVE deployment with NOT_STARTED or READY_FOR_SWITCHOVER status; '
                'a completed or in-progress switchover cannot be started again.')
    return ''


def deployment_delete_blocker(deployment):
    if deployment.get('lifecycle_state') not in {'ACTIVE', 'FAILED'}:
        return 'Wait for the deployment operation to finish before deleting the deployment.'
    if deployment.get('switchover_status') in {'SWITCHOVER_IN_PROGRESS', 'VALIDATING', 'REPLICATING_DATA'}:
        return 'Wait for switchover to finish before deleting the deployment.'
    return ''


def db_lifecycle_blocker(db, action):
    state = db.get('lifecycle_state')
    allowed = {'start': {'INACTIVE'}, 'stop': {'ACTIVE'}, 'delete': {'ACTIVE', 'INACTIVE', 'FAILED'},
               'disable_delete_protection': {'ACTIVE', 'INACTIVE'}}
    if action not in allowed:
        return 'Unsupported DB lifecycle action.'
    if state not in allowed[action]:
        return f'{action.replace("_", " ").capitalize()} requires DB state {" or ".join(sorted(allowed[action]))}; current state is {state or "unknown"}.'
    protected = (db.get('deletion_policy') or {}).get('is_delete_protected')
    if action == 'delete' and protected is not False:
        return 'Disable deletion protection first.' if protected is True else 'Deletion protection could not be verified. Refresh DB-system details first.'
    if action == 'disable_delete_protection' and protected is not True:
        return 'Deletion protection is already disabled.' if protected is False else 'Deletion protection could not be verified.'
    return ''


def require_allowed(reason):
    if reason:
        raise ValueError(reason)


def db_actions(db):
    return {action: {'allowed': not (reason := db_lifecycle_blocker(db, action)), 'reason': reason}
            for action in sorted(DB_LIFECYCLE_ACTIONS)}


def deployment_actions(deployment):
    return {action: {'allowed': not (reason := check(deployment)), 'reason': reason}
            for action, check in [('switchover', switchover_blocker), ('delete', deployment_delete_blocker)]}


def confirmation_snapshot(resource):
    # Exclude transient timestamps/SQL telemetry so ordinary dashboard reads do
    # not invalidate a review. Include the identity, role and deletion policy.
    fields = ['id', 'compartment_id', 'display_name', 'lifecycle_state', 'deletion_policy',
              'is_highly_available', 'shape_name', 'database_mode', 'configuration_id',
              'source_db_system_id', 'target_db_system_id', 'active_db_system_id',
              'replication_channel_id', 'switchover_status', 'is_heat_wave_cluster_attached']
    return {key: resource.get(key) for key in fields}
