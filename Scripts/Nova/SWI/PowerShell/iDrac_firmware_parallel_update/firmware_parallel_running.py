import os
import sys
import subprocess
import time
import re
import json
import shutil
import threading
import logging
import msvcrt
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("firmware_update.log", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
progress_data  = {}
server_ips     = {}
data_lock      = threading.Lock()
summary_lock   = threading.Lock()
summary_counter = 0
stop_listener  = False

# RACADM path
DEFAULT_RACADM_PATH = r"C:\Program Files\Dell\SysMgt\iDRACTools\racadm\racadm.exe"
RACADM_PATH = DEFAULT_RACADM_PATH if os.path.exists(DEFAULT_RACADM_PATH) else shutil.which("racadm")

# Summary display order: server name → IP suffix (for sorting)
SUFFIX_ORDER = {
    "FM1": 122, "FM2": 123,
    "PMC1": 124, "PMC2": 125, "PMC3": 126,
    "SRVMGT": 127, "NGINX": 128
}

# ---------------------------------------------------------------------------
# RACADM wrapper
# ---------------------------------------------------------------------------
def run_racadm(ip, user, pwd, args, timeout=600):
    cmd = [RACADM_PATH, "-r", ip, "-u", user, "-p", pwd, "--nocertwarn"] + args
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace"
        )
        return (proc.stdout + proc.stderr).strip()
    except subprocess.TimeoutExpired:
        return "ERROR_TIMEOUT: RACADM command timed out."
    except Exception as e:
        return f"ERROR_CONNECTION: {e}"

# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------
def update_progress_and_log(srv, fname, status):
    with data_lock:
        if srv not in progress_data:
            progress_data[srv] = {}
        old = progress_data[srv].get(fname)
        progress_data[srv][fname] = status
    if old != status:
        logger.info(f"[{srv} ({server_ips.get(srv, 'N/A')})] {fname} -> {status}")

# ---------------------------------------------------------------------------
# Status summary (P keypress)
# ---------------------------------------------------------------------------
def print_status_table():
    global summary_counter
    with summary_lock:
        summary_counter += 1
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        lines = [
            f"\n=======================================================",
            f"SUMMARY #{summary_counter} | {ts}",
            f"=======================================================",
        ]
        with data_lock:
            for srv in sorted(progress_data.keys(), key=lambda s: SUFFIX_ORDER.get(s, 999)):
                lines.append(f"\nServer: {srv} ({server_ips.get(srv, 'Unknown IP')})")
                lines.append(f"{'Firmware File':<70} | Status")
                lines.append("-" * 90)
                for fname, status in progress_data[srv].items():
                    lines.append(f"{fname:<70} | {status}")
        lines.append("=======================================================\n")
        text = "\n".join(lines)
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
        try:
            with open("firmware_update.log", "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Keyboard listener  (P = summary, Q = quit)
# ---------------------------------------------------------------------------
def keyboard_listener():
    global stop_listener
    while not stop_listener:
        if msvcrt.kbhit():
            try:
                key = msvcrt.getch().decode('utf-8', errors='ignore').lower()
                if key == 'p':
                    print_status_table()
                elif key == 'q':
                    logger.info("Exiting on user command 'q'.")
                    os._exit(0)
            except Exception:
                pass
        time.sleep(0.1)

# ---------------------------------------------------------------------------
# Software inventory helpers  (version check before installing)
# ---------------------------------------------------------------------------
def parse_swinventory(text):
    """Parse racadm swinventory output into {element_name: version} dict."""
    inventory = {}
    current = None
    for line in (text or "").splitlines():
        line = line.strip()
        if "ElementName" in line:
            parts = line.split("=", 1) if "=" in line else line.split(":", 1)
            if len(parts) == 2:
                current = parts[1].strip()
        elif "Version" in line and current:
            parts = line.split("=", 1) if "=" in line else line.split(":", 1)
            if len(parts) == 2:
                inventory[current] = parts[1].strip()
                current = None
    return inventory

def extract_version_from_filename(filename):
    """Extract version string from a Dell firmware filename."""
    m = re.search(r'_(?:WN64|LN)_([a-zA-Z0-9.\-]+)', filename, re.IGNORECASE)
    if m:
        return m.group(1).split('_')[0]
    return ""

def get_inventory_search_target(filename):
    """Map filename to the keyword to look for in swinventory ElementName."""
    f = filename.upper()
    if "BIOS"       in f: return "bios"
    if "CPLD"       in f: return "cpld"
    if "DIAGNOSTIC" in f: return "diag"
    if "IDRAC"      in f or "LIFECYCLE" in f: return "lifecycle"
    if "DRIVERS"    in f: return "driver pack"
    if "SAS"        in f or "PERC" in f: return "perc"
    if "BOSS"       in f: return "boss"
    if "R4NT8"      in f or "6Y9X7" in f: return "backplane"
    if "7PTPF"      in f: return "broadcom"
    if "D81J3"      in f: return "mellanox"
    if "5V215"      in f: return "bcm5720"
    if "HVN2R"      in f: return "adv"
    return None

def is_already_installed(fname, inventory):
    """
    Return True if the target version from fname already exists in inventory.
    Return False (needs update) otherwise.
    If we cannot determine, return False to be safe.
    """
    search_key = get_inventory_search_target(fname)
    target_ver = extract_version_from_filename(fname)
    if not search_key or not target_ver:
        return False   # cannot determine → try installing

    search_key  = search_key.lower()
    target_norm = target_ver.lower().replace("-", "").replace(" ", "")

    for comp, ver in inventory.items():
        if search_key in comp.lower():
            actual_norm = ver.lower().replace("-", "").replace(" ", "")
            if target_norm in actual_norm or actual_norm in target_norm:
                return True   # already at this version
    return False

# ---------------------------------------------------------------------------
# Job queue helpers
# ---------------------------------------------------------------------------
def find_new_jid_in_queue(ip, user, pwd, fname):
    """
    Search jobqueue for a JID that matches this firmware and is NOT
    in a terminal state (completed / failed).  This is used only right
    after a 'Copying Operation' response to locate the newly created job.
    """
    out = run_racadm(ip, user, pwd, ["jobqueue", "view"])
    short = fname.split('_')[0].lower()
    blocks = re.split(r'job id\s*[=:]\s*', out, flags=re.IGNORECASE)
    for block in blocks[1:]:
        jid_m = re.match(r'(JID_[A-Z0-9_]+)', block.strip(), re.IGNORECASE)
        if not jid_m:
            continue
        jid   = jid_m.group(1)
        blow  = block.lower()
        if short in blow and not any(x in blow for x in ["completed", "failed"]):
            return jid
    return None

# ---------------------------------------------------------------------------
# Job monitoring
# ---------------------------------------------------------------------------
def monitor_job(ip, user, pwd, jid, srv, fname):
    """Poll a JID until it completes, fails, or times out (2 h)."""
    start = time.time()
    while (time.time() - start) < 7200:
        out      = run_racadm(ip, user, pwd, ["jobqueue", "view", "-i", jid])
        out_low  = out.lower()

        # Percentage
        pct_m = re.search(r'(\d+)%', out)
        if not pct_m:
            pct_m = re.search(r'Percent Complete\s*[=:]\s*\[?(\d+)\]?', out, re.IGNORECASE)
        pct = f"({pct_m.group(1)}%)" if pct_m else "(0%)"

        # Terminal states — check failure variants FIRST before generic "completed"
        if "failed" in out_low or "completed with error" in out_low:
            return False
        if "completed" in out_low:
            return True

        # Intermediate states
        if "pending reboot" in out_low or "reboot pending" in out_low or "waiting for reboot" in out_low:
            update_progress_and_log(srv, fname, f"⏳ PENDING REBOOT {pct}")
            run_racadm(ip, user, pwd, ["serveraction", "powercycle"])
        elif "scheduled" in out_low:
            update_progress_and_log(srv, fname, f"⏳ SCHEDULED {pct}")
            run_racadm(ip, user, pwd, ["serveraction", "powercycle"])
        elif "pending" in out_low:
            update_progress_and_log(srv, fname, f"⏳ PENDING {pct}")
        elif "running" in out_low or "downloading" in out_low:
            update_progress_and_log(srv, fname, f"🚀 RUNNING {pct}")
        else:
            sm = re.search(r'Status\s*[=:]\s*([a-zA-Z ]+)', out, re.IGNORECASE)
            label = sm.group(1).strip().upper() if sm else "MONITORING"
            update_progress_and_log(srv, fname, f"🔄 {label} {pct}")

        time.sleep(20)

    return False   # timeout

# ---------------------------------------------------------------------------
# Single firmware update  (version-check → 2 attempts max → mark result)
# ---------------------------------------------------------------------------
def update_firmware_file(ip, user, pwd, srv, fname, base_dir):
    # 1. Resolve local file path
    fpath = os.path.join(base_dir, fname)
    if not os.path.exists(fpath):
        for ext in [".EXE", ".exe", ".BIN", ".bin"]:
            if os.path.exists(fpath + ext):
                fpath += ext
                break
        else:
            update_progress_and_log(srv, fname, "❌ ERROR: FILE NOT FOUND")
            return False

    # 2. Version check via swinventory — skip if already at target version
    update_progress_and_log(srv, fname, "🔍 CHECKING VERSION")
    inv_out   = run_racadm(ip, user, pwd, ["swinventory"])
    inventory = parse_swinventory(inv_out)
    if is_already_installed(fname, inventory):
        update_progress_and_log(srv, fname, "✅ ALREADY UP-TO-DATE (SKIPPED)")
        return True

    # 3. Upload attempts (max 2), fresh every run
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        label = f"(Attempt {attempt}/{max_attempts})"
        update_progress_and_log(srv, fname, f"📤 UPLOADING {label}")

        resp      = run_racadm(ip, user, pwd, ["update", "-f", fpath], timeout=600)
        resp_low  = resp.lower()

        # iDRAC says already installed
        if "already installed" in resp_low or "rac1056" in resp_low:
            update_progress_and_log(srv, fname, "✅ ALREADY INSTALLED/DONE")
            return True

        # iDRAC returned a JID directly
        jid = None
        jid_m = re.search(r'JID_[A-Z0-9_]+', resp, re.IGNORECASE)
        if jid_m:
            jid = jid_m.group(0)

        # iDRAC said "Copying Operation has begun" — JID was queued in background
        elif any(x in resp_low for x in
                 ["copying operation has begun", "has begun", "rac1047"]):
            update_progress_and_log(srv, fname, f"📤 COPYING — waiting for JID {label}")
            for wait in [15, 20, 25]:          # up to ~60 s total
                time.sleep(wait)
                jid = find_new_jid_in_queue(ip, user, pwd, fname)
                if jid:
                    break

        # Monitor if we have a JID
        if jid:
            update_progress_and_log(srv, fname, f"🚀 RUNNING {label} (0%)")
            if monitor_job(ip, user, pwd, jid, srv, fname):
                update_progress_and_log(srv, fname, "✅ COMPLETE")
                return True
            logger.warning(f"[{srv}] {fname} attempt {attempt} — job {jid} failed.")
        else:
            # Classify the error
            err = "UPLOAD FAILED"
            if "RAC1024" in resp:
                err = "BUSY (RAC1024)"
            elif "file" in resp_low and "copying" not in resp_low:
                err = "PATH/FILE ERROR"
            logger.warning(f"[{srv}] {fname} attempt {attempt} — {err}: {resp[:200]}")

        if attempt < max_attempts:
            time.sleep(20)   # brief cooldown before retry

    update_progress_and_log(srv, fname, "❌ FAILED (Tried 2 times and failed)")
    return False

# ---------------------------------------------------------------------------
# Per-server thread
# ---------------------------------------------------------------------------
def process_server(srv, ip, user, pwd, files, base_dir):
    for f in files:
        update_progress_and_log(srv, f, "⚪ WAITING")
    for fname in files:
        update_firmware_file(ip, user, pwd, srv, fname, base_dir)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # Pre-flight checks
    if not os.path.exists("config.json"):
        logger.error("CRITICAL: config.json not found in the current directory.")
        sys.exit(1)
    if not RACADM_PATH or not os.path.exists(RACADM_PATH):
        logger.error(f"CRITICAL: racadm.exe not found at {DEFAULT_RACADM_PATH} or in PATH.")
        sys.exit(1)

    with open("config.json", "r", encoding="utf-8") as fh:
        config = json.load(fh)

    global server_ips
    server_ips = config.get("servers", {})

    # Credentials (env-var override; default root/admin1234)
    user = os.getenv("IDRAC_USER",     "root")
    pwd  = os.getenv("IDRAC_PASSWORD", "admin1234")

    # ── Menu ────────────────────────────────────────────────────────────────
    print("\n=============================================")
    print("           iDRAC Firmware Update             ")
    print("=============================================")
    print("To run this on ALL servers Press - 1")
    print("To Exit Press - 2")
    print("")
    print("Or enter specific IP suffixes to update (type 'done' when finished):")
    print("To run this on FM1 Press - 122")
    print("To run this on FM2 Press - 123")
    print("To run this on PMC1 Press - 124")
    print("To run this on PMC2 Press - 125")
    print("To run this on PMC3 Press - 126")
    print("To run this on SRVMGT Press - 127")
    print("To run this on NGINX Press - 128")
    print("=============================================")

    suffix_map = {
        "122": "FM1",   "123": "FM2",
        "124": "PMC1",  "125": "PMC2", "126": "PMC3",
        "127": "SRVMGT","128": "NGINX"
    }

    selected_targets = set()
    while True:
        choice = input("\nEnter option (1, 2), suffix, 'rm <suffix>' to remove, or 'done': ").strip().lower()
        if choice == '1':
            selected_targets = set(suffix_map.values())
            print(f"\nQueue: {', '.join(sorted(selected_targets))}")
            break
        elif choice == '2':
            print("Exiting...")
            sys.exit(0)
        elif choice == 'done':
            if not selected_targets:
                print("No servers selected. Exiting.")
                sys.exit(0)
            break
        elif choice.startswith("rm "):
            rm_sfx = choice.split(" ", 1)[1].strip()
            if rm_sfx in suffix_map:
                srv = suffix_map[rm_sfx]
                if srv in selected_targets:
                    selected_targets.discard(srv)
                    print(f"Removed {srv} from queue.")
            else:
                print(f"Invalid suffix '{rm_sfx}'.")
            print(f"Current Queue: {', '.join(sorted(selected_targets))}" if selected_targets else "Current Queue: [Empty]")
        elif choice in suffix_map:
            selected_targets.add(suffix_map[choice])
            print(f"Added {suffix_map[choice]} to queue.")
            print(f"Current Queue: {', '.join(sorted(selected_targets))}")
        else:
            print("Invalid value. Try again.")

    # ── Firmware directory paths ─────────────────────────────────────────────
    plans = config.get("plans", {})
    active_plans = [
        name for name, data in plans.items()
        if any(t in selected_targets for t in data.get("targets", []))
    ]

    print("\n=============================================")
    print("        FIRMWARE DIRECTORY PATHS             ")
    print("=============================================")
    plan_dirs = {}
    for plan_name in active_plans:
        while True:
            dir_path = input(f"Enter the firmware folder path for plan [{plan_name}]: ").strip().strip('"')
            if os.path.isdir(dir_path):
                plan_dirs[plan_name] = dir_path
                break
            print(f"ERROR: Directory '{dir_path}' does not exist! Please try again.")

    # ── Pre-populate progress table ──────────────────────────────────────────
    with data_lock:
        for p_name, p_data in plans.items():
            if p_name in plan_dirs:
                for srv_name in p_data['targets']:
                    if srv_name in selected_targets:
                        progress_data[srv_name] = {f: "⚪ WAITING" for f in p_data['files']}

    # ── Start keyboard listener ──────────────────────────────────────────────
    threading.Thread(target=keyboard_listener, daemon=True).start()
    logger.info(f"Starting update for: {', '.join(sorted(selected_targets, key=lambda s: SUFFIX_ORDER.get(s,999)))}")
    logger.info("Press 'P' for status summary | Press 'Q' to quit.")

    # ── Parallel execution (one thread per server) ───────────────────────────
    with ThreadPoolExecutor(max_workers=len(selected_targets)) as executor:
        futures = []
        for p_name, p_data in plans.items():
            if p_name not in plan_dirs:
                continue
            base_dir = plan_dirs[p_name]
            for srv_name in p_data['targets']:
                if srv_name in selected_targets:
                    ip = server_ips.get(srv_name)
                    if ip:
                        futures.append(
                            executor.submit(
                                process_server,
                                srv_name, ip, user, pwd, p_data['files'], base_dir
                            )
                        )
        for fut in futures:
            try:
                fut.result()
            except Exception as e:
                logger.error(f"Thread error: {e}")

    global stop_listener
    stop_listener = True
    logger.info("All updates completed.")
    print_status_table()

if __name__ == "__main__":
    main()