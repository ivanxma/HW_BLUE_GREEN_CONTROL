"""Expiring OCI health snapshots and explicitly reviewed status generation."""
from copy import deepcopy
import pytest
from test_sdk_execution_path import harness, IDS, route_ids
from modules.resource_actions import confirmation_snapshot


def reviewed(e):
    path=route_ids('/api/channels/inbound/action')
    params={'action':'refresh_channel_status'}
    preview=e.post('/api/operation-plan',{'path':path,'params':params})
    assert preview.status_code==200,preview.json
    return path,{**params,'confirmation_token':preview.json['confirmation_token'],'confirmed':True},preview.json


@pytest.mark.parametrize('generic', [False,True])
def test_missing_health_snapshot_is_informational_and_polling_never_generates(harness,generic):
    e=harness;e.cloud.channel_status_missing=True;e.cloud.channel_status_generic_denied=generic
    inventory=e.client.get('/api/dashboard').json
    detail=e.client.get('/api/db/blue').json
    for channels in [inventory['channels'],detail['channels']]:
        health=channels[0]['health']
        assert health['snapshot_state']=='UNAVAILABLE'
        assert health['snapshot_missing'] is (not generic)
        assert '12 hours' in health['information'] and 'Refresh health' in health['information']
        assert '404' in health['diagnostic_error']
        assert 'error' not in health and 'channel_status_result' not in health
        if generic:assert 'selected compartment' in health['information']
    assert inventory['channel_scope_verified'] is True
    assert inventory['systems'][0]['tone']=='orange'
    assert not [c for c in e.cloud.calls if c['method']!='GET']


def test_refresh_native_channel_requires_confirmation_and_only_generates_status(harness):
    e=harness;e.cloud.created=True;e.cloud.dep['replicationChannelId']='inbound'
    e.cloud.channel_status_missing=True
    before=deepcopy((e.cloud.systems,e.cloud.channels,e.cloud.dep))
    path=route_ids('/api/channels/inbound/action')
    assert e.post(path,{'action':'refresh_channel_status'}).status_code==400
    path,body,plan=reviewed(e)
    assert 'diagnostic work request' in str(plan) and 'No channel, DB configuration, HA or data changes' in str(plan)
    assert not [c for c in e.cloud.calls if c['method']!='GET']
    job=e.wait(e.post(path,body));assert job['status']=='SUCCEEDED',job
    posts=[c for c in e.cloud.calls if c['method']!='GET']
    assert len(posts)==1 and posts[0]['path'].endswith('/actions/generateChannelStatus')
    assert any(event['work_request'] for event in job['events'])
    assert (e.cloud.systems,e.cloud.channels,e.cloud.dep)==before
    detail=e.client.get('/api/db/blue').json
    assert detail['channels'][0]['health']['channel_status_result']['is_healthy'] is True
    assert e.post(path,body).status_code==400


def test_generation_failure_does_not_become_a_healthy_snapshot(harness):
    e=harness;e.cloud.channel_status_missing=True;e.cloud.channel_status_remains_missing=True
    path,body,_=reviewed(e)
    job=e.wait(e.post(path,body));assert job['status']=='FAILED',job
    assert '404' in job['events'][-1]['detail']
    health=e.client.get('/api/db/blue').json['channels'][0]['health']
    assert health['snapshot_state']=='UNAVAILABLE' and 'channel_status_result' not in health


@pytest.mark.parametrize('change',['channel_compartment','target_compartment','target_state','channel_state'])
def test_health_refresh_rechecks_scope_and_state_in_worker(harness,change):
    e=harness;channel=e.service.channel(IDS['inbound'])
    params={'compartment_id':IDS['contract-comp'],'expected_resource':confirmation_snapshot(channel)}
    if change=='channel_compartment':e.cloud.channels['inbound']['compartmentId']='denied-comp'
    if change=='target_compartment':e.cloud.systems['blue']['compartmentId']='denied-comp'
    if change=='target_state':e.cloud.systems['blue']['lifecycleState']='INACTIVE'
    if change=='channel_state':e.cloud.channels['inbound']['lifecycleState']='UPDATING'
    workflow=e.app.extensions['bluegreen']['runtime']()[2]
    job_id=e.store.start(IDS['inbound'],'refresh_channel_status',params)
    workflow.run(job_id,'refresh_channel_status',IDS['inbound'],params)
    assert e.store.history()[0]['status']=='FAILED'
    assert not [c for c in e.cloud.calls if c['method']!='GET']


def test_read_only_mode_does_not_generate_status(harness):
    e=harness;e.app.config['OCI_READ_ONLY']=True
    path,body,plan=reviewed(e)
    assert plan['execution_allowed'] is False
    assert e.post(path,body).status_code==400
    assert not [c for c in e.cloud.calls if c['method']!='GET']
