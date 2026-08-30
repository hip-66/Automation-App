# -*- coding: utf-8 -*-
"""
ESXi Host Configuration Tool  (the ESX equivalent of the racadm scripts)
========================================================================

Connects to one or more standalone ESXi hosts (VMware Host Client targets)
using the official VMware Python SDK (pyVmomi - the "racadm for ESX"), pulls
the live configuration off each host, and opens a tabbed window that mirrors
the ESXi Host Client dialogs so you can review and change:

  * NTP            - startup policy + NTP servers, then start/restart ntpd
                     (mirrors "System > Time & date > Edit NTP Settings")
  * Licensing      - Check a key (decode -> "valid for vSphere 8 Standard"),
                     then Assign it   (mirrors "Manage > Licensing")
  * Virtual Switches - list / add a standard vSwitch
  * Port Groups    - list / add a port group on a chosen vSwitch, with VLAN
                     (mirrors "Networking > Port groups > Add port group")
  * VMKernel NICs  - list / add a vmk: Port group, MTU, DHCP/Static, Address,
                     Subnet mask, TCP/IP stack, and the 6 service checkboxes
                     (mirrors "Networking > VMkernel NICs > Add VMkernel NIC")

Login uses the app's configured credentials by default (ESXI_USER / ESXI_PASS
are resolved below - never hardcoded in this file).

Usage
-----
  * Through PS Automation: type the ESXi IP(s) in the form (one per line) and
    run - the app writes them to addresses.txt next to this script.
  * Standalone: run from a console; it prompts for one IP.

A host dropdown at the top switches between multiple hosts. Nothing changes on
a host until you press an Apply / Add / Save button on a tab.

  Set PSAUTO_ESXI_SELFTEST=1 to open the GUI with sample data (no connection).
"""

import os
import sys
import ssl
import queue
import threading
import subprocess


def _ensure_pyvmomi():
    """pyVmomi is the VMware Python SDK; install it quietly if missing."""
    try:
        import pyVim.connect  # noqa: F401
        import pyVmomi        # noqa: F401
        return True
    except Exception:
        print("[SETUP] pyVmomi not found - installing it (one-time)...", flush=True)
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--quiet", "pyvmomi"],
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
            )
            import pyVim.connect  # noqa: F401
            import pyVmomi        # noqa: F401
            print("[SETUP] pyVmomi installed.", flush=True)
            return True
        except Exception as e:
            print(f"[SETUP] ERROR: could not install pyVmomi automatically: {e}", flush=True)
            print("        Install it manually:  python -m pip install pyvmomi", flush=True)
            return False


SELFTEST = os.environ.get("PSAUTO_ESXI_SELFTEST") == "1"

if not SELFTEST:
    if not _ensure_pyvmomi():
        sys.exit(1)
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim

import tkinter as tk
from tkinter import ttk, messagebox

# Never hardcoded: PSAUTO_USERNAME/PASSWORD (explicit override from the app's
# UI) wins; otherwise PSAUTO_DEFAULT_USERNAME/PASSWORD (the app's encrypted
# .env default) is used; a standalone run with neither set prompts instead.
ESXI_USER = os.environ.get("PSAUTO_USERNAME", "").strip() or os.environ.get("PSAUTO_DEFAULT_USERNAME", "").strip()
ESXI_PASS = os.environ.get("PSAUTO_PASSWORD", "") or os.environ.get("PSAUTO_DEFAULT_PASSWORD", "")
if not ESXI_USER or not ESXI_PASS:
    import getpass
    if not ESXI_USER:
        ESXI_USER = input("ESXi username: ").strip()
    if not ESXI_PASS:
        ESXI_PASS = getpass.getpass("ESXi password: ")

# ESXi Host Client startup-policy labels <-> pyVmomi service policy values.
NTP_POLICY_LABELS = {
    "Start and stop with host": "on",
    "Start and stop manually": "off",
    "Start and stop with port usage": "automatic",
}
NTP_POLICY_FROM_VALUE = {v: k for k, v in NTP_POLICY_LABELS.items()}

# VMKernel service checkbox label -> pyVmomi nicType (for SelectVnicForNicType).
VMK_SERVICES = [
    ("vMotion", "vmotion"),
    ("Provisioning", "vSphereProvisioning"),
    ("Fault tolerance logging", "faultToleranceLogging"),
    ("Management", "management"),
    ("Replication", "vSphereReplication"),
    ("NFC replication", "vSphereReplicationNFC"),
]

# ===========================================================================
# Connection layer
# ===========================================================================
def connect_host(ip):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    si = SmartConnect(host=ip, user=ESXI_USER, pwd=ESXI_PASS, sslContext=ctx)
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.HostSystem], True)
    hosts = list(view.view)
    view.Destroy()
    if not hosts:
        Disconnect(si)
        raise RuntimeError("No ESXi host found at this address.")
    return si, content, hosts[0]


# ===========================================================================
# Data pull - getters return plain Python types (GUI/self-test never touch a
# live pyVmomi object).
# ===========================================================================
def _vmk_services_map(host):
    """Return {vmk_device: [enabled nicType, ...]} so the GUI can show/edit which
    services (vMotion, Management, ...) are turned on for each VMkernel NIC."""
    result = {}
    try:
        info = host.configManager.virtualNicManager.info
        for nc in (info.netConfig or []):
            key_to_dev = {c.key: c.device for c in (nc.candidateVnic or [])}
            for sel_key in (nc.selectedVnic or []):
                dev = key_to_dev.get(sel_key)
                if dev:
                    result.setdefault(dev, []).append(nc.nicType)
    except Exception:
        pass
    return result


def pull_config(content, host):
    ns = host.configManager.networkSystem
    net = ns.networkInfo

    ntp, ntp_policy, ntp_running = [], "off", False
    try:
        ntp = list(host.configManager.dateTimeSystem.dateTimeInfo.ntpConfig.server or [])
    except Exception:
        pass
    try:
        svc = next((s for s in host.configManager.serviceSystem.serviceInfo.service if s.key == "ntpd"), None)
        if svc:
            ntp_policy, ntp_running = svc.policy, bool(svc.running)
    except Exception:
        pass

    lic_name, lic_key = "Evaluation Mode", "00000-00000-00000-00000-00000"
    try:
        lics = content.licenseManager.licenses
        if lics:
            lic_name, lic_key = lics[0].name, lics[0].licenseKey
    except Exception:
        pass

    vswitches = []
    try:
        for vsw in (net.vswitch or []):
            uplinks = list(vsw.spec.bridge.nicDevice) if getattr(vsw.spec, "bridge", None) and hasattr(vsw.spec.bridge, "nicDevice") else []
            vswitches.append({"name": vsw.name, "ports": vsw.spec.numPorts, "uplinks": uplinks})
    except Exception:
        pass

    portgroups = []
    try:
        for pg in (net.portgroup or []):
            portgroups.append({"name": pg.spec.name, "vswitch": pg.spec.vswitchName, "vlan": pg.spec.vlanId})
    except Exception:
        pass

    vmkernels = []
    try:
        svc_map = _vmk_services_map(host)
        for vnic in (net.vnic or []):
            ipc = vnic.spec.ip
            vmkernels.append({"device": vnic.device, "portgroup": vnic.portgroup,
                              "ip": getattr(ipc, "ipAddress", ""), "mask": getattr(ipc, "subnetMask", ""),
                              "mtu": getattr(vnic.spec, "mtu", 1500),
                              "services": svc_map.get(vnic.device, [])})
    except Exception:
        pass

    pnics = []
    try:
        pnics = [p.device for p in (net.pnic or [])]
    except Exception:
        pass

    name = ""
    try:
        name = host.name
    except Exception:
        pass

    return {"name": name, "ntp": ntp, "ntp_policy": ntp_policy, "ntp_running": ntp_running,
            "license_name": lic_name, "license_key": lic_key,
            "vswitches": vswitches, "portgroups": portgroups,
            "vmkernels": vmkernels, "pnics": pnics}


def sample_config():
    return {
        "name": "esxi-demo.local",
        "ntp": ["10.168.90.40", "10.169.201.1"], "ntp_policy": "off", "ntp_running": False,
        "license_name": "Evaluation Mode", "license_key": "00000-00000-00000-00000-00000",
        "vswitches": [{"name": "vSwitch0", "ports": 128, "uplinks": ["vmnic0"]}],
        "portgroups": [
            {"name": "Management Network", "vswitch": "vSwitch0", "vlan": 0},
            {"name": "VM Network", "vswitch": "vSwitch0", "vlan": 0},
            {"name": "vMotion", "vswitch": "vSwitch0", "vlan": 0},
        ],
        "vmkernels": [
            {"device": "vmk0", "portgroup": "Management Network", "ip": "10.168.224.148",
             "mask": "255.255.255.0", "mtu": 1500, "services": ["management"]},
            {"device": "vmk1", "portgroup": "vMotion", "ip": "10.0.0.44",
             "mask": "255.255.255.0", "mtu": 1500, "services": ["vmotion"]},
        ],
        "pnics": ["vmnic0", "vmnic1", "vmnic2", "vmnic3"],
    }


# ===========================================================================
# Apply layer - real changes (skipped in self-test)
# ===========================================================================
def apply_ntp(host, servers, policy_value):
    dt = host.configManager.dateTimeSystem
    dt.UpdateDateTimeConfig(config=vim.host.DateTimeConfig(ntpConfig=vim.host.NtpConfig(server=servers)))
    svc = host.configManager.serviceSystem
    try:
        svc.UpdateServicePolicy(id="ntpd", policy=policy_value)
    except Exception:
        pass
    try:
        svc.RestartService(id="ntpd")
    except Exception:
        try:
            svc.StartService(id="ntpd")
        except Exception:
            pass


def check_license(content, key):
    """Decode a key WITHOUT assigning it (the UI's 'Check license'). Returns a
    human string like 'valid for vSphere 8 Standard'."""
    info = content.licenseManager.DecodeLicense(licenseKey=key)
    name = getattr(info, "name", "") or ""
    if not name or name.lower() == "evaluation mode":
        raise RuntimeError("License key is not valid.")
    return name


def apply_license(content, key):
    content.licenseManager.UpdateLicense(licenseKey=key)


def add_vswitch(host, name, num_ports, uplink):
    spec = vim.host.VirtualSwitch.Specification()
    spec.numPorts = int(num_ports)
    if uplink:
        spec.bridge = vim.host.VirtualSwitch.BondBridge(nicDevice=[uplink])
    host.configManager.networkSystem.AddVirtualSwitch(vswitchName=name, spec=spec)


def add_portgroup(host, name, vswitch, vlan):
    spec = vim.host.PortGroup.Specification()
    spec.name = name
    spec.vlanId = int(vlan)
    spec.vswitchName = vswitch
    spec.policy = vim.host.NetworkPolicy()
    host.configManager.networkSystem.AddPortGroup(portgrp=spec)


def add_vmkernel(host, portgroup, dhcp, ip, mask, mtu, services):
    ip_cfg = vim.host.IpConfig()
    if dhcp:
        ip_cfg.dhcp = True
    else:
        ip_cfg.dhcp = False
        ip_cfg.ipAddress = ip
        ip_cfg.subnetMask = mask
    nic_spec = vim.host.VirtualNic.Specification(ip=ip_cfg)
    try:
        nic_spec.mtu = int(mtu)
    except Exception:
        pass
    device = host.configManager.networkSystem.AddVirtualNic(portgroup=portgroup, nic=nic_spec)
    vnm = host.configManager.virtualNicManager
    for nic_type in services:
        try:
            vnm.SelectVnicForNicType(nicType=nic_type, device=device)
        except Exception:
            pass
    return device


# ---------------------------------------------------------------------------
# Edit / delete an EXISTING item (the "select a row and change it" flow)
# ---------------------------------------------------------------------------
def update_vswitch(host, name, num_ports, uplink):
    spec = vim.host.VirtualSwitch.Specification()
    spec.numPorts = int(num_ports)
    if uplink:
        spec.bridge = vim.host.VirtualSwitch.BondBridge(nicDevice=[uplink])
    host.configManager.networkSystem.UpdateVirtualSwitch(vswitchName=name, spec=spec)


def remove_vswitch(host, name):
    host.configManager.networkSystem.RemoveVirtualSwitch(vswitchName=name)


def update_portgroup(host, name, vswitch, vlan):
    spec = vim.host.PortGroup.Specification()
    spec.name = name
    spec.vlanId = int(vlan)
    spec.vswitchName = vswitch
    spec.policy = vim.host.NetworkPolicy()
    host.configManager.networkSystem.UpdatePortGroup(pgName=name, portgrp=spec)


def remove_portgroup(host, name):
    host.configManager.networkSystem.RemovePortGroup(pgName=name)


def set_vmk_services(host, device, services):
    """Turn on exactly the given service nicTypes for a vmk and turn off the rest."""
    vnm = host.configManager.virtualNicManager
    for _label, nic_type in VMK_SERVICES:
        try:
            if nic_type in services:
                vnm.SelectVnicForNicType(nicType=nic_type, device=device)
            else:
                vnm.DeselectVnicForNicType(nicType=nic_type, device=device)
        except Exception:
            pass


def update_vmkernel(host, device, dhcp, ip, mask, mtu):
    """Change an existing vmk's IP config + MTU in place (the port group cannot
    be changed in place - use move_vmkernel for that)."""
    ip_cfg = vim.host.IpConfig()
    if dhcp:
        ip_cfg.dhcp = True
    else:
        ip_cfg.dhcp = False
        ip_cfg.ipAddress = ip
        ip_cfg.subnetMask = mask
    nic_spec = vim.host.VirtualNic.Specification(ip=ip_cfg)
    try:
        nic_spec.mtu = int(mtu)
    except Exception:
        pass
    host.configManager.networkSystem.UpdateVirtualNic(device=device, nic=nic_spec)


def remove_vmkernel(host, device):
    host.configManager.networkSystem.RemoveVirtualNic(device=device)


def update_vmk_full(host, device, dhcp, ip, mask, mtu, services):
    """Edit an existing vmk (IP/MTU) and re-apply its service selection."""
    update_vmkernel(host, device, dhcp, ip, mask, mtu)
    set_vmk_services(host, device, services)


def move_vmkernel(host, device, new_portgroup, dhcp, ip, mask, mtu, services):
    """ESXi can't move a vmk between port groups in place, so delete it and
    recreate it on the new port group with the same settings."""
    remove_vmkernel(host, device)
    return add_vmkernel(host, new_portgroup, dhcp, ip, mask, mtu, services)


# ===========================================================================
# Copy layer - replicate a source host's settings onto other hosts
# ===========================================================================
def apply_source_config(source_cfg, target_content, target_host, categories, log):
    """Apply the selected categories from source_cfg onto an already-connected
    target host. Existing vSwitches / port groups / vmk portgroups are skipped
    (never overwritten); every item is logged individually."""
    target_cfg = pull_config(target_content, target_host)

    if "ntp" in categories:
        try:
            apply_ntp(target_host, source_cfg.get("ntp", []), source_cfg.get("ntp_policy", "on"))
            log(f"  NTP: set {source_cfg.get('ntp', [])}  (policy {source_cfg.get('ntp_policy', 'on')})")
        except Exception as e:
            log(f"  NTP: ERROR {e}")

    if "license" in categories:
        key = source_cfg.get("license_key", "") or ""
        if key and not key.startswith("00000"):
            try:
                apply_license(target_content, key)
                log(f"  License: assigned {key}")
            except Exception as e:
                log(f"  License: ERROR {e}")
        else:
            log("  License: source is Evaluation Mode - nothing to copy")

    if "vswitches" in categories:
        existing = {v["name"] for v in target_cfg.get("vswitches", [])}
        for v in source_cfg.get("vswitches", []):
            if v["name"] in existing:
                log(f"  vSwitch '{v['name']}': already exists - skipped")
                continue
            try:
                uplink = v["uplinks"][0] if v.get("uplinks") else ""
                add_vswitch(target_host, v["name"], v.get("ports", 128), uplink)
                log(f"  vSwitch '{v['name']}': added")
            except Exception as e:
                log(f"  vSwitch '{v['name']}': ERROR {e}")

    if "portgroups" in categories:
        existing = {p["name"] for p in target_cfg.get("portgroups", [])}
        for p in source_cfg.get("portgroups", []):
            if p["name"] in existing:
                log(f"  Port group '{p['name']}': already exists - skipped")
                continue
            try:
                add_portgroup(target_host, p["name"], p["vswitch"], p.get("vlan", 0))
                log(f"  Port group '{p['name']}': added (vSwitch {p['vswitch']}, VLAN {p.get('vlan', 0)})")
            except Exception as e:
                log(f"  Port group '{p['name']}': ERROR {e}")

    if "vmkernels" in categories:
        existing_pg = {m["portgroup"] for m in target_cfg.get("vmkernels", [])}
        for m in source_cfg.get("vmkernels", []):
            if m["portgroup"] in existing_pg:
                log(f"  VMkernel on '{m['portgroup']}': target already has one - skipped")
                continue
            try:
                add_vmkernel(target_host, m["portgroup"], False, m.get("ip", ""),
                             m.get("mask", "255.255.255.0"), m.get("mtu", 1500), m.get("services", []))
                log(f"  VMkernel on '{m['portgroup']}': added (IP {m.get('ip', '')} - verify no conflict!)")
            except Exception as e:
                log(f"  VMkernel on '{m['portgroup']}': ERROR {e}")


def copy_to_targets(source_cfg, target_ips, categories, log):
    """Connect to each target IP in turn and apply the selected categories."""
    for ip in target_ips:
        log(f"=== {ip} ===")
        si = None
        try:
            si, content, host = connect_host(ip)
        except Exception as e:
            log(f"  CONNECT ERROR: {e}")
            continue
        try:
            apply_source_config(source_cfg, content, host, categories, log)
        except Exception as e:
            log(f"  ERROR: {e}")
        finally:
            try:
                Disconnect(si)
            except Exception:
                pass
    log("=== Done ===")


# ===========================================================================
# GUI
# ===========================================================================
class CopyDialog:
    """Second window: copy the current (source) host's settings to other hosts.
    You pick the target IP(s) and tick exactly which categories to copy."""

    CATEGORIES = [("NTP", "ntp"), ("License", "license"), ("Virtual Switches", "vswitches"),
                  ("Port Groups", "portgroups"), ("VMkernel NICs", "vmkernels")]

    def __init__(self, parent, source_cfg, source_ip, live):
        self.source_cfg = source_cfg
        self.live = live
        self.q = queue.Queue()

        self.top = tk.Toplevel(parent)
        self.top.title("Copy configuration to other server(s)")
        self.top.geometry("600x580")

        ttk.Label(self.top, text=f"Source host:  {source_ip}", font=("", 10, "bold")).pack(anchor="w", padx=12, pady=(12, 6))
        ttk.Label(self.top, text="Target server IP(s) - one per line:").pack(anchor="w", padx=12)
        self.targets = tk.Text(self.top, height=4, width=46)
        self.targets.pack(anchor="w", padx=12, pady=4)

        box = ttk.LabelFrame(self.top, text="What to copy", padding=10)
        box.pack(anchor="w", fill="x", padx=12, pady=6)
        self.all_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="Select all", variable=self.all_var, command=self._toggle_all).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.vars = {}
        for i, (label, key) in enumerate(self.CATEGORIES):
            v = tk.BooleanVar(value=True)
            self.vars[key] = v
            ttk.Checkbutton(box, text=label, variable=v).grid(row=1 + i // 2, column=i % 2, sticky="w", padx=6, pady=2)

        ttk.Label(self.top,
                  text="Existing vSwitches / port groups on a target are skipped (never overwritten).\n"
                       "VMkernel IPs are host-specific - copied as-is, so verify there is no IP conflict.",
                  foreground="#a60", justify="left").pack(anchor="w", padx=12, pady=(0, 4))

        self.btn = ttk.Button(self.top, text="Copy to target(s)", command=self._start)
        self.btn.pack(anchor="w", padx=12, pady=8)

        self.log_text = tk.Text(self.top, height=12, bg="#111", fg="#7fd", state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self._bring_to_front()
        self._drain()

    def _toggle_all(self):
        for v in self.vars.values():
            v.set(self.all_var.get())

    def _bring_to_front(self):
        try:
            self.top.attributes("-topmost", True)
            self.top.lift()
            self.top.focus_force()
            self.top.after(600, lambda: self.top.attributes("-topmost", False))
        except Exception:
            pass

    def _log(self, msg):
        self.q.put(msg)

    def _drain(self):
        """Main-thread poll of the worker's log queue (Tkinter isn't thread-safe)."""
        try:
            while True:
                msg = self.q.get_nowait()
                if isinstance(msg, tuple) and msg and msg[0] == "__done__":
                    self.btn.configure(state="normal")
                    continue
                self.log_text.configure(state="normal")
                self.log_text.insert("end", str(msg) + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.top.after(150, self._drain)

    def _start(self):
        ips = [l.strip() for l in self.targets.get("1.0", "end").splitlines() if l.strip()]
        cats = [k for k, v in self.vars.items() if v.get()]
        if not ips:
            self._log("Enter at least one target IP.")
            return
        if not cats:
            self._log("Select at least one thing to copy.")
            return
        self.btn.configure(state="disabled")

        def worker():
            try:
                if not self.live:
                    for ip in ips:
                        self._log(f"=== {ip} ===")
                        for c in cats:
                            self._log(f"  [SELF-TEST] would copy {c} (no host connected)")
                    self._log("=== Done (self-test - nothing sent) ===")
                else:
                    copy_to_targets(self.source_cfg, ips, cats, self._log)
            finally:
                self.q.put(("__done__",))

        threading.Thread(target=worker, daemon=True).start()


class EsxiConfigGUI:
    def __init__(self, root, hosts):
        self.root = root
        self.hosts = hosts
        self.current = hosts[0] if hosts else None

        root.title("ESXi Host Configuration")
        root.geometry("880x680")

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Host:").pack(side="left")
        self.host_var = tk.StringVar(value=self.current["ip"] if self.current else "")
        self.host_combo = ttk.Combobox(top, textvariable=self.host_var, state="readonly",
                                       values=[h["ip"] for h in hosts], width=26)
        self.host_combo.pack(side="left", padx=6)
        self.host_combo.bind("<<ComboboxSelected>>", lambda e: self.switch_host())
        ttk.Button(top, text="Refresh", command=self.refresh_current).pack(side="left", padx=4)
        ttk.Button(top, text="Copy to Other Server...", command=self._open_copy).pack(side="left", padx=8)

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=4)
        self._build_ntp_tab()
        self._build_license_tab()
        self._build_vswitch_tab()
        self._build_portgroup_tab()
        self._build_vmkernel_tab()

        self.status = tk.Text(root, height=7, state="disabled", bg="#111", fg="#7fd", wrap="word")
        self.status.pack(fill="x", padx=8, pady=(0, 8))

        self.populate()
        self._bring_to_front()

    # ---- helpers ----------------------------------------------------------
    def log(self, msg):
        print(msg, flush=True)
        self.status.configure(state="normal")
        self.status.insert("end", msg + "\n")
        self.status.see("end")
        self.status.configure(state="disabled")

    def cfg(self):
        return self.current["cfg"] if self.current else {}

    def _host(self):
        return self.current.get("host") if self.current else None

    def _content(self):
        return self.current.get("content") if self.current else None

    def _bring_to_front(self):
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
            self.root.focus_force()
            self.root.after(600, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

    def _live(self):
        return not SELFTEST and self._host() is not None

    def _guard(self, fn, *args):
        if not self._live():
            self.log("[SELF-TEST] (no host connected) - change not sent.")
            return
        try:
            fn(*args)
            self.log("OK: change applied. Refreshing...")
            self.refresh_current()
        except Exception as e:
            self.log(f"ERROR: {e}")
            messagebox.showerror("ESXi", str(e))

    def _confirm(self, msg):
        """Yes/No dialog for destructive actions. Auto-yes under self-test so a
        headless preview never blocks on a dialog."""
        if SELFTEST:
            return True
        try:
            return messagebox.askyesno("ESXi - please confirm", msg)
        except Exception:
            return False

    def _confirm_delete(self, desc):
        return self._confirm(f"Delete {desc}?\nThis cannot be undone.")

    def switch_host(self):
        ip = self.host_var.get()
        self.current = next((h for h in self.hosts if h["ip"] == ip), self.current)
        self.populate()

    def _open_copy(self):
        """Open the 'copy to other server(s)' window, using the currently
        selected host as the source of the settings to copy."""
        if not self.current:
            return
        CopyDialog(self.root, self.cfg(), self.host_var.get(), self._live())

    def refresh_current(self):
        if self._live():
            try:
                self.current["cfg"] = pull_config(self._content(), self._host())
            except Exception as e:
                self.log(f"ERROR refreshing: {e}")
        self.populate()

    # ---- NTP tab (Edit NTP Settings) -------------------------------------
    def _build_ntp_tab(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="NTP")
        self.ntp_status_lbl = ttk.Label(f, text="NTP service status: -")
        self.ntp_status_lbl.pack(anchor="w", pady=(0, 8))
        ttk.Label(f, text="NTP service startup policy:").pack(anchor="w")
        self.ntp_policy = ttk.Combobox(f, state="readonly", width=32, values=list(NTP_POLICY_LABELS.keys()))
        self.ntp_policy.pack(anchor="w", pady=4)
        ttk.Label(f, text="NTP servers (separate with commas or new lines):").pack(anchor="w", pady=(8, 0))
        self.ntp_text = tk.Text(f, height=4, width=52)
        self.ntp_text.pack(anchor="w", pady=4)
        ttk.Button(f, text="Save NTP + start/restart ntpd", command=self._save_ntp).pack(anchor="w", pady=8)

    def _save_ntp(self):
        raw = self.ntp_text.get("1.0", "end").replace(",", "\n")
        servers = [s.strip() for s in raw.splitlines() if s.strip()]
        policy = NTP_POLICY_LABELS.get(self.ntp_policy.get(), "on")
        self._guard(apply_ntp, self._host(), servers, policy)

    # ---- Licensing tab ----------------------------------------------------
    def _build_license_tab(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="Licensing")
        self.lic_current = ttk.Label(f, text="Current: -")
        self.lic_current.pack(anchor="w")
        ttk.Label(f, text="License key:").pack(anchor="w", pady=(12, 0))
        self.lic_entry = ttk.Entry(f, width=44)
        self.lic_entry.pack(anchor="w", pady=4)
        self.lic_result = ttk.Label(f, text="")
        self.lic_result.pack(anchor="w", pady=4)
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=6)
        ttk.Button(row, text="Check license", command=self._check_license).pack(side="left")
        ttk.Button(row, text="Assign license", command=self._assign_license).pack(side="left", padx=8)

    def _check_license(self):
        key = self.lic_entry.get().strip()
        if not self._live():
            self.lic_result.configure(text="[self-test] cannot validate without a host.")
            return
        try:
            name = check_license(self._content(), key)
            self.lic_result.configure(text=f"✔ License key is valid for {name}")
            self.log(f"License valid: {name}")
        except Exception as e:
            self.lic_result.configure(text=f"✖ {e}")
            self.log(f"License check failed: {e}")

    def _assign_license(self):
        self._guard(apply_license, self._content(), self.lic_entry.get().strip())

    # ---- Virtual Switches tab --------------------------------------------
    def _build_vswitch_tab(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="Virtual Switches")
        self.vsw_selected = None
        self.vsw_tree = ttk.Treeview(f, columns=("ports", "uplinks"), show="tree headings", height=6)
        self.vsw_tree.heading("#0", text="Name"); self.vsw_tree.heading("ports", text="Ports"); self.vsw_tree.heading("uplinks", text="Uplinks")
        self.vsw_tree.column("#0", width=200); self.vsw_tree.column("ports", width=70); self.vsw_tree.column("uplinks", width=200)
        self.vsw_tree.pack(anchor="w", pady=4, fill="x")
        self.vsw_tree.bind("<<TreeviewSelect>>", lambda e: self._vsw_on_select())

        box = ttk.LabelFrame(f, text="Add / edit standard virtual switch", padding=8)
        box.pack(anchor="w", fill="x", pady=8)
        ttk.Label(box, text="Name:").grid(row=0, column=0, sticky="w"); self.vsw_name = ttk.Entry(box, width=20); self.vsw_name.grid(row=0, column=1, padx=4)
        ttk.Label(box, text="Ports:").grid(row=0, column=2, sticky="w"); self.vsw_ports = ttk.Entry(box, width=8); self.vsw_ports.insert(0, "128"); self.vsw_ports.grid(row=0, column=3, padx=4)
        ttk.Label(box, text="Uplink:").grid(row=0, column=4, sticky="w"); self.vsw_uplink = ttk.Combobox(box, width=12, state="readonly"); self.vsw_uplink.grid(row=0, column=5, padx=4)
        btns = ttk.Frame(box); btns.grid(row=1, column=0, columnspan=6, sticky="w", pady=(8, 0))
        ttk.Button(btns, text="Add new", command=lambda: self._guard(
            add_vswitch, self._host(), self.vsw_name.get().strip(),
            self.vsw_ports.get().strip() or "128", self.vsw_uplink.get().strip())).pack(side="left")
        ttk.Button(btns, text="Update selected", command=self._vsw_update).pack(side="left", padx=6)
        ttk.Button(btns, text="Delete selected", command=self._vsw_delete).pack(side="left")
        ttk.Label(box, text="Tip: click a row above to load it, then Update or Delete.", foreground="#888").grid(row=2, column=0, columnspan=6, sticky="w", pady=(6, 0))

    def _vsw_on_select(self):
        sel = self.vsw_tree.selection()
        if not sel:
            return
        item = self.vsw_tree.item(sel[0])
        self.vsw_selected = item["text"]
        vals = list(item["values"]) + ["", ""]
        self.vsw_name.delete(0, "end"); self.vsw_name.insert(0, item["text"])
        self.vsw_ports.delete(0, "end"); self.vsw_ports.insert(0, str(vals[0]))
        first_uplink = str(vals[1]).split(",")[0].strip() if vals[1] else ""
        self.vsw_uplink.set(first_uplink)

    def _vsw_update(self):
        if not self.vsw_selected:
            self.log("Select a vSwitch row first."); return
        self._guard(update_vswitch, self._host(), self.vsw_selected,
                    self.vsw_ports.get().strip() or "128", self.vsw_uplink.get().strip())

    def _vsw_delete(self):
        if not self.vsw_selected:
            self.log("Select a vSwitch row first."); return
        if not self._confirm_delete(f"virtual switch '{self.vsw_selected}'"):
            return
        self._guard(remove_vswitch, self._host(), self.vsw_selected)

    # ---- Port Groups tab -------------------------------------------------
    def _build_portgroup_tab(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="Port Groups")
        self.pg_selected = None
        self.pg_tree = ttk.Treeview(f, columns=("vlan", "type", "vswitch"), show="tree headings", height=7)
        self.pg_tree.heading("#0", text="Name"); self.pg_tree.heading("vlan", text="VLAN ID"); self.pg_tree.heading("type", text="Type"); self.pg_tree.heading("vswitch", text="vSwitch")
        self.pg_tree.column("#0", width=200); self.pg_tree.column("vlan", width=70); self.pg_tree.column("type", width=150); self.pg_tree.column("vswitch", width=120)
        self.pg_tree.pack(anchor="w", pady=4, fill="x")
        self.pg_tree.bind("<<TreeviewSelect>>", lambda e: self._pg_on_select())

        box = ttk.LabelFrame(f, text="Add / edit port group", padding=8)
        box.pack(anchor="w", fill="x", pady=8)
        ttk.Label(box, text="Name:").grid(row=0, column=0, sticky="w"); self.pg_name = ttk.Entry(box, width=22); self.pg_name.grid(row=0, column=1, padx=4)
        ttk.Label(box, text="VLAN ID:").grid(row=0, column=2, sticky="w"); self.pg_vlan = ttk.Entry(box, width=6); self.pg_vlan.insert(0, "0"); self.pg_vlan.grid(row=0, column=3, padx=4)
        ttk.Label(box, text="Virtual switch:").grid(row=0, column=4, sticky="w"); self.pg_vsw = ttk.Combobox(box, width=14, state="readonly"); self.pg_vsw.grid(row=0, column=5, padx=4)
        btns = ttk.Frame(box); btns.grid(row=1, column=0, columnspan=6, sticky="w", pady=(8, 0))
        ttk.Button(btns, text="Add new", command=lambda: self._guard(
            add_portgroup, self._host(), self.pg_name.get().strip(),
            self.pg_vsw.get().strip(), self.pg_vlan.get().strip() or "0")).pack(side="left")
        ttk.Button(btns, text="Update selected", command=self._pg_update).pack(side="left", padx=6)
        ttk.Button(btns, text="Delete selected", command=self._pg_delete).pack(side="left")
        ttk.Label(box, text="Tip: click a row above to load it, then Update or Delete.", foreground="#888").grid(row=2, column=0, columnspan=6, sticky="w", pady=(6, 0))

    def _pg_on_select(self):
        sel = self.pg_tree.selection()
        if not sel:
            return
        item = self.pg_tree.item(sel[0])
        self.pg_selected = item["text"]
        vals = list(item["values"]) + ["", "", ""]
        self.pg_name.delete(0, "end"); self.pg_name.insert(0, item["text"])
        self.pg_vlan.delete(0, "end"); self.pg_vlan.insert(0, str(vals[0]))
        self.pg_vsw.set(str(vals[2]))

    def _pg_update(self):
        if not self.pg_selected:
            self.log("Select a port group row first."); return
        self._guard(update_portgroup, self._host(), self.pg_selected,
                    self.pg_vsw.get().strip(), self.pg_vlan.get().strip() or "0")

    def _pg_delete(self):
        if not self.pg_selected:
            self.log("Select a port group row first."); return
        if not self._confirm_delete(f"port group '{self.pg_selected}'"):
            return
        self._guard(remove_portgroup, self._host(), self.pg_selected)

    # ---- VMKernel tab (Add VMkernel NIC) ---------------------------------
    def _build_vmkernel_tab(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="VMkernel NICs")
        self.vmk_selected = None
        self.vmk_selected_pg = None
        self.vmk_tree = ttk.Treeview(f, columns=("pg", "ip", "mask"), show="tree headings", height=5)
        self.vmk_tree.heading("#0", text="Name"); self.vmk_tree.heading("pg", text="Port group"); self.vmk_tree.heading("ip", text="IPv4 address"); self.vmk_tree.heading("mask", text="Subnet mask")
        self.vmk_tree.column("#0", width=70); self.vmk_tree.column("pg", width=200); self.vmk_tree.column("ip", width=130); self.vmk_tree.column("mask", width=130)
        self.vmk_tree.pack(anchor="w", pady=4, fill="x")
        self.vmk_tree.bind("<<TreeviewSelect>>", lambda e: self._vmk_on_select())

        add = ttk.LabelFrame(f, text="Add / edit VMkernel NIC", padding=8)
        add.pack(anchor="w", fill="x", pady=8)
        ttk.Label(add, text="Port group:").grid(row=0, column=0, sticky="w"); self.vmk_pg = ttk.Combobox(add, width=20, state="readonly"); self.vmk_pg.grid(row=0, column=1, padx=4, pady=2)
        ttk.Label(add, text="MTU:").grid(row=0, column=2, sticky="w"); self.vmk_mtu = ttk.Entry(add, width=8); self.vmk_mtu.insert(0, "1500"); self.vmk_mtu.grid(row=0, column=3, padx=4)
        ttk.Label(add, text="TCP/IP stack:").grid(row=1, column=0, sticky="w"); self.vmk_stack = ttk.Combobox(add, width=20, state="readonly", values=["Default TCP/IP stack"]); self.vmk_stack.current(0); self.vmk_stack.grid(row=1, column=1, padx=4, pady=2)

        self.vmk_dhcp = tk.StringVar(value="static")
        cfgrow = ttk.Frame(add); cfgrow.grid(row=2, column=0, columnspan=4, sticky="w", pady=2)
        ttk.Label(cfgrow, text="Configuration:").pack(side="left")
        ttk.Radiobutton(cfgrow, text="DHCP", variable=self.vmk_dhcp, value="dhcp", command=self._toggle_vmk_ip).pack(side="left", padx=4)
        ttk.Radiobutton(cfgrow, text="Static", variable=self.vmk_dhcp, value="static", command=self._toggle_vmk_ip).pack(side="left")

        self.vmk_iprow = ttk.Frame(add); self.vmk_iprow.grid(row=3, column=0, columnspan=4, sticky="w", pady=2)
        ttk.Label(self.vmk_iprow, text="Address:").pack(side="left"); self.vmk_ip = ttk.Entry(self.vmk_iprow, width=16); self.vmk_ip.pack(side="left", padx=4)
        ttk.Label(self.vmk_iprow, text="Subnet mask:").pack(side="left"); self.vmk_mask = ttk.Entry(self.vmk_iprow, width=16); self.vmk_mask.insert(0, "255.255.255.0"); self.vmk_mask.pack(side="left", padx=4)

        svc = ttk.LabelFrame(add, text="Services", padding=6); svc.grid(row=4, column=0, columnspan=4, sticky="w", pady=6)
        self.vmk_svc_vars = {}
        for i, (label, nic_type) in enumerate(VMK_SERVICES):
            var = tk.BooleanVar()
            self.vmk_svc_vars[nic_type] = var
            ttk.Checkbutton(svc, text=label, variable=var).grid(row=i // 3, column=i % 3, sticky="w", padx=6, pady=2)

        btns = ttk.Frame(add); btns.grid(row=5, column=0, columnspan=4, sticky="w", pady=6)
        ttk.Button(btns, text="Create new", command=self._add_vmk).pack(side="left")
        ttk.Button(btns, text="Update selected", command=self._vmk_update).pack(side="left", padx=6)
        ttk.Button(btns, text="Delete selected", command=self._vmk_delete).pack(side="left")
        ttk.Label(add, text="Tip: click a vmk row above to load it. Changing the Port group on Update recreates the adapter.",
                  foreground="#888").grid(row=6, column=0, columnspan=4, sticky="w", pady=(4, 0))

    def _toggle_vmk_ip(self):
        state = "disabled" if self.vmk_dhcp.get() == "dhcp" else "normal"
        for child in self.vmk_iprow.winfo_children():
            try:
                child.configure(state=state)
            except Exception:
                pass

    def _add_vmk(self):
        services = [nt for nt, var in self.vmk_svc_vars.items() if var.get()]
        self._guard(add_vmkernel, self._host(), self.vmk_pg.get().strip(),
                    self.vmk_dhcp.get() == "dhcp", self.vmk_ip.get().strip(),
                    self.vmk_mask.get().strip() or "255.255.255.0",
                    self.vmk_mtu.get().strip() or "1500", services)

    def _vmk_on_select(self):
        sel = self.vmk_tree.selection()
        if not sel:
            return
        item = self.vmk_tree.item(sel[0])
        device = item["text"]
        vals = list(item["values"]) + ["", "", ""]
        pg, ip, mask = str(vals[0]), str(vals[1]), str(vals[2])
        self.vmk_selected = device
        self.vmk_selected_pg = pg
        entry = next((m for m in self.cfg().get("vmkernels", []) if m.get("device") == device), {})

        self.vmk_pg.set(pg)
        self.vmk_mtu.delete(0, "end"); self.vmk_mtu.insert(0, str(entry.get("mtu", 1500)))
        # Enable the IP fields before writing into them, then apply DHCP/static.
        self.vmk_ip.configure(state="normal"); self.vmk_mask.configure(state="normal")
        self.vmk_ip.delete(0, "end"); self.vmk_ip.insert(0, ip)
        self.vmk_mask.delete(0, "end"); self.vmk_mask.insert(0, mask or "255.255.255.0")
        self.vmk_dhcp.set("static" if ip.strip() else "dhcp")
        self._toggle_vmk_ip()
        enabled = set(entry.get("services", []))
        for nt, var in self.vmk_svc_vars.items():
            var.set(nt in enabled)

    def _vmk_update(self):
        if not self.vmk_selected:
            self.log("Select a VMkernel row first."); return
        device = self.vmk_selected
        new_pg = self.vmk_pg.get().strip()
        dhcp = self.vmk_dhcp.get() == "dhcp"
        ip = self.vmk_ip.get().strip()
        mask = self.vmk_mask.get().strip() or "255.255.255.0"
        mtu = self.vmk_mtu.get().strip() or "1500"
        services = [nt for nt, var in self.vmk_svc_vars.items() if var.get()]
        # ESXi can't move a vmk between port groups in place - recreate it.
        if new_pg and self.vmk_selected_pg and new_pg != self.vmk_selected_pg:
            if not self._confirm(f"Moving {device} to port group '{new_pg}' will DELETE and RECREATE "
                                 f"the adapter (brief network blip - avoid on the management vmk).\nContinue?"):
                return
            self._guard(move_vmkernel, self._host(), device, new_pg, dhcp, ip, mask, mtu, services)
        else:
            self._guard(update_vmk_full, self._host(), device, dhcp, ip, mask, mtu, services)

    def _vmk_delete(self):
        if not self.vmk_selected:
            self.log("Select a VMkernel row first."); return
        if not self._confirm_delete(f"VMkernel NIC '{self.vmk_selected}'"):
            return
        self._guard(remove_vmkernel, self._host(), self.vmk_selected)

    # ---- populate ---------------------------------------------------------
    def populate(self):
        cfg = self.cfg()

        # Clear any stale row selection when (re)loading a host's config.
        self.vsw_selected = None
        self.pg_selected = None
        self.vmk_selected = None
        self.vmk_selected_pg = None

        status = "Running" if cfg.get("ntp_running") else "Stopped"
        self.ntp_status_lbl.configure(text=f"NTP service status: {status}    |    Current servers: {', '.join(cfg.get('ntp', [])) or 'None'}")
        self.ntp_policy.set(NTP_POLICY_FROM_VALUE.get(cfg.get("ntp_policy", "off"), "Start and stop manually"))
        self.ntp_text.delete("1.0", "end")
        self.ntp_text.insert("1.0", ", ".join(cfg.get("ntp", [])))

        self.lic_current.configure(text=f"Current: {cfg.get('license_name', '-')}   Key: {cfg.get('license_key', '-')}")

        for tree, rows in (
            (self.vsw_tree, [(v["name"], (v["ports"], ", ".join(v["uplinks"]))) for v in cfg.get("vswitches", [])]),
            (self.pg_tree, [(p["name"], (p["vlan"], "Standard port group", p["vswitch"])) for p in cfg.get("portgroups", [])]),
            (self.vmk_tree, [(m["device"], (m["portgroup"], m["ip"], m["mask"])) for m in cfg.get("vmkernels", [])]),
        ):
            tree.delete(*tree.get_children())
            for text, vals in rows:
                tree.insert("", "end", text=text, values=vals)

        self.vsw_uplink.configure(values=cfg.get("pnics", []))
        self.pg_vsw.configure(values=[v["name"] for v in cfg.get("vswitches", [])])
        self.vmk_pg.configure(values=[p["name"] for p in cfg.get("portgroups", [])])

        self.log(f"Loaded config for {self.host_var.get() or cfg.get('name', '(unknown)')}")


# ===========================================================================
# Entry point
# ===========================================================================
def read_targets():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    addr = os.path.join(script_dir, "addresses.txt")
    servers = []
    if os.path.exists(addr):
        with open(addr, "r", encoding="utf-8", errors="ignore") as f:
            servers = [ln.strip() for ln in f if ln.strip()]
    if not servers and sys.stdin is not None and sys.stdin.isatty():
        ip = input("Enter ESXi host IP: ").strip()
        if ip:
            servers = [ip]
    return servers


def main():
    print("=== ESXi Host Configuration Tool ===", flush=True)

    if SELFTEST:
        hosts = [{"ip": "10.0.0.50 (demo)", "si": None, "content": None, "host": None, "cfg": sample_config()}]
        print("[SELF-TEST] Opening GUI with sample data (no connection).", flush=True)
    else:
        targets = read_targets()
        if not targets:
            print("ERROR: No ESXi IP provided (addresses.txt is empty).", flush=True)
            sys.exit(1)
        hosts = []
        for ip in targets:
            print(f"Connecting to {ip} ...", flush=True)
            try:
                si, content, host = connect_host(ip)
                cfg = pull_config(content, host)
                hosts.append({"ip": ip, "si": si, "content": content, "host": host, "cfg": cfg})
                print(f"  Connected. NTP={cfg['ntp']}  vSwitches={len(cfg['vswitches'])}  "
                      f"PortGroups={len(cfg['portgroups'])}  VMkernels={len(cfg['vmkernels'])}", flush=True)
            except Exception as e:
                print(f"  ERROR connecting to {ip}: {e}", flush=True)
        if not hosts:
            print("ERROR: could not connect to any host. Check IP / credentials / network.", flush=True)
            sys.exit(1)

    root = tk.Tk()
    EsxiConfigGUI(root, hosts)
    # PSAUTO_ESXI_AUTOCLOSE is used only for automated headless verification;
    # a normal self-test run keeps the window open so it can be previewed.
    if SELFTEST and os.environ.get("PSAUTO_ESXI_AUTOCLOSE") == "1":
        root.after(900, root.destroy)
    try:
        root.mainloop()
    finally:
        if not SELFTEST:
            for h in hosts:
                try:
                    Disconnect(h["si"])
                except Exception:
                    pass
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
