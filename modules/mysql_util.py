import json
import os
import threading
from dataclasses import dataclass

try:
    import mysql.connector
    from mysql.connector.constants import ClientFlag
except ImportError:  # pragma: no cover - setup installs the dependency.
    mysql = None
    ClientFlag = None

    class _MissingMySQLError(Exception):
        pass

    MySQLError = _MissingMySQLError
else:
    MySQLError = mysql.connector.Error


def require_mysql_connector():
    if mysql is None:
        raise RuntimeError("mysql-connector-python is not installed.")


def normalized_ssl_mode(profile):
    value = str(profile.get("ssl_mode") or profile.get("sslMode") or "REQUIRED").strip().upper()
    aliases = {
        "REQUIRED": "REQUIRED",
        "VERIFY_CA": "VERIFY_CA",
        "VERIFY_IDENTITY": "VERIFY_IDENTITY",
        "DISABLED": "DISABLED",
        "PREFERRED": "REQUIRED",
    }
    return aliases.get(value, "REQUIRED")


def build_tls_kwargs(profile):
    ssl_mode = normalized_ssl_mode(profile)
    if ssl_mode == "DISABLED":
        return {"ssl_disabled": True}

    kwargs = {
        "ssl_disabled": False,
        "client_flags": [ClientFlag.SSL],
    }
    if profile.get("ssl_ca"):
        kwargs["ssl_ca"] = os.path.expanduser(str(profile["ssl_ca"]))
    if profile.get("ssl_cert"):
        kwargs["ssl_cert"] = os.path.expanduser(str(profile["ssl_cert"]))
    if profile.get("ssl_key"):
        kwargs["ssl_key"] = os.path.expanduser(str(profile["ssl_key"]))
    if ssl_mode in {"VERIFY_CA", "VERIFY_IDENTITY"}:
        kwargs["ssl_verify_cert"] = True
    if ssl_mode == "VERIFY_IDENTITY":
        kwargs["ssl_verify_identity"] = True
    return kwargs


def build_connection_kwargs(profile, username, password):
    require_mysql_connector()
    mode = str(profile.get("mode") or "tcp").strip().lower()
    kwargs = {
        "user": username,
        "password": password,
        "connection_timeout": int(profile.get("connect_timeout") or 10),
        "autocommit": True,
    }
    if profile.get("database"):
        kwargs["database"] = profile["database"]

    if mode == "socket":
        socket_path = str(profile.get("socket") or "").strip()
        if not socket_path:
            raise ValueError("Socket profile is missing a socket path.")
        kwargs["unix_socket"] = socket_path
    else:
        kwargs["host"] = str(profile.get("host") or "127.0.0.1").strip()
        kwargs["port"] = int(profile.get("port") or 3306)
        kwargs.update(build_tls_kwargs(profile))

    return kwargs


def open_ssh_tunnel(profile):
    if str(profile.get("mode") or "").lower() != "ssh":
        return None

    try:
        from sshtunnel import SSHTunnelForwarder
    except ImportError as exc:
        raise RuntimeError("Install sshtunnel and paramiko to use SSH-enabled profiles.") from exc

    ssh_host = str(profile.get("ssh_host") or profile.get("jump_host") or "").strip()
    ssh_user = str(profile.get("ssh_user") or "").strip()
    ssh_port = int(profile.get("ssh_port") or 22)
    ssh_key_path = os.path.expanduser(str(profile.get("ssh_key_path") or "").strip())
    mysql_host = str(profile.get("host") or "127.0.0.1").strip()
    mysql_port = int(profile.get("port") or 3306)

    if not ssh_host or not ssh_user or not ssh_key_path:
        raise ValueError("SSH profiles require jump host, SSH user, and SSH private-key path.")
    if not os.path.exists(ssh_key_path):
        raise ValueError("SSH private-key path does not exist on the app host.")

    tunnel = SSHTunnelForwarder(
        (ssh_host, ssh_port),
        ssh_username=ssh_user,
        ssh_pkey=ssh_key_path,
        remote_bind_address=(mysql_host, mysql_port),
        local_bind_address=("127.0.0.1", 0),
    )
    tunnel.start()
    return tunnel


def open_connection(profile, username, password):
    tunnel = open_ssh_tunnel(profile)
    connect_profile = dict(profile)
    if tunnel is not None:
        connect_profile["mode"] = "tcp"
        connect_profile["host"] = "127.0.0.1"
        connect_profile["port"] = tunnel.local_bind_port

    try:
        connection_kwargs = build_connection_kwargs(connect_profile, username, password)
        connection = mysql.connector.connect(**connection_kwargs)
    except Exception:
        if tunnel is not None:
            tunnel.stop()
        raise
    return connection, tunnel


def test_mysql_connection(profile, username, password):
    connection, tunnel = open_connection(profile, username, password)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close()
    finally:
        connection.close()
        if tunnel is not None:
            tunnel.stop()


def profile_signature(profile, username):
    public_profile = {
        key: value
        for key, value in dict(profile).items()
        if key.lower() not in {"password", "passwd", "token", "secret", "private_key_content", "private_key_passphrase"}
    }
    return json.dumps({"profile": public_profile, "username": username}, sort_keys=True, default=str)


@dataclass
class CachedConnection:
    signature: str
    connection: object
    tunnel: object = None


class ConnectionCache:
    def __init__(self):
        self._lock = threading.RLock()
        self._connections = {}

    def borrow(self, connection_id, profile, username, password):
        signature = profile_signature(profile, username)
        with self._lock:
            cached = self._connections.get(connection_id)
            if cached and cached.signature == signature:
                if self._healthy(cached.connection):
                    return cached.connection
                self._close_cached(cached)

            connection, tunnel = open_connection(profile, username, password)
            cached = CachedConnection(signature=signature, connection=connection, tunnel=tunnel)
            self._health_check(connection)
            self._connections[connection_id] = cached
            return connection

    def close(self, connection_id):
        with self._lock:
            cached = self._connections.pop(connection_id, None)
            if cached:
                self._close_cached(cached)

    def close_all(self):
        with self._lock:
            for cached in list(self._connections.values()):
                self._close_cached(cached)
            self._connections.clear()

    def _healthy(self, connection):
        try:
            self._health_check(connection)
            return True
        except Exception:
            return False

    @staticmethod
    def _health_check(connection):
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        finally:
            cursor.close()

    @staticmethod
    def _close_cached(cached):
        try:
            cached.connection.close()
        except Exception:
            pass
        if cached.tunnel is not None:
            try:
                cached.tunnel.stop()
            except Exception:
                pass


def escape_sql_literal(value):
    if value is None:
        return "NULL"
    replacements = {
        "\\": "\\\\",
        "\0": "\\0",
        "\n": "\\n",
        "\r": "\\r",
        "\b": "\\b",
        "\t": "\\t",
        "\x1a": "\\Z",
        "'": "\\'",
        '"': '\\"',
    }
    text = str(value)
    return "'" + "".join(replacements.get(ch, ch) for ch in text) + "'"
