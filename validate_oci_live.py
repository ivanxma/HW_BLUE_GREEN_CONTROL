#!/usr/bin/env python3
"""Read-only Compute/OCI verification. Never submits a DB or channel mutation."""
import json
import sys
import urllib.request
from pathlib import Path

import oci
from modules.oci_service import OCIService, public_error, rows

result={'mode':'live read-only','sdk_version':oci.__version__,'checks':[]}
for name,path in [('instance','instance/'),('vnics','vnics/')]:
    request=urllib.request.Request('http://169.254.169.254/opc/v2/'+path,headers={'Authorization':'Bearer Oracle'})
    with urllib.request.urlopen(request,timeout=5) as response:data=json.load(response)
    if name=='instance':result[name]={k:data.get(k) for k in ['id','compartmentId','region','shape','image','displayName','availabilityDomain']}
    else:result[name]=[{k:v.get(k) for k in ['vnicId','privateIp','subnetCidrBlock','virtualRouterIp']} for v in data]
try:
    service=OCIService()
except Exception as exc:
    result['instance_principal']={'status':'BLOCKED','detail':public_error(exc)}
else:
    result['instance_principal']={'status':'PASS','region':service.region,'authentication':'InstancePrincipalsSecurityTokenSigner'}
    checks=[('Compartment read',lambda:service.identity.get_compartment(service.default_compartment).data.id),
            ('Accessible compartments',lambda:len(service.compartments())),
            ('MySQL DB-system listing',lambda:len(rows(service.db.list_db_systems,service.default_compartment))),
            ('Blue-green deployment listing',lambda:len(rows(service.bg.list_blue_green_deployments,service.default_compartment))),
            ('Replication channel listing',lambda:len(rows(service.ch.list_channels,service.default_compartment))),
            ('Work-request listing',lambda:len(rows(service.wr.list_work_requests,service.default_compartment))),
            ('Vault listing',lambda:len(service.vault_catalog(service.default_compartment))),
            ('Secret metadata listing',lambda:len(service.secret_catalog(service.default_compartment)))]
    for name,call in checks:
        try:result['checks'].append({'name':name,'status':'PASS','result':call()})
        except Exception as exc:result['checks'].append({'name':name,'status':'BLOCKED','detail':public_error(exc)})
    network=oci.core.VirtualNetworkClient(service.config,signer=service.signer)
    try:
        vnic=network.get_vnic(result['vnics'][0]['vnicId']).data
        subnet=network.get_subnet(vnic.subnet_id).data
        result['network']={'vnic_id':vnic.id,'subnet_id':subnet.id,'vcn_id':subnet.vcn_id,'nsg_ids':vnic.nsg_ids,'security_list_ids':subnet.security_list_ids}
    except Exception as exc:result['network']={'status':'BLOCKED','detail':public_error(exc)}
Path(sys.argv[1]).write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))
