"""
iDRAC Automated Firmware Update Utility
---------------------------------------
This script automates the sequential process of applying firmware updates 
(BIOS, iDRAC, Network, CPLD, etc.) to multiple Dell servers via RACADM.
It handles interactive target selection, prompts for dynamic directory paths, 
schedules updates, monitors the internal hardware job queue, and finally 
performs validation via pre/post software inventory comparisons.
"""

import os
import sys
import subprocess
import time
import logging
import re
import getpass
import json

# ---------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------
# Configure standard logging to output contextual information to the console 
# while simultaneously saving all logs persistently to 'fw_update.log'.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("fw_update.log")
    ]
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# Global Executable Constants
# ---------------------------------------------------------
# Define the strict path to the local Dell RACADM executable required to interact with iDRAC.
RACADM_PATH = r"C:\Program Files\Dell\SysMgt\iDRACTools\racadm\racadm.exe"

# Dynamically locate the config.json file relative to where this Python script currently resides.
# Dynamically locate the config.json file relative to where this Python script currently resides.
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# ---------------------------------------------------------
# Target Validation Definitions
# ---------------------------------------------------------

def extract_version_from_filename(filename):
    """
    Parses dynamic Dell Update Package (DUP) file names to extract version boundaries natively.
    E.g., 'BIOS_11N9M_WN64_1.7.5.EXE' -> '1.7.5'
    """
    match = re.search(r'_(?:WN64|LN)_([a-zA-Z0-9\.\-]+)', filename, re.IGNORECASE)
    if match:
        version_string = match.group(1)
        # Eliminate trailing suffix tags like '_A00' or '_A03'
        version_string = version_string.split('_')[0]
        return version_string
    return ""

def get_inventory_search_target(filename):
    """
    Maps dynamic base file names logically to their appropriate 'swinventory' search targets.
    """
    f = filename.upper()
    if "BIOS" in f: return "bios"
    if "CPLD" in f: return "cpld"
    if "DIAGNOSTICS" in f: return "diag"
    if "IDRAC" in f or "LIFECYCLE" in f: return "lifecycle"
    if "DRIVERS" in f: return "driver pack"
    if "SAS" in f or "PERC" in f: return "perc"
    if "BOSS" in f: return "boss"
    
    # Platform-specific unique hex identifiers mappings for components missing descriptive prefixes
    if "R4NT8" in f or "6Y9X7" in f: return "backplane"
    if "7PTPF" in f: return "broadcom"
    if "D81J3" in f: return "mellanox"
    if "5V215" in f: return "bcm5720"
    if "HVN2R" in f: return "adv"
    
    return None

def check_if_fw_needs_update(raw_name, inv_dict):
    """
    Looks up the firmware dependency dynamically.
    Instead of hardcoding, extracts versions from the filename directly and matches it.
    """
    search_str = get_inventory_search_target(raw_name)
    target_ver = extract_version_from_filename(raw_name)
    
    # If the system can't confidently parse it or map it, default to apply it safely.
    if not search_str or not target_ver:
        return True 
        
    search_str = search_str.lower()
    target_ver = target_ver.lower()
    
    for comp, ver in inv_dict.items():
        if search_str in comp.lower():
            actual_norm = ver.lower().replace(" ", "").replace("-", "")
            target_norm = target_ver.replace(" ", "").replace("-", "")
            if target_norm in actual_norm or actual_norm in target_norm:
                return False # Verified Match -> Skip Needed
    return True

def load_config():
    """
    Loads and parses the external configuration (config.json).
    The config dictates the Server arrays (Hosts and IPs) and categorizes updates into 'Plans'.
    Exits safely early on if the file is unavailable or corrupted.
    """
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
    """
    Attempts to locate the complete file path for a given firmware binary.
    If the provided filename does not carry an extension, it iteratively checks standard
    Dell extensions like .EXE, .exe, .BIN, .bin to ensure the file exists.
    Returns None if the file cannot be found physically on disk to avoid execution fatal crashes.
    """
    path = os.path.join(base_dir, raw_name)
    if os.path.exists(path):
        return path
    for ext in [".EXE", ".exe", ".BIN", ".bin"]:
        if os.path.exists(path + ext):
            return path + ext
    return None

def parse_swinventory(text):
    """
    Parses the raw text output from 'racadm swinventory' command.
    Generates a Python dictionary mapping the component's 'ElementName' strictly to its 'Version'.
    This dictionary is later used to verify if numeric versions changed after an attempted update.
    """
    inventory = {}
    current_element = None
    if not text:
        return inventory
        
    for line in text.splitlines():
        line = line.strip()
        # Look for the firmware component name identifier
        if "ElementName" in line:
            # Safely split by '=' or ':' depending on RACADM version formatting
            parts = line.split("=", 1) if "=" in line else line.split(":", 1)
            if len(parts) == 2:
                current_element = parts[1].strip()
        # Look for the firmware version metric linked to the previously found component name
        elif "Version" in line and current_element is not None:
            parts = line.split("=", 1) if "=" in line else line.split(":", 1)
            if len(parts) == 2:
                inventory[current_element] = parts[1].strip()
                current_element = None # Reset state element mapping memory block
    return inventory

def run_racadm(ip, username, password, args_list, timeout_sec=120):
    """
    A core security-focused function encapsulating all OS-level calls to the RACADM utility.
    Constructs an array argument list avoiding shell=True rendering command-injections impossible.
    Wraps execution around explicit timeout bounds.
    """
    # Force --nocertwarn to ensure SSL issues do not block automation sequences
    cmd = [RACADM_PATH, "-r", ip, "-u", username, "-p", password, "--nocertwarn"] + args_list
    try:
        # Launch the underlying process and capture all console standard outputs natively
        process = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        return process.returncode, process.stdout, process.stderr
    except subprocess.TimeoutExpired:
        logger.error(f"[{ip}] Command timed out after {timeout_sec} seconds")
        return -1, "", "TimeoutExpired"
    except Exception as e:
        logger.error(f"[{ip}] Execution error triggered: {e}")
        return -1, "", str(e)

def monitor_job(ip, username, password, jid):
    """
    Continually polls the iDRAC remote Job Queue checking the status of a scheduled Job ID (JID).
    Returns True if the Job completed successfully. Returns False on a failure.
    Has a circuit-breaker threshold of 60 minutes built-in to prevent infinite stalling blocks.
    """
    logger.info(f"[{ip}] Monitoring Job {jid} in Job Queue...")
    start_time = time.time()
    max_wait = 3600 # 60 Minutes max wait limit logic
    
    while time.time() - start_time < max_wait:
        # Check specific job id properties remotely
        code, out, _ = run_racadm(ip, username, password, ["jobqueue", "view", "-i", jid], timeout_sec=60)
        
        # Non-Zero error code implies remote iDRAC might be heavily loaded or rebooting. Stall execution.
        if code != 0:
            logger.warning(f"[{ip}] Failed to retrieve job status (possible iDRAC reboot). Retrying in 30s...")
            time.sleep(30)
            continue
            
        # Parse output for 'Status=' or 'Status:'
        status_match = re.search(r"Status\s*[=:]\s*([a-zA-Z\s]+)", out)
        if status_match:
            status = status_match.group(1).strip()
            
            # Successful target hits mapping
            if status.lower() in ["completed", "completed successfully"]:
                logger.info(f"[{ip}] Job {jid} finished: {status}")
                return True
            # Hardware/Platform or corruption failures mapping    
            elif status.lower() in ["failed", "completed with errors", "completed with error"]:
                logger.error(f"[{ip}] Job {jid} failed: {status}")
                return False
            else:
                # Any other queue status (E.g 'Scheduled', 'Downloading', 'Running') loops around 
                logger.info(f"[{ip}] Job {jid} status: {status}. Waiting...")
        else:
            logger.warning(f"[{ip}] Unrecognized job output block, continuing monitor loop...")
            
        time.sleep(60) # Wait 60 further seconds before hammering the iDRAC interface again
        
    logger.error(f"[{ip}] Monitoring for job {jid} timed out after {max_wait} seconds.")
    return False

def update_firmware(ip, username, password, filepath):
    """
    Instructs the iDRAC controller to upload the physical executable file and initiate an update string.
    Returns (True, None) if the machine detects the component is already on this exact version.
    Returns (True, JID) if a Job was successfully pushed to the queue schedule.
    Returns (False, None) upon fatal execution failure.
    """
    logger.info(f"[{ip}] Initiating update payload: {os.path.basename(filepath)}")
    # Firmware updates typically involve heavy payload transfer times. Assign a 30-min threshold (1800 sec).
    code, out, err = run_racadm(ip, username, password, ["update", "-f", filepath], timeout_sec=1800)
    
    # Analyze raw text response indicating component doesn't need upgrading
    if "already installed" in out.lower() or "rac1056" in out.lower():
        logger.info(f"[{ip}] Firmware already up-to-date")
        return True, None
        
    # Analyze raw text for scheduling success via tracking number
    jid_match = re.search(r"(JID_[A-Z0-9_]+)", out)
    if jid_match:
        jid = jid_match.group(1)
        logger.info(f"[{ip}] Job successfully scheduled: {jid}")
        return True, jid
        
    # Standard failover error logging behavior
    logger.error(f"[{ip}] Failed to schedule update. Code: {code}")
    logger.debug(f"[{ip}] Output: {out}\nError: {err}")
    return False, None

def process_server(server_name, ip, username, password, files_list, base_dir):
    """
    Main orchestration logic for iterating over a group's assigned firmware payload paths.
    Tracks update statuses on a per-file basis generating a cohesive summary return object.
    It encapsulates max retry limits (3 attempts) before totally aborting the server's chain to guarantee stability.
    """
    summary = {}
    
    # Pre-fetch inventory to silently prune out firmwares already verified mathematically 
    logger.info(f"[{server_name}] Fetching pre-execution SW Inventory to validate skip criteria...")
    _, out_initial, _ = run_racadm(ip, username, password, ["swinventory"])
    global_inv = parse_swinventory(out_initial)
    
    for raw_name in files_list:
        # Prevent NGINX Adv. Dual 25Gb Ethernet crossing into SRVMGT nodes per strict policy
        if server_name == "SRVMGT" and "HVN2R" in raw_name:
            logger.info(f"[{server_name}] Skipping {raw_name} as it acts exclusively for NGINX architectures.")
            summary[raw_name] = "Skipped (NGINX Only Policy)"
            continue
            
        fw_path = resolve_fw_path(base_dir, raw_name)
        if not fw_path:
            logger.error(f"[{server_name}] Firmware file {raw_name} missing from {base_dir}. Skipping sequence.")
            summary[raw_name] = "Failed (File Missing)"
            continue
            
        # Crosscheck target validation versions 
        needs_update = check_if_fw_needs_update(raw_name, global_inv)
        if not needs_update:
            logger.info(f"[{server_name}] {raw_name} meets target version profile inside memory bounds. Pruning payload run.")
            summary[raw_name] = "Already Up-to-date (Pre-flight check)"
            continue
            
        success = False
        attempts = 0
        max_attempts = 3
        
        while attempts < max_attempts and not success:
            attempts += 1
            logger.info(f"[{server_name}] Applying {raw_name} (Attempt {attempts}/{max_attempts})")
            
            _, out_before, _ = run_racadm(ip, username, password, ["swinventory"])
            inv_before = parse_swinventory(out_before)
            
            # Trigger OS execution upload string 
            ok, jid = update_firmware(ip, username, password, fw_path)
            
            # Scenario A: Firmware is natively identical to current payload 
            if ok and not jid:
                logger.info(f"[{server_name}] Firmware already up-to-date by iDRAC validation.")
                summary[raw_name] = "Already Up-to-date"
                success = True
                break
                
            # Scenario B: Fatal scheduling error occurred. Pause and retry payload.
            if not ok:
                logger.warning(f"[{server_name}] Update initiation failed. Retrying in 30 seconds.")
                time.sleep(30)
                continue
                
            # Scenario C: Payload upload executed. Now monitor progress inside the memory queue.
            if jid:
                job_success = monitor_job(ip, username, password, jid)
                # If Job ended in failure bounds, pause and retry pushing the payload from zero.
                if not job_success:
                    logger.warning(f"[{server_name}] Update job did not complete successfully.")
                    time.sleep(30)
                    continue
            
            # Wait allowing iDRAC database architecture to synchronize internal numeric parameters safely
            logger.info(f"[{server_name}] Validating inventory reflections...")
            time.sleep(15)
            
            # Snapshoot post-update inventory profile
            _, out_after, _ = run_racadm(ip, username, password, ["swinventory"])
            inv_after = parse_swinventory(out_after)
            
            # Compute differential changes verifying success via live attributes updates
            changed = False
            for comp, ver in inv_after.items():
                if comp not in inv_before or inv_before[comp] != ver:
                    logger.info(f"[{server_name}] Inventory incremented: {comp} -> {ver}")
                    changed = True
                    summary[raw_name] = f"Updated (to {ver})"
                    
            # Fallback evaluation mapping context if queue logic returned pure success but inventory string mapping stayed stale. 
            if not changed:
                logger.info(f"[{server_name}] Inventory unaffected (no numeric version increment detected). Validation assumed success based on execution context.")
                summary[raw_name] = "Updated (No Inv Change Detected)"
                
            success = True
            
            # Cascade verified state onwards so downstream components rely on latest validation details 
            global_inv = inv_after
            logger.info(f"[{server_name}] Firmware validation complete: {raw_name}")
            
        # Hard Stop Failure sequence execution. We don't want corrupt downstream payload dependencies.
        if not success:
            logger.error(f"[{server_name}] Hard failure on {raw_name} after {max_attempts} attempts. Aborting chain.")
            summary[raw_name] = "Failed (Max Retries)"
            
            # Cascade "Skipped" annotation downstream identifying untouched payload elements via mapping index
            idx = files_list.index(raw_name) + 1
            for skip_name in files_list[idx:]:
                summary[skip_name] = "Skipped (Due to previous failure)"
            break
            
    logger.info(f"[{server_name}] Firmware pipeline concluded.")
    return summary

def main():
    """
    Main entry point configuring the interactive CLI context, authenticating variables securely,
    grouping inputs, defining interactive folder paths on the fly, and finally iterating targets reliably 
    until aggregate summaries are dispatched.
    """
    
    # ---------------------------------------------------------
    # System Execution Verification Step
    # ---------------------------------------------------------
    # Guarantees the binary execution component exists before prompting user or allocating network blocks
    if not os.path.exists(RACADM_PATH):
        logger.error(f"CRITICAL: Dependent executable racadm.exe missing at {RACADM_PATH}")
        sys.exit(1)
        
    # ---------------------------------------------------------
    # Interactive Queue/Queue Builder CLI Implementation
    # ---------------------------------------------------------
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
    
    # Suffix translation dictionary correlating to config.json specific array names 
    suffix_map = {
        "122": "FM1",
        "123": "FM2",
        "124": "PMC1",
        "125": "PMC2",
        "126": "PMC3",
        "127": "SRVMGT",
        "128": "NGINX"
    }

    selected_targets = set()
    
    # Infinite Input Loop allowing robust multi-dimensional queue generations
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
            # Halt sequence dynamically if user requested sequence without targets 
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
                    print(f"{srv} is not in the queue.")
            else:
                print(f"Invalid suffix '{rm_suffix}'. Please try again.")
            print(f"Current Queue: {', '.join(sorted(selected_targets))}" if selected_targets else "Current Queue: [Empty]")
        elif choice in suffix_map:
            # Map choice identifier back into queue framework array securely
            selected_targets.add(suffix_map[choice])
            print(f"Added {suffix_map[choice]} to queue.")
            print(f"Current Queue: {', '.join(sorted(selected_targets))}")
        else:
            print(f"Invalid value '{choice}'. Please enter a valid suffix (e.g., 122), 'rm <suffix>', '1', '2', or 'done'.")
        
    # ---------------------------------------------------------
    # Secure Architecture Authentication Properties
    # ---------------------------------------------------------
    # Attempts extraction safely from current OS Environment Variables strictly bypassing string integrations handling. 
    # Otherwise triggers native masked user I/O Prompts masking password layouts safely.
    username = "root"
    password = "admin1234"

    # Standard JSON loading functionality 
    config = load_config()
    plans = config.get("plans", {})
    servers = config.get("servers", {})
    
    # ---------------------------------------------------------
    # Dynamic Resource Allocation Generation Matrix
    # ---------------------------------------------------------
    active_plans = []
    
    # Match the targets inputted by human against available static Plan mapping definitions arrays
    for plan_name, plan_data in plans.items():
        targets = plan_data.get("targets", [])
        if any(t in selected_targets for t in targets):
            active_plans.append(plan_name)
            
    # Display specialized target requirements prompting the local Admin dynamically
    print("\n=============================================")
    print("        FIRMWARE DIRECTORY PATHS             ")
    print("=============================================")
    plan_dirs = {}
    for plan_name in active_plans:
        dir_path = input(f"Enter the base firmware folder path for {plan_name}: ").strip()
        # Validation blocking mechanism guarding file transfer logic structures securely
        while not os.path.exists(dir_path) or not os.path.isdir(dir_path):
            print(f"ERROR: Directory '{dir_path}' does not exist!")
            dir_path = input(f"Please re-enter the valid folder path for {plan_name}: ").strip()
        plan_dirs[plan_name] = dir_path
        
    global_summary = {}
    
    # ---------------------------------------------------------
    # Deep Root Target Pipeline Iterator Matrix
    # ---------------------------------------------------------
    # Re-iterate configurations targeting solely user defined structures elements 
    for plan_name, plan_data in plans.items():
        targets = plan_data["targets"]
        
        # Intercept and validate whether the group target possesses any valid matching nodes within loop block 
        run_targets = [t for t in targets if t in selected_targets]
        if not run_targets:
            continue
            
        base_dir = plan_dirs[plan_name]
        files = plan_data["files"]
        
        logger.info(f"=== Beginning Plan: {plan_name} ===")
        # Launch sequential payload matrix dynamically mapped inside `process_server` function natively 
        for srv in run_targets:
            ip = servers.get(srv)
            if not ip:
                logger.warning(f"Server {srv} has no IP tracked in config map. Skipping.")
                continue
                
            logger.info(f"Connecting to {srv} at {ip}")
            # Dispatch specific node configuration details downwards, caching its comprehensive logical summary returns
            srv_summary = process_server(srv, ip, username, password, files, base_dir)
            global_summary[srv] = srv_summary

    # ---------------------------------------------------------
    # Aggregated Log Generation
    # ---------------------------------------------------------
    # Display clear precise human-readable metrics upon sequential completion metrics execution 
    print("\n=============================================")
    print("               EXECUTION SUMMARY             ")
    print("=============================================")
    for srv, srv_summary in global_summary.items():
        print(f"\nServer: {srv}")
        for fw, stat in srv_summary.items():
            print(f"  - {fw}: {stat}")
    print("=============================================\n")

if __name__ == "__main__":
    main()
