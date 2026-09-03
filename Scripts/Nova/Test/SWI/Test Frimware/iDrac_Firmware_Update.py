import os
import sys
import subprocess
import time
import logging
import re
import getpass
import json
import threading
import msvcrt
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------
# Remove FileHandler to avoid saving output to file, as requested.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# Global Executable Constants
# ---------------------------------------------------------
RACADM_PATH = r"C:\Program Files\Dell\SysMgt\iDRACTools\racadm\racadm.exe"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# ---------------------------------------------------------
# Shared State for Progress Tracking
# ---------------------------------------------------------
status_lock = threading.Lock()
server_fw_status = {}
stop_input_thread = False

def update_status(server_name, fw_name, status):
    with status_lock:
        if server_name not in server_fw_status:
            server_fw_status[server_name] = {}
        server_fw_status[server_name][fw_name] = status

def print_status_table():
    with status_lock:
        print("\n" + "="*80)
        print(f"{'SERVER / FIRMWARE':<50} | {'STATUS'}")
        print("="*80)
        for srv, fws in server_fw_status.items():
            print(f"[{srv}]")
            for fw, stat in fws.items():
                print(f"  {fw:<48} | {stat}")
        print("="*80 + "\n")

def input_listener():
    """Listens for 'P' keypress to print progress table."""
    while not stop_input_thread:
        if msvcrt.kbhit():
            try:
                key = msvcrt.getch().decode('utf-8', errors='ignore').lower()
                if key == 'p':
                    print_status_table()
            except Exception:
                pass
        time.sleep(0.1)

# ---------------------------------------------------------
# Target Validation Definitions
# ---------------------------------------------------------

def extract_version_from_filename(filename):
    match = re.search(r'_(?:WN64|LN)_([a-zA-Z0-9\.\-]+)', filename, re.IGNORECASE)
    if match:
        version_string = match.group(1)
        version_string = version_string.split('_')[0]
        return version_string
    return ""

def get_inventory_search_target(filename):
    f = filename.upper()
    if "BIOS" in f: return "bios"
    if "CPLD" in f: return "cpld"
    if "DIAGNOSTICS" in f: return "diag"
    if "IDRAC" in f or "LIFECYCLE" in f: return "lifecycle"
    if "DRIVERS" in f: return "driver pack"
    if "SAS" in f or "PERC" in f: return "perc"
    if "BOSS" in f: return "boss"
    if "R4NT8" in f or "6Y9X7" in f: return "backplane"
    if "7PTPF" in f: return "broadcom"
    if "D81J3" in f: return "mellanox"
    if "5V215" in f: return "bcm5720"
    if "HVN2R" in f: return "adv"
    return None

def check_if_fw_needs_update(raw_name, inv_dict):
    search_str = get_inventory_search_target(raw_name)
    target_ver = extract_version_from_filename(raw_name)
    if not search_str or not target_ver:
        return True 
        
    search_str = search_str.lower()
    target_ver = target_ver.lower()
    
    for comp, ver in inv_dict.items():
        if search_str in comp.lower():
            actual_norm = ver.lower().replace(" ", "").replace("-", "")
            target_norm = target_ver.replace(" ", "").replace("-", "")
            if target_norm in actual_norm or actual_norm in target_norm:
                return False 
    return True

def load_config():
    if not os.path.exists(CONFIG_PATH):
        logger.error(f"Configuration file missing: {CONFIG_PATH}")
        sys.exit(1)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to parse config.json error: {e}")
        sys.exit(1)

def resolve_fw_path(base_dir, raw_name):
    path = os.path.join(base_dir, raw_name)
    if os.path.exists(path):
        return path
    for ext in [".EXE", ".exe", ".BIN", ".bin"]:
        if os.path.exists(path + ext):
            return path + ext
    return None

def parse_swinventory(text):
    inventory = {}
    current_element = None
    if not text:
        return inventory
        
    for line in text.splitlines():
        line = line.strip()
        if "ElementName" in line:
            parts = line.split("=", 1) if "=" in line else line.split(":", 1)
            if len(parts) == 2:
                current_element = parts[1].strip()
        elif "Version" in line and current_element is not None:
            parts = line.split("=", 1) if "=" in line else line.split(":", 1)
            if len(parts) == 2:
                inventory[current_element] = parts[1].strip()
                current_element = None
    return inventory

def run_racadm(ip, username, password, args_list, timeout_sec=120):
    cmd = [RACADM_PATH, "-r", ip, "-u", username, "-p", password, "--nocertwarn"] + args_list
    try:
        process = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, creationflags=subprocess.CREATE_NO_WINDOW)
        return process.returncode, process.stdout, process.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "TimeoutExpired"
    except Exception as e:
        return -1, "", str(e)

def monitor_job(server_name, fw_name, ip, username, password, jid):
    start_time = time.time()
    max_wait = 3600
    
    while time.time() - start_time < max_wait:
        code, out, _ = run_racadm(ip, username, password, ["jobqueue", "view", "-i", jid], timeout_sec=60)
        
        if code != 0:
            time.sleep(30)
            continue
            
        status_match = re.search(r"Status\s*[=:]\s*([a-zA-Z\s]+)", out)
        percent_match = re.search(r"Percent Complete\s*[=:]\s*\[?(\d+)\]?", out)
        
        status_str = "Running"
        if status_match:
            status_str = status_match.group(1).strip()
        if percent_match:
            status_str += f" - {percent_match.group(1)}%"
            
        update_status(server_name, fw_name, status_str)
            
        if status_match:
            status = status_match.group(1).strip()
            if status.lower() in ["completed", "completed successfully"]:
                update_status(server_name, fw_name, "Completed")
                return True
            elif status.lower() in ["failed", "completed with errors", "completed with error"]:
                update_status(server_name, fw_name, "Failed")
                return False
                
        time.sleep(60)
        
    update_status(server_name, fw_name, "Failed (Timeout)")
    return False

def update_firmware(ip, username, password, filepath):
    code, out, err = run_racadm(ip, username, password, ["update", "-f", filepath], timeout_sec=1800)
    
    if "already installed" in out.lower() or "rac1056" in out.lower():
        return True, None
        
    jid_match = re.search(r"(JID_[A-Z0-9_]+)", out)
    if jid_match:
        jid = jid_match.group(1)
        return True, jid
        
    return False, None

def process_single_firmware(server_name, ip, username, password, raw_name, base_dir):
    """Processes a single firmware on a given server."""
    if server_name == "SRVMGT" and "HVN2R" in raw_name:
        update_status(server_name, raw_name, "Skipped (NGINX Only)")
        return True
        
    fw_path = resolve_fw_path(base_dir, raw_name)
    if not fw_path:
        update_status(server_name, raw_name, "Failed (File Missing)")
        return False
        
    update_status(server_name, raw_name, "Fetching Inventory")
    _, out_initial, _ = run_racadm(ip, username, password, ["swinventory"])
    global_inv = parse_swinventory(out_initial)
    
    needs_update = check_if_fw_needs_update(raw_name, global_inv)
    if not needs_update:
        update_status(server_name, raw_name, "Already Up-to-date")
        return True
        
    success = False
    attempts = 0
    max_attempts = 3
    
    while attempts < max_attempts and not success:
        attempts += 1
        update_status(server_name, raw_name, f"Applying (Attempt {attempts})")
        
        ok, jid = update_firmware(ip, username, password, fw_path)
        
        if ok and not jid:
            update_status(server_name, raw_name, "Already Up-to-date")
            success = True
            break
            
        if not ok:
            update_status(server_name, raw_name, "Update Push Failed, Retrying")
            time.sleep(30)
            continue
            
        if jid:
            update_status(server_name, raw_name, "Monitoring Job")
            job_success = monitor_job(server_name, raw_name, ip, username, password, jid)
            if not job_success:
                update_status(server_name, raw_name, "Job Failed, Retrying")
                time.sleep(30)
                continue
                
        update_status(server_name, raw_name, "Validating Post-Install")
        time.sleep(15)
        _, out_after, _ = run_racadm(ip, username, password, ["swinventory"])
        inv_after = parse_swinventory(out_after)
        
        changed = False
        for comp, ver in inv_after.items():
            if comp not in global_inv or global_inv[comp] != ver:
                update_status(server_name, raw_name, f"Completed (to {ver})")
                changed = True
                
        if not changed:
            update_status(server_name, raw_name, "Completed (No Inv Change)")
            
        success = True
        
    if not success:
        update_status(server_name, raw_name, "Failed (Max Retries)")
        return False
        
    return True

def main():
    global stop_input_thread
    
    if not os.path.exists(RACADM_PATH):
        logger.error(f"CRITICAL: Dependent executable racadm.exe missing at {RACADM_PATH}")
        sys.exit(1)
        
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
        "122": "FM1", "123": "FM2", "124": "PMC1",
        "125": "PMC2", "126": "PMC3", "127": "SRVMGT", "128": "NGINX"
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
            rm_suffix = choice.split(" ", 1)[1].strip()
            if rm_suffix in suffix_map:
                srv = suffix_map[rm_suffix]
                if srv in selected_targets:
                    selected_targets.remove(srv)
                    print(f"Removed {srv} from queue.")
            else:
                print(f"Invalid suffix '{rm_suffix}'.")
            print(f"Current Queue: {', '.join(sorted(selected_targets))}" if selected_targets else "Current Queue: [Empty]")
        elif choice in suffix_map:
            selected_targets.add(suffix_map[choice])
            print(f"Added {suffix_map[choice]} to queue.")
            print(f"Current Queue: {', '.join(sorted(selected_targets))}")
        else:
            print(f"Invalid value. Try again.")
        
    username = "root"
    password = "admin1234"

    config = load_config()
    plans = config.get("plans", {})
    servers = config.get("servers", {})
    
    active_plans = []
    for plan_name, plan_data in plans.items():
        targets = plan_data.get("targets", [])
        if any(t in selected_targets for t in targets):
            active_plans.append(plan_name)
            
    print("\n=============================================")
    print("        FIRMWARE DIRECTORY PATHS             ")
    print("=============================================")
    plan_dirs = {}
    for plan_name in active_plans:
        dir_path = input(f"Enter the base firmware folder path for {plan_name}: ").strip()
        while not os.path.exists(dir_path) or not os.path.isdir(dir_path):
            print(f"ERROR: Directory '{dir_path}' does not exist!")
            dir_path = input(f"Please re-enter the valid folder path for {plan_name}: ").strip()
        plan_dirs[plan_name] = dir_path
        
    # Initialize shared status dictionary
    for plan_name, plan_data in plans.items():
        run_targets = [t for t in plan_data["targets"] if t in selected_targets]
        for srv in run_targets:
            update_status(srv, "Initialization", "Pending")
            for f in plan_data["files"]:
                update_status(srv, f, "Pending")

    print("\n[INFO] Press 'P' at any time to view progress table.")
    
    # Start background thread to listen for 'P'
    listener_thread = threading.Thread(target=input_listener, daemon=True)
    listener_thread.start()
    
    # Determine maximum number of steps
    max_steps = 0
    for plan_name in active_plans:
        max_steps = max(max_steps, len(plans[plan_name]["files"]))
        
    # Execute in parallel by step index
    for step in range(max_steps):
        futures = {}
        with ThreadPoolExecutor(max_workers=len(selected_targets)) as executor:
            for plan_name in active_plans:
                plan_data = plans[plan_name]
                run_targets = [t for t in plan_data["targets"] if t in selected_targets]
                if not run_targets: continue
                
                files = plan_data["files"]
                if step < len(files):
                    fw_name = files[step]
                    base_dir = plan_dirs[plan_name]
                    for srv in run_targets:
                        ip = servers.get(srv)
                        if ip:
                            futures[executor.submit(process_single_firmware, srv, ip, username, password, fw_name, base_dir)] = (srv, fw_name)
                            
            # Wait for all servers to finish the current step
            for f in as_completed(futures):
                srv, fw_name = futures[f]
                try:
                    success = f.result()
                    if not success:
                        # If a firmware fails for a server, we could mark remaining as skipped
                        # but we'll let it try next firmwares unless specified otherwise.
                        pass
                except Exception as e:
                    logger.error(f"[{srv}] Error on {fw_name}: {e}")
                    update_status(srv, fw_name, "Failed (Exception)")
                    
    # Stop listener thread
    stop_input_thread = True
    
    # Print final summary table
    print("\n=============================================")
    print("               FINAL EXECUTION SUMMARY       ")
    print("=============================================")
    print_status_table()

if __name__ == "__main__":
    main()
