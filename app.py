#!/usr/bin/env python3
"""HeatWave operations console. Run a single worker; SQL credentials live in Vault."""
import json
import os
import secrets
import threading
import time
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from modules.local_auth import LocalAuth

from modules.events import fingerprint
from modules.oci_service import OCIService, public_error, visible_resources, db_update_mode
from modules.observer import Observer, resolve_profile
from modules.store import Store
from modules.workflow import Workflow
from modules.operation_plan import build_plan
from modules.ha_configuration import promotion_configuration

ROOT = Path(__file__).resolve().parent


def create_app(config=None, service=None, observer=None, store=None, auth=None):
    app = Flask(__name__)
    app.config.update(DATA_DIR=os.environ.get('BLUEGREEN_DATA_DIR', str(ROOT / '.data')),
                      DEMO=os.environ.get('BLUEGREEN_DEMO') == '1',
                      OCI_READ_ONLY=os.environ.get('BLUEGREEN_OCI_READ_ONLY') == '1',
                      STORE_CONFIG=os.environ.get('BLUEGREEN_STORE_CONFIG', str(ROOT / '.data/store.json')),
                      SESSION_COOKIE_NAME='heatwave_bluegreen_session', SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Strict', SESSION_COOKIE_SECURE=os.environ.get('BLUEGREEN_COOKIE_SECURE') == '1',
                      MAX_CONTENT_LENGTH=256 * 1024)
    app.config.update(config or {})
    data_dir = Path(app.config['DATA_DIR'])
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_dir.chmod(0o700)
    key = data_dir / 'session.key'
    if not key.exists():
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(secrets.token_hex(32))
    key.chmod(0o600)
    app.config['SECRET_KEY'] = key.read_text().strip()
    worker_lock = None
    if not app.config.get('TESTING'):
        import fcntl
        worker_lock = open(data_dir / 'worker.lock', 'a')
        try:
            fcntl.flock(worker_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another application worker uses this data directory. Run exactly one worker.') from exc
    app.extensions['worker_lock'] = worker_lock
    store = store or Store(app.config['STORE_CONFIG'])
    auth = auth or LocalAuth(store.connection_options['unix_socket'], store)
    store.recover()
    sessions, login_attempts = {}, {}
    state_lock = threading.RLock()
    if app.config['DEMO'] and service is None:
        from modules.demo import DemoService, DemoObserver
        service = DemoService()
        observer = DemoObserver(service)
    backend = {'service': service, 'observer': observer, 'workflow': None, 'monitor_started': False, 'active_compartment': None}

    def health_monitor():
        cooldown = {}
        while True:
            time.sleep(30)
            try:
                svc, _, workflow = runtime()
                if svc.demo or any(j['status'] == 'RUNNING' for j in store.history()):
                    continue
                for comp in (backend['active_compartment'] or svc.default_compartment,):
                    channels = svc.inventory(comp)['channels']
                    for channel in channels:
                        unhealthy = (channel.get('health', {}).get('channel_status_result') or {}).get('is_healthy') is False
                        if channel.get('is_enabled') and unhealthy and time.time() - cooldown.get(channel['id'], 0) > 300:
                            if comp != (backend['active_compartment'] or svc.default_compartment):
                                break
                            workflow.submit('quarantine_channel', channel['id'], {'compartment_id':comp})
                            cooldown[channel['id']] = time.time()
                            break
            except Exception:
                # No secret-bearing exception text is emitted by the monitor. Dashboard fetches surface access errors.
                continue

    def runtime():
        with state_lock:
            if backend['service'] is None:
                backend['service'] = OCIService()
            if backend['observer'] is None:
                backend['observer'] = Observer(backend['service'], store)
            if backend['workflow'] is None:
                backend['workflow'] = Workflow(backend['service'], backend['observer'], store)
            if not backend['monitor_started'] and not backend['service'].demo and not app.config.get('TESTING') and not app.config['OCI_READ_ONLY']:
                backend['monitor_started'] = True
                threading.Thread(target=health_monitor, name='channel-health-monitor', daemon=True).start()
            return backend['service'], backend['observer'], backend['workflow']

    app.extensions['bluegreen'] = {'store': store, 'runtime': runtime, 'sessions': sessions, 'auth': auth}

    def record():
        with state_lock:
            sid = session.get('sid')
            value = sessions.get(sid)
            if not value or value['expires'] < time.time():
                sessions.pop(sid, None)
                return None
            return value

    def protected(function):
        @wraps(function)
        def wrap(*args, **kwargs):
            if not record():
                return (jsonify(error='Sign in to continue'), 401) if request.path.startswith('/api/') else redirect(url_for('login'))
            current = record()
            if auth.must_change(current['username']):
                if function.__name__ not in {'change_password', 'logout'}:
                    return (jsonify(error='Change your initial admin password first'), 403) if request.path.startswith('/api/') else redirect(url_for('change_password'))
            try:
                auth.healthy(session['sid'], current['username'], current['password'])
            except Exception:
                auth.close(session.get('sid'))
                sessions.pop(session.get('sid'), None)
                session.clear()
                return (jsonify(error='Embedded MySQL session is unavailable; sign in again'), 401) if request.path.startswith('/api/') else redirect(url_for('login'))
            return function(*args, **kwargs)
        return wrap

    @app.before_request
    def csrf():
        if request.method == 'POST':
            expected = (record() or {}).get('csrf', session.get('login_csrf'))
            submitted = request.headers.get('X-CSRF-Token') or request.form.get('csrf')
            if not expected or not submitted or not secrets.compare_digest(expected, submitted):
                return jsonify(error='Refresh the page and retry: CSRF validation failed'), 400

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.errorhandler(Exception)
    def error(exc):
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        return jsonify(error=public_error(exc)), 400

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        message = ''
        if request.method == 'POST':
            address = request.remote_addr or 'local'
            recent = [t for t in login_attempts.get(address, []) if time.time() - t < 300]
            if len(recent) >= 10:
                return render_template('login.html', csrf=session.get('login_csrf'), error='Too many attempts. Retry in five minutes.', demo=app.config['DEMO']), 429
            user, password = request.form.get('username', ''), request.form.get('password', '')
            valid = auth.authenticate(user, password)
            if valid:
                login_attempts.pop(address, None)
                session.clear()
                sid = secrets.token_urlsafe(32)
                sessions[sid] = {'username': user, 'password': password, 'csrf': secrets.token_urlsafe(32), 'expires': time.time() + 8 * 3600,
                                 'compartment': store.get('operator-compartment:' + user), 'reviews': {}}
                session['sid'] = sid
                return redirect(url_for('change_password') if auth.must_change(user) else url_for('dashboard'))
            login_attempts[address] = recent + [time.time()]
            message = 'Invalid admin credentials.'
        session.setdefault('login_csrf', secrets.token_urlsafe(32))
        return render_template('login.html', csrf=session['login_csrf'], error=message, demo=app.config['DEMO'])

    @app.post('/logout')
    @protected
    def logout():
        auth.close(session.get('sid'))
        sessions.pop(session.get('sid'), None)
        session.clear()
        return redirect(url_for('login'))

    @app.route('/change-password', methods=['GET', 'POST'])
    @protected
    def change_password():
        current = record()
        error = ''
        if request.method == 'POST':
            new = request.form.get('new_password', '')
            if new != request.form.get('confirm_password', ''):
                error = 'Passwords do not match.'
            else:
                try:
                    auth.change_password(current['username'], current['password'], new)
                except ValueError as exc:
                    error = str(exc)
                else:
                    # Invalidate every admin session after credential rotation.
                    sessions.clear()
                    session.clear()
                    return redirect(url_for('login', changed='1'))
        return render_template('change_password.html', csrf=current['csrf'], error=error)

    @app.get('/')
    @protected
    def dashboard():
        return render_template('dashboard.html', csrf=record()['csrf'], username=record()['username'], demo=app.config['DEMO'], read_only=app.config['OCI_READ_ONLY'])

    def compartment():
        service, _, _ = runtime()
        return record()['compartment'] or service.default_compartment

    def scoped_db(db_id):
        db = runtime()[0].db_system(db_id)
        if db['compartment_id'] != compartment():
            raise ValueError('Choose a DB system in the selected compartment')
        return db

    def scoped_deployment(deployment_id):
        deployment = runtime()[0].deployment(deployment_id)
        if deployment['compartment_id'] != compartment():
            raise ValueError('Choose a deployment in the selected compartment')
        return deployment

    @app.get('/api/dashboard')
    @protected
    def inventory():
        service, _, _ = runtime()
        backend['active_compartment'] = compartment()
        data = service.inventory(compartment())
        for key in ['systems', 'channels', 'deployments', 'read_replicas']:
            data[key] = visible_resources(data.get(key, []))
        occupied = {d.get(k) for d in data['deployments'] if d['lifecycle_state'] != 'DELETED'
                    for k in ['source_db_system_id', 'target_db_system_id', 'active_db_system_id']}
        system_ids = {s['id'] for s in data['systems']}
        data['deployments'] = [d for d in data['deployments'] if d['compartment_id'] == compartment() or
                               d.get('source_db_system_id') in system_ids or d.get('target_db_system_id') in system_ids]
        active = {'ACCEPTED', 'IN_PROGRESS', 'CANCELING'}
        for db in data['systems']:
            membership = next((d for d in data['deployments'] if db['id'] in
                               {d.get('source_db_system_id'), d.get('target_db_system_id')}), None)
            db['blue_green_role'] = ('BLUE' if membership['source_db_system_id'] == db['id'] else 'GREEN') if membership else None
            db['blue_green_deployment'] = membership['id'] if membership else None
            db['is_current_primary'] = bool(membership and membership.get('active_db_system_id') == db['id'])
            db['read_replicas'] = [r for r in data['read_replicas'] if r.get('db_system_id') == db['id']]
            reasons = []
            if db['read_replicas']:
                reasons.append('Read replicas present — blue-green creation is not supported')
            if not data.get('replica_scope_verified', True):
                reasons.append('Read replica discovery is unverified')
            if not data.get('registration_scope_verified', True):
                reasons.append('Registration discovery in this compartment is unverified')
            if not data.get('channel_scope_verified', True):
                reasons.append('Channel discovery in this compartment is unverified')
            if not data.get('work_request_scope_verified', True):
                reasons.append('Work request discovery in this compartment is unverified')
            if db['id'] in occupied:
                reasons.append('Already registered in a blue-green deployment')
            if db['lifecycle_state'] != 'ACTIVE':
                reasons.append('DB system must be ACTIVE')
            db['channels'] = [c for c in data['channels'] if c.get('target', {}).get('db_system_id') == db['id']]
            busy = any(w.get('status') in active and any(r.get('identifier') == db['id'] for r in w.get('resources', [])) for w in data['work_requests'])
            bad = db['lifecycle_state'] in {'FAILED', 'NEEDS_ATTENTION'} or any(c.get('lifecycle_state') in {'FAILED', 'NEEDS_ATTENTION'} or
                       (c.get('health', {}).get('channel_status_result') or {}).get('is_healthy') is False for c in db['channels'])
            unknown = any(c.get('is_enabled') and (c.get('health', {}).get('channel_status_result') or {}).get('is_healthy') is not True for c in db['channels'])
            db['tone'] = 'red' if bad else 'orange' if busy or unknown or db['lifecycle_state'] not in {'ACTIVE', 'INACTIVE'} else 'green'
            if busy:
                reasons.append('Work request in progress')
            db['creation_blockers'] = reasons
            db['eligible'] = not reasons
        history = store.history()
        for deployment in data['deployments']:
            failed = deployment.get('switchover_status') == 'SWITCHOVER_FAILED' or deployment['lifecycle_state'] == 'FAILED'
            running = deployment.get('switchover_status') in {'VALIDATING', 'REPLICATING_DATA', 'SWITCHOVER_IN_PROGRESS'} or any(
                j['status'] == 'RUNNING' and j['resource'] in {deployment['id'], deployment.get('source_db_system_id'), deployment.get('target_db_system_id')} for j in history)
            deployment['tone'] = 'red' if failed else 'orange' if running or deployment['lifecycle_state'] != 'ACTIVE' else 'green'
        data.update(compartment_id=compartment(), default_compartment_id=service.default_compartment,
                    region=service.region, demo=service.demo, history=history)
        return jsonify(data)

    @app.get('/api/compartments')
    @protected
    def compartments():
        return jsonify(runtime()[0].compartments())

    @app.post('/api/compartment')
    @protected
    def change_compartment():
        value = request.get_json()['compartment_id']
        if value not in {c['id'] for c in runtime()[0].compartments()}:
            raise ValueError('Choose an accessible compartment')
        record()['compartment'] = value
        backend['active_compartment'] = value
        store.put('operator-compartment:' + record()['username'], value)
        return jsonify(ok=True)

    @app.get('/api/catalog')
    @protected
    def catalog():
        service, _, _ = runtime()
        comp = request.args.get('compartment_id') or compartment()
        if comp not in {c['id'] for c in service.compartments()}:
            raise ValueError('Choose an accessible compartment')
        return jsonify(secrets=service.secret_catalog(comp), vaults=service.vault_catalog(comp))

    @app.get('/api/keys')
    @protected
    def keys():
        service, _, _ = runtime()
        return jsonify(service.key_catalog(request.args['vault_id'], request.args.get('compartment_id') or compartment()))

    def live():
        service, observer, workflow = runtime()
        if app.config['OCI_READ_ONLY']:
            raise ValueError('OCI execution is disabled in this validation deployment; real DB systems will not be changed')
        if service.demo:
            raise ValueError('Demo is read-only. Run on OCI Compute with BLUEGREEN_DEMO=0 to execute operations.')
        return service, observer, workflow

    @app.post('/api/secrets')
    @protected
    def create_secret():
        service, _, _ = live()
        value = request.get_json()
        if not value.get('password') or not value.get('name'):
            raise ValueError('Secret name and password are required')
        result = service.create_secret(value['compartment_id'], value['vault_id'], value['key_id'], value['name'], value['password'])
        return jsonify(id=result['id'], secret_name=result['secret_name'])

    def observed_channels(service, db_id, compartment_id):
        channels = service.channels(db_id, compartment_id)
        for channel in channels:
            try:
                channel['health'] = service.channel_health(channel['id'])
            except Exception as exc:
                channel['health'] = {'error': public_error(exc)}
        return channels

    def configuration_details(panel):
        try:
            panel['configuration'] = runtime()[0].configuration(panel['db_system'])
        except Exception as exc:
            panel['configuration'] = {'id': panel['db_system'].get('configuration_id')}
            panel['configuration_information'] = 'Configuration name unavailable: ' + public_error(exc)

    @app.get('/api/deployments/<deployment_id>')
    @protected
    def detail(deployment_id):
        service, observer, _ = runtime()
        deployment = scoped_deployment(deployment_id)
        panels = {}
        for side, db_id in [('blue', deployment['source_db_system_id']), ('green', deployment.get('target_db_system_id'))]:
            if db_id:
                panel = {'db_system': scoped_db(db_id), 'telemetry_available': False}
                try:
                    panel.update(observer.sample(db_id))
                    panel['telemetry_available'] = True
                except Exception as exc:
                    panel['telemetry_error'] = public_error(exc)
                panel['channels'] = observed_channels(service, db_id, deployment['compartment_id'])
                try:
                    panel['read_replicas'] = service.read_replicas(db_id, deployment['compartment_id'])
                except Exception as exc:
                    panel['read_replicas'] = []
                    panel['replica_error'] = public_error(exc)
                try:
                    panel['observer_profile'] = resolve_profile(service, store, db_id, panel['db_system'])
                except Exception as exc:
                    panel['observer_profile'] = {}
                    panel['profile_information'] = public_error(exc)
                configuration_details(panel)
                panels[side] = panel
        related_ids = {deployment['id'], deployment.get('replication_channel_id')}
        for panel in panels.values():
            related_ids.add(panel['db_system']['id'])
            related_ids.update(c['id'] for c in panel['channels'])
            related_ids.update(r['id'] for r in panel['read_replicas'])
        related_ids.discard(None)
        journal_ids = {e['work_request'] for j in store.history()
                       if j['resource'] in related_ids or j['params'].get('source_id') in related_ids
                       for e in j['events'] if e.get('work_request')}
        work_warning = None
        try:
            requests = service.work_requests(deployment['compartment_id'])
        except Exception as exc:
            requests = []
            work_warning = 'Work requests could not be verified in the selected compartment: ' + public_error(exc)
        for panel in panels.values():
            panel['work_requests'] = [w for w in requests if w['id'] in journal_ids or
                                     any(r.get('identifier') in related_ids for r in w.get('resources') or [])]
            panel['work_request_scope'] = 'This Blue Green deployment, both DB systems, related channels and journaled operations'
            if work_warning:
                panel['work_request_warning'] = work_warning
            elif any(w.get('resource_discovery_error') for w in requests):
                panel['work_request_warning'] = 'Some work-request resource associations could not be verified; the filtered list may be incomplete.'
        plan = store.get('plan:' + deployment_id, {})
        try:
            ha_default = promotion_configuration({}, plan, panels['blue']['db_system'], panels['green']['db_system']) if 'blue' in panels and 'green' in panels else None
        except ValueError:
            ha_default = None
        return jsonify(deployment=deployment, panels=panels, plan=plan, promotion_ha_default=ha_default,
                       verification=store.get('verification:' + deployment_id))

    @app.get('/api/db/<db_id>')
    @protected
    def db_detail(db_id):
        service, observer, _ = runtime()
        db = scoped_db(db_id)
        if db.get('lifecycle_state') == 'DELETED':
            raise ValueError('This DB system has been deleted')
        if db['compartment_id'] != compartment():
            raise ValueError('Choose a DB system in the selected compartment')
        panel = {'db_system': db, 'telemetry_available': False}
        try:
            panel['observer_profile'] = resolve_profile(service, store, db_id, db)
        except Exception as exc:
            panel['observer_profile'] = {}
            panel['profile_information'] = public_error(exc)
        try:
            panel.update(observer.sample(db_id))
            panel['telemetry_available'] = True
        except Exception as exc:
            panel['telemetry_error'] = public_error(exc)
        configuration_details(panel)
        panel['channels'] = observed_channels(service, db_id, db['compartment_id'])
        try:
            panel['read_replicas'] = service.read_replicas(db_id, db['compartment_id'])
        except Exception as exc:
            panel['read_replicas'] = []
            panel['replica_error'] = public_error(exc)
        related_ids = {db_id} | {c['id'] for c in panel['channels']} | {r['id'] for r in panel['read_replicas']}
        panel['blue_green_context'] = None
        try:
            deployments = [d for d in visible_resources(service.list_deployments(db['compartment_id']))
                           if d['compartment_id'] == db['compartment_id'] and db_id in
                           {d.get('source_db_system_id'), d.get('target_db_system_id')}]
            for deployment in deployments:
                related_ids.update(v for k, v in deployment.items() if k in
                                   {'id','source_db_system_id','target_db_system_id','active_db_system_id','replication_channel_id'} and v)
            if deployments:
                deployment = deployments[0]
                members = []
                for role, field in [('BLUE','source_db_system_id'), ('GREEN','target_db_system_id')]:
                    member_id = deployment.get(field)
                    if not member_id:
                        continue
                    try:
                        member = db if member_id == db_id else service.db_system(member_id)
                        if member.get('lifecycle_state') == 'DELETED':
                            continue
                        if member.get('compartment_id') != db['compartment_id']:
                            raise ValueError('Paired DB system is outside the selected compartment')
                        if member_id != db_id:
                            try:
                                related_ids.update(c['id'] for c in service.channels(member_id, db['compartment_id']))
                            except Exception as exc:
                                panel['relationship_warning'] = 'Paired replication-channel discovery is unverified: ' + public_error(exc)
                        members.append({k: member.get(k) for k in ['id','display_name','lifecycle_state','is_highly_available']})
                        members[-1].update(role=role, selected=member_id == db_id,
                                          is_current_primary=deployment.get('active_db_system_id') == member_id, available=True)
                    except Exception as exc:
                        members.append({'id':member_id,'role':role,'display_name':role.title()+' DB system',
                                        'available':False,'information':public_error(exc)})
                panel['blue_green_context'] = {'id':deployment['id'],'display_name':deployment.get('display_name'),
                                               'lifecycle_state':deployment['lifecycle_state'],'members':members}
        except Exception as exc:
            panel['relationship_warning'] = 'Blue Green relationships could not be verified: ' + public_error(exc)
        journal_ids = {e['work_request'] for j in store.history()
                       if j['resource'] in related_ids or j['params'].get('source_id') in related_ids
                       for e in j['events'] if e.get('work_request')}
        requests = service.work_requests(db['compartment_id'])
        panel['work_requests'] = [w for w in requests if w['id'] in journal_ids or
                                  any(r.get('identifier') in related_ids for r in w.get('resources') or [])]
        panel['work_request_scope'] = 'Selected DB system, its Blue Green deployment, paired DB system and journaled operations'
        if any(w.get('resource_discovery_error') for w in requests):
            panel['work_request_warning'] = 'Some work-request resource associations could not be verified; the filtered list may be incomplete.'
        return jsonify(panel)

    @app.get('/api/observer/<db_id>')
    @protected
    def observation_profile_details(db_id):
        db = scoped_db(db_id)
        try:
            return jsonify(profile=resolve_profile(runtime()[0], store, db_id, db))
        except Exception as exc:
            return jsonify(profile={}, information=public_error(exc))

    @app.get('/api/db/<db_id>/ha-configurations')
    @protected
    def ha_configurations(db_id):
        return jsonify(runtime()[0].ha_configurations(scoped_db(db_id)))

    @app.get('/api/db/<db_id>/sql/<kind>')
    @protected
    def sql_global_values(db_id, kind):
        if kind not in {'variables', 'status'}:
            raise ValueError('Choose Variables or Status')
        db = scoped_db(db_id)
        if db.get('lifecycle_state') == 'DELETED':
            raise ValueError('This DB system has been deleted')
        return jsonify(db_system_id=db_id, scope='GLOBAL', source='SQL',
                       rows=runtime()[1].global_values(db_id, kind))

    @app.get('/api/db/<db_id>/creation-options')
    @protected
    def creation_options(db_id):
        return jsonify(runtime()[0].creation_options(scoped_db(db_id), request.args.get('shape_name')))

    @app.post('/api/observer/<db_id>')
    @protected
    def observation_profile(db_id):
        service, _, _ = runtime()
        if service.demo:
            raise ValueError('Demo uses sample SQL observation profiles')
        value = request.get_json()
        scoped_db(db_id)
        if not value.get('username') or not value.get('secret_id'):
            raise ValueError('SQL username and Vault secret are required')
        service.assert_secret_compartment(value['secret_id'], value['secret_compartment_id'])
        store.put('observer:' + db_id, {k: value[k] for k in ['username', 'secret_id', 'secret_compartment_id']})
        return jsonify(ok=True)

    @app.get('/api/events/<db_id>')
    @protected
    def event_review(db_id):
        _, observer, _ = runtime()
        scoped_db(db_id)
        events = observer.events(db_id)
        token = secrets.token_urlsafe(24)
        state = record()['reviews']
        if len(state) > 20:
            state.clear()
        state[token] = {'db_id': db_id, 'events': events, 'fingerprint': fingerprint(events), 'created': time.time()}
        store.put('event-review:' + record()['username'] + ':' + db_id, state[token])
        return jsonify(token=token, events=events, originals=store.get('registered-events:' + db_id, []))

    def reviewed(value, db_id, mode):
        review = record()['reviews'].get(value.get('token')) if isinstance(value, dict) else None
        if not review or review['db_id'] != db_id or time.time() - review['created'] > 1800:
            raise ValueError('Review the event inventory before execution')
        supplied = value.get('choices', [])
        if len(supplied) != len(review['events']):
            raise ValueError('Choose a state for every event')
        choices = []
        for event, choice in zip(review['events'], supplied):
            before = choice.get('disable_before') is True
            if before and event['status'] == 'REPLICA_SIDE_DISABLED':
                raise ValueError('Replica-side disabled events should be left unchanged')
            after = 'RESTORE' if mode == 'create' else choice.get('after', 'KEEP')
            if after not in {'KEEP', 'ENABLE', 'DISABLE', 'RESTORE'}:
                raise ValueError('Invalid event choice')
            choices.append({'event_schema': event['event_schema'], 'event_name': event['event_name'],
                            'disable_before': before, 'after': after})
        return {'fingerprint': review['fingerprint'], 'choices': choices}

    @app.post('/api/operation-plan')
    @protected
    def operation_plan():
        service, _, _ = runtime()
        value = request.get_json()
        if not isinstance(value.get('params'), dict):
            raise ValueError('Operation parameters are required')
        result = build_plan(service, store, compartment(), value.get('path',''), value['params'], reviewed)
        result['execution_allowed'] = not service.demo and (not app.config['OCI_READ_ONLY'] or
                                    not (result['will_mutate_oci'] or result['will_mutate_sql']))
        return jsonify(result)

    @app.post('/api/create')
    @protected
    def create():
        service, _, workflow = live()
        value = request.get_json()
        required = ['source_id', 'display_name', 'replication_username', 'replication_secret_id', 'secret_compartment_id']
        if any(not value.get(k) for k in required):
            raise ValueError('Complete all required creation fields')
        params = {k: value[k] for k in required}
        params['compartment_id'] = compartment()
        params['db_update_mode'] = db_update_mode(value)
        for key in ['mysql_version', 'shape_name', 'configuration_id', 'blue_ha_configuration_id', 'promoted_ha_configuration_id']:
            if value.get(key):
                params[key] = str(value[key]).strip()
        if value.get('data_storage_size_in_gbs'):
            params['data_storage_size_in_gbs'] = int(value['data_storage_size_in_gbs'])
            if params['data_storage_size_in_gbs'] < service.db_system(params['source_id'])['data_storage_size_in_gbs']:
                raise ValueError('Green storage must be at least the Blue storage size')
        params['restore_blue_ha'] = value.get('restore_blue_ha') is True
        params['channel_credentials'] = {str(k): {'username': str(v.get('username', '')), 'secret_id': str(v.get('secret_id', ''))}
                                         for k, v in value.get('channel_credentials', {}).items()}
        params['event_review'] = reviewed(value.get('event_review'), params['source_id'], 'create')
        return jsonify(job_id=workflow.submit('create', params['source_id'], params)), 202

    @app.post('/api/deployments/<deployment_id>/action')
    @protected
    def deployment_action(deployment_id):
        service, _, workflow = live()
        deployment = scoped_deployment(deployment_id)
        value = request.get_json()
        action = value.get('action')
        if action not in {'precheck', 'switchover', 'activate_copies'}:
            raise ValueError('Unsupported deployment action')
        for member in ['source_db_system_id', 'target_db_system_id']:
            if deployment.get(member):
                scoped_db(deployment[member])
        params = {}
        if action == 'switchover':
            params = {'wait_seconds': int(value.get('wait_seconds', 300)),
                      'db_update_mode': db_update_mode(value), 'compartment_id': compartment(),
                      'enable_ha': value.get('enable_ha') is True, 'activate_channels': value.get('activate_channels') is True,
                      'ha_configuration_id': str(value.get('ha_configuration_id') or '').strip()}
            if not 1 <= params['wait_seconds'] <= 3600:
                raise ValueError('Switchover wait must be between 1 and 3600 seconds')
            params['source_event_review'] = reviewed(value.get('source_event_review'), deployment['source_db_system_id'], 'switch')
            params['target_event_review'] = reviewed(value.get('target_event_review'), deployment['target_db_system_id'], 'switch')
        return jsonify(job_id=workflow.submit(action, deployment_id, params)), 202

    @app.post('/api/db/<db_id>/action')
    @protected
    def db_action(db_id):
        service, _, workflow = live()
        db = scoped_db(db_id)
        value = request.get_json()
        action = value.get('action')
        if action == 'events':
            params = {'event_review': reviewed(value.get('event_review'), db_id, 'switch')}
            return jsonify(job_id=workflow.submit('event_states', db_id, params)), 202
        allowed = {'read_only': {'database_mode': 'READ_ONLY'}, 'read_write': {'database_mode': 'READ_WRITE'},
                   'ha_off': {'is_highly_available': False}, 'ha_on': {'is_highly_available': True}}
        if action not in allowed:
            raise ValueError('Unsupported DB action')
        params = dict(allowed[action])
        params.update(db_update_mode=db_update_mode(value), compartment_id=compartment())
        if action == 'ha_on':
            params['configuration_id'] = str(value.get('configuration_id') or db['configuration_id']).strip()
        return jsonify(job_id=workflow.submit('update_db', db_id, params)), 202

    @app.post('/api/channels/<channel_id>/action')
    @protected
    def channel_action(channel_id):
        service, _, workflow = live()
        channel = service.channel(channel_id)
        if channel['compartment_id'] != compartment():
            raise ValueError('Choose a channel in the selected compartment')
        scoped_db(channel['target']['db_system_id'])
        action = request.get_json().get('action')
        if action not in {'disable_channel', 'enable_channel', 'delete_channel'}:
            raise ValueError('Unsupported channel action')
        return jsonify(job_id=workflow.submit(action, channel_id, {'compartment_id':compartment()})), 202

    @app.get('/api/work-requests/<request_id>')
    @protected
    def work_request(request_id):
        return jsonify(runtime()[0].work_request(request_id, compartment()))

    return app


if __name__ == '__main__':
    create_app().run(host=os.environ.get('HOST', '127.0.0.1'), port=int(os.environ.get('PORT', 5000)), threaded=True)
