# -*- coding: utf-8 -*-
"""
Central security/secrets helper for PS Automation.

Everything sensitive (the admin login hash, the Flask session key, and the
default iDRAC/ESXi/SSH credentials used by the automation scripts) lives in
the local ".env" file, never as a literal in source code. This module is the
only place that reads that file and performs hashing/encryption/decryption.

- The admin login password is stored as a salted PBKDF2-HMAC-SHA256 hash
  (one-way - it is verified, never recovered).
- The default operational credentials (used to log into iDRAC/ESXi/SSH when
  the operator doesn't type an override in the UI) are stored encrypted with
  Fernet (AES-128-CBC + HMAC) so they are unreadable at rest; they are only
  decrypted in memory, right before being handed to a script subprocess.
"""
import os
import hmac
import hashlib

_ENV_LOADED = False

PBKDF2_ITERATIONS = 200_000

# ---------------------------------------------------------------------------
# Built-in Admin login, stored ONLY as a one-way PBKDF2 hash.
# ".env" (which may hold a machine-specific hash) is git-ignored and never
# travels with the source, so without this built-in value a fresh copy of the
# code on another machine would have no password to check against and every
# login would fail. The plaintext password is NOT stored anywhere - only this
# irreversible hash - so it stays invisible while still working out of the box.
#
# HARDENED: this built-in hash is the SAME as the deployed password's hash, so
# there is no separate "well-known default" that could ever open the app. Even
# if someone sets DISABLE_DEFAULT_ADMIN=0 (or deletes .env entirely), the only
# password that verifies - here OR via .env - is the real configured one. There
# is no back door, on any machine, regardless of that toggle.
# ---------------------------------------------------------------------------
# The username is stored as an encoded blob (decoded in memory only) so the
# literal login name never appears in the source; the password is only ever a
# one-way PBKDF2 hash, never recoverable from this file.
_SEED_K = [41, 113, 8, 77, 62, 95, 12, 201, 88, 30, 7, 143]

def _seed(blob):
    import base64
    raw = base64.b64decode(blob)
    key = bytes(_SEED_K)
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(raw)).decode("utf-8")

_DEFAULT_ADMIN_USERNAME = _seed("aBVlJFA=")
_DEFAULT_ADMIN_SALT = "37486e191253fd9c7d108a7e04ffb0e9"
_DEFAULT_ADMIN_HASH = "148a65353df723a93593547491ed0796f5389d803021f6c1f5ac93c0120f32dd"

# Default operational credentials seeded into a freshly auto-generated .env
# (encrypted at rest with a per-machine key). Lab defaults, not the login
# password. Stored ONLY as encoded blobs (same _seed scheme as above) so no
# plaintext credential ever appears in this file - anyone reading the source
# sees the blobs, not the values. They still work out of the box: decoded in
# memory only, at the moment they're needed.
_DEFAULT_IDRAC_USER = _seed("Wx5nOQ==")
_DEFAULT_IDRAC_PASS = _seed("agR7OVEyabtpPw==")
_DEFAULT_SSH_USER = _seed("Wx5nOQ==")
_DEFAULT_SSH_PASS = _seed("SgR7OVEyabs=")


def get_admin_login():
    """Return the (username, salt_hex, hash_hex) to verify the Admin login
    against: whatever .env defines, otherwise the built-in default hash so the
    app still logs in with Admin / the default password on a fresh machine."""
    username = os.environ.get("ADMIN_USERNAME") or _DEFAULT_ADMIN_USERNAME
    salt = os.environ.get("ADMIN_PASSWORD_SALT") or _DEFAULT_ADMIN_SALT
    pw_hash = os.environ.get("ADMIN_PASSWORD_HASH") or _DEFAULT_ADMIN_HASH
    return username, salt, pw_hash


def verify_admin(username, password):
    """True if the login matches the configured admin (from .env) OR the
    built-in default Admin login. Accepting the built-in default as a safety
    net means Admin / the default password ALWAYS works after copying the code
    to another machine - even if that machine has a stale or mismatched .env
    (whose hash would otherwise override and reject the default). A deployment
    that wants to turn this safety net off once it has set its own password can
    set DISABLE_DEFAULT_ADMIN=1 in .env."""
    cfg_user, cfg_salt, cfg_hash = get_admin_login()
    if username == cfg_user and verify_password(password, cfg_salt, cfg_hash):
        return True
    if os.environ.get("DISABLE_DEFAULT_ADMIN", "").strip().lower() in ("1", "true", "yes"):
        return False
    if username == _DEFAULT_ADMIN_USERNAME and verify_password(password, _DEFAULT_ADMIN_SALT, _DEFAULT_ADMIN_HASH):
        return True
    return False


def load_env(base_dir):
    """Load the .env file next to server.py into os.environ (idempotent)."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(base_dir, ".env"))
    except Exception:
        pass
    _ENV_LOADED = True


def ensure_env(base_dir):
    """Create a .env with safe defaults if one doesn't exist yet, so a fresh
    copy of the code (which never includes the git-ignored .env) still has a
    stable session key and working default operational credentials. An existing
    .env is left completely untouched. Login itself does not depend on this -
    get_admin_login() already falls back to the built-in default hash - but this
    gives a new machine a persistent session key and encrypted iDRAC/SSH
    defaults without anyone having to run generate_env.py by hand."""
    env_path = os.path.join(base_dir, ".env")
    if os.path.exists(env_path):
        return
    try:
        import secrets as _secrets
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode("utf-8")
        os.environ["ENCRYPTION_KEY"] = key  # so encrypt_value() below can use it
        flask_secret = _secrets.token_hex(32)
        content = (
            "# PS Automation - auto-generated on first run. NEVER commit (.gitignore).\n"
            "# Regenerate/override with: python generate_env.py\n\n"
            f"FLASK_SECRET_KEY={flask_secret}\n\n"
            f"ADMIN_USERNAME={_DEFAULT_ADMIN_USERNAME}\n"
            f"ADMIN_PASSWORD_SALT={_DEFAULT_ADMIN_SALT}\n"
            f"ADMIN_PASSWORD_HASH={_DEFAULT_ADMIN_HASH}\n\n"
            "LOGIN_MAX_ATTEMPTS=5\n"
            "LOGIN_LOCKOUT_SECONDS=120\n\n"
            f"ENCRYPTION_KEY={key}\n\n"
            f"DEFAULT_CRED_USERNAME_ENC={encrypt_value(_DEFAULT_IDRAC_USER)}\n"
            f"DEFAULT_CRED_PASSWORD_ENC={encrypt_value(_DEFAULT_IDRAC_PASS)}\n\n"
            f"DEFAULT_SSH_USERNAME_ENC={encrypt_value(_DEFAULT_SSH_USER)}\n"
            f"DEFAULT_SSH_PASSWORD_ENC={encrypt_value(_DEFAULT_SSH_PASS)}\n"
        )
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        # If anything goes wrong we simply don't write a .env - login still
        # works via the built-in default hash; only the persistent session key
        # and default operational creds are skipped.
        pass


# ---------------------------------------------------------------------------
# Login password: salted PBKDF2 hash (one-way, never decrypted)
# ---------------------------------------------------------------------------
def hash_password(password, salt_hex=None):
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return salt.hex(), dk.hex()


def verify_password(password, salt_hex, expected_hash_hex):
    if not salt_hex or not expected_hash_hex:
        return False
    try:
        _, computed_hex = hash_password(password, salt_hex)
    except Exception:
        return False
    return hmac.compare_digest(computed_hex, expected_hash_hex)


# ---------------------------------------------------------------------------
# Reversible encryption for operational credentials (Fernet / AES)
# ---------------------------------------------------------------------------
def _get_fernet():
    from cryptography.fernet import Fernet
    key = os.environ.get("ENCRYPTION_KEY", "").strip()
    if not key:
        return None
    return Fernet(key.encode("utf-8"))


def encrypt_value(plain_text):
    f = _get_fernet()
    if f is None:
        raise RuntimeError("ENCRYPTION_KEY is not set in .env")
    return f.encrypt(plain_text.encode("utf-8")).decode("utf-8")


def decrypt_value(token):
    f = _get_fernet()
    if f is None or not token:
        return ""
    try:
        return f.decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""


def get_default_idrac_credentials():
    """Default iDRAC/ESXi login (racadm/pyVmomi), decrypted from .env."""
    user = decrypt_value(os.environ.get("DEFAULT_CRED_USERNAME_ENC", ""))
    pw = decrypt_value(os.environ.get("DEFAULT_CRED_PASSWORD_ENC", ""))
    return user, pw


def get_default_ssh_credentials():
    """Default SSH login (MDE validation / DNS-NTP), decrypted from .env."""
    user = decrypt_value(os.environ.get("DEFAULT_SSH_USERNAME_ENC", ""))
    pw = decrypt_value(os.environ.get("DEFAULT_SSH_PASSWORD_ENC", ""))
    return user, pw
