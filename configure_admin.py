#!/usr/bin/env python3
"""Optional local CLI password change for the embedded admin account."""
import getpass
from modules.local_auth import LocalAuth
from modules.store import Store

store=Store('.data/store.json')
auth=LocalAuth(store.connection_options['unix_socket'],store)
current=getpass.getpass('Current admin password: ')
if not auth.authenticate('admin',current):
    raise SystemExit('Invalid admin password')
password=getpass.getpass('New admin password: ')
if password!=getpass.getpass('Confirm new password: '):
    raise SystemExit('Passwords do not match')
auth.change_password('admin',current,password)
print('Admin password changed in embedded MySQL. Sign in again.')
