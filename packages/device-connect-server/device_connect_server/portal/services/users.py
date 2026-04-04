"""User account management backed by etcd."""

import json
import logging
from datetime import datetime, timezone

import bcrypt

from .. import config

logger = logging.getLogger(__name__)

_USERS_PREFIX = "/device-connect/portal/users/"


def _etcd_client():
    from etcd3gw import Etcd3Client
    return Etcd3Client(host=config.ETCD_HOST, port=config.ETCD_PORT)


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def get_user(username: str) -> dict | None:
    """Fetch a user record from etcd. Returns None if not found."""
    client = _etcd_client()
    key = f"{_USERS_PREFIX}{username}"
    values = client.get(key)
    if not values:
        return None
    raw = values[0]
    if isinstance(raw, bytes):
        raw = raw.decode()
    return json.loads(raw)


def create_user(username: str, password: str, role: str = "user") -> dict:
    """Create a new user in etcd. Raises ValueError if already exists."""
    if get_user(username):
        raise ValueError(f"User '{username}' already exists")

    user = {
        "username": username,
        "password_hash": _hash_password(password),
        "role": role,
        "tenant": username,  # 1 user = 1 tenant
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    client = _etcd_client()
    key = f"{_USERS_PREFIX}{username}"
    client.put(key, json.dumps(user))
    logger.info("Created user: %s (role=%s, tenant=%s)", username, role, username)
    return user


def authenticate(username: str, password: str) -> dict | None:
    """Verify credentials. Returns user dict or None."""
    user = get_user(username)
    if not user:
        return None
    if not _check_password(password, user["password_hash"]):
        return None
    return user


def list_users() -> list[dict]:
    """List all user accounts."""
    client = _etcd_client()
    results = client.get_prefix(_USERS_PREFIX)
    users = []
    for raw, _meta in results:
        if isinstance(raw, bytes):
            raw = raw.decode()
        users.append(json.loads(raw))
    return users


def ensure_admin():
    """Seed the admin account if it doesn't exist."""
    if not get_user(config.ADMIN_USER):
        create_user(config.ADMIN_USER, config.ADMIN_PASS, role="admin")
        logger.info("Seeded admin account: %s", config.ADMIN_USER)
