import time
import traceback
import urllib3
import requests
import json
import re
from pathlib import Path

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =========================================================
# CONFIGURATION
# =========================================================
IDRAC_USERNAME = "root"
IDRAC_PASSWORD = "admin1234"

BASE_DIR = Path.home() / "Downloads"

PROJECT_FOLDERS = {
    "1": "R470-FMs",
    "2": "R470-PMCs",
    "3": "R260-MGMT & NGINX",
}

REQUEST_TIMEOUT = 120
POLL_INTERVAL = 15
TASK_TIMEOUT = 7200
IDRAC_RESTART_WAIT = 240
HOST_REBOOT_WAIT = 300
POST_INSTALL_SETTLE_WAIT = 60

# Set to False if you do not want optional packages
INCLUDE_OPTIONAL_PACKAGES = True

# =========================================================
# TARGET VERSIONS PER PROJECT
# =========================================================
TARGETS = {
    "1": {  # FM
        "project_name": "R470 (FMs)",
        "components": [
            {
                "label": "iDRAC & Lifecycle Controller",
                "match_keywords": ["idrac", "lifecycle"],
                "target_version": "1.20.60.50",
                "file_keywords": ["idrac-with-lifecycle-controller"],
                "reboot": True,
                "type": "idrac"
            },
            {
                "label": "BIOS",
                "match_keywords": ["bios"],
                "target_version": "1.7.5",
                "file_keywords": ["bios"],
                "reboot": True,
                "type": "host"
            },
            {
                "label": "PERC H365i Front",
                "match_keywords": ["perc", "h365", "sas-raid", "raid"],
                "target_version": "8.11.2.0.15-26",
                "file_keywords": ["sas-raid", "perc"],
                "reboot": True,
                "type": "host"
            },
            {
                "label": "Flop CPLD 1",
                "match_keywords": ["cpld"],
                "target_version": "1.0.4",
                "file_keywords": ["cpld"],
                "reboot": True,
                "type": "host"
            },
            {
                "label": "Broadcom NetXtreme Gigabit Ethernet",
                "match_keywords": ["broadcom", "gigabit", "netxtreme"],
                "target_version": "233.1.181.0",
                "file_keywords": ["network_firmware_7ptpf", "233.1.181.0"],
                "reboot": True,
                "type": "host"
            },
            {
                "label": "Mellanox Network Adapter",
                "match_keywords": ["mellanox", "network adapter", "25gb"],
                "target_version": "26.46.3048",
                "file_keywords": ["network_firmware_d81j3", "26.46.3048"],
                "reboot": True,
                "type": "host"
            },
            {
                "label": "Backplane1",
                "match_keywords": ["backplane", "firmware"],
                "target_version": "1.77",
                "file_keywords": ["firmware_r4nt8", "1.77"],
                "reboot": True,
                "type": "host"
            },
            {
                "label": "Dell 64 Bit uEFI Diagnostics",
                "match_keywords": ["diagnostics", "uefi diagnostics"],
                "target_version": "4303A47",
                "file_keywords": ["diagnostics"],
                "reboot": False,
                "type": "host"
            },
            {
                "label": "Dell OS Driver Pack",
                "match_keywords": ["os driver pack", "driver pack", "drivers-for-os-deployment"],
                "target_version": "25.07.05",
                "file_keywords": ["drivers-for-os-deployment"],
                "reboot": False,
                "type": "host"
            },
        ]
    },
    "2": {  # PMC
        "project_name": "R470 (PMCs)",
        "components": [
            {
                "label": "iDRAC & Lifecycle Controller",
                "match_keywords": ["idrac", "lifecycle"],
                "target_version": "1.20.60.50",
                "file_keywords": ["idrac-with-lifecycle-controller"],
                "reboot": True,
                "type": "idrac"
            },
            {
                "label": "BIOS",
                "match_keywords": ["bios"],
                "target_version": "1.7.5",
                "file_keywords": ["bios"],
                "reboot": True,
                "type": "host"
            },
            {
                "label": "Mellanox Network Adapter",
                "match_keywords": ["mellanox", "network adapter", "25gb"],
                "target_version": "26.46.3048",
                "file_keywords": ["network_firmware", "26.46.3048", "d81j3"],
                "reboot": True,
                "type": "host"
            },
            {
                "label": "Dell 64 Bit uEFI Diagnostics",
                "match_keywords": ["diagnostics", "uefi diagnostics"],
                "target_version": "4303A47",
                "file_keywords": ["diagnostics"],
                "reboot": False,
                "type": "host"
            },
            {
                "label": "Dell OS Driver Pack",
                "match_keywords": ["os driver pack", "driver pack", "drivers-for-os-deployment"],
                "target_version": "25.07.05",
                "file_keywords": ["drivers-for-os-deployment"],
                "reboot": False,
                "type": "host"
            },
            {
                "label": "Backplane1",
                "match_keywords": ["backplane", "firmware"],
                "target_version": "1.77",
                "file_keywords": ["firmware", "1.77"],
                "reboot": True,
                "type": "host"
            },
            {
                "label": "BOSS-N1 DC-MHS",
                "match_keywords": ["boss", "boss-n1", "dc-mhs"],
                "target_version": "2.2.13.2034",
                "file_keywords": ["boss", "2.2.13.2034"],
                "reboot": True,
                "type": "host"
            },
        ]
    },
    "3": {  # SRVMGT & NGINX
        "project_name": "R260 (SRVMGT & NGINX)",
        "components_common": [
            {
                "label": "iDRAC & Lifecycle Controller",
                "match_keywords": ["idrac", "lifecycle"],
                "target_version": "7.20.60.50",
                "file_keywords": ["idrac-with-lifecycle-controller"],
                "reboot": True,
                "type": "idrac"
            },
            {
                "label": "BIOS",
                "match_keywords": ["bios"],
                "target_version": "2.5.2",
                "file_keywords": ["bios"],
                "reboot": True,
                "type": "host"
            },
            {
                "label": "System CPLD",
                "match_keywords": ["cpld"],
                "target_version": "1.4.0",
                "file_keywords": ["cpld"],
                "reboot": True,
                "type": "host"
            },
            {
                "label": "Dell 64 Bit uEFI Diagnostics",
                "match_keywords": ["diagnostics", "uefi diagnostics"],
                "target_version": "4303A46",
                "file_keywords": ["diagnostics"],
                "reboot": False,
                "type": "host"
            },
            {
                "label": "Dell OS Driver Pack",
                "match_keywords": ["os driver pack", "driver pack", "drivers-for-os-deployment"],
                "target_version": "25.07.04",
                "file_keywords": ["drivers-for-os-deployment"],
                "reboot": False,
                "type": "host"
            },
            {
                "label": "Backplane 1",
                "match_keywords": ["backplane"],
                "target_version": "7.10",
                "file_keywords": ["backplane", "firmware"],
                "reboot": True,
                "type": "host"
            },
            {
                "label": "Broadcom NetXtreme Gigabit Ethernet (BCM5720)",
                "match_keywords": ["bcm5720", "broadcom", "gigabit", "netxtreme"],
                "target_version": "23.31.1",
                "file_keywords": ["network_firmware", "23.31.1", "broadcom"],
                "reboot": True,
                "type": "host"
            },
        ],
        "nginx_only": [
            {
                "label": "Broadcom Adv. Dual 25Gb Ethernet",
                "match_keywords": ["25gb", "dual 25gb", "broadcom adv"],
                "target_version": "23.31.18.10",
                "file_keywords": ["network_firmware", "25gb", "23.31.18.10", "broadcom"],
                "reboot": True,
                "type": "host"
            }
        ]
    }
}


# =========================================================
# HELPERS
# =========================================================
def normalize_text(value: str) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9\.&\- ]+", " ", str(value).lower()).strip()


def normalize_version(value: str) -> str:
    if value is None:
        return ""
    return str(value).strip().lower().replace("installed from the os", "").strip()


def version_matches(current: str, target: str) -> bool:
    current_n = normalize_version(current)
    target_n = normalize_version(target)
    if not current_n or not target_n:
        return False
    return target_n in current_n or current_n == target_n


def should_include_file(filename: str) -> bool:
    if not filename.lower().endswith(".exe"):
        return False

    if not INCLUDE_OPTIONAL_PACKAGES:
        lower_name = filename.lower()
        if "diagnostics" in lower_name or "drivers-for-os-deployment" in lower_name:
            return False

    return True


def choose_project_folder():
    print("\nSelect project:")
    print("1. FM")
    print("2. PMC")
    print("3. MGMT&NGINX")

    while True:
        choice = input("Enter project number: ").strip()
        if choice in PROJECT_FOLDERS:
            folder = BASE_DIR / PROJECT_FOLDERS[choice]
            if folder.exists() and folder.is_dir():
                return choice, folder
            print(f"Folder does not exist: {folder}")
        else:
            print("Invalid selection. Please choose 1, 2, or 3.")


def choose_srvmgt_profile():
    print("\nFor project 3, choose server profile:")
    print("1. SRVMGT")
    print("2. NGINX")
    print("3. AUTO")

    while True:
        choice = input("Enter profile number: ").strip()
        if choice == "1":
            return "SRVMGT"
        if choice == "2":
            return "NGINX"
        if choice == "3":
            return "AUTO"
        print("Invalid selection. Please choose 1, 2, or 3.")


def collect_idrac_ips():
    print("\nEnter iDRAC IP addresses, one per line.")
    print("Type 'done' when finished.\n")

    ips = []
    counter = 1

    while True:
        value = input(f"Enter iDRAC IP #{counter}: ").strip()
        if not value:
            continue

        if value.lower() == "done":
            break

        ips.append(value)
        counter += 1

    if not ips:
        raise ValueError("No iDRAC IP addresses were entered.")

    return ips


def resolve_project_components(project_choice: str, profile_choice: str):
    if project_choice in ["1", "2"]:
        return TARGETS[project_choice]["components"]

    if project_choice == "3":
        components = list(TARGETS["3"]["components_common"])
        if profile_choice == "NGINX":
            components.extend(TARGETS["3"]["nginx_only"])
        elif profile_choice == "AUTO":
            # In AUTO, include NGINX-only item too. If not found in inventory, it will just skip.
            components.extend(TARGETS["3"]["nginx_only"])
        return components

    return []


def find_file_for_component(files, file_keywords):
    for file_path in files:
        name = normalize_text(file_path.name)
        if all(keyword.lower() in name for keyword in [k.lower() for k in file_keywords if len(k) > 2]):
            return file_path

    for file_path in files:
        name = normalize_text(file_path.name)
        for keyword in file_keywords:
            if keyword.lower() in name:
                return file_path

    return None


# =========================================================
# IDRAC CLIENT
# =========================================================
class IdracClient:
    def __init__(self, host: str, username: str, password: str):
        self.host = host
        self.base_url = f"https://{host}"
        self.session = requests.Session()
        self.session.verify = False
        self.session.auth = (username, password)
        self.session.headers.update({"Accept": "application/json"})

    def build_url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def get(self, path: str, **kwargs):
        return self.session.get(self.build_url(path), timeout=REQUEST_TIMEOUT, **kwargs)

    def post(self, path: str, **kwargs):
        return self.session.post(self.build_url(path), timeout=REQUEST_TIMEOUT, **kwargs)

    def wait_for_idrac(self, timeout=1800):
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                response = self.get("/redfish/v1/")
                if response.ok:
                    return True
            except requests.RequestException:
                pass
            time.sleep(10)
        return False

    def get_update_service(self):
        response = self.get("/redfish/v1/UpdateService")
        response.raise_for_status()
        return response.json()

    def get_multipart_push_uri(self):
        data = self.get_update_service()
        return data.get("MultipartHttpPushUri")

    def upload_firmware_file(self, file_path: Path):
        multipart_uri = self.get_multipart_push_uri()
        if not multipart_uri:
            raise RuntimeError(f"{self.host}: MultipartHttpPushUri is not available on this iDRAC.")

        update_parameters = {
            "@Redfish.OperationApplyTime": "OnReset"
        }

        with open(file_path, "rb") as file_handle:
            files = {
                "UpdateParameters": (
                    None,
                    json.dumps(update_parameters),
                    "application/json"
                ),
                "UpdateFile": (
                    file_path.name,
                    file_handle,
                    "application/octet-stream"
                )
            }

            response = self.post(multipart_uri, files=files)

        if not response.ok:
            raise RuntimeError(
                f"Upload failed for {file_path.name}: HTTP {response.status_code} - {response.text[:1000]}"
            )

        return response

    def reboot_host(self):
        payload = {"ResetType": "GracefulRestart"}
        response = self.post(
            "/redfish/v1/Systems/System.Embedded.1/Actions/ComputerSystem.Reset",
            json=payload
        )

        if not response.ok:
            raise RuntimeError(
                f"Host reboot failed: HTTP {response.status_code} - {response.text[:500]}"
            )

        return response

    def extract_task_uri(self, response):
        location = response.headers.get("Location")
        if location:
            if location.startswith("http"):
                return location.replace(self.base_url, "")
            return location

        try:
            data = response.json()
        except Exception:
            return None

        for key in ("@odata.id", "TaskMonitor", "Task"):
            value = data.get(key)
            if isinstance(value, str) and value.startswith("/"):
                return value

        return None

    def poll_task(self, task_uri, is_idrac_package=False, timeout=TASK_TIMEOUT):
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                response = self.get(task_uri)
            except requests.RequestException:
                print(f"    [{self.host}] Connection lost while polling task.")

                if is_idrac_package:
                    print(f"    [{self.host}] iDRAC firmware update likely restarted the controller.")
                    print(f"    [{self.host}] Waiting for iDRAC to come back...")

                    if not self.wait_for_idrac(timeout=1800):
                        raise RuntimeError(f"{self.host}: iDRAC did not come back while polling task.")

                    print(f"    [{self.host}] iDRAC is reachable again.")
                    print(f"    [{self.host}] Assuming iDRAC update task completed after controller restart.")
                    time.sleep(60)
                    return {"TaskState": "CompletedAfterReconnect"}

                print(f"    [{self.host}] Waiting for iDRAC to come back...")
                if not self.wait_for_idrac(timeout=1800):
                    raise RuntimeError(f"{self.host}: iDRAC did not come back while polling task.")
                time.sleep(20)
                continue

            if not response.ok:
                time.sleep(POLL_INTERVAL)
                continue

            try:
                data = response.json()
            except Exception:
                data = {}

            task_state = str(data.get("TaskState", "")).lower()
            job_state = str(data.get("JobState", "")).lower()
            task_status = str(data.get("TaskStatus", "")).lower()
            percent_complete = data.get("PercentComplete", "N/A")

            print(
                f"    Task status on {self.host}: "
                f"TaskState={task_state or 'N/A'}, "
                f"JobState={job_state or 'N/A'}, "
                f"TaskStatus={task_status or 'N/A'}, "
                f"Percent={percent_complete}"
            )

            if task_state in ["completed", "completedwitherrors"]:
                return data

            if job_state in ["completed", "completedwitherrors"]:
                return data

            if task_state in ["exception", "killed", "cancelled", "interrupted"]:
                raise RuntimeError(f"Task failed: {data}")

            if job_state in ["failed", "exception", "cancelled"]:
                raise RuntimeError(f"Job failed: {data}")

            time.sleep(POLL_INTERVAL)

        raise TimeoutError(f"Task polling timed out on {self.host}.")


    def get_firmware_inventory(self):
        response = self.get("/redfish/v1/UpdateService/FirmwareInventory?$expand=*($levels=1)")
        response.raise_for_status()
        data = response.json()

        items = []
        members = data.get("Members", [])

        for member in members:
            entry = {
                "name": str(member.get("Name", "")),
                "id": str(member.get("Id", "")),
                "version": str(member.get("Version", "")),
                "software_id": str(member.get("SoftwareId", "")),
                "description": str(member.get("Description", "")),
            }
            items.append(entry)

        return items

    def find_component_version(self, component_def):
        inventory = self.get_firmware_inventory()

        best_match = None
        best_score = -1

        for item in inventory:
            searchable = " | ".join([
                item.get("name", ""),
                item.get("id", ""),
                item.get("description", ""),
                item.get("software_id", "")
            ])
            searchable_norm = normalize_text(searchable)

            score = 0
            for kw in component_def["match_keywords"]:
                if normalize_text(kw) in searchable_norm:
                    score += 1

            if score > best_score:
                best_score = score
                best_match = item

        if best_match and best_score > 0:
            return best_match["version"], best_match

        return None, None


# =========================================================
# INSTALL ORDER
# =========================================================
def build_execution_plan(project_choice, files, profile_choice):
    components = resolve_project_components(project_choice, profile_choice)
    plan = []

    for component in components:
        file_path = find_file_for_component(files, component["file_keywords"])
        plan.append({
            "component": component,
            "file_path": file_path
        })

    return plan


# =========================================================
# PER SERVER PROCESSING
# =========================================================
def process_single_server(ip_address: str, execution_plan, project_choice, profile_choice):
    print("\n" + "=" * 90)
    print(f"STARTING SERVER: {ip_address}")
    print("=" * 90)

    client = IdracClient(ip_address, IDRAC_USERNAME, IDRAC_PASSWORD)

    if not client.wait_for_idrac(timeout=180):
        raise RuntimeError(f"{ip_address}: iDRAC is not reachable before starting.")

    update_service = client.get_update_service()
    multipart_uri = update_service.get("MultipartHttpPushUri")
    http_push_uri = update_service.get("HttpPushUri")

    print(f"[{ip_address}] MultipartHttpPushUri: {multipart_uri}")
    print(f"[{ip_address}] HttpPushUri: {http_push_uri}")

    if not multipart_uri:
        raise RuntimeError(f"{ip_address}: MultipartHttpPushUri is not available.")

    for index, item in enumerate(execution_plan, start=1):
        component = item["component"]
        file_path = item["file_path"]
        label = component["label"]
        target_version = component["target_version"]
        is_idrac_package = component["type"] == "idrac"
        needs_reboot = component["reboot"]

        print("\n" + "-" * 90)
        print(f"[{ip_address}] Step {index}/{len(execution_plan)} - {label}")
        print("-" * 90)

        current_version, matched_item = client.find_component_version(component)

        if matched_item:
            print(f"[{ip_address}] Current version found for {label}: {current_version}")
            print(f"[{ip_address}] Inventory matched item: {matched_item['name']} | {matched_item['id']}")
        else:
            print(f"[{ip_address}] Current version for {label} was not found in inventory.")

        if current_version and version_matches(current_version, target_version):
            print(f"[{ip_address}] SUCCESS - {label} is already at target version: {current_version}")
            continue

        if not file_path:
            print(f"[{ip_address}] WARNING - No matching file found for {label}. Skipping.")
            continue

        print(f"[{ip_address}] Target version required: {target_version}")
        print(f"[{ip_address}] File selected: {file_path.name}")
        print(f"[{ip_address}] Uploading firmware package...")

        upload_response = client.upload_firmware_file(file_path)
        task_uri = client.extract_task_uri(upload_response)

        if task_uri:
            print(f"[{ip_address}] Task detected: {task_uri}")
            client.poll_task(task_uri, is_idrac_package=is_idrac_package)
        else:
            print(f"[{ip_address}] No task URI returned. Waiting 60 seconds...")
            time.sleep(60)

        if needs_reboot:
            print(f"[{ip_address}] Rebooting host to apply update...")
            client.reboot_host()

            print(f"[{ip_address}] Waiting initial reboot time...")
            time.sleep(HOST_REBOOT_WAIT)

            if not client.wait_for_idrac(timeout=1800):
                raise RuntimeError(f"{ip_address}: iDRAC did not come back after reboot.")

            print(f"[{ip_address}] Host/iDRAC is back online.")
        else:
            print(f"[{ip_address}] No reboot required for this component. Waiting for settle time...")
            time.sleep(POST_INSTALL_SETTLE_WAIT)

        if is_idrac_package:
            print(f"[{ip_address}] Extra wait for iDRAC firmware stabilization...")
            time.sleep(IDRAC_RESTART_WAIT)

            if not client.wait_for_idrac(timeout=1200):
                raise RuntimeError(f"{ip_address}: iDRAC did not stabilize after iDRAC update.")

            print(f"[{ip_address}] iDRAC is stable.")

        print(f"[{ip_address}] Re-checking installed version for {label}...")
        new_version, new_item = client.find_component_version(component)

        if new_item:
            print(f"[{ip_address}] Installed version now: {new_version}")
            print(f"[{ip_address}] Inventory matched item: {new_item['name']} | {new_item['id']}")
        else:
            print(f"[{ip_address}] Could not re-read inventory item for {label}.")

        if new_version and version_matches(new_version, target_version):
            print(f"[{ip_address}] SUCCESS - {label} updated successfully to {new_version}")
        else:
            raise RuntimeError(
                f"{ip_address}: Version validation failed for {label}. "
                f"Expected {target_version}, found {new_version}"
            )

    print("\n" + "=" * 90)
    print(f"FINISHED SERVER: {ip_address}")
    print("=" * 90)


# =========================================================
# MAIN
# =========================================================
def main():
    project_choice, project_folder = choose_project_folder()
    profile_choice = None

    if project_choice == "3":
        profile_choice = choose_srvmgt_profile()

    files = [f for f in project_folder.iterdir() if f.is_file() and should_include_file(f.name)]

    if not files:
        raise RuntimeError(f"No EXE files were found in: {project_folder}")

    execution_plan = build_execution_plan(project_choice, files, profile_choice)

    print(f"\nSelected folder: {project_folder}")
    if project_choice == "3":
        print(f"Selected profile: {profile_choice}")

    print("\nExecution plan:")
    for i, item in enumerate(execution_plan, start=1):
        component = item["component"]
        file_path = item["file_path"]
        file_name = file_path.name if file_path else "NO FILE MATCHED"
        print(f"{i}. {component['label']} -> target {component['target_version']} -> file: {file_name}")

    ips = collect_idrac_ips()

    confirm = input("\nStart update process? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Operation cancelled.")
        return

    results = []

    for ip_address in ips:
        try:
            process_single_server(ip_address, execution_plan, project_choice, profile_choice)
            results.append((ip_address, "SUCCESS", ""))
        except Exception as error:
            results.append((ip_address, "FAILED", str(error)))
            print(f"\n[{ip_address}] ERROR: {error}")
            break

    print("\n" + "=" * 90)
    print("FINAL SUMMARY")
    print("=" * 90)

    for ip_address, status, message in results:
        if status == "SUCCESS":
            print(f"{ip_address}: SUCCESS")
        else:
            print(f"{ip_address}: FAILED - {message}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("\nFATAL ERROR:")
        print(str(e))
        print("\nFULL TRACEBACK:")
        traceback.print_exc()

    input("\nPress Enter to exit...")