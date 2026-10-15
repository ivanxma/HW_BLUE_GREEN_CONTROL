"""Read-only SQL telemetry, with short-lived connections and Vault credentials."""
import os
import re
from .mysql_util import open_connection


def resolve_profile(service, store, db_id, db=None):
    own = store.get('observer:' + db_id)
    if own:
        return own
    db = db or service.db_system(db_id)
    for deployment in service.list_deployments(db['compartment_id']):
        if (deployment.get('lifecycle_state') != 'DELETED' and
                deployment.get('compartment_id') == db['compartment_id'] and
                deployment.get('target_db_system_id') == db_id):
            source_id = deployment.get('source_db_system_id')
            source = service.db_system(source_id)
            if source.get('compartment_id') != db['compartment_id'] or source.get('lifecycle_state') == 'DELETED':
                continue
            profile = store.get('observer:' + source_id)
            if profile:
                return {**profile, 'inherited_from': source_id}
    return {}


class Observer:
    def __init__(self, service, store):
        self.service, self.store = service, store

    def sample(self, db_id, host_override=None):
        db = self.service.db_system(db_id)
        profile = resolve_profile(self.service, self.store, db_id, db)
        if not profile:
            raise ValueError('Register SQL access on Blue; Green inherits its username and Vault secret unless separately configured')
        connection, _ = self.connect(db, profile, host_override)
        try:
            cursor = connection.cursor(dictionary=True, buffered=True)
            cursor.execute('SELECT @@global.gtid_executed AS gtid, @@global.server_uuid AS server_uuid, '
                           '@@hostname AS hostname, @@global.read_only AS read_only, '
                           '@@global.super_read_only AS super_read_only, @@version AS mysql_version, '
                           '@@version_comment AS version_comment, @@global.offline_mode AS offline_mode')
            result = cursor.fetchone()
            result['database_mode'] = 'READ_ONLY' if result['read_only'] or result['super_read_only'] else 'READ_WRITE'
            result['access_mode'] = 'OFFLINE' if result['offline_mode'] else 'ONLINE'
            cursor.execute("SHOW GLOBAL STATUS LIKE 'Uptime'")
            result['uptime'] = int(cursor.fetchone()['Value'])
            cursor.execute('SELECT CHANNEL_NAME AS channel_name, SERVICE_STATE AS service_state, '
                           'LAST_ERROR_NUMBER AS last_error_number, LAST_ERROR_MESSAGE AS last_error_message '
                           'FROM performance_schema.replication_connection_status')
            result['replication'] = cursor.fetchall()
            cursor.close()
            result['db_system'] = db
            return result
        finally:
            connection.close()

    def global_values(self, db_id, kind):
        statements = {'variables': 'SHOW GLOBAL VARIABLES', 'status': 'SHOW GLOBAL STATUS'}
        if kind not in statements:
            raise ValueError('Choose Variables or Status')
        db = self.service.db_system(db_id)
        profile = resolve_profile(self.service, self.store, db_id, db)
        if not profile:
            raise ValueError('Configure SQL access to read MySQL variables and status')
        connection, _ = self.connect(db, profile)
        try:
            cursor = connection.cursor(dictionary=True, buffered=True)
            try:
                cursor.execute(statements[kind])
                return sorted([{'name': row['Variable_name'], 'value': safe_global_value(row['Variable_name'], row['Value'])}
                               for row in cursor.fetchall()], key=lambda row: row['name'].casefold())
            finally:
                cursor.close()
        finally:
            connection.close()

    def connect(self, db, profile, host_override=None):
        connection_profile = {'host': host_override or db.get('ip_address'), 'port': db.get('port') or 3306,
                              'ssl_mode': 'REQUIRED', 'connect_timeout': 5}
        ca = os.environ.get('BLUEGREEN_MYSQL_SSL_CA')
        if ca:
            connection_profile.update(ssl_mode='VERIFY_IDENTITY', ssl_ca=ca)
            if not host_override:
                endpoints = db.get('endpoints') or []
                connection_profile['host'] = next((e['hostname'] for e in endpoints if e.get('hostname')), db.get('ip_address'))
        return open_connection(connection_profile, profile['username'], self.service.secret_value(profile['secret_id']))

    def events(self, db_id):
        db = self.service.db_system(db_id)
        profile = resolve_profile(self.service, self.store, db_id, db)
        if not profile:
            raise ValueError('Configure SQL observation before reviewing scheduled events')
        connection, _ = self.connect(db, profile)
        try:
            cursor = connection.cursor(dictionary=True, buffered=True)
            cursor.execute('SELECT EVENT_SCHEMA AS event_schema, EVENT_NAME AS event_name, STATUS AS status, '
                           'DEFINER AS definer, EVENT_DEFINITION AS definition, EVENT_TYPE AS event_type, '
                           'EXECUTE_AT AS execute_at, INTERVAL_VALUE AS interval_value, '
                           'INTERVAL_FIELD AS interval_field, STARTS AS starts, ENDS AS ends, '
                           'TIME_ZONE AS time_zone, ON_COMPLETION AS on_completion, '
                           'LAST_EXECUTED AS last_executed FROM information_schema.EVENTS '
                           'ORDER BY EVENT_SCHEMA, EVENT_NAME')
            events = cursor.fetchall()
            cursor.close()
            return events
        finally:
            connection.close()

    def set_event(self, db_id, event_schema, event_name, enabled):
        # Identifiers must come from a reviewed server-side inventory, never raw form SQL.
        existing = {(e['event_schema'], e['event_name']) for e in self.events(db_id)}
        if (event_schema, event_name) not in existing:
            raise ValueError('Reviewed event no longer exists')
        db = self.service.db_system(db_id)
        profile = resolve_profile(self.service, self.store, db_id, db)
        connection, _ = self.connect(db, profile)
        try:
            cursor = connection.cursor()
            schema = '`' + event_schema.replace('`', '``') + '`'
            name = '`' + event_name.replace('`', '``') + '`'
            # Use regular logged DDL. Replicated events remain REPLICA_SIDE_DISABLED on Green.
            cursor.execute(f'ALTER EVENT {schema}.{name} ' + ('ENABLE' if enabled else 'DISABLE'))
            cursor.close()
        finally:
            connection.close()
        status = next(e['status'] for e in self.events(db_id)
                      if (e['event_schema'], e['event_name']) == (event_schema, event_name))
        if status != ('ENABLED' if enabled else 'DISABLED'):
            raise ValueError('Event did not reach the requested state')
        return {'event_schema': event_schema, 'event_name': event_name, 'status': status}


def safe_global_value(name, value):
    """Keep credential-bearing plugin variables out of browser responses."""
    if re.search(r'password|passwd|secret|token|credential|private_key|keyring.*(?:config|uri|url)', name, re.I):
        # Password policy values are useful, but connection/plugin secrets are not.
        if not name.lower().startswith('validate_password.'):
            return 'Managed securely'
    text = '' if value is None else str(value)
    if re.search(r'://[^/\s]*:[^/\s]*@|-----BEGIN .*PRIVATE KEY-----', text, re.I):
        return 'Managed securely'
    return text
