"""
iDRAC Automated Firmware Update Utility - V5 AUTO REBOOT
-------------------------------------------
Main fixes in this version:
1. Before every firmware file, the script checks iDRAC SW Inventory against the target version from config.json filename.
   If the server already has the target version, it SKIPS the file and prints the current/target version.
2. During long RACADM commands, the script prints a heartbeat with elapsed time so it will not look stuck.
3. BIOS updates get a much longer timeout because Dell BIOS DUP upload/scheduling can legitimately take a long time.
4. If RACADM times out or returns Code 0 without a JID, the script checks Job Queue before deciding failure.
5. RACADM stdout + stderr are checked together.
6. If a firmware job stays Scheduled, the script can automatically power-cycle the server so BIOS/firmware jobs actually start instead of waiting forever.
"""

import os
import sys
import subprocess
import time
import logging
import re
import json
import shutil
from datetime import datetime

# ---------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------
# Log file is resolved relative to the script's own folder (or the app's
# PSAUTO_RUN_OUTPUT_DIR, when the app hands one over) instead of the current
# working directory - PS Automation launches this script with an arbitrary cwd.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_RUN_OUTPUT_DIR = os.environ.get("PSAUTO_RUN_OUTPUT_DIR", "").strip()
LOG_DIR = _RUN_OUTPUT_DIR or SCRIPT_DIR
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except Exception:
    pass
LOG_FILE_PATH = os.path.join(LOG_DIR, "fw_update.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# Global Constants
# ---------------------------------------------------------
DEFAULT_RACADM_PATH = r"C:\Program Files\Dell\SysMgt\iDRACTools\racadm\racadm.exe"
CWD = os.getcwd()

ABORT_SERVER_ON_FAILURE = False
RETRY_DELAY_SECONDS = 30
JOB_MAX_WAIT_SECONDS = 7200  # 120 minutes. BIOS/Lifecycle jobs may wait for reboot and take time.
HEARTBEAT_SECONDS = 15

# When RACADM says a job is Scheduled, Dell often means: waiting for host reboot.
# True = the script will run: racadm serveraction powercycle
# This is usually required for BIOS, CPLD, PERC, Backplane, Driver Pack and many iDRAC jobs.
AUTO_POWER_CYCLE_FOR_SCHEDULED_JOBS = True
SCHEDULED_POLLS_BEFORE_POWER_CYCLE = 2
POWER_CYCLE_WAIT_SECONDS = 900  # 15 minutes for server/iDRAC to become responsive again

# RACADM local file upload/schedule timeouts.
# BIOS DUP files are often slow over iDRAC. Do not kill them after 30 minutes.
DEFAULT_UPDATE_TIMEOUT_SECONDS = 3600       # 60 minutes
BIOS_UPDATE_TIMEOUT_SECONDS = 7200          # 120 minutes
IDRAC_UPDATE_TIMEOUT_SECONDS = 5400         # 90 minutes
DRIVER_PACK_TIMEOUT_SECONDS = 5400          # 90 minutes
DIAGNOSTICS_TIMEOUT_SECONDS = 3600          # 60 minutes

# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------
def find_racadm_path():
    if os.path.exists(DEFAULT_RACADM_PATH):
        return DEFAULT_RACADM_PATH
    path_from_env = shutil.which("racadm") or shutil.which("racadm.exe")
    if path_from_env and os.path.exists(path_from_env):
        return path_from_env
    logger.error("CRITICAL: racadm.exe was not found.")
    logger.error(f"Checked default path: {DEFAULT_RACADM_PATH}")
    logger.error("Install Dell iDRAC Tools / RACADM or add racadm.exe to PATH.")
    sys.exit(1)

RACADM_PATH = find_racadm_path()


def find_config_path():
    candidates = [
        os.path.join(SCRIPT_DIR, "config.json"),
        os.path.join(CWD, "config.json"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path

    logger.error("config.json was not found next to the script or in the current folder.")
    if not (sys.stdin is not None and sys.stdin.isatty()):
        # Non-interactive run (launched by PS Automation - stdin is a closed
        # pipe): prompting here would hang forever with zero output instead
        # of failing. Fail fast with a clear error instead.
        logger.error("Non-interactive run: cannot prompt for config.json path. Place config.json next to the script.")
        sys.exit(1)
    print("\nconfig.json was not found.")
    print("Put config.json in the same folder as this Python script, or paste the full path now.")
    manual_path = input("Enter full config.json path: ").strip().strip('"')
    if os.path.exists(manual_path):
        return manual_path
    logger.error(f"Configuration file missing: {manual_path}")
    sys.exit(1)

CONFIG_PATH = find_config_path()


def short_text(text, max_chars=4000):
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... [TRUNCATED]"


def fmt_elapsed(seconds):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def extract_version_from_filename(filename):
    """
    Extract target version from Dell DUP filename.
    BIOS_11N9M_WN64_1.7.5.EXE -> 1.7.5
    Network_Firmware_D81J3_LN_26.46.3048_A00_03.BIN -> 26.46.3048

    Important fix:
    older versions sometimes returned 1.7.5.EXE instead of 1.7.5,
    so the pre-check could fail even when the server was already updated.
    """
    name = os.path.basename(filename or "")
    match = re.search(r"_(?:WN64|LN)_([^_]+)", name, re.IGNORECASE)
    if not match:
        return ""

    version = match.group(1).strip()
    for ext in [".EXE", ".BIN", ".exe", ".bin"]:
        if version.endswith(ext):
            version = version[:-len(ext)]
    return version.strip()

def normalize_version(value):
    return (value or "").lower().replace(" ", "").replace("-", "").replace("_", "")


def get_inventory_search_target(filename):
    """Maps firmware filename to RACADM swinventory component search keyword."""
    f = filename.upper()
    if "BIOS" in f:
        return "bios"
    if "CPLD" in f:
        return "cpld"
    if "DIAGNOSTICS" in f:
        return "diag"
    if "IDRAC" in f or "LIFECYCLE" in f:
        return "idrac"
    if "DRIVERS" in f or "OS-DEPLOYMENT" in f:
        return "driver pack"
    if "SAS" in f or "PERC" in f or "RAID" in f:
        return "perc"
    if "BOSS" in f:
        return "boss"
    if "R4NT8" in f or "6Y9X7" in f:
        return "backplane"
    if "7PTPF" in f:
        return "broadcom"
    if "D81J3" in f:
        return "mellanox"
    if "5V215" in f:
        return "bcm5720"
    if "HVN2R" in f:
        return "network"
    return None


def load_config():
    logger.info(f"Using config file: {CONFIG_PATH}")
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to parse config.json. Error: {e}")
        sys.exit(1)


def resolve_fw_path(base_dir, raw_name):
    base_dir = base_dir.strip().strip('"')
    path = os.path.join(base_dir, raw_name)
    if os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    if ext:
        return None
    for ext in [".EXE", ".exe", ".BIN", ".bin"]:
        test_path = path + ext
        if os.path.exists(test_path):
            return test_path
    return None


def parse_swinventory(text):
    """
    Robust parser for racadm swinventory.
    Returns {ElementName: Version}.
    Supports both '=' and ':' formats, and common Dell fields like VersionString.
    """
    inventory = {}
    current = {}

    if not text:
        return inventory

    def flush_block():
        nonlocal current
        if not current:
            return
        name = current.get("elementname") or current.get("name") or current.get("fqdd") or current.get("componentid")
        ver = current.get("version") or current.get("versionstring") or current.get("currentversion") or current.get("rollbackversion")
        if name and ver:
            inventory[name.strip()] = ver.strip()
        current = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush_block()
            continue

        if "=" in line:
            key, val = line.split("=", 1)
        elif ":" in line:
            key, val = line.split(":", 1)
        else:
            continue

        key_norm = key.strip().lower().replace(" ", "")
        val = val.strip()

        if key_norm in {"elementname", "name", "fqdd", "componentid"}:
            # New component starts. Flush previous one first.
            if current.get("elementname") and (current.get("version") or current.get("versionstring") or current.get("currentversion")):
                flush_block()
            current[key_norm] = val
        elif key_norm in {"version", "versionstring", "currentversion", "rollbackversion"}:
            current[key_norm] = val
        else:
            # Keep a little extra context if useful later.
            current[key_norm] = val

    flush_block()
    return inventory


def find_inventory_match(raw_name, inv_dict):
    """
    Returns dict:
      {
        can_check: bool,
        already_current: bool,
        search: str,
        target: str,
        component: str,
        current: str,
        reason: str
      }
    """
    search_str = get_inventory_search_target(raw_name)
    target_ver = extract_version_from_filename(raw_name)

    result = {
        "can_check": bool(search_str and target_ver and inv_dict),
        "already_current": False,
        "search": search_str or "UNKNOWN",
        "target": target_ver or "UNKNOWN",
        "component": "NOT FOUND",
        "current": "UNKNOWN",
        "reason": ""
    }

    if not search_str or not target_ver:
        result["reason"] = "Cannot map filename to inventory component/version."
        return result
    if not inv_dict:
        result["reason"] = "Inventory is empty or unavailable."
        return result

    search_lower = search_str.lower()
    target_norm = normalize_version(target_ver)

    # Synonyms improve detection for Dell inventory names.
    synonyms = {
        "idrac": ["idrac", "lifecycle", "integrated dell remote access"],
        "driver pack": ["driver pack", "os driver", "os deployment", "drivers for os"],
        "diag": ["diag", "diagnostics"],
        "perc": ["perc", "raid", "sas"],
        "mellanox": ["mellanox", "connectx", "mlx"],
        "broadcom": ["broadcom", "netxtreme"],
        "bcm5720": ["bcm5720", "broadcom", "5720"],
    }
    terms = synonyms.get(search_lower, [search_lower])

    candidates = []
    for comp, ver in inv_dict.items():
        comp_lower = comp.lower()
        if any(term in comp_lower for term in terms):
            candidates.append((comp, ver))

    if not candidates:
        result["reason"] = f"No inventory component matched search target '{search_str}'."
        return result

    # Prefer candidate where version exactly/partially matches target.
    for comp, ver in candidates:
        actual_norm = normalize_version(ver)
        if target_norm and (target_norm in actual_norm or actual_norm in target_norm):
            result.update({
                "component": comp,
                "current": ver,
                "already_current": True,
                "reason": "Current version matches target version from config filename."
            })
            return result

    # Not current. Return first candidate for logging.
    comp, ver = candidates[0]
    result.update({
        "component": comp,
        "current": ver,
        "already_current": False,
        "reason": "Current version does not match target version."
    })
    return result



def get_update_timeout_seconds(filename):
    """Return a safe timeout per Dell DUP type. BIOS can be very slow through iDRAC/RACADM."""
    f = (filename or "").upper()
    if "BIOS" in f:
        return BIOS_UPDATE_TIMEOUT_SECONDS
    if "IDRAC" in f or "LIFECYCLE" in f:
        return IDRAC_UPDATE_TIMEOUT_SECONDS
    if "DRIVERS" in f or "OS-DEPLOYMENT" in f:
        return DRIVER_PACK_TIMEOUT_SECONDS
    if "DIAGNOSTICS" in f:
        return DIAGNOSTICS_TIMEOUT_SECONDS
    return DEFAULT_UPDATE_TIMEOUT_SECONDS


def is_bios_file(filename):
    return "BIOS" in (filename or "").upper()

def run_racadm(ip, username, password, args_list, timeout_sec=120, progress_label=None):
    """Run racadm safely without shell=True. Prints heartbeat for long commands."""
    cmd = [RACADM_PATH, "-r", ip, "-u", username, "-p", password, "--nocertwarn"] + args_list
    start = time.time()

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace"
        )

        while True:
            try:
                out, err = process.communicate(timeout=HEARTBEAT_SECONDS)
                return process.returncode, out or "", err or ""
            except subprocess.TimeoutExpired:
                elapsed = time.time() - start
                if elapsed >= timeout_sec:
                    process.kill()
                    out, err = process.communicate()
                    logger.error(f"[{ip}] Command timed out after {timeout_sec}s. Args: {' '.join(args_list)}")
                    return -1, out or "", (err or "") + "\nTimeoutExpired"

                label = progress_label or "RACADM command is still running"
                approx = min(95, int((elapsed / timeout_sec) * 100)) if timeout_sec else 0
                logger.info(f"[{ip}] {label}... elapsed {fmt_elapsed(elapsed)} / timeout {fmt_elapsed(timeout_sec)} (~{approx}%)")

    except Exception as e:
        logger.error(f"[{ip}] Execution error: {e}")
        return -1, "", str(e)


def get_job_ids_from_text(text):
    if not text:
        return []
    return re.findall(r"JID_[A-Z0-9_]+", text, flags=re.IGNORECASE)


def get_recent_job_id(ip, username, password):
    code, out, err = run_racadm(ip, username, password, ["jobqueue", "view"], timeout_sec=180, progress_label="Checking Job Queue")
    combined = f"{out}\n{err}"
    if code != 0:
        logger.warning(f"[{ip}] Could not read jobqueue view. Code={code}")
        logger.warning(short_text(combined))
        return None
    jobs = get_job_ids_from_text(combined)
    if jobs:
        return jobs[-1]
    return None


def get_jobqueue_snapshot(ip, username, password):
    code, out, err = run_racadm(ip, username, password, ["jobqueue", "view"], timeout_sec=180, progress_label="Checking Job Queue")
    combined = f"{out}\n{err}".strip()
    return code, combined

def is_already_installed_text(text):
    t = (text or "").lower()
    patterns = [
        "already installed",
        "already up to date",
        "already up-to-date",
        "rac1056",
        "same version",
        "current version is the same",
        "not applicable because the current version is the same",
    ]
    return any(p in t for p in patterns)


def is_success_without_jid_text(text):
    t = (text or "").lower()
    patterns = [
        "successfully",
        "firmware update has been scheduled",
        "update has been scheduled",
        "successfully scheduled",
        "successfully downloaded",
        "downloaded successfully",
        "update package successfully",
        "the update is in progress",
        "rac989",
        "rac1024",
    ]
    return any(p in t for p in patterns)


def powercycle_server(ip, username, password, reason="Firmware job is scheduled and waiting for reboot"):
    """Power-cycle the host through iDRAC so scheduled firmware jobs can start.
    This does not reboot the iDRAC itself; it reboots the server/host.
    """
    if not AUTO_POWER_CYCLE_FOR_SCHEDULED_JOBS:
        logger.warning(f"[{ip}] AUTO_POWER_CYCLE_FOR_SCHEDULED_JOBS=False. Job may stay Scheduled until manual reboot.")
        return False

    logger.warning(f"[{ip}] AUTO POWER CYCLE: {reason}")
    logger.warning(f"[{ip}] Running: racadm serveraction powercycle")
    code, out, err = run_racadm(
        ip, username, password,
        ["serveraction", "powercycle"],
        timeout_sec=180,
        progress_label="Sending server powercycle command"
    )
    combined = f"{out}\n{err}".strip()
    if combined:
        logger.info(f"[{ip}] Powercycle output:\n{short_text(combined, 2000)}")

    # RACADM can return different success messages. Code 0 is the main success signal.
    if code == 0 or "success" in combined.lower() or "initiated" in combined.lower():
        logger.info(f"[{ip}] Powercycle command accepted. Waiting for iDRAC/Job Queue to respond again...")
        return wait_for_idrac_ready(ip, username, password, max_wait=POWER_CYCLE_WAIT_SECONDS)

    logger.error(f"[{ip}] Failed to powercycle server. Code={code}")
    return False


def wait_for_idrac_ready(ip, username, password, max_wait=900):
    start = time.time()
    while time.time() - start < max_wait:
        elapsed = time.time() - start
        code, out, err = run_racadm(
            ip, username, password,
            ["jobqueue", "view"],
            timeout_sec=90,
            progress_label="Waiting for iDRAC after host reboot"
        )
        if code == 0:
            logger.info(f"[{ip}] iDRAC is responding again after {fmt_elapsed(elapsed)}.")
            return True
        logger.info(f"[{ip}] iDRAC/Job Queue not ready yet after reboot. Elapsed {fmt_elapsed(elapsed)}. Retrying in 30s...")
        time.sleep(30)

    logger.warning(f"[{ip}] iDRAC did not become ready within {fmt_elapsed(max_wait)}. Continuing monitor anyway.")
    return False


def monitor_job(ip, username, password, jid):
    logger.info(f"[{ip}] Monitoring Job {jid} in Job Queue...")
    start_time = time.time()
    scheduled_seen = 0
    powercycle_sent = False

    while time.time() - start_time < JOB_MAX_WAIT_SECONDS:
        elapsed = time.time() - start_time
        approx = min(95, int((elapsed / JOB_MAX_WAIT_SECONDS) * 100))
        code, out, err = run_racadm(
            ip, username, password,
            ["jobqueue", "view", "-i", jid],
            timeout_sec=90,
            progress_label=f"Reading Job {jid} status"
        )
        combined = f"{out}\n{err}"

        if code != 0:
            logger.warning(f"[{ip}] Failed to retrieve job status. Possible iDRAC reboot/busy. Elapsed {fmt_elapsed(elapsed)} (~{approx}%). Retrying in 30s...")
            logger.warning(short_text(combined))
            time.sleep(30)
            continue

        status_match = re.search(r"Status\s*[=:]\s*([^\r\n]+)", combined, re.IGNORECASE)
        message_match = re.search(r"Message\s*[=:]\s*([^\r\n]+)", combined, re.IGNORECASE)
        percent_match = re.search(r"(?:Percent|Progress)\s*[=:]\s*(\d+)", combined, re.IGNORECASE)

        status = status_match.group(1).strip() if status_match else "Unknown"
        message = message_match.group(1).strip() if message_match else ""
        real_percent = percent_match.group(1) + "%" if percent_match else f"~{approx}% estimated"
        status_lower = status.lower()
        combined_lower = combined.lower()

        if "completed" in status_lower and "error" not in status_lower and "fail" not in status_lower:
            logger.info(f"[{ip}] Job {jid} finished successfully. Status: {status}. Progress: 100%")
            return True

        if any(x in status_lower for x in ["failed", "completed with errors", "completed with error"]):
            logger.error(f"[{ip}] Job {jid} failed. Status: {status}. Message: {message}")
            logger.error(short_text(combined))
            return False

        if any(x in combined_lower for x in ["failed", "completed with errors", "completed with error"]):
            logger.error(f"[{ip}] Job {jid} failed according to job output. Status: {status}. Message: {message}")
            logger.error(short_text(combined))
            return False

        logger.info(f"[{ip}] Job {jid} status: {status}. Progress: {real_percent}. Elapsed: {fmt_elapsed(elapsed)}. {message}")

        # Dell behavior: after upload, BIOS/firmware jobs often stay "Scheduled" until the HOST reboots.
        # The original script looked stuck here forever. This version power-cycles once, then continues monitoring.
        if "scheduled" in status_lower and not powercycle_sent:
            scheduled_seen += 1
            logger.info(f"[{ip}] Job is Scheduled ({scheduled_seen}/{SCHEDULED_POLLS_BEFORE_POWER_CYCLE}). Dell usually waits for host reboot before applying firmware.")
            if scheduled_seen >= SCHEDULED_POLLS_BEFORE_POWER_CYCLE:
                powercycle_sent = True
                powercycle_server(ip, username, password, reason=f"Job {jid} is Scheduled and waiting for reboot")
                # After reboot command, give Lifecycle Controller a short window to transition the job.
                time.sleep(60)
                continue
        else:
            # Reset only when it actually moves away from Scheduled.
            if "scheduled" not in status_lower:
                scheduled_seen = 0

        time.sleep(60)

    logger.error(f"[{ip}] Monitoring job {jid} timed out after {JOB_MAX_WAIT_SECONDS} seconds.")
    return False

def update_firmware(ip, username, password, filepath):
    filename = os.path.basename(filepath)
    timeout_sec = get_update_timeout_seconds(filename)

    logger.info(f"[{ip}] Uploading/Scheduling update payload: {filename}")
    logger.info(f"[{ip}] Timeout for this file type: {fmt_elapsed(timeout_sec)}. Heartbeat every {HEARTBEAT_SECONDS}s.")

    if is_bios_file(filename):
        logger.info(f"[{ip}] BIOS update detected. This step can legitimately take 10-60+ minutes via iDRAC/RACADM.")
        logger.info(f"[{ip}] Do not close PowerShell while heartbeat is printing. It usually is not stuck.")

    code, out, err = run_racadm(
        ip, username, password,
        ["update", "-f", filepath],
        timeout_sec=timeout_sec,
        progress_label=f"Uploading/Scheduling {filename}"
    )
    combined = f"{out}\n{err}".strip()

    jid_list = get_job_ids_from_text(combined)
    if jid_list:
        jid = jid_list[-1]
        logger.info(f"[{ip}] Job scheduled: {jid}")
        return True, jid

    if is_already_installed_text(combined):
        logger.info(f"[{ip}] Firmware already installed / up-to-date according to RACADM: {filename}")
        return True, None

    # Very important: for BIOS, RACADM sometimes returns late, returns code 0 without printing JID,
    # or appears to timeout while iDRAC already created/started the job. Always check jobqueue first.
    if code in (0, -1) or is_success_without_jid_text(combined):
        if code == -1:
            logger.warning(f"[{ip}] RACADM command timed out for {filename}. Before marking failed, checking Job Queue...")
        else:
            logger.warning(f"[{ip}] RACADM returned code {code} but no JID was found. Checking Job Queue...")

        if combined:
            logger.info(f"[{ip}] RACADM output:\n{short_text(combined)}")

        fallback_jid = get_recent_job_id(ip, username, password)
        if fallback_jid:
            logger.info(f"[{ip}] Found fallback job in queue: {fallback_jid}")
            return True, fallback_jid

        if code == 0:
            logger.warning(f"[{ip}] No JID found, but RACADM returned Code 0. Treating as scheduled/success without job monitor.")
            return True, None

    logger.error(f"[{ip}] Failed to schedule update. Code: {code}. File: {filename}")
    logger.error(f"[{ip}] RACADM stdout/stderr:\n{short_text(combined)}")
    return False, None

def print_inventory_summary(server_name, inv_dict):
    logger.info(f"[{server_name}] Inventory components parsed: {len(inv_dict)}")
    if not inv_dict:
        logger.warning(f"[{server_name}] Inventory is empty. Version skip-check cannot work reliably for this server.")


def process_server(server_name, ip, username, password, files_list, base_dir):
    summary = {}
    total_files = len(files_list)

    logger.info(f"[{server_name}] Fetching SW Inventory for version pre-check...")
    code, out_initial, err_initial = run_racadm(ip, username, password, ["swinventory"], timeout_sec=180, progress_label="Fetching SW Inventory")

    if code != 0:
        logger.warning(f"[{server_name}] Could not fetch initial inventory. The script will still try updates when it cannot verify versions.")
        logger.warning(short_text(f"{out_initial}\n{err_initial}"))

    global_inv = parse_swinventory(out_initial)
    print_inventory_summary(server_name, global_inv)

    for index, raw_name in enumerate(files_list, start=1):
        file_progress = int((index - 1) / total_files * 100) if total_files else 0
        logger.info("------------------------------------------------------------")
        logger.info(f"[{server_name}] FILE {index}/{total_files} ({file_progress}%) - {raw_name}")

        if server_name == "SRVMGT" and "HVN2R" in raw_name.upper():
            logger.info(f"[{server_name}] SKIP: {raw_name}; NGINX-only firmware policy.")
            summary[raw_name] = "Skipped (NGINX Only Policy)"
            continue

        fw_path = resolve_fw_path(base_dir, raw_name)
        if not fw_path:
            logger.error(f"[{server_name}] Firmware file missing: {raw_name}")
            logger.error(f"[{server_name}] Expected folder: {base_dir}")
            summary[raw_name] = "Failed (File Missing)"
            if ABORT_SERVER_ON_FAILURE:
                break
            continue

        # ---------------- VERSION CHECK BEFORE UPDATE ----------------
        match = find_inventory_match(raw_name, global_inv)
        logger.info(f"[{server_name}] VERSION CHECK: {raw_name}")
        logger.info(f"[{server_name}] Search target: {match['search']} | Target from config: {match['target']}")
        logger.info(f"[{server_name}] Inventory component: {match['component']} | Current version: {match['current']}")
        logger.info(f"[{server_name}] Check result: {match['reason']}")

        if match["already_current"]:
            logger.info(f"[{server_name}] SKIP: Already updated according to config target version. Current={match['current']} Target={match['target']}")
            summary[raw_name] = f"Skipped - Already current ({match['current']})"
            continue

        logger.info(f"[{server_name}] UPDATE NEEDED: Current={match['current']} Target={match['target']} File={raw_name}")

        if is_bios_file(raw_name):
            jq_code, jq_text = get_jobqueue_snapshot(ip, username, password)
            if jq_code == 0 and get_job_ids_from_text(jq_text):
                logger.info(f"[{server_name}] Current Job Queue before BIOS update:\n{short_text(jq_text, 2000)}")
                logger.info(f"[{server_name}] If there is already a BIOS/update job queued/running, wait for it or clear jobqueue before retrying.")

        success = False
        max_attempts = 1 if is_bios_file(raw_name) else 3
        if is_bios_file(raw_name):
            logger.info(f"[{server_name}] BIOS file: using 1 attempt with a long timeout instead of 3 short retries.")

        for attempt in range(1, max_attempts + 1):
            logger.info(f"[{server_name}] Applying {raw_name} (Attempt {attempt}/{max_attempts})")

            code_before, out_before, _ = run_racadm(ip, username, password, ["swinventory"], timeout_sec=180, progress_label="Fetching inventory before update")
            inv_before = parse_swinventory(out_before) if code_before == 0 else {}

            ok, jid = update_firmware(ip, username, password, fw_path)

            if not ok:
                logger.warning(f"[{server_name}] Update initiation failed for {raw_name}.")
                if attempt < max_attempts:
                    logger.info(f"[{server_name}] Retrying in {RETRY_DELAY_SECONDS} seconds...")
                    time.sleep(RETRY_DELAY_SECONDS)
                continue

            if jid:
                job_success = monitor_job(ip, username, password, jid)
                if not job_success:
                    logger.warning(f"[{server_name}] Job failed/not completed for {raw_name}.")
                    if attempt < max_attempts:
                        logger.info(f"[{server_name}] Retrying in {RETRY_DELAY_SECONDS} seconds...")
                        time.sleep(RETRY_DELAY_SECONDS)
                    continue

            logger.info(f"[{server_name}] Validating inventory after update...")
            time.sleep(20)

            code_after, out_after, err_after = run_racadm(ip, username, password, ["swinventory"], timeout_sec=180, progress_label="Fetching inventory after update")
            inv_after = parse_swinventory(out_after) if code_after == 0 else {}

            # Re-check target after update
            after_match = find_inventory_match(raw_name, inv_after if inv_after else global_inv)
            if after_match["already_current"]:
                logger.info(f"[{server_name}] SUCCESS: Version now matches config. Current={after_match['current']} Target={after_match['target']}")
                summary[raw_name] = f"Updated/Verified ({after_match['current']})"
            else:
                # Some Dell components update inventory only after reboot.
                logger.info(f"[{server_name}] Update command/job completed, but inventory does not show target yet.")
                logger.info(f"[{server_name}] This can be normal if the component needs reboot or Lifecycle inventory refresh.")
                summary[raw_name] = "Updated/Scheduled (Inventory not refreshed yet)"

            success = True
            global_inv = inv_after if inv_after else global_inv
            break

        if not success:
            logger.error(f"[{server_name}] Failed after {max_attempts} attempts: {raw_name}")
            summary[raw_name] = "Failed (Max Retries)"

            if ABORT_SERVER_ON_FAILURE:
                for skip_name in files_list[index:]:
                    summary[skip_name] = "Skipped (Due to previous failure)"
                break

    logger.info(f"[{server_name}] Firmware pipeline concluded. Progress: 100%")
    return summary


def print_menu():
    print("\n=============================================")
    print("           iDRAC Firmware Update V5 AUTO REBOOT")
    print("=============================================")
    print("To run this on ALL servers Press - 1")
    print("To Exit Press - 2")
    print("")
    print("Or enter specific IP suffixes to update, then type 'done':")
    print("To run this on FM1 Press - 122")
    print("To run this on FM2 Press - 123")
    print("To run this on PMC1 Press - 124")
    print("To run this on PMC2 Press - 125")
    print("To run this on PMC3 Press - 126")
    print("To run this on SRVMGT Press - 127")
    print("To run this on NGINX Press - 128")
    print("=============================================")


def select_targets():
    suffix_map = {
        "122": "FM1",
        "123": "FM2",
        "124": "PMC1",
        "125": "PMC2",
        "126": "PMC3",
        "127": "SRVMGT",
        "128": "NGINX",
    }

    # Non-interactive path: PS Automation (or any headless caller) sets
    # PSAUTO_TARGETS to "ALL" (or "1") for every server, or a comma/newline
    # separated list of the menu suffixes (122,123,...) or server names
    # (FM1,FM2,...). Falls back to the original interactive menu below when
    # PSAUTO_TARGETS isn't set, so a standalone run is unchanged.
    env_targets = os.environ.get("PSAUTO_TARGETS", "").strip()
    if env_targets:
        if env_targets.lower() in ("all", "1"):
            selected_targets = set(suffix_map.values())
            logger.info(f"PSAUTO_TARGETS=ALL -> Queue: {', '.join(sorted(selected_targets))}")
            return selected_targets

        selected_targets = set()
        for item in re.split(r"[,\n\r]+", env_targets):
            item = item.strip()
            if not item:
                continue
            if item in suffix_map:
                selected_targets.add(suffix_map[item])
            elif item.upper() in suffix_map.values():
                selected_targets.add(item.upper())
            else:
                logger.warning(f"PSAUTO_TARGETS: ignoring unrecognized target '{item}'.")

        if not selected_targets:
            logger.error("PSAUTO_TARGETS was set but no valid targets were recognized. Exiting.")
            sys.exit(1)
        logger.info(f"PSAUTO_TARGETS -> Queue: {', '.join(sorted(selected_targets))}")
        return selected_targets

    if not (sys.stdin is not None and sys.stdin.isatty()):
        # Non-interactive run with no PSAUTO_TARGETS: prompting here would
        # hang forever with zero output instead of failing.
        logger.error("Non-interactive run: PSAUTO_TARGETS is not set. Set it to 'ALL' or a comma-separated list of target names/suffixes (e.g. FM1,FM2 or 122,123).")
        sys.exit(1)

    selected_targets = set()
    print_menu()

    while True:
        choice = input("\nEnter option (1, 2), suffix, 'rm <suffix>' to remove, or 'done': ").strip().lower()

        if choice == "1":
            selected_targets = set(suffix_map.values())
            print(f"\nQueue: {', '.join(sorted(selected_targets))}")
            break
        if choice == "2":
            print("Exiting...")
            sys.exit(0)
        if choice == "done":
            if not selected_targets:
                print("No servers selected. Exiting.")
                sys.exit(0)
            break
        if choice.startswith("rm "):
            rm_suffix = choice.split(" ", 1)[1].strip()
            if rm_suffix in suffix_map:
                srv = suffix_map[rm_suffix]
                selected_targets.discard(srv)
                print(f"Removed {srv} from queue.")
            else:
                print(f"Invalid suffix '{rm_suffix}'.")
            print(f"Current Queue: {', '.join(sorted(selected_targets))}" if selected_targets else "Current Queue: [Empty]")
            continue
        if choice in suffix_map:
            selected_targets.add(suffix_map[choice])
            print(f"Added {suffix_map[choice]} to queue.")
            print(f"Current Queue: {', '.join(sorted(selected_targets))}")
            continue
        print(f"Invalid value '{choice}'.")

    return selected_targets


def main():
    logger.info(f"Using RACADM: {RACADM_PATH}")
    config = load_config()
    plans = config.get("plans", {})
    servers = config.get("servers", {})

    selected_targets = select_targets()

    # Credentials: PSAUTO_USERNAME/PASSWORD (explicit override from the app's
    # UI) wins; otherwise PSAUTO_DEFAULT_USERNAME/PASSWORD (the app's
    # encrypted .env default) is used; a standalone run with neither set
    # keeps the original hardcoded fallback below.
    username = os.environ.get("PSAUTO_USERNAME", "").strip() or os.environ.get("PSAUTO_DEFAULT_USERNAME", "").strip() or "root"
    password = os.environ.get("PSAUTO_PASSWORD", "") or os.environ.get("PSAUTO_DEFAULT_PASSWORD", "") or "admin1234"

    active_plans = []
    for plan_name, plan_data in plans.items():
        targets = plan_data.get("targets", [])
        if any(t in selected_targets for t in targets):
            active_plans.append(plan_name)

    print("\n=============================================")
    print("        FIRMWARE DIRECTORY PATHS")
    print("=============================================")

    plan_dirs = {}
    non_interactive = not (sys.stdin is not None and sys.stdin.isatty())
    env_base_dir = os.environ.get("PSAUTO_FW_BASE_DIR", "").strip().strip('"')
    for plan_name in active_plans:
        # Non-interactive path: PSAUTO_FW_DIR_<PLAN> (e.g. PSAUTO_FW_DIR_FM)
        # wins for that specific plan; PSAUTO_FW_BASE_DIR is a shared
        # fallback applied to any plan without its own override. Falls back
        # to the original interactive prompt below when neither is set.
        env_dir = os.environ.get(f"PSAUTO_FW_DIR_{plan_name}", "").strip().strip('"') or env_base_dir
        if env_dir:
            if not os.path.exists(env_dir) or not os.path.isdir(env_dir):
                logger.error(f"Firmware folder for plan '{plan_name}' does not exist: {env_dir}")
                sys.exit(1)
            plan_dirs[plan_name] = env_dir
            continue

        if non_interactive:
            logger.error(f"Non-interactive run: no firmware folder configured for plan '{plan_name}'. Set PSAUTO_FW_DIR_{plan_name} or PSAUTO_FW_BASE_DIR.")
            sys.exit(1)

        dir_path = input(f"Enter the base firmware folder path for {plan_name}: ").strip().strip('"')
        while not os.path.exists(dir_path) or not os.path.isdir(dir_path):
            print(f"ERROR: Directory '{dir_path}' does not exist!")
            dir_path = input(f"Please re-enter the valid folder path for {plan_name}: ").strip().strip('"')
        plan_dirs[plan_name] = dir_path

    global_summary = {}

    for plan_name, plan_data in plans.items():
        targets = plan_data.get("targets", [])
        run_targets = [t for t in targets if t in selected_targets]
        if not run_targets:
            continue

        base_dir = plan_dirs[plan_name]
        files = plan_data.get("files", [])
        logger.info(f"=== Beginning Plan: {plan_name} | Servers: {len(run_targets)} | Files per server: {len(files)} ===")

        for srv_index, srv in enumerate(run_targets, start=1):
            ip = servers.get(srv)
            if not ip:
                logger.warning(f"Server {srv} has no IP in config. Skipping.")
                continue
            plan_progress = int((srv_index - 1) / len(run_targets) * 100) if run_targets else 0
            logger.info("============================================================")
            logger.info(f"PLAN {plan_name}: Server {srv_index}/{len(run_targets)} ({plan_progress}%) - Connecting to {srv} at {ip}")
            srv_summary = process_server(srv, ip, username, password, files, base_dir)
            global_summary[srv] = srv_summary

    print("\n=============================================")
    print("               EXECUTION SUMMARY")
    print("=============================================")
    for srv, srv_summary in global_summary.items():
        print(f"\nServer: {srv}")
        for fw, stat in srv_summary.items():
            print(f"  - {fw}: {stat}")
    print("=============================================\n")
    print(f"Log file: {os.path.abspath(LOG_FILE_PATH)}")


if __name__ == "__main__":
    main()
