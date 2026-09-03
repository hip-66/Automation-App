# -*- coding: utf-8 -*-
import os
import re
import sys
import uuid
import time
import base64
import hashlib
import getpass
import platform
import subprocess
import threading
import queue
import logging
import json
import secrets as secrets_module
import concurrent.futures
from datetime import timedelta, datetime
from flask import Flask, jsonify, request, Response, send_from_directory, send_file, session, redirect

import security

APP_VERSION = "3.0.0"

# Initialize Flask App
app = Flask(__name__, static_folder='static', static_url_path='')

# Never let the browser serve a stale copy of app.js/styles.css/index.html
# after an update: max-age=0 forces a revalidation round-trip on every load
# (a cheap 304 on localhost when the file hasn't changed). Without this,
# browsers heuristically cache static files and can keep running old
# frontend code even after F5.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# Base paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Secrets / session (login gate)
# ---------------------------------------------------------------------------
# Create a default .env on a brand-new machine (no-op if one already exists),
# then load it. Even if this is skipped, login still works via the built-in
# default hash in security.get_admin_login().
security.ensure_env(BASE_DIR)
security.load_env(BASE_DIR)

# Session-signing key is REGENERATED on every server start (deliberately NOT
# the persistent FLASK_SECRET_KEY from .env): a login cookie from a previous
# app session becomes invalid the moment a new server starts, so every fresh
# launch of the app ALWAYS lands on the login page - nobody can reopen the
# app and continue an old session without re-authenticating.
app.secret_key = secrets_module.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Sessions are non-permanent (see api_login): the cookie expires when the
    # browser closes, so a restored Chrome session never reopens straight into
    # the app. Idle detection - 15 minutes of no real mouse/keyboard activity -
    # lives in the frontend (app.js), which warns and then redirects to /login.
    # This lifetime only applies if a session is ever made permanent again.
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=15),
)

LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCKOUT_SECONDS = int(os.environ.get("LOGIN_LOCKOUT_SECONDS", "120"))
_login_attempts = {}  # client ip -> {"count": int, "locked_until": epoch-seconds}
_PUBLIC_PATHS = {"/login", "/login.html", "/api/login", "/favicon.ico", "/api/copyright", "/api/ping"}


def _client_ip():
    return request.remote_addr or "unknown"


def _lockout_remaining(ip):
    rec = _login_attempts.get(ip)
    if not rec:
        return 0
    remaining = rec.get("locked_until", 0) - time.time()
    return int(remaining) if remaining > 0 else 0


def _register_login_failure(ip):
    rec = _login_attempts.setdefault(ip, {"count": 0, "locked_until": 0})
    rec["count"] += 1
    if rec["count"] >= LOGIN_MAX_ATTEMPTS:
        rec["locked_until"] = time.time() + LOGIN_LOCKOUT_SECONDS
        rec["count"] = 0


def _register_login_success(ip):
    _login_attempts.pop(ip, None)


@app.before_request
def _require_login():
    if request.path in _PUBLIC_PATHS or request.path.startswith("/images/"):
        return None
    if session.get("authenticated"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "unauthenticated"}), 401
    return redirect("/login")


# CSRF defense: a malicious web page open in another tab could otherwise use
# the browser's logged-in session cookie to silently fire a state-changing
# request at this app (start a destructive run, delete reports...). Every
# mutating request (POST/PUT/DELETE/PATCH) must therefore originate from this
# app's OWN page - verified by matching the Origin (or, if absent, the Referer)
# host to the server's own host. A cross-site request carries the attacker's
# origin and is rejected. This is on top of the SameSite=Lax cookie already
# set above (defense in depth). /api/login is exempt since it has no session
# yet to protect.
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_CSRF_EXEMPT_PATHS = {"/api/login"}

@app.before_request
def _csrf_protect():
    if request.method in _CSRF_SAFE_METHODS or request.path in _CSRF_EXEMPT_PATHS:
        return None
    from urllib.parse import urlparse
    host = request.host  # e.g. "127.0.0.1:5000" or "192.168.1.20:5000"
    origin = request.headers.get("Origin")
    referer = request.headers.get("Referer")
    source = origin or referer
    # Browsers reliably send Origin (or at least Referer) on cross-site
    # mutating requests, so a mismatch here is the CSRF signal. If neither is
    # present we allow it - a genuine same-origin fetch from this app always
    # carries one, and rejecting on absence would risk breaking edge cases
    # without closing any real cross-site hole.
    if source:
        if urlparse(source).netloc != host:
            server_logger.warning(f"CSRF blocked: source '{source}' != host '{host}' on {request.method} {request.path}")
            return jsonify({"ok": False, "error": "CSRF validation failed - request did not originate from the application."}), 403
    return None


@app.route('/login')
def login_page():
    return app.send_static_file('login.html')


@app.route('/api/login', methods=['POST'])
def api_login():
    ip = _client_ip()
    remaining = _lockout_remaining(ip)
    if remaining > 0:
        return jsonify({"ok": False, "error": "locked", "retry_after": remaining}), 429

    data = request.json or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    valid = security.verify_admin(username, password)
    if not valid:
        _register_login_failure(ip)
        server_logger.warning(f"Failed login attempt (username={username!r}) from {ip}")
        return jsonify({"ok": False, "error": "invalid_credentials"}), 401

    _register_login_success(ip)
    session.clear()
    session["authenticated"] = True
    session["user"] = username
    # Non-permanent session: the cookie dies when the browser closes, so
    # reopening Chrome (which restores the previous tabs) can never land
    # straight inside the app - at most it shows the login page. The 15-minute
    # IDLE timeout is enforced by the frontend (app.js) as before.
    session.permanent = False
    server_logger.info(f"Successful login from {ip}")
    return jsonify({"ok": True})


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({"ok": True})
SCRIPTS_DIR = os.path.join(BASE_DIR, "Scripts")
CHROMEDRIVERS_DIR = os.path.join(BASE_DIR, "Chromedrivers")
RESULTS_DIR = os.path.join(BASE_DIR, "Outputs")             # generated .docx reports only
REPORT_FOLDER_DIR = os.path.join(BASE_DIR, "Logs")          # logs + run history
GUIDES_DIR = os.path.join(BASE_DIR, "Guides")               # platform documentation (docx/xlsx)
# Per-run artifacts live in auto-created folders named
# "<script>_<date>_<time>" - under Logs/ for the run log (+ validation
# logs), and under Outputs/ for the generated .docx files.
HISTORY_FILE = os.path.join(REPORT_FOLDER_DIR, "history.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
PROFILES_FILE = os.path.join(BASE_DIR, "command_profiles.json")
ERROR_LOG_FILE = os.path.join(REPORT_FOLDER_DIR, "app_errors.log")
COMPANIES_FILE = os.path.join(BASE_DIR, "companies.json")
CUSTOM_SCRIPTS_FILE = os.path.join(BASE_DIR, "custom_scripts.json")
TARGETS_FILE = os.path.join(BASE_DIR, "target_groups.json")
# Persistent app-data dir OUTSIDE the app folder (under ProgramData). BOTH
# all-time counters (screenshots + time saved) live here so they SURVIVE version
# updates / re-deploys - copying a new build over the old one no longer resets
# them (that was the cause of the numbers "resetting" before).
_pd_root = os.environ.get("PROGRAMDATA") or os.environ.get("LOCALAPPDATA") or BASE_DIR
PERSISTENT_DATA_DIR = os.path.join(_pd_root, "PS Automation")
try:
    os.makedirs(PERSISTENT_DATA_DIR, exist_ok=True)
except Exception:
    PERSISTENT_DATA_DIR = BASE_DIR

# Persistent, all-time "screenshots captured" tally - CUMULATIVE and MONOTONIC
# (only grows; never drops when Output .docx are deleted). Now stored in
# PERSISTENT_DATA_DIR; a one-time migration below carries over any value from the
# old in-app-folder location so the existing count is preserved.
SCREENSHOT_STATS_FILE = os.path.join(PERSISTENT_DATA_DIR, "screenshot_stats.json")
_LEGACY_SCREENSHOT_STATS_FILE = os.path.join(BASE_DIR, "screenshot_stats.json")
_screenshot_lock = threading.Lock()

# Persistent, all-time "time saved" figure (stored in seconds), shown on the
# Dashboard as minutes / hours / days. Only servers that actually SUCCEEDED add
# to it. Monotonic.
TIME_SAVED_FILE = os.path.join(PERSISTENT_DATA_DIR, "time_saved.json")
_time_saved_lock = threading.Lock()

# Minutes of MANUAL work saved per SERVER for each automation (manual - automated),
# from the approved estimates. Keyed by lowercase script basename; unlisted
# scripts contribute 0. esxi_host_config is interactive (no fixed runtime) but is
# included per the approved table - it only adds when a run reports successes.
TIME_SAVED_PER_SERVER_MIN = {
    # Reports (screenshot validations)
    "idrac_report.py": 6,
    "dcui_report.py": 3,
    "vmare_esxi_host_client_report.py": 9,
    "windows_report.ps1": 5,
    "get_idrac_hostname.py": 1.5,
    "pingcheck.ps1": 0.5,
    # Configuration
    "change_ip.py": 2.5,
    "set_idrac_hostname.py": 2.5, "set-idrac-hostname.ps1": 2.5,
    "configure_ntp+dns.ps1": 4.5,
    "esxi_host_config.py": 9,
    # Storage / RAID
    "configure_raid1.ps1": 10,
    "non_raid.ps1": 5,
    # Power
    "power_down_servers.py": 1.5, "power-down-servers.ps1": 1.5,
    "power_on_servers.py": 1.5,
    # Validation (RedHat / ATP)
    "mdevalidation2.0.ps1": 9, "mdevalidation.ps1": 9,
}

# Same table keyed by the display name (filename WITHOUT extension), because
# history entries store script_name as the bare stem (e.g. "Configure_Raid1").
# Used by the analytics endpoint to derive "time saved per week" from history.
TIME_SAVED_PER_SERVER_BY_STEM = {os.path.splitext(k)[0]: v for k, v in TIME_SAVED_PER_SERVER_MIN.items()}

# Hebrew display names for the functional categories + risk levels, used only by
# the dashboard analytics endpoint (chart labels).
_CATEGORY_HE = {
    "report": "דוחות", "power": "כיבוי/הדלקה", "storage": "אחסון / RAID",
    "validation": "אימות", "redhat_validation": "אימות", "network": "רשת",
    "configuration": "קונפיגורציה", "general": "כללי",
}
_RISK_HE = {
    "read": ("קריאה בלבד", "--accent-success"),
    "config": ("קונפיגורציה", "--accent-warning"),
    "destructive": ("הרסני", "--accent-danger"),
}

# One-time migration of legacy folder names ("Results" -> "Output_Validation",
# "Report Folder" -> "Logs"). Runs before anything opens files inside them.
# Handles all states: plain rename when the new folder doesn't exist yet,
# content merge when both exist (files locked by a still-running older app
# instance are simply skipped and retried automatically on the next startup).
def _migrate_legacy_dir(old_name, new_path):
    old_path = os.path.join(BASE_DIR, old_name)
    if not os.path.isdir(old_path):
        return
    try:
        if not os.path.exists(new_path):
            os.rename(old_path, new_path)
            print(f"Migrated folder: {old_name} -> {os.path.basename(new_path)}")
            return
        # Both exist: move over anything not already present in the new folder
        import shutil
        for item in os.listdir(old_path):
            src = os.path.join(old_path, item)
            dst = os.path.join(new_path, item)
            try:
                if not os.path.exists(dst):
                    shutil.move(src, dst)
                elif os.path.isdir(src) and os.path.isdir(dst):
                    for inner in os.listdir(src):
                        inner_dst = os.path.join(dst, inner)
                        if not os.path.exists(inner_dst):
                            shutil.move(os.path.join(src, inner), inner_dst)
                    if not os.listdir(src):
                        os.rmdir(src)
            except OSError:
                pass  # locked by an older running instance - retry next startup
        if not os.listdir(old_path):
            os.rmdir(old_path)
            print(f"Merged and removed legacy folder: {old_name}")
    except OSError as e:
        print(f"Could not migrate '{old_name}' yet ({e}). Will retry on next startup.")

_migrate_legacy_dir("Results", RESULTS_DIR)
_migrate_legacy_dir("Output_Validation", RESULTS_DIR)   # short-lived interim name
_migrate_legacy_dir("Report Folder", REPORT_FOLDER_DIR)

# Ensure required directories exist (before the log handler attaches below)
os.makedirs(CHROMEDRIVERS_DIR, exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)
os.makedirs(SCRIPTS_DIR, exist_ok=True)
os.makedirs(REPORT_FOLDER_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Central logging: console + central error-log file (Report Folder/app_errors.log)
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
server_logger = logging.getLogger("server")
try:
    _err_handler = logging.FileHandler(ERROR_LOG_FILE, encoding="utf-8")
    _err_handler.setLevel(logging.WARNING)
    _err_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s'))
    logging.getLogger().addHandler(_err_handler)
except Exception as e:
    print(f"Could not attach error log file handler: {e}")

# Windows flag: never open an extra console window for child processes
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# ===========================================================================
# PROTECTED COPYRIGHT MODULE
# The strings below are encrypted (XOR + Base64) and integrity-signed.
# Any modification of the encrypted blocks breaks the signature and the
# application will refuse to start. Changes require the maintainer password,
# which is stored only as a salted SHA-256 hash (cannot be recovered).
# ===========================================================================
_CR_A = [19, 84, 7, 42, 99, 120, 5, 61]
_CR_B = [88, 23, 44, 91, 77, 8, 130, 201]
_CR_EN = "0f0nGFNKMx0NZUUiLCjDs3I6bgpFWEBROXMMHyxu7Kgztoe+QzlpUXhFRTwlfPHpQTF0TxEOYFk="
_CR_HE = "0f0nGFNKMx2Ph/vOmqBVUMTAJ/3Br5PqyMCMjNQoVVzExNC2tNrSrnjAv4zp3yIeh3TlqvdY0qaPiwyM2d8UHoiDkv36r5Dq8jf78pqWVVzE/NC/tNI="
_CR_SIG = "40c57d69f534f67d38c4f329bb18db6002f35bdd5c741acf014cb2f28be3e16b"
_CR_PW = "3a65f7d02245d7c36241a16119ba756a1745904c119085daf810cd9c4ef44f92"

def _cr_decode(blob):
    key = bytes(_CR_A + _CR_B)
    raw = base64.b64decode(blob)
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(raw)).decode("utf-8")

def _cr_verify():
    calc = hashlib.sha256((_CR_HE + _CR_EN + "PSAPP-CR-LOCK").encode("utf-8")).hexdigest()
    if calc != _CR_SIG:
        msg = "FATAL: Copyright integrity violation detected. Application halted."
        try:
            server_logger.critical(msg)
        except Exception:
            pass
        print("=" * 70)
        print(msg)
        print("The copyright section of this application is protected.")
        print("Restore the original files or contact the owners.")
        print("=" * 70)
        sys.exit(1)

_cr_verify()

def _cr_check_password(candidate):
    h = hashlib.sha256(("PS::" + str(candidate) + "::2026").encode("utf-8")).hexdigest()
    return h == _CR_PW

# ---------------------------------------------------------------------------
# In-memory store for execution runs
# ---------------------------------------------------------------------------
active_runs = {}
# Guards the check-then-launch sequence in /api/run so it's atomic - without
# this, two near-simultaneous requests (rapid re-clicks, multiple browser
# tabs, a network retry) could both see "nothing running yet" and both spawn
# a process for what was meant to be a single click.
_run_launch_lock = threading.Lock()
_history_lock = threading.Lock()
_data_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Companies / Stages taxonomy (companies.json)
# Drives the entry (landing) screen: Company -> Code Language -> Stage.
# Stored in a JSON file so new stages can be added from the UI ("Add More")
# without any code change. "general" is NOT a selectable company - it is only
# an internal fallback key that parse_script_location returns for loose or
# misplaced files (they simply won't appear under any company until moved into
# a real Company/<Python|PowerShell> folder).
# ---------------------------------------------------------------------------
DEFAULT_COMPANIES = {
    "nova": {
        "label": "Nova", "label_en": "Nova", "has_stages": True,
        "stages": [
            {"key": "swi", "label": "SWI", "label_en": "SWI"},
            {"key": "antivirus", "label": "Anti Virus", "label_en": "Anti Virus"},
            {"key": "atp", "label": "ATP", "label_en": "ATP"}
        ]
    },
    "cognyte": {
        "label": "Cognyte", "label_en": "Cognyte", "has_stages": False, "stages": []
    },
    "applied": {
        "label": "Applied", "label_en": "Applied", "has_stages": True,
        "stages": [
            {"key": "link_flex", "label": "Link Flex", "label_en": "Link Flex"}
        ]
    },
    "test": {
        "label": "Test", "label_en": "Test", "has_stages": False, "stages": []
    }
}

def load_companies():
    if os.path.exists(COMPANIES_FILE):
        try:
            with open(COMPANIES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                return data
        except Exception as e:
            server_logger.error(f"Error loading companies.json: {e}")
    save_companies(DEFAULT_COMPANIES)
    return dict(DEFAULT_COMPANIES)

def save_companies(companies):
    with open(COMPANIES_FILE, "w", encoding="utf-8") as f:
        json.dump(companies, f, ensure_ascii=False, indent=2)

def slugify_key(text):
    key = re.sub(r"[^a-z0-9_]+", "_", text.strip().lower()).strip("_")
    return key or f"item_{int(time.time())}"

# ---------------------------------------------------------------------------
# Display-name overrides for automations, keyed by Scripts-relative path.
# Written by the "Add Automation" flow when a nicer name/description is
# given for a file that just got moved into a company/stage/language folder.
# Company/stage/type themselves are NOT stored here - they are always
# derived live from the file's physical location (see scan_all_scripts).
# ---------------------------------------------------------------------------
def load_custom_scripts():
    if os.path.exists(CUSTOM_SCRIPTS_FILE):
        try:
            with open(CUSTOM_SCRIPTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            server_logger.error(f"Error loading custom_scripts.json: {e}")
    return {}

def save_custom_scripts(overrides):
    with open(CUSTOM_SCRIPTS_FILE, "w", encoding="utf-8") as f:
        json.dump(overrides, f, ensure_ascii=False, indent=2)

# ---------------------------------------------------------------------------
# Saved target (server) groups - reusable IP lists for the run wizard
# ---------------------------------------------------------------------------
def load_targets():
    if os.path.exists(TARGETS_FILE):
        try:
            with open(TARGETS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception as e:
            server_logger.error(f"Error loading target_groups.json: {e}")
    return []

def save_targets(targets):
    with open(TARGETS_FILE, "w", encoding="utf-8") as f:
        json.dump(targets, f, ensure_ascii=False, indent=2)

# ---------------------------------------------------------------------------
# Scheduled (one-time) automation runs. A background thread (_scheduler_watcher,
# defined further down) triggers each entry through the same _launch_run() path
# /api/run uses, once its scheduled_at time arrives.
# ---------------------------------------------------------------------------
SCHEDULES_FILE = os.path.join(BASE_DIR, "scheduled_runs.json")
_schedule_lock = threading.Lock()

def load_schedules():
    if os.path.exists(SCHEDULES_FILE):
        try:
            with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception as e:
            server_logger.error(f"Error loading scheduled_runs.json: {e}")
    return []

def save_schedules(schedules):
    with open(SCHEDULES_FILE, "w", encoding="utf-8") as f:
        json.dump(schedules, f, ensure_ascii=False, indent=2)

def resolve_safe_script_path(rel_path):
    """Resolve a Scripts-relative path and guarantee it stays inside SCRIPTS_DIR."""
    candidate = os.path.normpath(os.path.join(SCRIPTS_DIR, rel_path))
    scripts_root = os.path.normpath(SCRIPTS_DIR)
    if not (candidate == scripts_root or candidate.startswith(scripts_root + os.sep)):
        return None
    return candidate

# ---------------------------------------------------------------------------
# Persistent app settings (settings.json)
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "results_dir": "",        # empty = default Results folder next to the app
    "auto_move_docx": True    # move generated .docx reports into Results automatically
}

def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                settings.update({k: saved[k] for k in DEFAULT_SETTINGS if k in saved})
        except Exception as e:
            server_logger.error(f"Error loading settings.json: {e}")
    return settings

def save_settings(settings):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

def effective_results_dir():
    settings = load_settings()
    custom = (settings.get("results_dir") or "").strip()
    if custom:
        try:
            os.makedirs(custom, exist_ok=True)
            return custom
        except Exception as e:
            server_logger.error(f"Invalid custom results dir '{custom}': {e}")
    return RESULTS_DIR

# ---------------------------------------------------------------------------
# Command profiles (server types) - stored in command_profiles.json so new
# server types can be added without touching the application code.
# ---------------------------------------------------------------------------
DEFAULT_PROFILES = {
    "atp": {
        "label": "ATP",
        "builtin": True,
        "commands": "echo hostname\nhostname\necho ----------------------------------------------------\necho ifconfig -a\nifconfig -a\necho ----------------------------------------------------\necho ip a\nip a\necho ----------------------------------------------------\nyum repolist\necho ----------------------------------------------------\nlsblk --list\necho ----------------------------------------------------\ncat /etc/resolv.conf\necho ----------------------------------------------------\ncat /etc/chrony.conf\necho ----------------------------------------------------\nfor i in $(ls /etc/*release); do echo ===$i===; cat $i; done\necho ----------------------------------------------------\ndate\necho ----------------------------------------------------\necho End Of Validation"
    },
    "tbos": {
        "label": "TBOS",
        "builtin": True,
        "commands": "hostname\necho \"=== TBOS Service Status ===\"\nsystemctl status tbos\necho \"=== Disk Usage ===\"\ndf -h\necho \"=== Network Interfaces ===\"\nip a\ndate\necho \"End Of Validation\""
    },
    "kafka": {
        "label": "KAFKA",
        "builtin": True,
        "commands": "hostname\necho \"=== Kafka Service Status ===\"\nsystemctl status kafka\nsystemctl status zookeeper\necho \"=== Disk Usage ===\"\ndf -h\necho \"=== Java Version ===\"\njava -version\ndate\necho \"End Of Validation\""
    },
    "worker": {
        "label": "WORKER",
        "builtin": True,
        "commands": "hostname\necho \"=== Docker Containers ===\"\ndocker ps -a\necho \"=== Resource Usage ===\"\nfree -m\ndf -h\ndate\necho \"End Of Validation\""
    }
}

def load_profiles():
    if os.path.exists(PROFILES_FILE):
        try:
            with open(PROFILES_FILE, "r", encoding="utf-8") as f:
                profiles = json.load(f)
            if isinstance(profiles, dict) and profiles:
                return profiles
        except Exception as e:
            server_logger.error(f"Error loading command_profiles.json: {e}")
    save_profiles(DEFAULT_PROFILES)
    return dict(DEFAULT_PROFILES)

def save_profiles(profiles):
    with open(PROFILES_FILE, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)

# ---------------------------------------------------------------------------
# Scripts engine - LOCATION-DRIVEN discovery.
#
# Company / Stage / Language are derived LIVE from where a .py/.ps1 file
# physically sits under Scripts/, e.g.:
#   Scripts/Nova/ATP/PowerShell/x.ps1     -> company=nova, stage=atp, powershell
#   Scripts/Cognyte/Python/x.py           -> company=cognyte, stage=None, python
# There is no static registry to keep in sync - moving a file (even by hand
# in Explorer) re-categorizes it automatically. Only a human-friendly
# display name/description can optionally be overridden, kept in
# custom_scripts.json and keyed by relative path.
#
# The legacy "ATP Validation" (1.0) is excluded from the menu by filename
# regardless of where it lives - only ATP Validation 2.0 is exposed.
# ---------------------------------------------------------------------------
# Scripts hidden from the catalog (file kept on disk, just not listed). Each is
# a functional duplicate of a Python script we already expose, so only the
# Python one is shown:
#   * power-down-servers.ps1  == power_down_servers.py ("racadm serveraction powerdown")
#   * set-idrac-hostname.ps1  == set_idrac_hostname.py (same iDRAC hostname push)
EXCLUDED_SCRIPT_BASENAMES = {"mdevalidation.ps1", "power-down-servers.ps1", "set-idrac-hostname.ps1"}
AUTO_IGNORED_DIRS = {"validations", "__pycache__"}

# File types treated as "report products" of a script run - these get swept
# into the run's dated Outputs/<run> folder and listed on the Outputs page.
REPORT_PRODUCT_EXTENSIONS = (".docx", ".csv")

# Curated descriptions (subtitle text shown under the automation name) for
# the scripts shipped with the app, keyed by lowercase basename. The NAME
# itself is always the exact filename on disk (no auto-prettifying) - only
# the description here is polish. Anything not listed just gets a generic
# fallback description.
KNOWN_SCRIPT_DISPLAY = {
    "dcui_report.py": {
        "description": "דוח צילומי מסך של ממשק ה-DCUI דרך ה-iDRAC (שילוב RACADM מקומי)",
        "description_en": "DCUI interface screenshots report via iDRAC (with local RACADM)"
    },
    "idrac_report.py": {
        "description": "דוח מערכת מפורט של ה-iDRAC עם צילומי מסך מהממשק",
        "description_en": "Detailed iDRAC system report with interface screenshots"
    },
    "vmare_esxi_host_client_report.py": {
        "description": "גרסה חלופית של דוח ממשק ESXi Host Client",
        "description_en": "Alternate version of the ESXi Host Client report"
    },
    "mdevalidation2.0.ps1": {
        "description": "ולידציית ATP מתקדמת: סטטוס לכל שרת, שליפת Hostname, לוג נפרד עם תאריך ושעה לכל שרת",
        "description_en": "Advanced ATP validation: per-server status, hostname detection, timestamped log per server"
    },
    "mdevalidation.ps1": {
        "description": "ולידציית MDE/ATP בסיסית דרך SSH (הגרסה שלפני 2.0) - מריץ את אותן פקודות בדיקה על כל כתובת ברשימה, עם לוג נפרד לכל שרת",
        "description_en": "Basic MDE/ATP validation over SSH (the version prior to 2.0) - runs the same set of check commands against every address in the list, with a separate log per server"
    },
    "windows_report.ps1": {
        "description": "צילומי מסך מקומיים של Server Manager, חיבורי רשת, ניהול דיסקים והפעלת Windows, בתוך מסמך Word",
        "description_en": "Local screenshots of Server Manager, Network Connections, Disk Management and Windows Activation, assembled into a Word report"
    },
    "pingcheck.ps1": {
        "description": "סריקת פינג לטווחי כתובות IP (או כתובות בודדות), עם דוח CSV של שרתים זמינים/לא זמינים",
        "description_en": "Ping-sweeps IP ranges (or single addresses) and exports a CSV report of which servers are reachable"
    },
    "configure_raid1.ps1": {
        "description": "מגדיר RAID1 + UEFI על שרתי iDRAC9: המרת דיסקים ל-RAID, יצירת vDisk1, ואתחול ל-UEFI. מריץ כל שרת בסשן נפרד (במקביל), עם דוח CSV מאוחד",
        "description_en": "Configures RAID1 + UEFI on iDRAC9 servers: converts disks to RAID, creates vDisk1, sets UEFI boot mode. Runs each server in its own parallel session, with a merged CSV report"
    },
    "non_raid.ps1": {
        "description": "המרת דיסקים פיזיים במצב Ready ל-Non-RAID דרך Dell RACADM - לא נוגע בדיסקים במצב Online",
        "description_en": "Converts physical disks in Ready state to Non-RAID via Dell RACADM - does not touch disks in Online state"
    },
    "power-down-servers.ps1": {
        "description": "כיבוי שרתים לפי כתובת iDRAC (racadm serveraction powerdown) - לפי טווח (התחלה + כמות) או רשימת כתובות",
        "description_en": "Power down servers by iDRAC IP (racadm serveraction powerdown) - by range (start + count) or a list of addresses"
    },
    "power_down_servers.py": {
        "description": "כיבוי שרתים לפי כתובת iDRAC (racadm serveraction powerdown) - לפי טווח (התחלה + כמות) או רשימת כתובות",
        "description_en": "Power down servers by iDRAC IP (racadm serveraction powerdown) - by range (start + count) or a list of addresses"
    },
    "power_on_servers.py": {
        "description": "הדלקת שרתים לפי כתובת iDRAC (racadm serveraction powerup) - לפי טווח (התחלה + כמות) או רשימת כתובות",
        "description_en": "Power on servers by iDRAC IP (racadm serveraction powerup) - by range (start + count) or a list of addresses"
    },
    "set-idrac-hostname.ps1": {
        "description": "שינוי שם (Hostname) של iDRAC לפי רשימת כתובות IP ורשימת שמות מקבילה - הכתובת הראשונה מקבלת את השם הראשון וכן הלאה",
        "description_en": "Rename iDRAC hostname (DNSRacName) from a parallel IP list and hostname list - first IP gets the first name, and so on"
    },
    "set_idrac_hostname.py": {
        "description": "שינוי שם (Hostname) של iDRAC לפי רשימת כתובות IP ורשימת שמות מקבילה - הכתובת הראשונה מקבלת את השם הראשון וכן הלאה",
        "description_en": "Rename iDRAC hostname (DNSRacName) from a parallel IP list and hostname list - first IP gets the first name, and so on"
    },
    "get_idrac_hostname.py": {
        "description": "שליפת שם ה-iDRAC (Hostname) הנוכחי לפי רשימת כתובות IP - לאימות מהיר שהשינוי אכן נקלט",
        "description_en": "Reads the CURRENT iDRAC hostname (DNSRacName) for a list of IPs - a quick way to confirm a rename actually took effect"
    },
    "change_ip.py": {
        "description": "שינוי כתובת ה-IP של iDRAC לפי רשימת כתובות נוכחיות ורשימת כתובות חדשות מקבילה (הכתובת הראשונה מקבלת את החדשה הראשונה וכן הלאה), עם Netmask/Gateway אחידים לכל השרתים. כל שרת בסשן נפרד (במקביל), עם אימות שה-IP החדש עלה",
        "description_en": "Change the iDRAC IP from a parallel list of current IPs and new IPs (first current gets the first new, and so on), with a shared Netmask/Gateway for all servers. Each server runs in its own parallel session, and success is verified by reaching the new IP"
    },
    "esxi_host_config.py": {
        "description": "כלי הגדרה ל-ESXi (מקביל ל-racadm): מתחבר לשרת/ים, שואב את התצורה החיה, ופותח חלון עם טאבים להגדרת NTP, רישיון, Virtual Switches, Port Groups ו-VMkernel (התחברות עם שם המשתמש/סיסמה המוגדרים)",
        "description_en": "ESXi configuration tool (the ESX analog of racadm): connects to host(s), pulls the live config, and opens a tabbed window to set NTP, licensing, Virtual Switches, Port Groups and VMkernel NICs (uses the configured login)"
    },
    "configure_ntp+dns.ps1": {
        "description": "עדכון שרתי DNS ו-NTP על שרתי Red Hat דרך SSH - מוסיף כתובות לראש /etc/resolv.conf ו-/etc/chrony.conf",
        "description_en": "Updates DNS and NTP servers on Red Hat server(s) over SSH - pushes addresses to the top of /etc/resolv.conf and /etc/chrony.conf"
    }
}

def default_inputs_for_type(script_type):
    if script_type == "python":
        return ["mode", "base_ip", "start_suffix", "count", "ips", "username", "password", "chromedriver"]
    return ["ips", "username", "password", "use_default_creds", "commands"]

# Per-script stdin/inputs overrides (keyed by lowercase basename) - a manual
# escape hatch for the handful of cases worth pinning explicitly. Anything
# NOT listed here goes through detect_script_inputs() below, which reads the
# script's own source and figures out what it actually needs (see there).
_PY_REPORT_INPUTS = ["mode", "base_ip", "start_suffix", "count", "ips", "username", "password", "chromedriver"]
INPUTS_OVERRIDE_BY_BASENAME = {
    "dcui_report.ps1": _PY_REPORT_INPUTS,
    "idrac_report.ps1": _PY_REPORT_INPUTS,
    "vmare_report.ps1": _PY_REPORT_INPUTS,
    "vmare_esxi_host_client_report.ps1": _PY_REPORT_INPUTS,
    # Power-down: choose a range (base IP + start + count) OR a plain IP list;
    # username/password default to the app's configured iDRAC credentials but
    # are shown and editable - never silently baked in.
    "power-down-servers.ps1": ["mode", "base_ip", "start_suffix", "count", "ips", "username", "password"],
    "power_down_servers.py": ["mode", "base_ip", "start_suffix", "count", "ips", "username", "password"],
    "power_on_servers.py": ["mode", "base_ip", "start_suffix", "count", "ips", "username", "password"],
    # Set-hostname: an IP list plus a parallel hostname list (line N <-> line N).
    # username/password default to the app's configured credentials but can
    # be overridden.
    "set-idrac-hostname.ps1": ["ips", "hostnames", "username", "password"],
    "set_idrac_hostname.py": ["ips", "hostnames", "username", "password"],
    # Get-hostname (read-only verification): just the target IP(s) + login -
    # no hostname list, it only reads the CURRENT DNSRacName.
    "get_idrac_hostname.py": ["ips", "username", "password"],
    # ESXi config tool: target IP(s) + login (defaults to the app's configured
    # credentials, editable); it opens its own GUI for everything else.
    "esxi_host_config.py": ["ips", "username", "password"],
    # MDE/ATP Validation 2.0: target IP(s) + an editable SSH login (defaults
    # to the app's configured SSH credentials, but can be overridden per-run
    # like every other automation). The validation command set stays baked in.
    "mdevalidation2.0.ps1": ["ips", "username", "password"],
    # DNS/NTP updater: target IP(s), the DNS/NTP servers to push (each pushed
    # to the TOP of the list on the remote Red Hat host), and an editable SSH
    # login (defaults to the app's configured SSH credentials).
    "configure_ntp+dns.ps1": ["ips", "dns", "ntp", "username", "password"],
    # Non-RAID conversion (Ready -> Non-RAID): choose a range (base IP + start +
    # count) OR a plain IP list; plus an editable iDRAC login (defaults to the
    # app's configured credentials, overridable per-run like the others).
    "non_raid.ps1": ["mode", "base_ip", "start_suffix", "count", "ips", "username", "password"],
    # Configure RAID1 (RAID1 + UEFI): target IP(s), an editable iDRAC login, and
    # the RAID (virtual disk) NAME - defaults to "vDisk1" in the form but can be
    # changed per-run. A RAID must have a name, so the field is required (both
    # the form and the script reject an empty value). Passed to the script as
    # PSAUTO_RAID_NAME.
    "configure_raid1.ps1": ["ips", "username", "password", "use_default_creds", "raid_name"],
    # Configure Raid (Test, interactive GUI): target iDRAC IP(s) + login. The
    # script pings, reads the disks, and opens its own window to pick RAID
    # level(s) + disks + name(s) - so no extra form fields are needed here.
    "configure_raid.py": ["ips", "username", "password"],
    # Change iDRAC IP: a CURRENT-IP list (ips) + a parallel NEW-IP list (newips),
    # both range-expandable in the form; a shared Netmask + Gateway for all
    # servers; and an editable iDRAC login.
    "change_ip.py": ["ips", "newips", "netmask", "gateway", "username", "password"],
}

_RE_READHOST_PROMPT = re.compile(r'read-host\s*(?:-prompt\s*)?["\']([^"\']*)["\']', re.IGNORECASE)
_RE_WRAPPER_PY_CALL = re.compile(r'^\s*python[\s\.]', re.IGNORECASE | re.MULTILINE)
_RE_PY_INPUT_PROMPT = re.compile(r'input\(\s*["\']([^"\']*)["\']', re.IGNORECASE)

def _prompt_mentions(prompts, *keywords):
    return any(any(kw in p.lower() for kw in keywords) for p in prompts)

def detect_script_inputs(full_path, script_type):
    """Reads a script's own source and infers which form fields it actually
    needs - so a brand-new script dropped into the Scripts folder gets a
    matching form automatically, without any code change here.

    Recognizes a handful of common shapes:
      - PowerShell wrapper that just calls a Python engine -> same inputs
        as a Python report script (addresses, creds, Chrome version).
      - Remote SSH validation driven by commands.txt (MDE-style) -> IP
        list + credentials + a commands editor.
      - Local IP/range scanner (Read-Host / input() prompt mentioning an
        IP or address) -> just the IP list field, plus credentials only if
        the script also prompts for a username/password.
      - Selenium/Chrome-driven script -> adds the Chrome version field.
      - No interactive input and no target list at all -> empty form,
        "click Run" is enough.
    """
    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return default_inputs_for_type(script_type)

    lower = content.lower()

    if script_type == "python":
        if "chromedriver" in lower or "selenium" in lower or "webdriver" in lower:
            return _PY_REPORT_INPUTS
        prompts = [m.group(1) for m in _RE_PY_INPUT_PROMPT.finditer(content)]
        if _prompt_mentions(prompts, "ip", "address", "כתובת"):
            return ["ips"]
        if prompts:
            return ["ips"]  # some interactive prompt exists - IP list is the safest guess
        return []  # no input() at all - fully non-interactive

    # PowerShell: a thin wrapper that just delegates to a Python engine
    if _RE_WRAPPER_PY_CALL.search(content) and len(content.strip().splitlines()) <= 15:
        return _PY_REPORT_INPUTS

    references_commands_file = "commands.txt" in lower
    references_addresses_file = "addresses.txt" in lower
    uses_remote_exec = any(tool in lower for tool in ("plink", "invoke-command", "new-pssession", "enter-pssession"))
    uses_selenium_or_chrome = "chromedriver" in lower or "selenium" in lower

    prompts = [m.group(1) for m in _RE_READHOST_PROMPT.finditer(content)]
    mentions_ip_prompt = _prompt_mentions(prompts, "ip", "address", "כתובת") or references_addresses_file
    mentions_username_prompt = _prompt_mentions(prompts, "user", "שם משתמש")
    mentions_password_prompt = _prompt_mentions(prompts, "password", "סיסמ")

    if references_commands_file and uses_remote_exec:
        return ["ips", "username", "password", "use_default_creds", "commands"]

    inputs = []
    if mentions_ip_prompt:
        inputs.append("ips")
    if mentions_username_prompt or mentions_password_prompt:
        inputs += ["username", "password", "use_default_creds"]
    if references_commands_file:
        inputs.append("commands")
    if uses_selenium_or_chrome:
        inputs.append("chromedriver")

    if not inputs:
        # Some unrecognized interactive prompt exists - default to just an
        # IP list rather than the full MDE-style protocol.
        return ["ips"] if prompts else []

    return inputs

def label_variants(entry):
    return {
        (entry.get("label") or "").strip().lower(),
        (entry.get("label_en") or "").strip().lower()
    } - {""}

def company_folder_map(companies):
    """company_key -> folder name (uses the Hebrew/company label as-is)."""
    return {key: (c.get("label") or key) for key, c in companies.items()}

def ensure_scripts_tree(companies):
    """Create Scripts/<Company>/[<Stage>/]<Python|PowerShell> for every
    company/stage so the physical folders always mirror the UI taxonomy."""
    for key, company in companies.items():
        if key == "general":
            continue  # "general" has no dedicated folder - it's the fallback bucket
        base = os.path.join(SCRIPTS_DIR, company.get("label") or key)
        if company.get("has_stages"):
            for stage in company.get("stages", []):
                stage_dir = os.path.join(base, stage.get("label") or stage["key"])
                os.makedirs(os.path.join(stage_dir, "Python"), exist_ok=True)
                os.makedirs(os.path.join(stage_dir, "PowerShell"), exist_ok=True)
        else:
            os.makedirs(os.path.join(base, "Python"), exist_ok=True)
            os.makedirs(os.path.join(base, "PowerShell"), exist_ok=True)

def parse_script_location(rel_path, companies):
    """Derive (company_key, stage_key, matched) from a Scripts-relative path."""
    parts = rel_path.split("/")
    if len(parts) < 2:
        return "general", None, False  # loose file directly under Scripts/

    company_by_label = {}
    for key, c in companies.items():
        for variant in label_variants(c) | {key.lower()}:
            company_by_label[variant] = key

    company_key = company_by_label.get(parts[0].strip().lower())
    if not company_key or company_key == "general":
        return "general", None, False

    company = companies[company_key]
    remaining = parts[1:]
    if not remaining:
        return "general", None, False

    # Flat company (no stage layer): <Company>/<Python|PowerShell>/file
    if remaining[0].strip().lower() in ("python", "powershell"):
        return company_key, None, True

    # Staged company: <Company>/<Stage>/<Python|PowerShell>/file
    if len(remaining) >= 2 and remaining[1].strip().lower() in ("python", "powershell"):
        stage_label = remaining[0].strip().lower()
        for s in company.get("stages", []):
            if stage_label in (label_variants(s) | {s["key"].lower()}):
                return company_key, s["key"], True

    return "general", None, False

_RISK_DESTRUCTIVE_KEYWORDS = ("power-down", "power_down", "powerdown", "power-on", "power_on", "powerup", "power-up", "non_raid", "raid-uefi", "raid_uefi", "configure_raid", "raid1")
_RISK_CONFIG_KEYWORDS = ("esxi_host_config", "set_idrac_hostname", "set-idrac-hostname", "dns-ntp", "dns_ntp", "ntp+dns", "change_ip", "change-ip")

def classify_script_risk(basename_lower):
    """read | config | destructive - a UI-only categorization (drives the
    risk badge and the destructive-action CONFIRM gate), not a security
    boundary. Anything not matched defaults to "read"."""
    if any(k in basename_lower for k in _RISK_DESTRUCTIVE_KEYWORDS):
        return "destructive"
    if any(k in basename_lower for k in _RISK_CONFIG_KEYWORDS):
        return "config"
    return "read"

# Functional category for the Run Automation catalog - groups automations by
# WHAT THEY DO (independent of the read/config/destructive risk axis) so the
# catalog can be filtered by a category sidebar. Keyword-based on the filename;
# order matters (first match wins). Anything unmatched falls back to "general".
_CATEGORY_RULES = [
    ("report",        ("report", "dcui")),
    ("power",         ("power-down", "power_down", "powerdown", "power-on", "power_on", "powerup", "power-up")),
    ("storage",       ("raid", "disk")),
    ("validation",    ("validation", "validate", "mde")),
    ("network",       ("dns", "ntp", "network")),
    ("configuration", ("config", "hostname", "setup", "set-idrac", "set_idrac", "dns-ntp")),
]

# Explicit per-file category assignments (exact lowercase basename). Checked
# before the keyword rules, so these always win.
_CATEGORY_BY_BASENAME = {
    "mdevalidation2.0.ps1":   "redhat_validation",
    "pingcheck.ps1":          "general",
    # Would otherwise auto-match the "network" keyword rule (dns/ntp) before
    # reaching "configuration" - this is a config-push tool, not a report.
    "configure_ntp+dns.ps1":  "configuration",
    # Changing an iDRAC's IP is an iDRAC configuration task (grouped with the
    # other set-* tools), not a generic "network" report.
    "change_ip.py":           "configuration",
    # Read-only hostname check (makes no changes) - would otherwise auto-match
    # the "configuration" keyword rule via "hostname" in the filename.
    "get_idrac_hostname.py":  "report",
}

def classify_script_category(basename_lower):
    if basename_lower in _CATEGORY_BY_BASENAME:
        return _CATEGORY_BY_BASENAME[basename_lower]
    for category, keywords in _CATEGORY_RULES:
        if any(k in basename_lower for k in keywords):
            return category
    return "general"

# Scripts whose "username"/"password" form fields authenticate over SSH
# (plink) rather than iDRAC/racadm - the two use DIFFERENT default accounts
# (SSH default: DEFAULT_SSH_USERNAME_ENC/PASSWORD_ENC vs iDRAC default:
# DEFAULT_CRED_USERNAME_ENC/PASSWORD_ENC), so the frontend needs to know which
# one to pre-fill. Exposed as each script's "cred_kind" ("ssh" or "idrac").
_SSH_CRED_BASENAMES = {"configure_ntp+dns.ps1", "mdevalidation2.0.ps1"}

def classify_cred_kind(basename_lower):
    return "ssh" if basename_lower in _SSH_CRED_BASENAMES else "idrac"

def scan_all_scripts():
    """Walk Scripts/ and build the full automations list, tagging each entry
    with company/stage/type derived purely from its physical location."""
    if not os.path.exists(SCRIPTS_DIR):
        return []

    companies = load_companies()
    overrides = load_custom_scripts()  # {rel_path: {name, description, description_en}}
    discovered = []

    for root, dirs, files in os.walk(SCRIPTS_DIR):
        dirs[:] = [d for d in dirs if d.lower() not in AUTO_IGNORED_DIRS]
        for fname in sorted(files):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in (".py", ".ps1"):
                continue
            if fname.lower() in EXCLUDED_SCRIPT_BASENAMES:
                continue

            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, SCRIPTS_DIR).replace("\\", "/")
            script_type = "python" if ext == ".py" else "powershell"
            company_key, stage_key, matched = parse_script_location(rel_path, companies)

            known = KNOWN_SCRIPT_DISPLAY.get(fname.lower(), {})
            override = overrides.get(rel_path, {})
            # The displayed name is exactly the filename (no extension) as it
            # sits on disk - no auto "prettifying". An explicit name typed in
            # the Add Automation dialog (override) still wins if set.
            exact_name = os.path.splitext(fname)[0]

            inputs = INPUTS_OVERRIDE_BY_BASENAME.get(fname.lower())
            if inputs is None:
                inputs = detect_script_inputs(full_path, script_type)

            discovered.append({
                "id": "s_" + slugify_key(rel_path),
                "name": override.get("name") or exact_name,
                "filename": rel_path,
                "type": script_type,
                "description": override.get("description") or known.get("description") or "סקריפט שזוהה בתיקיית Scripts",
                "description_en": override.get("description_en") or known.get("description_en") or "Script detected in the Scripts folder",
                "inputs": inputs,
                "company": company_key,
                "stage": stage_key,
                "location_matched": matched,
                "risk": classify_script_risk(fname.lower()),
                "category": classify_script_category(fname.lower()),
                "cred_kind": classify_cred_kind(fname.lower())
            })
    return discovered

def get_all_scripts():
    """Public entry point used across the app - the full automations list."""
    return scan_all_scripts()

def filter_scripts(scripts, company=None, stage=None, script_type=None):
    result = scripts
    if company:
        result = [s for s in result if (s.get("company") or "") == company]
    if stage:
        result = [s for s in result if (s.get("stage") or "") == stage]
    if script_type:
        result = [s for s in result if s.get("type") == script_type]
    return result

# Make sure the physical Company/Stage/Language tree exists on every startup
# (idempotent - creates only what's missing, never touches existing files).
try:
    ensure_scripts_tree(load_companies())
except Exception as e:
    server_logger.error(f"Failed to ensure scripts folder tree: {e}")

# ---------------------------------------------------------------------------
# History tracking helpers (full run audit: who, when, where, results)
# ---------------------------------------------------------------------------
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            server_logger.error(f"Error loading history.json: {e}")
    return []

def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        server_logger.error(f"Error saving history.json: {e}")

# ---------------------------------------------------------------------------
# Persistent "screenshots captured" tally (cumulative, monotonic)
# ---------------------------------------------------------------------------
def _load_screenshot_total():
    if os.path.exists(SCREENSHOT_STATS_FILE):
        try:
            with open(SCREENSHOT_STATS_FILE, "r", encoding="utf-8") as f:
                return int(json.load(f).get("total", 0))
        except Exception as e:
            server_logger.error(f"Error loading screenshot_stats.json: {e}")
    return 0

def _save_screenshot_total(total):
    # Atomic write (temp file + replace) so a crash mid-write can't corrupt
    # the counter.
    try:
        tmp = SCREENSHOT_STATS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"total": int(total)}, f)
        os.replace(tmp, SCREENSHOT_STATS_FILE)
    except Exception as e:
        server_logger.error(f"Error saving screenshot_stats.json: {e}")

def _count_all_docx_screenshots():
    total = 0
    results_dir = effective_results_dir()
    if os.path.exists(results_dir):
        for root, _dirs, files in os.walk(results_dir):
            for file in files:
                if file.lower().endswith(".docx"):
                    total += count_screenshots_in_docx(os.path.join(root, file))
    return total

def ensure_screenshot_seed():
    """One-time seed: if the stats file doesn't exist yet, initialize the
    counter from the screenshots already present in existing .docx on disk, so
    the all-time number STARTS at the true current total. Called once at
    startup - guaranteeing the file exists before any run can finish, which is
    what prevents a run's own new .docx from being counted twice (once by a
    disk seed, once by the per-run increment)."""
    with _screenshot_lock:
        if not os.path.exists(SCREENSHOT_STATS_FILE):
            # Migrate the old in-app-folder counter to the new persistent
            # location so the existing all-time total is preserved (not reset).
            migrated = None
            try:
                if os.path.exists(_LEGACY_SCREENSHOT_STATS_FILE):
                    with open(_LEGACY_SCREENSHOT_STATS_FILE, "r", encoding="utf-8") as f:
                        migrated = int(json.load(f).get("total", 0))
            except Exception as e:
                server_logger.warning(f"Screenshot counter migration read failed: {e}")
            _save_screenshot_total(migrated if migrated is not None else _count_all_docx_screenshots())
            if migrated is not None:
                server_logger.info(f"Migrated screenshot counter ({migrated}) to {SCREENSHOT_STATS_FILE}")

def get_screenshot_total():
    """Return the all-time screenshot tally (cumulative, monotonic)."""
    ensure_screenshot_seed()
    with _screenshot_lock:
        return _load_screenshot_total()

def add_screenshots_to_total(n):
    """Add the screenshots produced by ONE finished run. Deliberately does NOT
    seed from disk: by the time this runs the run's .docx is already on disk,
    so a disk seed here would count it twice. The startup seed (called once
    before any run can finish) is what establishes the baseline; this only ever
    reads the current stored total and adds this run's new screenshots."""
    if n <= 0:
        return
    with _screenshot_lock:
        _save_screenshot_total(_load_screenshot_total() + n)

# --------------------------------------------------------------------------
# Cumulative "time saved" tally (persistent, survives version updates)
# --------------------------------------------------------------------------
def _load_time_saved():
    if os.path.exists(TIME_SAVED_FILE):
        try:
            with open(TIME_SAVED_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            return {"seconds": int(d.get("seconds", 0)),
                    "runs": int(d.get("runs", 0)),
                    "servers": int(d.get("servers", 0))}
        except Exception as e:
            server_logger.error(f"Error loading time_saved.json: {e}")
    return {"seconds": 0, "runs": 0, "servers": 0}

def _save_time_saved(d):
    try:
        tmp = TIME_SAVED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, TIME_SAVED_FILE)
    except Exception as e:
        server_logger.error(f"Error saving time_saved.json: {e}")

def get_time_saved():
    """Return the cumulative time-saved figure {seconds, runs, servers}."""
    with _time_saved_lock:
        return _load_time_saved()

def add_time_saved(run_info):
    """Grow the cumulative time-saved total from ONE finished run, counting ONLY
    servers that actually SUCCEEDED. Time added = (per-server estimate for this
    script) x (successful servers). Scripts that don't emit per-server markers
    but COMPLETED are credited their target-server count (>=1); a FAILED run
    with no successes adds nothing."""
    try:
        basename = (run_info.get("script_filename") or "").lower()
        per_server = TIME_SAVED_PER_SERVER_MIN.get(basename)
        if not per_server:
            return
        # ANY failure -> add NOTHING at all. The run must have finished as
        # "completed" AND no individual server may have failed. A failed/killed
        # run, or a completed run with even one failed server, contributes 0.
        if run_info.get("status") != "completed":
            return
        servers = run_info.get("servers", []) or []
        if any((s.get("status") and s.get("status") != "success") for s in servers):
            return
        success_count = len([s for s in servers if s.get("status") == "success"])
        if success_count == 0:
            # completed run with no per-server markers -> credit the targets
            targets = run_info.get("target_servers") or []
            success_count = len(targets) if targets else 1
        if success_count <= 0:
            return
        add_seconds = int(round(per_server * 60 * success_count))
        with _time_saved_lock:
            d = _load_time_saved()
            d["seconds"] += add_seconds
            d["runs"] += 1
            d["servers"] += success_count
            _save_time_saved(d)
        server_logger.info(f"Time saved +{add_seconds}s ({success_count} server(s), {basename})")
    except Exception as e:
        server_logger.warning(f"Time-saved tally failed: {e}")

def get_local_ip():
    """Best-effort local (LAN) IP of the machine running this server - used
    to identify which workstation triggered a given run."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packet is actually sent
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return ""
    finally:
        s.close()

_LOCAL_IP_CACHE = None

def cached_local_ip():
    global _LOCAL_IP_CACHE
    if _LOCAL_IP_CACHE is None:
        _LOCAL_IP_CACHE = get_local_ip()
    return _LOCAL_IP_CACHE

def add_history_entry(run_id, script_name, status, servers=None, script_id=None,
                       company=None, stage=None, params=None, login_user=None):
    with _history_lock:
        history = load_history()
        now = time.localtime()
        entry = {
            "run_id": run_id,
            "script_id": script_id or "",                     # for "Run Again"
            "script_name": script_name,                       # which process ran
            "company": company or "",
            "stage": stage or "",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", now),
            "date": time.strftime("%Y-%m-%d", now),           # run date
            "start_time": time.strftime("%H:%M:%S", now),     # start time
            "end_time": "",                                   # end time
            "duration": "",                                   # total duration
            "duration_seconds": 0,
            "status": status,                                 # success / failure
            "user": getpass.getuser(),                        # OS account that triggered the run
            "login_user": login_user or "",                   # PS Automation login username (audit)
            "computer": platform.node(),                      # computer it ran from
            "machine_ip": cached_local_ip(),                  # IP of the machine that ran it
            "servers": servers or [],                         # target servers/IPs
            "server_results": [],                             # per-server outcome
            "outputs": [],                                    # generated reports + paths
            "output_file": "",
            "path": "",
            "run_log_path": "",                                # organized per-run log in Logs/Run Logs
            "fail_reason": "",                                 # one-line reason when the run failed
            # Full run parameters (password excluded) so "Run Again" can
            # pre-fill the form exactly as it was, ready to edit and re-run.
            "params": params or {}
        }
        history.insert(0, entry)
        if len(history) > 300:
            history = history[:300]
        save_history(history)

def update_history_entry(run_id, **fields):
    with _history_lock:
        history = load_history()
        for entry in history:
            if entry.get("run_id") == run_id:
                for k, v in fields.items():
                    if v is not None:
                        entry[k] = v
                break
        save_history(history)

def format_duration(seconds):
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

# ---------------------------------------------------------------------------
# Chrome / Chromedriver environment helpers
# ---------------------------------------------------------------------------
_chrome_version_cache = {"value": None, "ts": 0}

def get_chrome_version():
    """Detect the locally installed Google Chrome version (registry first)."""
    if _chrome_version_cache["value"] and time.time() - _chrome_version_cache["ts"] < 300:
        return _chrome_version_cache["value"]

    version = None
    try:
        import winreg
        for hive, key_path in [
            (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Google\Chrome\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Google\Chrome\BLBeacon"),
        ]:
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    value, _ = winreg.QueryValueEx(key, "version")
                    if value:
                        version = str(value).strip()
                        break
            except OSError:
                continue
    except Exception as e:
        server_logger.warning(f"Chrome registry detection failed: {e}")

    if not version:
        chrome_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
        for p in chrome_paths:
            if os.path.exists(p):
                try:
                    out = subprocess.check_output(
                        ["powershell", "-NoProfile", "-Command", f"(Get-Item '{p}').VersionInfo.ProductVersion"],
                        creationflags=CREATE_NO_WINDOW, timeout=15
                    )
                    version = out.decode("utf-8", errors="replace").strip()
                    if version:
                        break
                except Exception as e:
                    server_logger.warning(f"Chrome file version detection failed for {p}: {e}")

    _chrome_version_cache["value"] = version
    _chrome_version_cache["ts"] = time.time()
    return version

def parse_major(version_text):
    if not version_text:
        return None
    m = re.search(r"(\d+)", str(version_text))
    return int(m.group(1)) if m else None

# ---------------------------------------------------------------------------
# Office detection - some machines this app runs on are VMs without Word/
# Excel installed. The report/guide viewer (see /api/reports/preview) needs
# to know this up front to offer the in-browser fallback instead of a
# silently-failing os.startfile().
# ---------------------------------------------------------------------------
_office_cache = {"value": None, "ts": 0}

def get_office_availability():
    """Detect local Word/Excel install (registry ProgID first, common install
    paths as a fallback) - same two-step approach as get_chrome_version()."""
    if _office_cache["value"] is not None and time.time() - _office_cache["ts"] < 300:
        return _office_cache["value"]

    word = False
    excel = False
    try:
        import winreg
        # A registered COM ProgID is the most reliable signal Office is
        # actually installed (survives version/edition differences) - it's
        # only present when Word/Excel's own installer registers it.
        for prog_id, flag_name in (("Word.Application", "word"), ("Excel.Application", "excel")):
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{prog_id}\\CLSID"):
                    if flag_name == "word":
                        word = True
                    else:
                        excel = True
            except OSError:
                continue
    except Exception as e:
        server_logger.warning(f"Office registry detection failed: {e}")

    if not word or not excel:
        # Fallback: common install locations across Office versions (the
        # "OfficeNN" folder name is shared by 2016/2019/2021/365 - only
        # older Office 2013 uses Office15).
        program_files = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                          os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]
        office_versions = ["Office16", "Office15"]
        for pf in program_files:
            for ver in office_versions:
                if not word and os.path.exists(os.path.join(pf, "Microsoft Office", "root", ver, "WINWORD.EXE")):
                    word = True
                if not word and os.path.exists(os.path.join(pf, "Microsoft Office", ver, "WINWORD.EXE")):
                    word = True
                if not excel and os.path.exists(os.path.join(pf, "Microsoft Office", "root", ver, "EXCEL.EXE")):
                    excel = True
                if not excel and os.path.exists(os.path.join(pf, "Microsoft Office", ver, "EXCEL.EXE")):
                    excel = True

    result = {"word": word, "excel": excel}
    _office_cache["value"] = result
    _office_cache["ts"] = time.time()
    return result

def get_chromedriver_list():
    """Scan the Chromedrivers folder - adding a new version requires no code change."""
    drivers = []
    if os.path.exists(CHROMEDRIVERS_DIR):
        for root, dirs, files in os.walk(CHROMEDRIVERS_DIR):
            for file in files:
                if file.lower().startswith("chromedriver") and file.lower().endswith(".exe"):
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, CHROMEDRIVERS_DIR)
                    drivers.append({
                        "name": rel_path.replace("\\", "/"),
                        "path": full_path,
                        "major": parse_major(rel_path)
                    })
    drivers.sort(key=lambda d: (d["major"] is None, -(d["major"] or 0), d["name"]))
    return drivers

# ---------------------------------------------------------------------------
# Structured output parsing (per-server status, progress, stages)
# ---------------------------------------------------------------------------
MARKER_TOTAL = re.compile(r"^\[TOTAL-SERVERS\]\s+(\d+)")
MARKER_START = re.compile(r"^\[SERVER-START\]\s+(.+)$")
MARKER_OK = re.compile(r"^\[SERVER-OK\]\s+(.+)$")
MARKER_FAIL = re.compile(r"^\[SERVER-FAIL\]\s+(.+)$")
MARKER_STAGE = re.compile(r"^\[STAGE\]\s+(.+)$")
PY_TOTAL = re.compile(r"Starting.*for (\d+) servers", re.IGNORECASE)
PY_SERVER = re.compile(r"Processing (?:Server.*?:|URL:)\s*(.+)$", re.IGNORECASE)

def _find_server(run_info, ip):
    for s in run_info["servers"]:
        if s["ip"] == ip:
            return s
    return None

def parse_output_line(run_info, line):
    text = line.strip()
    if not text:
        return

    m = MARKER_TOTAL.match(text)
    if m:
        run_info["total_servers"] = int(m.group(1))
        return

    m = MARKER_START.match(text)
    if m:
        ip = m.group(1).strip()
        if not _find_server(run_info, ip):
            run_info["servers"].append({
                "ip": ip, "hostname": "", "status": "running", "log": "",
                "started": time.strftime("%H:%M:%S")
            })
        run_info["current_stage"] = f"{ip}"
        return

    m = MARKER_OK.match(text)
    if m:
        parts = [p.strip() for p in m.group(1).split("|")]
        ip = parts[0] if parts else ""
        srv = _find_server(run_info, ip)
        if not srv:
            srv = {"ip": ip, "started": ""}
            run_info["servers"].append(srv)
        srv["hostname"] = parts[1] if len(parts) > 1 else ""
        srv["status"] = "success"
        srv["log"] = parts[2] if len(parts) > 2 else ""
        srv["ended"] = time.strftime("%H:%M:%S")
        if srv.get("log"):
            log_abs = os.path.normpath(os.path.join(run_info.get("cwd", SCRIPTS_DIR), srv["log"]))
            _add_output(run_info, os.path.basename(log_abs), log_abs)
        return

    m = MARKER_FAIL.match(text)
    if m:
        parts = [p.strip() for p in m.group(1).split("|")]
        ip = parts[0] if parts else ""
        srv = _find_server(run_info, ip)
        if not srv:
            srv = {"ip": ip, "started": ""}
            run_info["servers"].append(srv)
        srv["status"] = "failed"
        srv["reason"] = parts[1] if len(parts) > 1 else "Unknown error"
        srv["ended"] = time.strftime("%H:%M:%S")
        if len(parts) > 2 and parts[2]:
            log_abs = os.path.normpath(os.path.join(run_info.get("cwd", SCRIPTS_DIR), parts[2]))
            srv["log"] = parts[2]
            _add_output(run_info, os.path.basename(log_abs), log_abs)
        return

    m = MARKER_STAGE.match(text)
    if m:
        run_info["current_stage"] = m.group(1).strip()
        return

    # Heuristics for the python (selenium) report scripts
    m = PY_TOTAL.search(text)
    if m:
        run_info["total_servers"] = int(m.group(1))
        return

    m = PY_SERVER.search(text)
    if m:
        run_info["py_server_count"] = run_info.get("py_server_count", 0) + 1
        run_info["current_stage"] = m.group(1).strip()
        return

    # Legacy output filename detection
    lower_line = text.lower()
    if "report saved" in lower_line or "report successfully saved" in lower_line or "progress saved incrementally to" in lower_line:
        parts = text.split(":")
        if len(parts) > 1:
            filename = parts[-1].strip().replace('"', '').replace("'", "").strip()
            if filename.endswith(".docx") or filename.endswith(".log"):
                run_info["output_file"] = filename

def _add_output(run_info, name, path):
    for o in run_info["outputs"]:
        if o["path"].lower() == path.lower():
            return
    run_info["outputs"].append({"name": name, "path": path})

def compute_progress(run_info):
    total = run_info.get("total_servers")
    if run_info.get("servers"):
        done = len([s for s in run_info["servers"] if s["status"] in ("success", "failed")])
        total = total or len(run_info["servers"])
        return done, total
    if run_info.get("py_server_count"):
        seen = run_info["py_server_count"]
        done = seen if run_info["status"] != "running" else max(seen - 1, 0)
        return done, total
    return None, total

_PY_EXCEPTION_LINE_RE = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning):")

def _extract_fail_reason(log_lines, status):
    """Best-effort one-line explanation of why a run failed, taken from the
    captured console output (last error-looking line, else last line).

    Checks for the canonical Python traceback exception line first (e.g.
    "IndentationError: unexpected indent") because its mixed-case class name
    never literally contains any of the generic markers below - without this,
    the generic scan can grab an unrelated string a few lines above it (e.g.
    a print() argument that happens to contain the word "ERROR") instead of
    the actual exception.
    """
    if status == "killed":
        return "Stopped manually by the user"
    for line in reversed(log_lines):
        text = line.strip()
        if text and _PY_EXCEPTION_LINE_RE.match(text):
            return text[:300]
    error_markers = ("CRITICAL", "ERROR", "Exception", "Traceback", "FAILED", "Failed", "fatal", "invalid")
    for line in reversed(log_lines):
        text = line.strip()
        if not text:
            continue
        if any(marker in text for marker in error_markers):
            return text[:300]
    for line in reversed(log_lines):
        text = line.strip()
        if text:
            return text[:300]
    return "No output captured"

# ---------------------------------------------------------------------------
# Worker thread: reads process output, tracks status, finalizes history
# ---------------------------------------------------------------------------
_STDOUT_EOF = object()  # sentinel: the reader thread hit real end-of-pipe

def run_script_worker(run_id, process):
    run_info = active_runs[run_id]

    # stdout is read on its own thread instead of blocking directly on
    # process.stdout.readline() here, because a killed run can otherwise
    # get stuck "Running" forever: some scripts (e.g. Configure_NTP+DNS.ps1,
    # which spawns plink.exe/ssh-keyscan.exe via .NET's Process.Start) have
    # grandchild processes that - by default Windows handle-inheritance
    # behavior - end up holding this SAME stdout pipe open. If Stop is
    # clicked while such a grandchild is still alive, taskkill /F /T kills
    # the tracked PowerShell process, but the pipe's write end doesn't
    # actually close until that lingering grandchild also exits - so a
    # blocking readline() here would never return, and the code below that
    # checks process.poll() would never even run. Reading on a separate
    # (daemon) thread lets the MAIN loop poll the tracked process's own
    # status on a short timeout instead, so a kill is noticed almost
    # immediately regardless of what any orphaned grandchild is doing.
    line_queue = queue.Queue()

    def _read_stdout():
        try:
            for raw_line in iter(process.stdout.readline, b""):
                line_queue.put(raw_line)
        except Exception:
            pass
        finally:
            line_queue.put(_STDOUT_EOF)

    reader_thread = threading.Thread(target=_read_stdout, daemon=True)
    reader_thread.start()

    finished = False
    while not finished:
        try:
            item = line_queue.get(timeout=0.5)
        except queue.Empty:
            # No new output in the last 0.5s - if the tracked process has
            # already exited, don't keep waiting on the pipe (a lingering
            # grandchild could hold it open indefinitely); finalize now.
            if process.poll() is not None:
                finished = True
            continue

        if item is _STDOUT_EOF:
            finished = True
            continue

        decoded_line = item.decode('utf-8', errors='replace')
        run_info["logs"].append(decoded_line)
        try:
            parse_output_line(run_info, decoded_line)
        except Exception as e:
            server_logger.warning(f"Output parse error: {e}")

    # Process finished
    process.wait()
    ended = time.time()
    if run_info["status"] != "killed":
        run_info["status"] = "completed" if process.returncode == 0 else "failed"
    run_info["exit_code"] = process.returncode
    run_info["ended_at"] = time.strftime("%H:%M:%S", time.localtime(ended))
    run_info["duration_seconds"] = ended - run_info["started_ts"]
    run_info["current_stage"] = ""

    # On failure, pull the most meaningful error line out of the console
    # output so the Logs page can show WHY it failed at a glance.
    run_info["fail_reason"] = ""
    if run_info["status"] != "completed":
        run_info["fail_reason"] = _extract_fail_reason(run_info["logs"], run_info["status"])

    # Any server still marked as running didn't finish -> mark failed
    for s in run_info["servers"]:
        if s["status"] == "running":
            s["status"] = "failed" if run_info["status"] != "completed" else "success"

    # Every run gets its own easy-to-read folder, named
    # "<script>_<date>_<time>":
    #   Outputs/<run folder>/  - the generated .docx files
    #   Logs/<run folder>/     - run.log + per-server validation logs
    settings = load_settings()
    results_dir = effective_results_dir()
    import shutil

    run_stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(run_info["started_ts"]))
    safe_script_name = re.sub(r'[<>:"/\\|?*]+', "_", run_info["script_name"]).strip() or "run"
    run_dir_name = f"{safe_script_name}_{run_stamp}"
    run_logs_dir = os.path.join(REPORT_FOLDER_DIR, run_dir_name)

    def _collision_safe_dest(dest_dir, fname):
        dst = os.path.join(dest_dir, fname)
        if os.path.exists(dst):
            base, ext = os.path.splitext(fname)
            idx = 1
            while os.path.exists(dst):
                dst = os.path.join(dest_dir, f"{base}_{idx}{ext}")
                idx += 1
        return dst

    if settings.get("auto_move_docx", True):
        try:
            run_results_dir = os.path.join(results_dir, run_dir_name)
            for root, dirs, files in os.walk(SCRIPTS_DIR):
                dirs[:] = [d for d in dirs if d.lower() not in AUTO_IGNORED_DIRS]
                for fname in files:
                    if not fname.lower().endswith(REPORT_PRODUCT_EXTENSIONS):
                        continue
                    os.makedirs(run_results_dir, exist_ok=True)
                    src = os.path.join(root, fname)
                    dst = _collision_safe_dest(run_results_dir, fname)
                    shutil.move(src, dst)
                    server_logger.info(f"Auto-moved report to Outputs: {dst}")
                    _add_output(run_info, os.path.basename(dst), dst)
                    if run_info.get("output_file") and fname.endswith(run_info["output_file"]):
                        run_info["output_file"] = os.path.basename(dst)
        except Exception as e:
            server_logger.warning(f"Auto-move of report files to Outputs failed: {e}")

    try:
        run_validations_dir = os.path.join(run_info.get("cwd", SCRIPTS_DIR), "Validations")
        if os.path.isdir(run_validations_dir):
            # MDE/ATP validation logs land under Outputs/<run folder>/validation/
            # - same per-run dated folder as every other script's report
            # products, just in its own subfolder (validation logs aren't
            # .docx/.csv, so they don't belong loose at the run folder's root).
            run_validation_out_dir = os.path.join(results_dir, run_dir_name, "validation")
            for fname in os.listdir(run_validations_dir):
                if not fname.lower().endswith(".log"):
                    continue
                os.makedirs(run_validation_out_dir, exist_ok=True)
                src = os.path.join(run_validations_dir, fname)
                dst = _collision_safe_dest(run_validation_out_dir, fname)
                shutil.move(src, dst)
                server_logger.info(f"Auto-moved validation log to Outputs: {dst}")
                _add_output(run_info, os.path.basename(dst), dst)
    except Exception as e:
        server_logger.warning(f"Auto-move of validation logs failed: {e}")

    # Tally the screenshots THIS run produced into the persistent all-time
    # counter. Counts only this run's own .docx outputs (each in its unique
    # dated folder), so it's exact and never double-counts across runs. The
    # counter only grows - deleting Output files later does not lower it.
    try:
        new_shots = 0
        for o in run_info.get("outputs", []):
            p = o.get("path", "")
            if p.lower().endswith(".docx") and os.path.exists(p):
                new_shots += count_screenshots_in_docx(p)
        if new_shots:
            add_screenshots_to_total(new_shots)
            server_logger.info(f"Screenshot tally +{new_shots} (run {run_id})")
    except Exception as e:
        server_logger.warning(f"Screenshot tally failed: {e}")

    # Grow the cumulative "time saved" figure - only for servers that succeeded.
    add_time_saved(run_info)

    # Resolve legacy single output_file into outputs list
    output_file = run_info.get("output_file", "")
    full_output_path = ""
    if output_file:
        candidates = [
            os.path.join(results_dir, run_dir_name, os.path.basename(output_file)),
            os.path.join(results_dir, os.path.basename(output_file)),
            os.path.join(SCRIPTS_DIR, output_file),
            os.path.join(run_info.get("cwd", SCRIPTS_DIR), output_file),
        ]
        for c in candidates:
            if os.path.exists(c):
                full_output_path = c
                break
        if full_output_path:
            _add_output(run_info, os.path.basename(full_output_path), full_output_path)

    if not full_output_path and run_info["outputs"]:
        full_output_path = run_info["outputs"][0]["path"]
        output_file = run_info["outputs"][0]["name"]
        run_info["output_file"] = output_file

    # One consolidated, human-readable run.log inside the run's own Logs
    # folder - what ran, whether it succeeded/failed/why, and the full
    # console output.
    run_log_path = ""
    try:
        run_log_path = write_run_log_file(run_id, run_info, run_logs_dir, process.returncode)
    except Exception as e:
        server_logger.warning(f"Failed to write per-run log file: {e}")
    run_info["run_log_path"] = run_log_path

    update_history_entry(
        run_id,
        status=run_info["status"],
        end_time=run_info["ended_at"],
        duration=format_duration(run_info["duration_seconds"]),
        duration_seconds=int(run_info["duration_seconds"]),
        server_results=run_info["servers"],
        outputs=run_info["outputs"],
        output_file=output_file,
        path=full_output_path,
        run_log_path=run_log_path,
        fail_reason=run_info.get("fail_reason", "")
    )
    server_logger.info(f"Run {run_id} finished with status: {run_info['status']} (code {process.returncode})")

def write_run_log_file(run_id, run_info, run_logs_dir, exit_code):
    """Write the organized run.log into the run's own Logs/<script>_<date>_<time>
    folder, with a metadata header (what ran, status, why it stopped, servers,
    etc.) followed by the full captured console output."""
    os.makedirs(run_logs_dir, exist_ok=True)
    path = os.path.join(run_logs_dir, "run.log")
    if os.path.exists(path):
        path = os.path.join(run_logs_dir, f"run_{run_id[:8]}.log")

    servers = run_info.get("servers", [])
    ok_servers = [s for s in servers if s.get("status") == "success"]
    fail_servers = [s for s in servers if s.get("status") not in ("success",) and s.get("status")]

    lines = []
    lines.append("=" * 70)
    lines.append(f"PS Automation - Run Log")
    lines.append("=" * 70)
    lines.append(f"Script:       {run_info['script_name']}")
    lines.append(f"Run ID:       {run_id}")
    lines.append(f"Started:      {run_info['started_at']}")
    lines.append(f"Ended:        {run_info.get('ended_at', '')}")
    lines.append(f"Duration:     {format_duration(run_info.get('duration_seconds', 0))}")
    lines.append(f"User:         {getpass.getuser()}")
    lines.append(f"Computer:     {platform.node()}")
    lines.append(f"Machine IP:   {cached_local_ip()}")
    lines.append(f"Status:       {run_info['status'].upper()}")
    lines.append(f"Exit code:    {exit_code}")
    if run_info.get("fail_reason"):
        lines.append(f"Failure reason: {run_info['fail_reason']}")
    if run_info.get("target_servers"):
        lines.append(f"Target servers: {', '.join(run_info['target_servers'])}")
    if servers:
        lines.append(f"Per-server result: {len(ok_servers)} succeeded, {len(fail_servers)} failed")
        for s in ok_servers:
            lines.append(f"  OK   {s.get('ip','')} {('(' + s['hostname'] + ')') if s.get('hostname') else ''}")
        for s in fail_servers:
            lines.append(f"  FAIL {s.get('ip','')} - {s.get('reason', 'Unknown reason')}")
    if run_info.get("outputs"):
        lines.append("Generated files:")
        for o in run_info["outputs"]:
            lines.append(f"  {o['name']} -> {o['path']}")
    lines.append("=" * 70)
    lines.append("CONSOLE OUTPUT")
    lines.append("=" * 70)
    for log_line in run_info.get("logs", []):
        lines.append(log_line.rstrip("\n"))

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    server_logger.info(f"Wrote run log: {path}")
    return path

# ---------------------------------------------------------------------------
# Static routes
# ---------------------------------------------------------------------------
@app.route('/')
def serve_index():
    return app.send_static_file('index.html')

@app.route('/images/<path:filename>')
def serve_images(filename):
    images_dir = os.path.join(BASE_DIR, "images")
    return send_from_directory(images_dir, filename)

# Browsers request /favicon.ico automatically; without this route every page
# load logged a 404 in the browser console. (Already in _PUBLIC_PATHS.)
@app.route('/favicon.ico')
def serve_favicon():
    return send_from_directory(os.path.join(BASE_DIR, "images"), "favicon.png")

# ---------------------------------------------------------------------------
# API: copyright (protected)
# ---------------------------------------------------------------------------
@app.route('/api/copyright', methods=['GET'])
def get_copyright():
    _cr_verify()
    return jsonify({"he": _cr_decode(_CR_HE), "en": _cr_decode(_CR_EN)})

# Lightweight identity probe (public, no auth) so a second launch can detect an
# already-running PS Automation instance and just open a browser tab to it
# instead of spawning a second server on another port. See __main__ below.
@app.route('/api/ping', methods=['GET'])
def api_ping():
    return jsonify({"app": "ps-automation", "version": APP_VERSION})

@app.route('/api/copyright/unlock', methods=['POST'])
def copyright_unlock():
    data = request.json or {}
    if _cr_check_password(data.get("password", "")):
        return jsonify({"authorized": True})
    return jsonify({"authorized": False}), 403

# ---------------------------------------------------------------------------
# API: environment info (version, chrome, drivers, paths)
# ---------------------------------------------------------------------------
@app.route('/api/environment', methods=['GET'])
def get_environment():
    chrome_version = get_chrome_version()
    chrome_major = parse_major(chrome_version)
    drivers = get_chromedriver_list()
    has_match = chrome_major is not None and any(d["major"] == chrome_major for d in drivers)

    import shutil as _shutil
    racadm_installed = _shutil.which("racadm") is not None or _shutil.which("racadm.exe") is not None
    plink_installed = _shutil.which("plink") is not None or _shutil.which("plink.exe") is not None
    office = get_office_availability()
    try:
        _, _, free_bytes = _shutil.disk_usage(BASE_DIR)
        disk_free_gb = round(free_bytes / (1024 ** 3), 1)
    except Exception:
        disk_free_gb = None

    return jsonify({
        "app_version": APP_VERSION,
        "chrome_version": chrome_version,
        "chrome_major": chrome_major,
        "drivers": drivers,
        "has_matching_driver": has_match,
        "racadm_installed": racadm_installed,
        "plink_installed": plink_installed,
        "word_installed": office["word"],
        "excel_installed": office["excel"],
        "disk_free_gb": disk_free_gb,
        "base_dir": BASE_DIR,
        "scripts_dir": SCRIPTS_DIR,
        "results_dir": effective_results_dir(),
        "report_folder_dir": REPORT_FOLDER_DIR,
        "chromedrivers_dir": CHROMEDRIVERS_DIR,
        "error_log_file": ERROR_LOG_FILE,
        "user": getpass.getuser(),
        "login_user": session.get("user", ""),
        "computer": platform.node(),
        "machine_ip": cached_local_ip()
    })

# ---------------------------------------------------------------------------
# API: settings
# ---------------------------------------------------------------------------
@app.route('/api/settings', methods=['GET'])
def api_get_settings():
    return jsonify(load_settings())

@app.route('/api/settings', methods=['POST'])
def api_save_settings():
    data = request.json or {}
    settings = load_settings()
    if "results_dir" in data:
        settings["results_dir"] = str(data["results_dir"] or "").strip()
    if "auto_move_docx" in data:
        settings["auto_move_docx"] = bool(data["auto_move_docx"])
    try:
        save_settings(settings)
        return jsonify({"status": "saved", "settings": settings})
    except Exception as e:
        server_logger.error(f"Failed saving settings: {e}")
        return jsonify({"error": f"Failed to save settings: {e}"}), 500

@app.route('/api/settings/reset', methods=['POST'])
def api_reset_settings():
    try:
        save_settings(dict(DEFAULT_SETTINGS))
        return jsonify({"status": "reset", "settings": DEFAULT_SETTINGS})
    except Exception as e:
        return jsonify({"error": f"Failed to reset settings: {e}"}), 500

# ---------------------------------------------------------------------------
# API: default operational credentials (decrypted from .env, in memory only)
# so the run wizard can prefill editable username/password fields without
# any literal credential ever appearing in this file or in app.js.
# ---------------------------------------------------------------------------
@app.route('/api/default-credentials', methods=['GET'])
def api_default_credentials():
    idrac_user, idrac_pass = security.get_default_idrac_credentials()
    ssh_user, ssh_pass = security.get_default_ssh_credentials()
    return jsonify({
        "idrac_username": idrac_user, "idrac_password": idrac_pass,
        "ssh_username": ssh_user, "ssh_password": ssh_pass
    })

# ---------------------------------------------------------------------------
# API: command profiles (server types)
# ---------------------------------------------------------------------------
@app.route('/api/command_profiles', methods=['GET'])
def api_get_profiles():
    return jsonify(load_profiles())

@app.route('/api/command_profiles', methods=['POST'])
def api_save_profile():
    data = request.json or {}
    label = str(data.get("label", "")).strip()
    commands = str(data.get("commands", "")).strip()
    if not label or not commands:
        return jsonify({"error": "Profile label and commands are required"}), 400

    key = str(data.get("key", "")).strip().lower()
    if not key:
        key = re.sub(r"[^a-z0-9_]+", "_", label.lower()).strip("_") or f"profile_{int(time.time())}"

    profiles = load_profiles()
    existing = profiles.get(key, {})
    profiles[key] = {
        "label": label,
        "builtin": existing.get("builtin", False),
        "commands": commands
    }
    try:
        save_profiles(profiles)
        return jsonify({"status": "saved", "key": key, "profiles": profiles})
    except Exception as e:
        return jsonify({"error": f"Failed to save profile: {e}"}), 500

@app.route('/api/command_profiles/<key>', methods=['DELETE'])
def api_delete_profile(key):
    profiles = load_profiles()
    if key not in profiles:
        return jsonify({"error": "Profile not found"}), 404
    if profiles[key].get("builtin"):
        return jsonify({"error": "Built-in profiles cannot be deleted"}), 400
    del profiles[key]
    try:
        save_profiles(profiles)
        return jsonify({"status": "deleted", "profiles": profiles})
    except Exception as e:
        return jsonify({"error": f"Failed to delete profile: {e}"}), 500

# ---------------------------------------------------------------------------
# API: client-side error reporting into the central error log
# ---------------------------------------------------------------------------
@app.route('/api/client_error', methods=['POST'])
def api_client_error():
    data = request.json or {}
    server_logger.error(f"[CLIENT] {data.get('message', 'Unknown client error')} | source: {data.get('source', '-')} | detail: {data.get('detail', '-')}")
    return jsonify({"status": "logged"})

# ---------------------------------------------------------------------------
# API: auto-translation for description editing (he <-> en).
# Uses Google's public translate endpoint; degrades gracefully to an
# "offline" response when there is no internet (closed client networks).
# ---------------------------------------------------------------------------
@app.route('/api/translate', methods=['POST'])
def api_translate():
    data = request.json or {}
    text = str(data.get("text", ""))[:2000]
    target = "he" if data.get("target") == "he" else "en"
    if not text.strip():
        return jsonify({"translated": ""})
    try:
        import urllib.parse
        import urllib.request
        url = ("https://translate.googleapis.com/translate_a/single"
               "?client=gtx&sl=auto&tl=" + target + "&dt=t&q=" + urllib.parse.quote(text))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        translated = "".join(seg[0] for seg in payload[0] if seg and seg[0])
        return jsonify({"translated": translated})
    except Exception as e:
        server_logger.warning(f"Auto-translate failed (offline?): {e}")
        return jsonify({"translated": "", "error": "offline"})

# ---------------------------------------------------------------------------
# API: history
# ---------------------------------------------------------------------------
def _reconcile_history_with_disk():
    """Mirror manual Explorer deletions into the app, so the UI and the
    project folder always agree in BOTH directions:
      - a history entry whose run-log folder was deleted by hand under Logs/
        is dropped from history.json (its row disappears from the Logs page);
      - output files deleted by hand under Outputs/ are pruned from every
        entry's outputs list (run details stop offering dead links).
    Entries still running, or with no recorded log folder, are never touched."""
    changed = False
    with _history_lock:
        history = load_history()
        kept = []
        for entry in history:
            if entry.get("status") != "running":
                log_path = entry.get("run_log_path") or ""
                if log_path:
                    folder = os.path.dirname(log_path)
                    if folder and not os.path.isdir(folder):
                        changed = True
                        server_logger.info(f"History entry pruned (run folder deleted manually): {entry.get('script_name')} {entry.get('started_at')}")
                        continue
            outputs = entry.get("outputs") or []
            if outputs:
                existing = [o for o in outputs if o.get("path") and os.path.exists(o["path"])]
                if len(existing) != len(outputs):
                    entry["outputs"] = existing
                    if entry.get("path") and not os.path.exists(entry["path"]):
                        entry["path"] = existing[0]["path"] if existing else ""
                        entry["output_file"] = existing[0]["name"] if existing else ""
                    changed = True
            kept.append(entry)
        if changed:
            save_history(kept)

@app.route('/api/history', methods=['GET'])
def get_history():
    _reconcile_history_with_disk()
    return jsonify(load_history())

# ---------------------------------------------------------------------------
# API: saved target (server) groups
# ---------------------------------------------------------------------------
@app.route('/api/targets', methods=['GET'])
def api_get_targets():
    return jsonify(load_targets())

@app.route('/api/targets', methods=['POST'])
def api_create_target():
    data = request.json or {}
    name = str(data.get("name", "")).strip()
    ips_raw = data.get("ips", "")
    ips = [x.strip() for x in (ips_raw if isinstance(ips_raw, list) else str(ips_raw).split("\n")) if x.strip()]
    if not name or not ips:
        return jsonify({"error": "Name and at least one IP are required"}), 400
    targets = load_targets()
    entry = {
        "id": "tg_" + uuid.uuid4().hex[:10],
        "name": name,
        "company": str(data.get("company", "")).strip(),
        "ips": ips
    }
    targets.append(entry)
    save_targets(targets)
    return jsonify({"status": "created", "target": entry, "targets": targets})

@app.route('/api/targets/<target_id>', methods=['PUT'])
def api_update_target(target_id):
    data = request.json or {}
    targets = load_targets()
    entry = next((t for t in targets if t.get("id") == target_id), None)
    if not entry:
        return jsonify({"error": "Target group not found"}), 404
    if "name" in data:
        entry["name"] = str(data["name"]).strip()
    if "company" in data:
        entry["company"] = str(data["company"]).strip()
    if "ips" in data:
        ips_raw = data["ips"]
        entry["ips"] = [x.strip() for x in (ips_raw if isinstance(ips_raw, list) else str(ips_raw).split("\n")) if x.strip()]
    save_targets(targets)
    return jsonify({"status": "updated", "target": entry, "targets": targets})

@app.route('/api/targets/<target_id>', methods=['DELETE'])
def api_delete_target(target_id):
    targets = load_targets()
    remaining = [t for t in targets if t.get("id") != target_id]
    if len(remaining) == len(targets):
        return jsonify({"error": "Target group not found"}), 404
    save_targets(remaining)
    return jsonify({"status": "deleted", "targets": remaining})

# ---------------------------------------------------------------------------
# API: scheduled (one-time) automation runs
# ---------------------------------------------------------------------------
_SCHEDULE_DT_FORMAT = "%Y-%m-%dT%H:%M"

@app.route('/api/schedules', methods=['GET'])
def api_get_schedules():
    schedules = load_schedules()
    schedules.sort(key=lambda s: s.get("scheduled_at") or "")
    return jsonify(schedules)

@app.route('/api/schedules', methods=['POST'])
def api_create_schedule():
    data = request.json or {}
    script_id = data.get("script_id")
    payload = data.get("payload") or {}
    scheduled_at_raw = str(data.get("scheduled_at", "")).strip()

    script_meta = next((s for s in get_all_scripts() if s["id"] == script_id), None)
    if not script_meta:
        return jsonify({"error": "Script not found"}), 404

    if script_meta.get("risk") == "destructive":
        return jsonify({"error": "Destructive automations cannot be scheduled - they must be run interactively with explicit confirmation."}), 400

    try:
        scheduled_at = datetime.strptime(scheduled_at_raw[:16], _SCHEDULE_DT_FORMAT)
    except ValueError:
        return jsonify({"error": "Invalid scheduled_at - expected a datetime-local value (YYYY-MM-DDTHH:MM)"}), 400
    if scheduled_at <= datetime.now():
        return jsonify({"error": "Scheduled time must be in the future"}), 400

    # A schedule's payload is persisted to scheduled_runs.json and must never
    # hold a plaintext password at rest - encrypt it with the same Fernet key
    # used for the .env default credentials, and decrypt it only in memory at
    # the moment the schedule actually fires (see _scheduler_watcher).
    payload = dict(payload)
    if payload.get("password"):
        try:
            payload["password_enc"] = security.encrypt_value(str(payload["password"]))
            del payload["password"]
        except Exception:
            # No encryption key available - refuse to persist the password in
            # plaintext rather than silently leaking it to disk.
            del payload["password"]
            server_logger.warning("Schedule created without a stored password (no ENCRYPTION_KEY) - the run will use the default credentials.")

    with _schedule_lock:
        schedules = load_schedules()
        entry = {
            "id": "sc_" + uuid.uuid4().hex[:10],
            "script_id": script_id,
            "script_name": script_meta["name"],
            "company": script_meta.get("company"),
            "stage": script_meta.get("stage"),
            "risk": script_meta.get("risk"),
            "payload": payload,
            "scheduled_at": scheduled_at.strftime(_SCHEDULE_DT_FORMAT),
            "status": "pending",
            "created_by": session.get("user", ""),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "run_id": None,
            "error": None
        }
        schedules.append(entry)
        save_schedules(schedules)
    return jsonify({"status": "created", "schedule": entry})

@app.route('/api/schedules/<schedule_id>', methods=['PUT'])
def api_update_schedule(schedule_id):
    """Edit a PENDING schedule's time. Only the scheduled_at can change -
    retargeting what runs/where means creating a new schedule through the
    wizard, so the pre-flight path is never bypassed."""
    data = request.json or {}
    scheduled_at_raw = str(data.get("scheduled_at", "")).strip()
    try:
        scheduled_at = datetime.strptime(scheduled_at_raw[:16], _SCHEDULE_DT_FORMAT)
    except ValueError:
        return jsonify({"error": "Invalid scheduled_at - expected a datetime-local value (YYYY-MM-DDTHH:MM)"}), 400
    if scheduled_at <= datetime.now():
        return jsonify({"error": "Scheduled time must be in the future"}), 400
    with _schedule_lock:
        schedules = load_schedules()
        entry = next((s for s in schedules if s.get("id") == schedule_id), None)
        if not entry:
            return jsonify({"error": "Schedule not found"}), 404
        if entry.get("status") != "pending":
            return jsonify({"error": "Only a pending schedule can be edited"}), 400
        entry["scheduled_at"] = scheduled_at.strftime(_SCHEDULE_DT_FORMAT)
        save_schedules(schedules)
    return jsonify({"status": "updated", "schedule": entry})

@app.route('/api/schedules/<schedule_id>/cancel', methods=['POST'])
def api_cancel_schedule(schedule_id):
    with _schedule_lock:
        schedules = load_schedules()
        entry = next((s for s in schedules if s.get("id") == schedule_id), None)
        if not entry:
            return jsonify({"error": "Schedule not found"}), 404
        if entry.get("status") != "pending":
            return jsonify({"error": "Only a pending schedule can be cancelled"}), 400
        entry["status"] = "cancelled"
        save_schedules(schedules)
    return jsonify({"status": "cancelled", "schedule": entry})

@app.route('/api/schedules/<schedule_id>', methods=['DELETE'])
def api_delete_schedule(schedule_id):
    with _schedule_lock:
        schedules = load_schedules()
        entry = next((s for s in schedules if s.get("id") == schedule_id), None)
        if not entry:
            return jsonify({"error": "Schedule not found"}), 404
        if entry.get("status") == "pending":
            return jsonify({"error": "Cancel the schedule before deleting it"}), 400
        remaining = [s for s in schedules if s.get("id") != schedule_id]
        save_schedules(remaining)
    return jsonify({"status": "deleted", "schedules": remaining})

def _scheduler_watcher():
    """Background thread: fires any schedule whose time has arrived through
    the same _launch_run() path /api/run uses. Runs unconditionally at import
    time, independent of any browser tab being open."""
    while True:
        time.sleep(20)
        try:
            with _schedule_lock:
                schedules = load_schedules()
                now = datetime.now()
                due = []
                for s in schedules:
                    if s.get("status") != "pending":
                        continue
                    try:
                        if datetime.strptime(s["scheduled_at"], _SCHEDULE_DT_FORMAT) <= now:
                            due.append(s)
                    except (ValueError, KeyError):
                        continue
            for entry in due:
                # Rebuild the runnable payload in memory only: the password is
                # stored encrypted (password_enc) in scheduled_runs.json and
                # decrypted here, right before launch - never written back.
                # script_id is stored as its own top-level field on the
                # schedule entry (not inside payload), but _launch_run expects
                # the SAME flat shape /api/run sends - so it must be merged
                # back in here, or every scheduled run fails immediately with
                # "Script 'None' was not found".
                fire_payload = dict(entry.get("payload") or {})
                fire_payload["script_id"] = entry.get("script_id")
                if fire_payload.get("password_enc"):
                    decrypted = security.decrypt_value(fire_payload.pop("password_enc"))
                    if decrypted:
                        fire_payload["password"] = decrypted
                result, status = _launch_run(fire_payload, entry.get("created_by", ""))
                with _schedule_lock:
                    schedules = load_schedules()
                    for s in schedules:
                        if s.get("id") == entry["id"]:
                            if status == 200:
                                s["status"] = "triggered"
                                s["run_id"] = result.get("run_id")
                            else:
                                s["status"] = "failed"
                                s["error"] = result.get("error")
                            break
                    save_schedules(schedules)
                server_logger.info(f"Scheduled run triggered: {entry.get('script_name')} (schedule {entry['id']})")
        except Exception:
            server_logger.exception("Scheduler watcher failed")

threading.Thread(target=_scheduler_watcher, daemon=True).start()

# ---------------------------------------------------------------------------
# API: pre-flight reachability check (real ICMP ping, run concurrently) -
# used by the run wizard's pre-flight step before a run actually starts.
# ---------------------------------------------------------------------------
_IP_RE = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')

def _ping_once(ip):
    if not _IP_RE.match(ip):
        return False
    try:
        result = subprocess.run(
            ["ping", "-n", "1", "-w", "800", ip],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW, timeout=2
        )
        return result.returncode == 0
    except Exception:
        return False

@app.route('/api/preflight/reachability', methods=['POST'])
def api_preflight_reachability():
    data = request.json or {}
    ips_raw = data.get("ips") or []
    if not isinstance(ips_raw, list):
        return jsonify({"error": "ips must be a list"}), 400
    ips = [str(x).strip() for x in ips_raw if str(x).strip()][:200]  # sane cap per request
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        future_map = {executor.submit(_ping_once, ip): ip for ip in ips}
        for future in concurrent.futures.as_completed(future_map):
            ip = future_map[future]
            try:
                results[ip] = future.result()
            except Exception:
                results[ip] = False
    reachable = sum(1 for v in results.values() if v)
    return jsonify({"results": results, "reachable": reachable, "total": len(ips)})

# ---------------------------------------------------------------------------
# API: companies / stages taxonomy (entry screen: Company -> Language -> Stage)
# ---------------------------------------------------------------------------
@app.route('/api/companies', methods=['GET'])
def api_get_companies():
    return jsonify(load_companies())

@app.route('/api/companies/<company_key>/stages', methods=['POST'])
def api_add_stage(company_key):
    data = request.json or {}
    label = str(data.get("label", "")).strip()
    if not label:
        return jsonify({"error": "Stage label is required"}), 400

    with _data_lock:
        companies = load_companies()
        if company_key not in companies:
            return jsonify({"error": f"Company '{company_key}' not found"}), 404

        key = slugify_key(data.get("key") or label)
        existing_keys = {s["key"] for s in companies[company_key].get("stages", [])}
        if key in existing_keys:
            return jsonify({"error": f"Stage '{key}' already exists for this company"}), 400

        companies[company_key].setdefault("stages", []).append({
            "key": key, "label": label, "label_en": str(data.get("label_en", label)).strip() or label
        })
        companies[company_key]["has_stages"] = True
        try:
            save_companies(companies)
            ensure_scripts_tree(companies)
        except Exception as e:
            return jsonify({"error": f"Failed to save companies.json: {e}"}), 500

    return jsonify({"status": "added", "key": key, "companies": companies})

# ---------------------------------------------------------------------------
# API: scripts & drivers
# ---------------------------------------------------------------------------
@app.route('/api/scripts', methods=['GET'])
def get_scripts():
    company = request.args.get("company") or None
    stage = request.args.get("stage") or None
    script_type = request.args.get("type") or None
    scripts = get_all_scripts()
    if company or stage or script_type:
        scripts = filter_scripts(scripts, company=company, stage=stage, script_type=script_type)
    return jsonify(scripts)

@app.route('/api/scripts/unassigned', methods=['GET'])
def api_unassigned_scripts():
    """Loose .py/.ps1 files sitting directly under Scripts/ (or in a folder
    that doesn't match any company/stage/language) - candidates to be moved
    into a proper location via the 'Add Automation' folder-tree picker."""
    script_type = request.args.get("type") or None
    files = [f for f in scan_all_scripts() if not f["location_matched"]]
    if script_type:
        files = [f for f in files if f["type"] == script_type]
    return jsonify([{"filename": f["filename"], "type": f["type"], "suggested_name": f["name"]} for f in files])

@app.route('/api/scripts/destinations', methods=['GET'])
def api_script_destinations():
    """The folder-tree of valid Add-Automation destinations: every real
    company/stage x Python/PowerShell leaf (the 'general' bucket is excluded
    - it is the default/unplaced state, not a placement target)."""
    companies = load_companies()
    tree = []
    for key, company in companies.items():
        if key == "general":
            continue
        node = {
            "key": key,
            "label": company.get("label") or key,
            "label_en": company.get("label_en") or key,
            "has_stages": bool(company.get("has_stages")),
            "stages": company.get("stages", [])
        }
        tree.append(node)
    return jsonify(tree)

@app.route('/api/scripts/custom', methods=['POST'])
def api_add_custom_script():
    """Move a loose script file into a company/[stage/]language folder,
    optionally saving a nicer display name/description for it."""
    data = request.json or {}
    filename = str(data.get("filename", "")).strip()
    company = str(data.get("company", "")).strip()
    stage = (data.get("stage") or "").strip() or None
    script_type = str(data.get("type", "")).strip()
    name = str(data.get("name", "")).strip()

    if not filename or not company or script_type not in ("python", "powershell"):
        return jsonify({"error": "Missing required fields: filename, company and a valid language are required"}), 400

    companies = load_companies()
    if company not in companies or company == "general":
        return jsonify({"error": f"Unknown or invalid destination company '{company}'"}), 400
    company_def = companies[company]
    if stage and stage not in {s["key"] for s in company_def.get("stages", [])}:
        return jsonify({"error": f"Unknown stage '{stage}' for company '{company}'"}), 400
    if company_def.get("has_stages") and not stage:
        return jsonify({"error": "This company requires a stage to be selected"}), 400

    source_path = resolve_safe_script_path(filename)
    if not source_path or not os.path.exists(source_path):
        return jsonify({"error": f"Script file not found under Scripts folder: {filename}"}), 404

    # Resolve destination folder from the folder tree (mirrors ensure_scripts_tree)
    dest_dir = os.path.join(SCRIPTS_DIR, company_def.get("label") or company)
    if stage:
        stage_def = next(s for s in company_def["stages"] if s["key"] == stage)
        dest_dir = os.path.join(dest_dir, stage_def.get("label") or stage)
    dest_dir = os.path.join(dest_dir, "Python" if script_type == "python" else "PowerShell")

    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, os.path.basename(source_path))
        if os.path.exists(dest_path):
            return jsonify({"error": f"A file named '{os.path.basename(source_path)}' already exists at the destination"}), 400

        import shutil
        shutil.move(source_path, dest_path)
        new_rel_path = os.path.relpath(dest_path, SCRIPTS_DIR).replace("\\", "/")
    except Exception as e:
        server_logger.error(f"Failed to move script to destination: {e}")
        return jsonify({"error": f"Failed to move file to destination folder: {e}"}), 500

    # Only store an override for fields the user actually typed - the name
    # otherwise stays exactly the filename (no auto-generated fallback here).
    if name or data.get("description") or data.get("description_en"):
        with _data_lock:
            overrides = load_custom_scripts()
            entry = {}
            if name:
                entry["name"] = name
            if data.get("description"):
                entry["description"] = str(data.get("description")).strip()
            if data.get("description_en"):
                entry["description_en"] = str(data.get("description_en")).strip()
            overrides[new_rel_path] = entry
            try:
                save_custom_scripts(overrides)
            except Exception as e:
                server_logger.warning(f"Failed to save display-name override: {e}")

    return jsonify({"status": "added", "path": new_rel_path, "scripts": get_all_scripts()})

@app.route('/api/scripts/description', methods=['POST'])
def api_edit_script_description():
    """Edit the description (he/en) of any existing script, keyed by its
    current relative path. Does not touch the name (always the filename)."""
    data = request.json or {}
    filename = str(data.get("filename", "")).strip()
    if not filename:
        return jsonify({"error": "filename is required"}), 400

    safe_path = resolve_safe_script_path(filename)
    if not safe_path or not os.path.exists(safe_path):
        return jsonify({"error": f"Script file not found: {filename}"}), 404

    rel_path = filename.replace("\\", "/")
    with _data_lock:
        overrides = load_custom_scripts()
        entry = dict(overrides.get(rel_path, {}))
        entry["description"] = str(data.get("description", "")).strip()
        entry["description_en"] = str(data.get("description_en", "")).strip()
        # Drop empty keys so a cleared field falls back to the curated/default text
        entry = {k: v for k, v in entry.items() if v}
        if entry:
            overrides[rel_path] = entry
        elif rel_path in overrides:
            del overrides[rel_path]
        try:
            save_custom_scripts(overrides)
        except Exception as e:
            return jsonify({"error": f"Failed to save description: {e}"}), 500

    return jsonify({"status": "saved", "scripts": get_all_scripts()})

@app.route('/api/chromedriver_versions', methods=['GET'])
def get_chromedriver_versions():
    return jsonify(get_chromedriver_list())

# ---------------------------------------------------------------------------
# API: run execution
# ---------------------------------------------------------------------------
def _launch_run(data, user):
    """Core run-launch logic: resolves the script, prepares its working
    directory/inputs/credentials, spawns the process, and registers the run.
    Shared by the interactive /api/run route (user = session's logged-in
    username) and the scheduled-run background trigger (user = whoever
    created the schedule) - both go through the exact same execution path,
    so a scheduled run is indistinguishable from a manual one in Logs/Reports.
    Returns (response_dict, http_status_code)."""
    try:
        script_id = data.get("script_id")

        # Find script metadata (including auto-discovered scripts)
        script_meta = next((s for s in get_all_scripts() if s["id"] == script_id), None)
        if not script_meta:
            return {"error": f"Script '{script_id}' was not found on the server. Refresh the page and try again."}, 404

        script_path = os.path.join(SCRIPTS_DIR, script_meta["filename"])
        if not os.path.exists(script_path):
            return {"error": f"Script file is missing from the server: {script_path}"}, 400

        # Sweep any stray .docx left under Scripts/ by previous (failed/killed)
        # runs into the Outputs root BEFORE starting, so this run's dated
        # folder only ever receives files this run actually generated.
        try:
            import shutil
            results_root = effective_results_dir()
            for root, dirs, files in os.walk(SCRIPTS_DIR):
                dirs[:] = [d for d in dirs if d.lower() not in AUTO_IGNORED_DIRS]
                for fname in files:
                    if fname.lower().endswith(REPORT_PRODUCT_EXTENSIONS):
                        stray_dst = os.path.join(results_root, fname)
                        if os.path.exists(stray_dst):
                            base, ext = os.path.splitext(fname)
                            idx = 1
                            while os.path.exists(stray_dst):
                                stray_dst = os.path.join(results_root, f"{base}_{idx}{ext}")
                                idx += 1
                        shutil.move(os.path.join(root, fname), stray_dst)
                        server_logger.info(f"Swept stray report from a previous run into Outputs: {stray_dst}")
        except Exception as e:
            server_logger.warning(f"Pre-run stray report sweep failed: {e}")

        run_id = str(uuid.uuid4())

        # Prepare environment variables
        env = os.environ.copy()

        # Always hand every script the decrypted default credentials (never
        # hardcoded in the script source) so it can fall back to them when
        # the operator leaves the Username/Password fields at default -
        # PSAUTO_DEFAULT_* for iDRAC/ESXi (racadm/pyVmomi), PSAUTO_DEFAULT_SSH_*
        # for SSH (MDE validation / DNS-NTP). An explicit PSAUTO_USERNAME/
        # PSAUTO_PASSWORD (below) always takes priority over these.
        try:
            d_user, d_pass = security.get_default_idrac_credentials()
            env["PSAUTO_DEFAULT_USERNAME"] = d_user
            env["PSAUTO_DEFAULT_PASSWORD"] = d_pass
        except Exception:
            pass
        try:
            s_user, s_pass = security.get_default_ssh_credentials()
            env["PSAUTO_DEFAULT_SSH_USERNAME"] = s_user
            env["PSAUTO_DEFAULT_SSH_PASSWORD"] = s_pass
        except Exception:
            pass

        # System notes echoed to the run console so the user can SEE exactly
        # which driver / addresses the run is actually using
        system_notes = []

        # Construct execution command. cwd is always the script's own folder,
        # regardless of language, so scripts keep working correctly no matter
        # where they physically live (they can be moved/reorganized freely).
        cwd = os.path.dirname(script_path)
        inputs = script_meta.get("inputs", [])

        # --- ChromeDriver resolution (guaranteed) -------------------------
        # Use the exact driver picked in the form. If it's missing/invalid,
        # auto-fall back to the driver matching the installed Chrome version,
        # then to the newest available one. The chosen path is echoed to the
        # console and passed via CHROMEDRIVER_PATH.
        if "chromedriver" in inputs:
            chosen_driver = str(data.get("chromedriver_path") or "").strip()
            if not chosen_driver or not os.path.exists(chosen_driver):
                drivers = get_chromedriver_list()
                chrome_major = parse_major(get_chrome_version())
                match = next((d for d in drivers if d["major"] == chrome_major), None) or (drivers[0] if drivers else None)
                if match:
                    if chosen_driver:
                        system_notes.append(f"[SYSTEM] Selected ChromeDriver not found, auto-switched to: {match['path']}")
                    chosen_driver = match["path"]
            if not chosen_driver or not os.path.exists(chosen_driver):
                return {"error": "No ChromeDriver found. Add a matching version folder under Chromedrivers and try again."}, 400
            env["CHROMEDRIVER_PATH"] = chosen_driver
            system_notes.append(f"[SYSTEM] Chrome installed: {get_chrome_version() or 'not detected'}")
            system_notes.append(f"[SYSTEM] Using ChromeDriver: {chosen_driver}")

        # --- Base IP normalization (Sequence mode) -------------------------
        # The report scripts expect a 3-octet prefix (e.g. 192.168.0) and
        # append the suffix themselves. If a full 4-octet address was typed,
        # trim it so the generated URLs stay valid.
        base_ip = str(data.get("base_ip", "")).strip()
        if data.get("mode") == "1" and base_ip:
            normalized = ".".join([p for p in base_ip.split(".") if p][:3])
            if normalized != base_ip:
                system_notes.append(f"[SYSTEM] Base IP normalized: {base_ip} -> {normalized}")
                base_ip = normalized

        # Collect target servers list for auditing
        ips_raw = data.get("ips", "") or ""
        ip_lines = [ip.strip() for ip in ips_raw.split("\n") if ip.strip()]
        target_servers = list(ip_lines)
        if data.get("mode") == "1" and base_ip:
            try:
                start_suffix = int(data.get("start_suffix", 1))
                count = int(data.get("count", 1))
                target_servers = [f"{base_ip}.{start_suffix + i}" for i in range(count)]
            except Exception:
                target_servers = [base_ip]

        # De-duplicate (order preserved, first occurrence wins) - a typed
        # range that overlaps a manually-added line, or simply the same
        # address entered twice, would otherwise make every affected script
        # validate/process that one server twice (two log files, two report
        # rows, double the work) with no indication anything was duplicated.
        seen_targets = set()
        deduped_targets = []
        for t in target_servers:
            if t not in seen_targets:
                seen_targets.add(t)
                deduped_targets.append(t)
        if len(deduped_targets) != len(target_servers):
            system_notes.append(
                f"[SYSTEM] Removed {len(target_servers) - len(deduped_targets)} duplicate address(es) from the target list."
            )
        target_servers = deduped_targets

        if script_meta["type"] == "python":
            cmd = [sys.executable, "-u", script_path]
        elif script_meta["type"] == "powershell":
            cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path]
        else:
            return {"error": f"Unsupported script type: {script_meta['type']}"}, 400

        # commands.txt is only relevant to MDE-style validation scripts
        # (those declaring "commands" as an input)
        if "commands" in inputs:
            custom_commands = data.get("commands")
            if custom_commands:
                commands_file = os.path.join(cwd, "commands.txt")
                try:
                    with open(commands_file, "w", encoding="utf-8") as f:
                        f.write(custom_commands.strip() + "\n")
                    server_logger.info("Overwrote commands.txt with custom user selection")
                except Exception as e:
                    return {"error": f"Failed to write commands.txt: {str(e)}"}, 500

        # addresses.txt holds the RESOLVED target list - range mode is already
        # expanded into individual IPs in target_servers, so a script never has
        # to understand ranges itself; it just reads one IP per line. Every
        # non-Selenium script that takes an IP list reads its targets from here
        # (ping scanners, MDE validation, power-down, set-hostname, ...). The
        # Selenium report scripts are the sole exception: they receive their
        # targets through the stdin "mode" protocol below, so they are excluded
        # here (identified by their chromedriver input).
        if "ips" in inputs and "chromedriver" not in inputs:
            addresses_file = os.path.join(cwd, "addresses.txt")
            try:
                with open(addresses_file, "w", encoding="utf-8") as f:
                    f.write("\n".join(target_servers))
                server_logger.info(f"Wrote {len(target_servers)} IPs to {addresses_file}")
            except Exception as e:
                return {"error": f"Failed to write addresses.txt: {str(e)}"}, 500

        # Extra list-style inputs are each written to their own <name>.txt next
        # to the script (one value per line):
        #   hostnames -> hostnames.txt  (parallel to addresses.txt, line N<->N;
        #                                used by Set-iDRAC-Hostname)
        #   dns       -> dns.txt        (DNS servers to push, top of the list)
        #   ntp       -> ntp.txt        (NTP servers to push, top of the list)
        #   newips    -> newips.txt     (parallel to addresses.txt, line N<->N;
        #                                used by Change_ip - the NEW iDRAC IPs)
        #   netmask   -> netmask.txt    (single value, applied to all servers)
        #   gateway   -> gateway.txt    (single value, applied to all servers)
        for list_input, list_file in (("hostnames", "hostnames.txt"),
                                      ("dns", "dns.txt"), ("ntp", "ntp.txt"),
                                      ("newips", "newips.txt"),
                                      ("netmask", "netmask.txt"),
                                      ("gateway", "gateway.txt")):
            if list_input in inputs:
                raw = data.get(list_input, "") or ""
                lines = [x.strip() for x in raw.split("\n") if x.strip()]
                fpath = os.path.join(cwd, list_file)
                try:
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write("\n".join(lines))
                    server_logger.info(f"Wrote {len(lines)} {list_input} to {fpath}")
                except Exception as e:
                    return {"error": f"Failed to write {list_file}: {str(e)}"}, 500

        # Credentials override: pass the username/password form fields to the
        # script through environment variables. Any script (PowerShell or
        # Python) can read PSAUTO_USERNAME / PSAUTO_PASSWORD and fall back to its
        # own hardcoded default when the field is left unchanged - so racadm /
        # SSH scripts keep working out of the box but can be pointed at other
        # credentials without editing the script.
        if "username" in inputs:
            env["PSAUTO_USERNAME"] = str(data.get("username", "")).strip()
        if "password" in inputs:
            env["PSAUTO_PASSWORD"] = str(data.get("password", ""))

        # RAID name override (Configure_Raid1): the virtual-disk name to create.
        # A RAID must have a name, so a PRESENT-but-empty value is rejected here
        # (the form marks it required too - this is the server-side backstop).
        # A missing key (e.g. a schedule saved before this field existed) is left
        # alone so the script falls back to its own "vDisk1" default rather than
        # breaking. New runs always send a value (the form defaults it to vDisk1).
        if "raid_name" in inputs and "raid_name" in data:
            raid_name = str(data.get("raid_name", "")).strip()
            if not raid_name:
                return {"error": "RAID name is required (a RAID must have a name)."}, 400
            env["PSAUTO_RAID_NAME"] = raid_name

        # Start process (no extra window is opened - see CREATE_NO_WINDOW)
        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=cwd,
                creationflags=CREATE_NO_WINDOW
            )
        except Exception as e:
            server_logger.error(f"Failed to start process: {e}")
            return {"error": f"Failed to launch process: {str(e)}"}, 500

        # Prepare and write stdin inputs. The payload shape is decided by the
        # script's declared inputs (not its language) - this lets PowerShell
        # wrapper scripts use the same range/list protocol as the Python
        # engines they delegate to.
        stdin_payload = ""
        if "mode" in inputs:
            mode = data.get("mode", "2")
            if mode == "1":
                start_suffix = str(data.get("start_suffix", "1")).strip()
                count = str(data.get("count", "1")).strip()
                stdin_payload = f"1\n{base_ip}\n{start_suffix}\n{count}\n"
            else:
                stdin_payload = "2\n" + "\n".join(ip_lines) + "\ndone\n"
        elif "use_default_creds" in inputs:
            use_default_creds = data.get("use_default_creds", True)
            if use_default_creds:
                stdin_payload = "y\n"
            else:
                username = data.get("username", "root").strip()
                password = data.get("password", "").strip()
                stdin_payload = f"n\n{username}\n{password}\n"

        try:
            process.stdin.write(stdin_payload.encode('utf-8'))
            process.stdin.flush()
            process.stdin.close()
        except Exception as e:
            server_logger.warning(f"Error writing to process stdin: {e}")

        # Register run in-memory (system notes appear first in the console)
        active_runs[run_id] = {
            "script_id": script_id,
            "script_name": script_meta["name"],
            # basename of the actual script file (e.g. "configure_raid1.ps1"),
            # used to look up the per-server time-saved estimate when the run ends.
            "script_filename": os.path.basename(script_meta.get("filename", "")).lower(),
            "status": "running",
            "process": process,
            # Records the launching user so /api/run's duplicate-run guard can
            # compare each active run's "user" to the requester and reject a
            # second concurrent submission from the same user.
            "user": user,
            "logs": [note + "\n" for note in system_notes],
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "started_ts": time.time(),
            "ended_at": "",
            "duration_seconds": 0,
            "cwd": cwd,
            "servers": [],
            "outputs": [],
            "total_servers": len(target_servers) if target_servers else None,
            "target_servers": target_servers,
            "current_stage": "",
            "exit_code": None
        }

        # Persist the run's parameters (minus password) so "Run Again" can
        # restore this exact configuration into the form for editing.
        safe_params = {k: v for k, v in data.items() if k != "password"}
        safe_params["language"] = script_meta.get("type")
        add_history_entry(
            run_id, script_meta["name"], "running", servers=target_servers,
            script_id=script_id, company=script_meta.get("company"), stage=script_meta.get("stage"),
            params=safe_params, login_user=user
        )

        t = threading.Thread(target=run_script_worker, args=(run_id, process))
        t.daemon = True
        t.start()

        return {"run_id": run_id, "status": "running"}, 200
    except Exception as e:
        server_logger.exception("Unhandled error in /api/run")
        return {"error": f"Unexpected server error while starting the run: {e}"}, 500

@app.route('/api/run', methods=['POST'])
def run_script():
    data = request.json or {}
    user = session.get("user", "")
    # Applies to every automation, present and future, since it's keyed on
    # the logged-in user rather than any specific script: if THIS user
    # already has a run in flight, a repeat /api/run (rapid re-clicks, a
    # second browser tab, a network retry replaying the same POST) is
    # rejected outright instead of spawning a second process. The lock makes
    # the "is one already running?" check and the launch atomic, closing the
    # race a plain if-check would leave open between two near-simultaneous
    # requests.
    with _run_launch_lock:
        already_running = any(
            r.get("status") == "running" and r.get("user") == user
            for r in active_runs.values()
        )
        if already_running:
            return jsonify({"error": "A run is already in progress for this user - wait for it to finish (or stop it) before starting another."}), 409
        result, status = _launch_run(data, user)
    return jsonify(result), status

# ---------------------------------------------------------------------------
# API: run status (progress, per-server state, stage, outputs, summary data)
# ---------------------------------------------------------------------------
@app.route('/api/status/<run_id>', methods=['GET'])
def run_status(run_id):
    run_info = active_runs.get(run_id)
    if not run_info:
        # Fall back to persisted history (e.g., after server restart)
        entry = next((h for h in load_history() if h.get("run_id") == run_id), None)
        if not entry:
            return jsonify({"error": "Run not found"}), 404
        return jsonify({
            "run_id": run_id,
            "script_name": entry.get("script_name", ""),
            "status": entry.get("status", "unknown"),
            "started_at": entry.get("started_at", ""),
            "end_time": entry.get("end_time", ""),
            "duration": entry.get("duration", ""),
            "servers": entry.get("server_results", []),
            "target_servers": entry.get("servers", []),
            "outputs": entry.get("outputs", []),
            "progress_done": None,
            "progress_total": None,
            "current_stage": "",
            "user": entry.get("user", ""),
            "computer": entry.get("computer", ""),
            "run_log_path": entry.get("run_log_path", ""),
            "fail_reason": entry.get("fail_reason", "")
        })

    done, total = compute_progress(run_info)
    return jsonify({
        "run_id": run_id,
        "script_name": run_info["script_name"],
        "status": run_info["status"],
        "started_at": run_info["started_at"],
        "end_time": run_info.get("ended_at", ""),
        "duration": format_duration(run_info["duration_seconds"]) if run_info["duration_seconds"] else "",
        "servers": run_info["servers"],
        "target_servers": run_info.get("target_servers", []),
        "outputs": run_info["outputs"],
        "progress_done": done,
        "progress_total": total,
        "current_stage": run_info.get("current_stage", ""),
        "exit_code": run_info.get("exit_code"),
        "user": getpass.getuser(),
        "computer": platform.node(),
        "run_log_path": run_info.get("run_log_path", ""),
        "fail_reason": run_info.get("fail_reason", "")
    })

# ---------------------------------------------------------------------------
# API: real-time SSE logs streaming
# ---------------------------------------------------------------------------
@app.route('/api/stream/<run_id>')
def stream_logs(run_id):
    if run_id not in active_runs:
        return Response("Run not found", status=404)

    def event_stream():
        run_info = active_runs[run_id]
        printed_idx = 0

        while True:
            if printed_idx < len(run_info["logs"]):
                for i in range(printed_idx, len(run_info["logs"])):
                    yield f"data: {run_info['logs'][i]}\n\n"
                printed_idx = len(run_info["logs"])

            if run_info["status"] != "running" and printed_idx >= len(run_info["logs"]):
                yield f"event: finish\ndata: {run_info['status']}\n\n"
                break

            time.sleep(0.2)

    return Response(event_stream(), mimetype="text/event-stream")

# ---------------------------------------------------------------------------
# API: kill a running automation (terminates the whole process tree,
# including plink/chrome child processes)
# ---------------------------------------------------------------------------
@app.route('/api/kill/<run_id>', methods=['POST'])
def kill_run(run_id):
    if run_id not in active_runs:
        return jsonify({"error": "Run not found"}), 404

    run_info = active_runs[run_id]
    if run_info["status"] == "running":
        try:
            run_info["status"] = "killed"
            pid = run_info["process"].pid
            try:
                subprocess.call(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    creationflags=CREATE_NO_WINDOW,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except Exception:
                run_info["process"].terminate()
            update_history_entry(run_id, status="killed", end_time=time.strftime("%H:%M:%S"))
            return jsonify({"status": "killed"})
        except Exception as e:
            server_logger.error(f"Failed to kill run {run_id}: {e}")
            return jsonify({"error": f"Failed to kill: {str(e)}"}), 500
    return jsonify({"status": run_info["status"], "msg": "Process was not running"})

# ---------------------------------------------------------------------------
# API: report files listing
# ---------------------------------------------------------------------------
@app.route('/api/reports', methods=['GET'])
def list_reports():
    """Outputs page: the report products of the scripts (.docx, .csv), PLUS
    MDE/ATP validation .log files - but ONLY the ones under a "validation"
    subfolder (where the run-finalize step above places them), never .log
    files anywhere else. Every other .log in the app still belongs solely to
    the Logs side (Logs folder + Logs page)."""
    reports = []
    results_dir = effective_results_dir()

    if os.path.exists(results_dir):
        for root, dirs, files in os.walk(results_dir):
            in_validation_subfolder = os.path.basename(root).lower() == "validation"
            for file in files:
                is_report = file.lower().endswith(REPORT_PRODUCT_EXTENSIONS)
                is_validation_log = in_validation_subfolder and file.lower().endswith(".log")
                if not (is_report or is_validation_log):
                    continue
                full_path = os.path.join(root, file)
                rel = os.path.relpath(full_path, results_dir).replace("\\", "/")
                stat = os.stat(full_path)
                reports.append({
                    "name": rel,
                    "path": full_path,
                    "type": "word" if file.lower().endswith(".docx") else ("log" if is_validation_log else "csv"),
                    "size": stat.st_size,
                    "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                    "mtime": stat.st_mtime
                })

    reports.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify(reports)

def count_screenshots_in_docx(path):
    """Count embedded images inside a .docx report - a .docx file is itself a
    zip archive, and every screenshot placed in it lives under word/media/,
    so this is a real, exact count (no need to open the file in Word)."""
    import zipfile
    count = 0
    try:
        with zipfile.ZipFile(path, "r") as z:
            for name in z.namelist():
                if name.startswith("word/media/") and not name.endswith("/"):
                    count += 1
    except Exception:
        pass
    return count

def _run_success_count(h):
    """Successful servers in a history entry - from per-server results, else (a
    completed run with no per-server markers) its target count, min 1."""
    ok = len([s for s in (h.get("server_results") or []) if s.get("status") == "success"])
    if ok == 0 and h.get("status") == "completed":
        tgt = h.get("servers") or []
        ok = len(tgt) if tgt else 1
    return ok

def _run_fully_succeeded(h):
    """True only when the run completed AND no server failed - matches the
    time-saved rule (any failure => this run contributes 0 saved time)."""
    if h.get("status") != "completed":
        return False
    return not any((s.get("status") and s.get("status") != "success") for s in (h.get("server_results") or []))

@app.route('/api/stats/analytics', methods=['GET'])
def api_analytics():
    """Everything the Dashboard's charts need, computed from run history + the
    persistent all-time counters. Derived server-side so the frontend just
    renders. (Runs/success are from recent history; time-saved, servers, and
    screenshots are the persistent all-time figures.)"""
    import datetime
    hist = load_history()
    finished = [h for h in hist if h.get("status") in ("completed", "failed", "killed")]
    total_runs = len(finished)
    success = len([h for h in finished if h.get("status") == "completed"])
    success_rate = round(success / total_runs * 100) if total_runs else 0
    durs = [h.get("duration_seconds", 0) for h in finished if h.get("duration_seconds", 0) > 0]
    avg = int(sum(durs) / len(durs)) if durs else 0
    avg_dur = f"{avg // 60}:{avg % 60:02d}"

    today = datetime.date.today()
    # daily run counts, oldest -> newest, last 30 days
    dcount = {}
    for h in finished:
        d = h.get("date", "")
        if d:
            dcount[d] = dcount.get(d, 0) + 1
    daily = [dcount.get((today - datetime.timedelta(days=i)).strftime("%Y-%m-%d"), 0) for i in range(29, -1, -1)]

    # time saved per week (minutes), oldest -> newest, last 8 weeks
    weekly = [0] * 8
    for h in finished:
        try:
            d = datetime.datetime.strptime(h.get("date", ""), "%Y-%m-%d").date()
        except Exception:
            continue
        wk = (today - d).days // 7
        if 0 <= wk < 8 and _run_fully_succeeded(h):   # any failure => 0 saved time
            per = TIME_SAVED_PER_SERVER_BY_STEM.get((h.get("script_name", "") or "").lower(), 0)
            weekly[7 - wk] += per * _run_success_count(h)
    weekly = [int(round(x)) for x in weekly]

    catc = {}
    for h in finished:
        he = _CATEGORY_HE.get(classify_script_category((h.get("script_name", "") or "").lower()), "כללי")
        catc[he] = catc.get(he, 0) + 1
    by_category = sorted([{"name": k, "v": v} for k, v in catc.items()], key=lambda x: -x["v"])

    riskc = {"read": 0, "config": 0, "destructive": 0}
    for h in finished:
        riskc[classify_script_risk((h.get("script_name", "") or "").lower())] += 1
    by_risk = [{"name": _RISK_HE[k][0], "v": riskc[k], "c": _RISK_HE[k][1]}
               for k in ("read", "config", "destructive") if riskc[k] > 0]

    def _rel(started_at, status):
        if status == "running":
            return "עכשיו"
        try:
            secs = (datetime.datetime.now() - datetime.datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")).total_seconds()
        except Exception:
            return ""
        if secs < 60: return "הרגע"
        if secs < 3600: return f"לפני {int(secs // 60)} דק'"
        if secs < 86400: return f"לפני {int(secs // 3600)} שע'"
        return f"לפני {int(secs // 86400)} ימים"

    recent = []
    for h in hist[:6]:
        st = h.get("status", "")
        pill = "running" if st == "running" else ("success" if st == "completed" else "failed")
        recent.append({
            "name": h.get("script_name", ""),
            "cat": _CATEGORY_HE.get(classify_script_category((h.get("script_name", "") or "").lower()), "כללי"),
            "servers": len(h.get("servers") or []) or _run_success_count(h),
            "status": pill,
            "dur": h.get("duration") or "—",
            "when": _rel(h.get("started_at", ""), st),
        })

    ts = get_time_saved()
    return jsonify({
        "totals": {
            "runs": total_runs, "success_rate": success_rate, "failed": total_runs - success,
            "servers": ts["servers"], "screenshots": get_screenshot_total(),
            "time_saved_min": ts["seconds"] // 60, "avg_dur": avg_dur,
        },
        "daily": daily, "weekly": weekly,
        "by_category": by_category, "by_risk": by_risk, "recent": recent,
    })

@app.route('/api/stats/time-saved', methods=['GET'])
def api_time_saved():
    """Cumulative time saved across all runs (all-time, persistent). Returns the
    raw seconds plus convenience fields; the frontend rolls it up to d/h/m."""
    d = get_time_saved()
    return jsonify({
        "seconds": d["seconds"],
        "minutes": d["seconds"] // 60,
        "runs": d["runs"],
        "servers": d["servers"],
    })

@app.route('/api/stats/screenshots', methods=['GET'])
def api_screenshot_count():
    """All-time screenshots captured - a CUMULATIVE, MONOTONIC figure read
    from the persistent counter. It only grows as new runs capture
    screenshots; deleting Output files never lowers it (unlike a live disk
    recount). Reset only via the password-gated endpoint below."""
    return jsonify({"count": get_screenshot_total()})

# Screenshot-counter reset is password-gated. The password is stored ONLY as a
# one-way PBKDF2 hash (salt+hash below) - the plaintext appears nowhere in the
# source, exactly like the login. Only the correct password can zero the
# all-time tally.
_SS_RESET_SALT = "f28bc18ada4f86e1f0e969b4b44f5912"
_SS_RESET_HASH = "330bee464ee12a035f0428723f789394c421b5507607b3214cfa145d4f9a8961"

@app.route('/api/stats/screenshots/reset', methods=['POST'])
def api_screenshot_reset():
    data = request.json or {}
    if not security.verify_password(data.get("password", ""), _SS_RESET_SALT, _SS_RESET_HASH):
        server_logger.warning("Blocked screenshot-counter reset: wrong password")
        return jsonify({"ok": False, "error": "wrong password"}), 403
    with _screenshot_lock:
        _save_screenshot_total(0)
    server_logger.info("Screenshot counter reset to 0 (authorized)")
    return jsonify({"ok": True, "count": 0})

# ---------------------------------------------------------------------------
# Opening files/folders locally - brings the window to the foreground
# ---------------------------------------------------------------------------
def _find_explorer_window(title_substr):
    """First visible File-Explorer folder window (class 'CabinetWClass') whose
    title contains title_substr (case-insensitive).

    Restricting to the Explorer window class means we never accidentally grab a
    browser tab or the PS Automation app window that merely has the same word
    ('Logs'/'Outputs') in its title. Substring (not exact) matching handles the
    'show full path in title bar' folder option and Windows 11 tabbed Explorer,
    where the raw title isn't just the bare folder name."""
    import ctypes
    user32 = ctypes.windll.user32
    found = []
    target = (title_substr or "").lower()
    if not target:
        return 0
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value != "CabinetWClass":
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if target in buf.value.lower():
                found.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return found[0] if found else 0

def _force_window_foreground(hwnd):
    """Robustly bring hwnd to the OS foreground even from a hidden/background
    process. Plain SetForegroundWindow is silently blocked by Windows when the
    caller (the hidden Flask server) doesn't own the current input, so this
    combines SetWindowPos(HWND_TOPMOST) with AttachThreadInput - the same
    proven technique used to foreground the Selenium Chrome windows."""
    import ctypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    SW_RESTORE, HWND_TOPMOST, HWND_NOTOPMOST = 9, -1, -2
    SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0002, 0x0001, 0x0040

    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)

    current_tid = kernel32.GetCurrentThreadId()
    fg_hwnd = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
    target_tid = user32.GetWindowThreadProcessId(hwnd, None)

    attached_fg = attached_target = False
    if fg_tid and fg_tid != current_tid:
        attached_fg = bool(user32.AttachThreadInput(current_tid, fg_tid, True))
    if target_tid and target_tid != current_tid:
        attached_target = bool(user32.AttachThreadInput(current_tid, target_tid, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached_fg:
            user32.AttachThreadInput(current_tid, fg_tid, False)
        if attached_target:
            user32.AttachThreadInput(current_tid, target_tid, False)

    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)

def _focus_window_by_title(title):
    """Best-effort: wait for the freshly opened Explorer folder window (its title
    is the folder's display name) to appear, then force it to the foreground.
    Explorer can take a moment to create the window, so poll briefly."""
    try:
        for _ in range(20):
            time.sleep(0.2)
            hwnd = _find_explorer_window(title)
            if hwnd:
                _force_window_foreground(hwnd)
                return
    except Exception as e:
        server_logger.warning(f"Focus window failed: {e}")

# ---------------------------------------------------------------------------
# S1 hardening for /api/reports/open: a FILE may be opened with os.startfile
# ONLY when it is a real file inside an approved root AND has an allowed
# extension for that root:
#   * Outputs folder + configured effective_results_dir()  -> output allow-list
#   * Logs folder (run.log / validation logs)              -> logs-only list
# This blocks executables/scripts (.exe/.bat/.ps1/.vbs/.hta/.msi/.scr/.lnk/.js/
# .jar, etc.) everywhere (incl. inside Outputs and Logs), files outside those
# roots (Scripts/, Windows, Desktop, Downloads, ...), UNC paths, and ..\
# traversal. No Reports folder is created or used - REPORT_FOLDER_DIR here is
# simply the app's existing "Logs" folder.
# ---------------------------------------------------------------------------
SAFE_OUTPUT_OPEN_EXTS = {
    ".docx", ".xlsx", ".csv", ".txt", ".log", ".pdf",
    ".png", ".jpg", ".jpeg", ".json",
}
# The Logs folder holds only run/validation logs, so it gets a stricter list.
SAFE_LOG_OPEN_EXTS = {".log", ".txt"}

def _safe_output_open_path(path):
    """Return the resolved absolute path if `path` is a safe file inside an
    approved root (Outputs / effective_results_dir() / Logs) with an allowed
    extension for that root, otherwise None."""
    if not path or not isinstance(path, str):
        return None
    # (root, allowed-extensions) pairs, checked in order.
    root_rules = []
    for r in (RESULTS_DIR, effective_results_dir()):
        try:
            root_rules.append((os.path.normcase(os.path.realpath(r)), SAFE_OUTPUT_OPEN_EXTS))
        except Exception:
            pass
    try:  # REPORT_FOLDER_DIR is the app's "Logs" folder (run.log + validation logs)
        root_rules.append((os.path.normcase(os.path.realpath(REPORT_FOLDER_DIR)), SAFE_LOG_OPEN_EXTS))
    except Exception:
        pass
    try:
        resolved = os.path.realpath(path)          # collapses ..\ and symlinks
    except Exception:
        return None
    resolved_nc = os.path.normcase(resolved)
    allowed_exts = None
    for root, exts in root_rules:
        if resolved_nc == root or resolved_nc.startswith(root + os.sep):
            allowed_exts = exts
            break
    if allowed_exts is None:
        return None                                # outside every approved root
    if not os.path.isfile(resolved):
        return None                                # must be an existing file
    if os.path.splitext(resolved)[1].lower() not in allowed_exts:
        return None                                # not an allowed type for this root
    return resolved

@app.route('/api/reports/open', methods=['POST'])
def open_report():
    data = request.json or {}
    path = data.get("path")
    if not path:
        return jsonify({"error": "Path parameter is missing"}), 400

    # Translate special keywords to actual directories
    if path == "SCRIPTS_DIR":
        path = SCRIPTS_DIR
    elif path == "RESULTS_DIR":
        path = effective_results_dir()
    elif path == "CHROMEDRIVERS_DIR":
        path = CHROMEDRIVERS_DIR
    elif path == "REPORT_FOLDER_DIR":
        path = REPORT_FOLDER_DIR

    if not os.path.exists(path):
        return jsonify({"error": f"Path invalid or not found: {path}"}), 404

    try:
        if os.path.isdir(path):
            subprocess.Popen(["explorer", os.path.normpath(path)], creationflags=CREATE_NO_WINDOW)
            threading.Thread(target=_focus_window_by_title, args=(os.path.basename(os.path.normpath(path)),), daemon=True).start()
        else:
            # S1: only ever open a safe output file from the Outputs area.
            safe_path = _safe_output_open_path(path)
            if not safe_path:
                server_logger.warning(f"Blocked unsafe /api/reports/open request: {path}")
                return jsonify({"ok": False, "error": "Blocked unsafe output file path or file type"}), 400
            os.startfile(safe_path)
            path = safe_path
        server_logger.info(f"Opened locally: {path}")
        return jsonify({"status": "success", "message": f"Opened {os.path.basename(path)}", "path": path})
    except Exception as e:
        server_logger.error(f"Failed to open {path}: {e}")
        return jsonify({"error": f"Failed to open '{path}': {str(e)}"}), 500

def _safe_reveal_path(path):
    """Return the resolved path if it's an existing file inside an approved
    root (Outputs / effective_results_dir() / Logs), otherwise None. No
    extension restriction - reveal only selects the file in Explorer, it
    never opens/executes it."""
    if not path or not isinstance(path, str):
        return None
    roots = []
    for r in (RESULTS_DIR, effective_results_dir(), REPORT_FOLDER_DIR):
        try:
            roots.append(os.path.normcase(os.path.realpath(r)))
        except Exception:
            pass
    try:
        resolved = os.path.realpath(path)
    except Exception:
        return None
    resolved_nc = os.path.normcase(resolved)
    if not any(resolved_nc == root or resolved_nc.startswith(root + os.sep) for root in roots):
        return None
    if not os.path.isfile(resolved):
        return None
    return resolved

@app.route('/api/reports/reveal', methods=['POST'])
def reveal_report():
    data = request.json or {}
    path = data.get("path")
    safe_path = _safe_reveal_path(path)
    if not safe_path:
        server_logger.warning(f"Blocked unsafe /api/reports/reveal request: {path}")
        return jsonify({"ok": False, "error": "Blocked unsafe file path"}), 400

    try:
        norm = os.path.normpath(safe_path)
        # List-form Popen (no shell=True): the path is passed as a single
        # argument, never interpreted by a shell, so it can't be used to
        # inject extra commands regardless of what characters it contains.
        subprocess.Popen(["explorer", f"/select,{norm}"], creationflags=CREATE_NO_WINDOW)
        # The window that opens shows the file's PARENT folder - bring it to the
        # foreground so it doesn't get buried behind the browser/app.
        parent_name = os.path.basename(os.path.dirname(norm))
        if parent_name:
            threading.Thread(target=_focus_window_by_title, args=(parent_name,), daemon=True).start()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------------
# API: in-browser preview fallback for .docx/.xlsx (reports AND guides) - for
# machines without Word/Excel installed (see get_office_availability()).
# Streams the raw file bytes; mammoth.js/SheetJS on the frontend do the actual
# docx/xlsx parsing entirely client-side, so no server-side conversion runs.
# ---------------------------------------------------------------------------
PREVIEWABLE_EXTS = {".docx", ".xlsx"}

def _safe_preview_path(path):
    """Same shape as _safe_output_open_path, but also allows GUIDES_DIR (read
    -only viewing, never listed as an os.startfile() target) and is scoped to
    just the two previewable extensions regardless of which root matched."""
    if not path or not isinstance(path, str):
        return None
    roots = []
    for r in (RESULTS_DIR, effective_results_dir(), REPORT_FOLDER_DIR, GUIDES_DIR):
        try:
            roots.append(os.path.normcase(os.path.realpath(r)))
        except Exception:
            pass
    try:
        resolved = os.path.realpath(path)
    except Exception:
        return None
    resolved_nc = os.path.normcase(resolved)
    if not any(resolved_nc == root or resolved_nc.startswith(root + os.sep) for root in roots):
        return None
    if not os.path.isfile(resolved):
        return None
    if os.path.splitext(resolved)[1].lower() not in PREVIEWABLE_EXTS:
        return None
    return resolved

@app.route('/api/reports/preview', methods=['GET'])
def preview_report():
    path = request.args.get("path")
    safe_path = _safe_preview_path(path)
    if not safe_path:
        server_logger.warning(f"Blocked unsafe /api/reports/preview request: {path}")
        return jsonify({"ok": False, "error": "Blocked unsafe file path or file type"}), 400
    return send_file(safe_path, as_attachment=False, download_name=os.path.basename(safe_path))

# ---------------------------------------------------------------------------
# API: Guides catalog - Word/Excel platform documentation under Guides/, kept
# entirely separate from the Scripts/ automation catalog (no run/wizard
# semantics, just browse + preview via the endpoint above).
# ---------------------------------------------------------------------------
_REV_RE = re.compile(r"[_\s]*Rev[_\s]*([A-Za-z0-9]+)", re.IGNORECASE)

def _guide_title_and_revision(filename):
    """Split 'Nova_Hub_ATP_Rev_A01.docx' into ('Nova Hub ATP', 'A01') so
    same-topic revisions can be grouped and sorted; files with no Rev_XX
    marker (e.g. the .xlsx) get revision None."""
    stem = os.path.splitext(filename)[0]
    m = _REV_RE.search(stem)
    revision = m.group(1).upper() if m else None
    title = (stem[:m.start()] if m else stem).replace("_", " ").strip()
    return title, revision

def scan_guides():
    """Walk Guides/<Platform>/... for .docx/.xlsx files. Platform is the
    first path segment under Guides/; everything below that is folded into
    one flat list per platform, grouped by guide title (see above) so the
    frontend can offer a revision picker instead of listing every file."""
    guides_by_platform = {}
    if not os.path.isdir(GUIDES_DIR):
        return guides_by_platform
    for root, _dirs, files in os.walk(GUIDES_DIR):
        rel_root = os.path.relpath(root, GUIDES_DIR)
        if rel_root == ".":
            continue  # platform folders only start one level down
        platform = rel_root.split(os.sep)[0]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in PREVIEWABLE_EXTS:
                continue
            full_path = os.path.join(root, fname)
            title, revision = _guide_title_and_revision(fname)
            try:
                size_bytes = os.path.getsize(full_path)
            except OSError:
                size_bytes = 0
            guides_by_platform.setdefault(platform, []).append({
                "title": title,
                "revision": revision,
                "filename": fname,
                "type": "word" if ext == ".docx" else "excel",
                "path": full_path,
                "size_bytes": size_bytes,
            })
    return guides_by_platform

@app.route('/api/guides', methods=['GET'])
def api_guides():
    return jsonify(scan_guides())

def _resolve_run_output_selection(path):
    """Given an output file/folder path from a finished run, return the path to
    the TOP-LEVEL item directly under the Outputs root that should be
    highlighted in Explorer. Walks up from the output to the direct child of
    Outputs (usually the run's own sub-folder), so `explorer /select` opens the
    Outputs folder itself with the just-created run highlighted. Returns None if
    the path isn't inside an approved Outputs root (or IS the root itself)."""
    if not path or not isinstance(path, str):
        return None
    try:
        resolved = os.path.realpath(path)
    except Exception:
        return None
    if not os.path.exists(resolved):
        return None
    resolved_nc = os.path.normcase(resolved)
    for r in (effective_results_dir(), RESULTS_DIR):
        try:
            root = os.path.realpath(r)
        except Exception:
            continue
        root_nc = os.path.normcase(root)
        if resolved_nc == root_nc:
            return None  # the Outputs folder itself - nothing to highlight
        if resolved_nc.startswith(root_nc + os.sep):
            rel = os.path.relpath(resolved, root)
            first = rel.split(os.sep)[0]
            if first and first not in ('.', '..'):
                return os.path.join(root, first)
    return None

@app.route('/api/reports/reveal-run', methods=['POST'])
def reveal_run_output():
    """End-of-run 'open output folder' action: open the project's Outputs
    folder and select the exact output the finished run produced, so the user
    lands on it instantly. Falls back to just opening Outputs if the path can't
    be resolved."""
    data = request.json or {}
    path = data.get("path")
    target = _resolve_run_output_selection(path)
    if not target:
        # Fallback: open the Outputs folder without a selection.
        try:
            rd = os.path.normpath(effective_results_dir())
            subprocess.Popen(["explorer", rd], creationflags=CREATE_NO_WINDOW)
            threading.Thread(target=_focus_window_by_title, args=(os.path.basename(rd),), daemon=True).start()
            return jsonify({"status": "success", "fallback": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    try:
        norm = os.path.normpath(target)
        subprocess.Popen(["explorer", f"/select,{norm}"], creationflags=CREATE_NO_WINDOW)
        parent_name = os.path.basename(os.path.dirname(norm))
        if parent_name:
            threading.Thread(target=_focus_window_by_title, args=(parent_name,), daemon=True).start()
        server_logger.info(f"Revealed run output in Explorer: {norm}")
        return jsonify({"status": "success", "selected": norm})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _safe_delete_target(path, allow_dir=False):
    """Return the resolved path if it's safely deletable - strictly inside
    Outputs or Logs, never the root folder itself, and (for files) has an
    allowed report/log extension. Used by the Reports/Logs delete endpoints
    so a request can never remove anything outside those two folders."""
    if not path or not isinstance(path, str):
        return None
    roots = []
    for r in (RESULTS_DIR, effective_results_dir(), REPORT_FOLDER_DIR):
        try:
            roots.append(os.path.normcase(os.path.realpath(r)))
        except Exception:
            pass
    try:
        resolved = os.path.realpath(path)
    except Exception:
        return None
    resolved_nc = os.path.normcase(resolved)
    if resolved_nc in roots:
        return None  # never delete an approved root folder itself
    if not any(resolved_nc.startswith(root + os.sep) for root in roots):
        return None
    if allow_dir and os.path.isdir(resolved):
        return resolved
    if os.path.isfile(resolved) and os.path.splitext(resolved)[1].lower() in SAFE_OUTPUT_OPEN_EXTS:
        return resolved
    return None

@app.route('/api/reports/delete', methods=['POST'])
def delete_reports():
    data = request.json or {}
    paths = data.get("paths") or []
    if not isinstance(paths, list) or not paths:
        return jsonify({"error": "paths must be a non-empty list"}), 400
    deleted, failed = [], []
    for p in paths:
        safe = _safe_delete_target(p, allow_dir=False)
        if not safe:
            server_logger.warning(f"Blocked unsafe /api/reports/delete request: {p}")
            failed.append(p)
            continue
        try:
            os.remove(safe)
            server_logger.info(f"Deleted report: {safe}")
            deleted.append(p)
        except Exception as e:
            server_logger.error(f"Failed to delete report {p}: {e}")
            failed.append(p)
    return jsonify({"deleted": deleted, "failed": failed})

@app.route('/api/history/delete', methods=['POST'])
def delete_history_entries():
    data = request.json or {}
    run_ids = set(data.get("run_ids") or [])
    if not run_ids:
        return jsonify({"error": "run_ids must be a non-empty list"}), 400
    import shutil
    with _history_lock:
        history = load_history()
        remaining = []
        deleted_count = 0
        for entry in history:
            if entry.get("run_id") in run_ids:
                deleted_count += 1
                log_path = entry.get("run_log_path", "")
                if log_path:
                    folder = os.path.dirname(log_path)
                    safe_folder = _safe_delete_target(folder, allow_dir=True)
                    if safe_folder:
                        try:
                            shutil.rmtree(safe_folder, ignore_errors=True)
                            server_logger.info(f"Deleted log folder: {safe_folder}")
                        except Exception as e:
                            server_logger.warning(f"Failed to delete log folder {safe_folder}: {e}")
            else:
                remaining.append(entry)
        save_history(remaining)
    return jsonify({"status": "deleted", "count": deleted_count, "history": remaining})

def _is_own_instance_running(host, port):
    """True if PS Automation is already listening on host:port. Probes the
    /api/ping identity endpoint so we only match OUR app, never some
    unrelated service that happens to hold the port."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/ping", timeout=1.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("app") == "ps-automation"
    except Exception:
        return False

if __name__ == '__main__':
    import webbrowser

    # Local by default - each machine runs its own clone (see bootstrap.ps1),
    # so Selenium/ChromeDriver/every automation subprocess executes on
    # whichever machine actually launched it, using that machine's own
    # network. Override PS_AUTOMATION_HOST in .env only for a machine that's
    # deliberately meant to be reachable from other machines on the LAN.
    RUN_HOST = os.environ.get("PS_AUTOMATION_HOST", "127.0.0.1")
    RUN_PORT = int(os.environ.get("PS_AUTOMATION_PORT", "4444"))
    # 0.0.0.0 is bind-only (can't be connected TO); binding to it already
    # covers loopback, so self-checks below fall back to 127.0.0.1 in that
    # case. Otherwise they talk to RUN_HOST directly (also reachable from
    # this same machine, not just remotely).
    PROBE_HOST = "127.0.0.1" if RUN_HOST == "0.0.0.0" else RUN_HOST

    # Single-instance: if PS Automation is already running on this port, don't
    # start a second server. Just open a browser tab to the existing instance
    # and exit.
    if _is_own_instance_running(PROBE_HOST, RUN_PORT):
        server_logger.info(f"PS Automation is already running on port {RUN_PORT} - opening that window instead of starting a second server.")
        try:
            webbrowser.open(f"http://{PROBE_HOST}:{RUN_PORT}")
        except Exception:
            pass
        sys.exit(0)

    # Seed the persistent screenshot counter ONCE, before any run can finish,
    # so the all-time baseline is established from existing .docx and a run's
    # own new screenshots are never double-counted.
    try:
        ensure_screenshot_seed()
    except Exception as e:
        server_logger.warning(f"Screenshot seed at startup failed: {e}")

    def open_browser():
        # Open the browser the moment Flask is actually accepting connections,
        # instead of a blind fixed wait. This both opens sooner (typically well
        # under a second) and is more reliable on a slow machine.
        import socket
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection((PROBE_HOST, RUN_PORT), timeout=0.3):
                    break
            except OSError:
                time.sleep(0.1)
        webbrowser.open(f"http://{PROBE_HOST}:{RUN_PORT}")

    threading.Thread(target=open_browser, daemon=True).start()

    server_logger.info(f"Starting PS Automation v{APP_VERSION} on http://{RUN_HOST}:{RUN_PORT}")
    try:
        # threaded=True: without it, Werkzeug's dev server handles ONE HTTP
        # connection at a time - and /api/stream/<run_id> (the live console)
        # holds its connection open in a polling loop for the run's entire
        # duration, so a single long-running automation would otherwise
        # freeze the whole app for any other tab/request. Shared mutable
        # state (history/schedules/counters) is already lock-protected (see
        # _history_lock etc.), so serving requests on separate threads is safe.
        app.run(host=RUN_HOST, port=RUN_PORT, debug=False, threaded=True)
    except OSError as e:
        server_logger.error(f"Could not bind to {RUN_HOST}:{RUN_PORT} - {e}. Is port {RUN_PORT} already in use by another program?")
        sys.exit(1)
