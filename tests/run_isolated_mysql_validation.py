#!/usr/bin/env python3
"""Run full MySQL tests in an ephemeral server, preserving the operator database."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import embedded_runtime as runtime

with tempfile.TemporaryDirectory(prefix='bgv-') as directory:
    stage=Path(directory)
    runtime.DATA=stage
    runtime.CONFIG=stage/'my.cnf'
    runtime.SOCKET=stage/'mysql.sock'
    runtime.CREDENTIALS=stage/'store.json'
    # Reuse the already verified embedded binary; no download, upgrade or change
    # to the deployed server, socket, password or registration schema.
    release=json.loads((runtime.ROOT/'.embedded/mysql-server/release.json').read_text())
    runtime.install=lambda *args:release
    try:
        runtime.provision('ol9')
        environment={**os.environ,'BLUEGREEN_MYSQL_INTEGRATION':'1',
                     'BLUEGREEN_TEST_STORE_CONFIG':str(runtime.CREDENTIALS)}
        result=subprocess.run([sys.executable,'-m','pytest',*sys.argv[1:]],cwd=runtime.ROOT,env=environment)
    finally:
        if runtime.CREDENTIALS.exists():runtime.stop()
    print('Isolated validation MySQL stopped and removed; deployed admin and registrations preserved.')
    raise SystemExit(result.returncode)
