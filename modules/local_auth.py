"""Socket-only admin authentication. Passwords exist only in server-owned sessions."""
import mysql.connector
from .mysql_util import ConnectionCache, test_mysql_connection

BOOTSTRAP_PASSWORD = 'ChangeMe123!'


class LocalAuth:
    def __init__(self, socket, store):
        self.profile = {'mode': 'socket', 'socket': socket, 'name': 'local-admin-profile'}
        self.store = store
        self.connections = ConnectionCache()

    def authenticate(self, username, password):
        if username != 'admin':
            return False
        try:
            test_mysql_connection(self.profile, username, password)
        except mysql.connector.Error:
            return False
        return True

    def healthy(self, sid, username, password):
        return self.connections.borrow(sid, self.profile, username, password)

    def must_change(self, username):
        return self.store.must_change_password(username)

    def change_password(self, username, old_password, new_password):
        if len(new_password) < 12 or new_password in {old_password, BOOTSTRAP_PASSWORD}:
            raise ValueError('Choose a different password of at least 12 characters')
        connection = mysql.connector.connect(unix_socket=self.profile['socket'], user=username,
                                              password=old_password, connection_timeout=5, autocommit=True)
        try:
            cursor = connection.cursor()
            # The account is fixed, and values are bound through Connector/Python.
            cursor.execute("ALTER USER 'admin'@'localhost' IDENTIFIED BY %s", (new_password,))
            cursor.close()
        finally:
            connection.close()
        self.store.password_changed(username)
        self.connections.close_all()

    def close(self, sid):
        self.connections.close(sid)
