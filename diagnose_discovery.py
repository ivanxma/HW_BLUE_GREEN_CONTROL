#!/usr/bin/env python3
"""Read-only diagnostic for compartment-scoped MySQL discovery failures."""
import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import oci
from modules.oci_service import OCIService, rows, public_error

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('output')
parser.add_argument('--compartment-id',help='Inspect only this explicitly selected compartment')
args=parser.parse_args()
service=OCIService()
selected=args.compartment_id or service.default_compartment
compartments=[{'id':selected,'name':'Selected compartment'}]
# Expected access denials must not trip a shared breaker and hide later results.
options=dict(config=service.config,signer=service.signer,timeout=(5,30),
             circuit_breaker_strategy=oci.circuit_breaker.NoCircuitBreakerStrategy())
service.bg=oci.mysql.BlueGreenDeploymentsClient(**options)
service.db=oci.mysql.DbSystemClient(**options)
def probe(compartment):
    result={'compartment_id':compartment['id'],'compartment_name':compartment['name']}
    try:
        result.update(status='PASS',deployments=len(rows(service.bg.list_blue_green_deployments,compartment['id'],retry_strategy=oci.retry.NoneRetryStrategy())))
    except oci.exceptions.ServiceError as exc:
        result.update(status='DENIED_OR_NOT_FOUND' if exc.status in {403,404} else 'ERROR',error=public_error(exc),http_status=exc.status,code=exc.code,
                      operation='ListBlueGreenDeployments',request_id=exc.request_id,service_message=exc.message)
        try:
            result['db_system_list']={'status':'PASS','count':len(rows(service.db.list_db_systems,compartment['id'],retry_strategy=oci.retry.NoneRetryStrategy()))}
        except Exception as db_exc:
            result['db_system_list']={'status':'DENIED_OR_NOT_FOUND','error':public_error(db_exc)}
    except Exception as exc:
        result.update(status='ERROR',error=public_error(exc))
    return result
results=[probe(c) for c in compartments]
replicas=oci.mysql.ReplicasClient(service.config,signer=service.signer)
try:
    found=rows(replicas.list_replicas,selected)
    replica_check={'status':'PASS','replicas':[{k:r.get(k) for k in ['id','display_name','db_system_id','lifecycle_state','ip_address','mysql_version']} for r in found]}
except oci.exceptions.ServiceError as exc: replica_check={'status':'DENIED_OR_NOT_FOUND','error':public_error(exc)}
report={'timestamp':datetime.now(timezone.utc).isoformat(),'region':service.region,'selected_compartment':selected,
        'endpoint':service.bg.base_client.endpoint,'operation':'ListBlueGreenDeployments','counts':dict(Counter(r['status'] for r in results)),
        'compartments':results,'read_replicas':replica_check}
Path(args.output).write_text(json.dumps(report,indent=2))
print(json.dumps({'counts':report['counts'],'failures':[r for r in results if r['status']!='PASS'],'read_replicas':replica_check},indent=2))
