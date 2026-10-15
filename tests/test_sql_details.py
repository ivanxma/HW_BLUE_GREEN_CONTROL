from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app import create_app
from fakes import FakeAuth
from sqlite_store import Store
from modules.demo import DemoService, DemoObserver
from modules.observer import Observer
from modules.oci_service import OCIService


@pytest.mark.parametrize('read_only,super_read_only,offline,mode', [(0,0,0,'READ_WRITE'),(1,0,0,'READ_ONLY'),(0,1,1,'READ_ONLY')])
def test_sample_uses_sql_runtime_values_and_closes_connection(read_only,super_read_only,offline,mode):
    service=DemoService();store=Mock();store.get.return_value={'username':'observer','secret_id':'reference'}
    observer=Observer(service,store);connection=Mock();cursor=connection.cursor.return_value
    observer.connect=Mock(return_value=(connection,None))
    cursor.fetchone.side_effect=[{'mysql_version':'9.7.2-sql','version_comment':'HeatWave','read_only':read_only,
                                 'super_read_only':super_read_only,'offline_mode':offline}, {'Value':'77'}]
    cursor.fetchall.return_value=[]
    sample=observer.sample('demo-blue')
    assert sample['mysql_version']=='9.7.2-sql' and sample['database_mode']==mode
    assert sample['access_mode']==('OFFLINE' if offline else 'ONLINE') and sample['uptime']==77
    assert '@@version AS mysql_version' in cursor.execute.call_args_list[0].args[0]
    assert '@@global.offline_mode' in cursor.execute.call_args_list[0].args[0]
    connection.close.assert_called_once()


@pytest.mark.parametrize('kind', ['variables','status'])
def test_global_values_are_read_only_sorted_redacted_and_connections_close(kind):
    observer=Observer(DemoService(),Mock());observer.store.get.return_value={'username':'observer'}
    connection=Mock();cursor=connection.cursor.return_value;observer.connect=Mock(return_value=(connection,None))
    cursor.fetchall.return_value=[{'Variable_name':'z_value','Value':'<script>example</script>'},
                                {'Variable_name':'plugin_token','Value':'sensitive'},
                                {'Variable_name':'a_value','Value':'https://user:secret@example.invalid'},
                                {'Variable_name':'validate_password.length','Value':'12'}]
    rows=observer.global_values('demo-blue',kind)
    assert [r['name'] for r in rows]==sorted(r['name'] for r in rows)
    values={r['name']:r['value'] for r in rows}
    assert values['plugin_token']==values['a_value']=='Managed securely'
    assert values['validate_password.length']=='12'
    cursor.execute.assert_called_once_with('SHOW GLOBAL '+kind.upper())
    cursor.close.assert_called_once();connection.close.assert_called_once()
    cursor.execute.side_effect=RuntimeError('SQL unavailable');connection.reset_mock();cursor.reset_mock()
    with pytest.raises(RuntimeError):observer.global_values('demo-blue',kind)
    cursor.close.assert_called_once();connection.close.assert_called_once()
    observer.connect.reset_mock()
    with pytest.raises(ValueError):observer.global_values('demo-blue','variables; SET GLOBAL read_only=0')
    observer.connect.assert_not_called()


def test_global_route_authentication_scope_and_configuration_fallback(tmp_path):
    service=DemoService();observer=DemoObserver(service);observer.global_values=Mock(return_value=[{'name':'version','value':'from SQL'}])
    store=Store(tmp_path/'test.sqlite');app=create_app({'TESTING':True,'DATA_DIR':str(tmp_path)},service,observer,store,FakeAuth(store))
    client=app.test_client()
    assert client.get('/api/db/demo-blue/sql/variables').status_code==401
    client.get('/login')
    with client.session_transaction() as session:csrf=session['login_csrf']
    client.post('/login',data={'csrf':csrf,'username':'admin','password':'test-private-password'})
    result=client.get('/api/db/demo-blue/sql/variables')
    assert result.status_code==200 and result.json['source']=='SQL' and result.json['rows'][0]['value']=='from SQL'
    assert client.get('/api/db/demo-blue/sql/not-sql').status_code==400
    observer.global_values.reset_mock();service.systems[0]['compartment_id']='outside'
    assert client.get('/api/db/demo-blue/sql/variables').status_code==400
    observer.global_values.assert_not_called()
    service.systems[0]['compartment_id']=service.default_compartment;service.systems[0]['lifecycle_state']='DELETED'
    assert client.get('/api/db/demo-blue/sql/status').status_code==400
    observer.global_values.assert_not_called()
    service.systems[0]['lifecycle_state']='ACTIVE'
    result=client.get('/api/db/demo-blue').json
    assert result['configuration']['display_name']=='MySQL.8.HA'
    service.configuration=Mock(side_effect=ValueError('Configuration is outside the selected DB compartment'))
    result=client.get('/api/db/demo-blue')
    assert result.status_code==200 and result.json['telemetry_available']
    assert 'unavailable' in result.json['configuration_information']
    observer.global_values.side_effect=RuntimeError('SQL unavailable')
    result=client.get('/api/db/demo-blue/sql/status')
    assert result.status_code==400 and 'rows' not in result.json


def test_configuration_name_lookup_allows_defaults_and_scopes_custom_configuration():
    service=OCIService.__new__(OCIService);service.mysql=Mock()
    db={'configuration_id':'config-id','compartment_id':'selected'}
    service.mysql.get_configuration.return_value=SimpleNamespace(data={'id':'config-id','display_name':'My config','type':'DEFAULT','compartment_id':None})
    assert service.configuration(db)=={'id':'config-id','display_name':'My config'}
    service.mysql.get_configuration.return_value.data.update(type='CUSTOM',compartment_id='outside')
    with pytest.raises(ValueError,match='outside'):service.configuration(db)
    service.mysql.get_configuration.return_value.data['compartment_id']='selected'
    assert service.configuration(db)['display_name']=='My config'
