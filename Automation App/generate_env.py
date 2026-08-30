# -*- coding: utf-8 -*-
"""
Generate (or regenerate) the local .env file for PS Automation.

Run this any time you want to:
  - set up the app on a new machine
  - change the Admin login password
  - rotate the encryption key / default credentials

Usage:
    python generate_env.py
    python generate_env.py --admin-password "NewPass123!"
    python generate_env.py --idrac-user "<user>" --idrac-pass "<password>" --ssh-user "<user>" --ssh-pass "<password>"

Anything not passed on the command line falls back to the existing value in
.env (if present) or a safe default. The written .env is never printed.
"""
import argparse
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import security
from cryptography.fernet import Fernet

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")


def _read_existing():
    if not os.path.isfile(ENV_PATH):
        return {}
    values = {}
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    return values


def main():
    parser = argparse.ArgumentParser(description="Generate/regenerate .env")
    parser.add_argument("--admin-password", help="New Admin login password (default: keep the existing hash, else the built-in default)")
    parser.add_argument("--idrac-user", help="Default iDRAC/ESXi username (default: keep existing, else built-in default)")
    parser.add_argument("--idrac-pass", help="Default iDRAC/ESXi password (default: keep existing, else built-in default)")
    parser.add_argument("--ssh-user", help="Default SSH username (default: keep existing, else built-in default)")
    parser.add_argument("--ssh-pass", help="Default SSH password (default: keep existing, else built-in default)")
    parser.add_argument("--rotate-key", action="store_true", help="Generate a brand new encryption key (re-encrypts the default credentials)")
    args = parser.parse_args()

    existing = _read_existing()

    # When rotating the key, decrypt the current credentials with the OLD key
    # FIRST (it's still available in the existing .env), so we can re-encrypt
    # them with the new key instead of losing them to the fallback defaults.
    old_key = existing.get("ENCRYPTION_KEY", "")

    def _prev(enc_key):
        """Existing plaintext credential, decrypted with the OLD key."""
        if old_key:
            os.environ["ENCRYPTION_KEY"] = old_key
        return security.decrypt_value(existing.get(enc_key, ""))

    prev_idrac_user = _prev("DEFAULT_CRED_USERNAME_ENC")
    prev_idrac_pass = _prev("DEFAULT_CRED_PASSWORD_ENC")
    prev_ssh_user = _prev("DEFAULT_SSH_USERNAME_ENC")
    prev_ssh_pass = _prev("DEFAULT_SSH_PASSWORD_ENC")

    # Now decide the key to write (rotate = brand new, else keep existing).
    fernet_key = "" if args.rotate_key else old_key
    if not fernet_key:
        fernet_key = Fernet.generate_key().decode("utf-8")
    os.environ["ENCRYPTION_KEY"] = fernet_key  # everything below encrypts with the NEW key

    flask_secret = existing.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

    # Admin login: an explicit --admin-password wins; otherwise keep whatever
    # hash is already in .env; otherwise fall back to the built-in default hash
    # (security._DEFAULT_ADMIN_*). The plaintext default password is never
    # referenced here - only its one-way hash.
    if args.admin_password:
        admin_salt, admin_hash = security.hash_password(args.admin_password)
    else:
        admin_salt = existing.get("ADMIN_PASSWORD_SALT") or security._DEFAULT_ADMIN_SALT
        admin_hash = existing.get("ADMIN_PASSWORD_HASH") or security._DEFAULT_ADMIN_HASH

    idrac_user = args.idrac_user or prev_idrac_user or security._DEFAULT_IDRAC_USER
    idrac_pass = args.idrac_pass or prev_idrac_pass or security._DEFAULT_IDRAC_PASS
    ssh_user = args.ssh_user or prev_ssh_user or security._DEFAULT_SSH_USER
    ssh_pass = args.ssh_pass or prev_ssh_pass or security._DEFAULT_SSH_PASS

    content = f"""# PS Automation - local secrets. NEVER commit this file (see .gitignore).
# Regenerate with: python generate_env.py

FLASK_SECRET_KEY={flask_secret}

ADMIN_USERNAME=Admin
ADMIN_PASSWORD_SALT={admin_salt}
ADMIN_PASSWORD_HASH={admin_hash}

LOGIN_MAX_ATTEMPTS={existing.get("LOGIN_MAX_ATTEMPTS", "5")}
LOGIN_LOCKOUT_SECONDS={existing.get("LOGIN_LOCKOUT_SECONDS", "120")}

ENCRYPTION_KEY={fernet_key}

DEFAULT_CRED_USERNAME_ENC={security.encrypt_value(idrac_user)}
DEFAULT_CRED_PASSWORD_ENC={security.encrypt_value(idrac_pass)}

DEFAULT_SSH_USERNAME_ENC={security.encrypt_value(ssh_user)}
DEFAULT_SSH_PASSWORD_ENC={security.encrypt_value(ssh_pass)}
"""
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    print("Wrote .env at:", ENV_PATH)
    print("Admin username: Admin")
    print("Restart the app for changes to take effect.")


if __name__ == "__main__":
    main()
