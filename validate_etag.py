#!/usr/bin/env python3
"""GET-only diagnosis of HeatWave ETags; never submits a DB-system update."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from modules.oci_service import OCIService
from modules.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('db_system_id')
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    service = OCIService()
    original = service.db.base_client.session.request
    samples = []

    def read_only(method, url, **kwargs):
        if method != 'GET':
            raise RuntimeError('ETag validation permits GET requests only')
        response = original(method, url, **kwargs)
        samples.append({
            'accept_encoding': service.db.base_client.session.headers['Accept-Encoding'],
            'status': response.status_code,
            'etag': response.headers.get('etag'),
            'content_encoding': response.headers.get('content-encoding'),
            'request_id': response.headers.get('opc-request-id'),
            'body_sha256': hashlib.sha256(response.content).hexdigest(),
        })
        return response

    service.db.base_client.session.request = read_only
    for encoding in ['gzip', 'identity']:
        service.db.base_client.session.headers['Accept-Encoding'] = encoding
        for _ in range(4):
            db = service.db.get_db_system(args.db_system_id).data
            if db.compartment_id != service.default_compartment:
                raise RuntimeError('Diagnostic source must be in the VM default compartment')
            samples[-1].update(lifecycle_state=db.lifecycle_state,
                               is_highly_available=db.is_highly_available,
                               time_updated=str(db.time_updated))
    groups = []
    for encoding in ['gzip', 'identity']:
        selected = [s for s in samples if s['accept_encoding'] == encoding]
        groups.append({'encoding': encoding, 'samples': len(selected),
                       'unique_etags': len({s['etag'] for s in selected}),
                       'unique_bodies': len({s['body_sha256'] for s in selected})})
    store = Store(Path(__file__).resolve().parent / '.data/store.json')
    jobs = store.history()
    report = {'timestamp': datetime.now(timezone.utc).isoformat(), 'region': service.region,
              'compartment_id': service.default_compartment, 'db_system_id': args.db_system_id,
              'groups': groups, 'samples': samples, 'mutation_requests': 0,
              'running_jobs': sum(j['status'] == 'RUNNING' for j in jobs),
              'latest_source_job': next(({k: j[k] for k in ['id', 'status', 'kind', 'events']}
                                         for j in jobs if j['resource'] == args.db_system_id), None),
              'service_etag_inconsistent': any(g['unique_bodies'] == 1 and g['unique_etags'] > 1 for g in groups)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({k: report[k] for k in ['groups', 'mutation_requests', 'running_jobs', 'service_etag_inconsistent']}))


if __name__ == '__main__':
    main()
