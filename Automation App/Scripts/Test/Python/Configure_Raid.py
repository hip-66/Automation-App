# -*- coding: utf-8 -*-
"""
Configure Raid - interactive RAID builder for Dell iDRAC9
=========================================================
Flow:
  1. You type the iDRAC IP(s) + username/password in the form and run.
  2. The tool PINGs each target, then reads every physical disk.
  3. A window opens showing all disks (slot/bay, size, media, bus, state).
  4. You build ONE OR MORE arrays in the same session, e.g.
        disks 1,2 -> RAID 1     disks 5,6 -> RAID 5
     A disk assigned to one array can NOT be picked again for another
     (it's locked), so there's never any overlap/confusion.
  5. "Create all arrays" builds them together (convert -> apply/reboot ->
     create every vDisk -> apply/reboot -> verify), so the server reboots
     once for the whole plan.

RAID levels: RAID 0, RAID 1, RAID 5, RAID 6, RAID 10.
Credentials: PSAUTO_USERNAME/PASSWORD (form) -> PSAUTO_DEFAULT_* (.env) ->
             prompt when standalone. Never hardcoded.
Targets    : the app writes the IP(s) to addresses.txt next to this script;
             the "Server" dropdown switches between them.

Set PSAUTO_RAID_SELFTEST=1 to open the GUI with SAMPLE disks (no connection).

NOTE: creating a RAID is DESTRUCTIVE - it wipes the chosen disks. The app marks
this automation "destructive"; the window also asks you to confirm.
"""

import os
import sys
import re
import time
import shutil
import platform
import subprocess
import threading
import queue

import tkinter as tk
from tkinter import ttk, messagebox

SELFTEST = os.environ.get("PSAUTO_RAID_SELFTEST") == "1"

USER = os.environ.get("PSAUTO_USERNAME", "").strip() or os.environ.get("PSAUTO_DEFAULT_USERNAME", "").strip()
PASS = os.environ.get("PSAUTO_PASSWORD", "") or os.environ.get("PSAUTO_DEFAULT_PASSWORD", "")
if not SELFTEST and (not USER or not PASS):
    import getpass
    if not USER:
        USER = input("iDRAC username: ").strip()
    if not PASS:
        PASS = getpass.getpass("iDRAC password: ")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# code -> (label, min disks, must-be-even)
RAID_LEVELS = [
    ("r0",  "RAID 0",  2, False),
    ("r1",  "RAID 1",  2, False),
    ("r5",  "RAID 5",  3, False),
    ("r6",  "RAID 6",  4, False),
    ("r10", "RAID 10", 4, True),
]
RAID_MIN = {c: mn for c, _l, mn, _e in RAID_LEVELS}
RAID_EVEN = {c: ev for c, _l, _mn, ev in RAID_LEVELS}


def load_targets():
    ips = []
    f = os.path.join(SCRIPT_DIR, "addresses.txt")
    if os.path.exists(f):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                ips = [ln.strip() for ln in fh if ln.strip()]
        except Exception:
            ips = []
    if not ips and not SELFTEST:
        ip = input("iDRAC IP: ").strip()
        if ip:
            ips = [ip]
    return ips


def ping(ip):
    try:
        n = "-n" if platform.system().lower().startswith("win") else "-c"
        p = subprocess.run(["ping", n, "1", "-w", "1500", ip], capture_output=True, text=True, timeout=8)
        return p.returncode == 0
    except Exception:
        return False


# =====================================================================
# racadm plumbing
# =====================================================================
def racadm(ip, *args, timeout=180):
    cmd = ["racadm", "-r", ip, "-u", USER, "-p", PASS, "--nocertwarn"] + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or ""))
    except Exception as e:
        return 1, str(e)


def _val(block, key):
    m = re.search(r"(?im)^\s*" + re.escape(key) + r"\s*=\s*(.+?)\s*$", block)
    return m.group(1).strip() if m else ""


def controller_of(fqdd):
    return fqdd.rsplit(":", 1)[-1] if ":" in fqdd else ""


def slot_of(fqdd):
    m = re.search(r"Disk\.Bay\.(\d+)", fqdd)
    return int(m.group(1)) if m else -1


def read_disks(ip):
    rc, out = racadm(ip, "storage", "get", "pdisks")
    fqdds = sorted(set(re.findall(r"Disk\.Bay\.[^\s\r\n]+", out)), key=lambda f: (controller_of(f), slot_of(f)))
    disks = []
    for f in fqdds:
        rc2, d = racadm(ip, "storage", "get", "pdisks:" + f, "-o")
        size = _val(d, "Size")
        sz = 0.0
        m = re.search(r"([\d.]+)", size)
        if m:
            try:
                sz = float(m.group(1))
            except Exception:
                sz = 0.0
        disks.append({
            "fqdd": f, "slot": slot_of(f), "controller": controller_of(f),
            "size": size or "?", "size_gb": sz,
            "media": _val(d, "MediaType") or "",
            "proto": _val(d, "BusProtocol") or "",
            "state": _val(d, "RaidStatus") or _val(d, "State") or "?",
            "model": _val(d, "Model") or _val(d, "DeviceDescription") or "",
        })
    return disks


SAMPLE_DISKS = [
    {"fqdd": f"Disk.Bay.{i}:Enclosure.Internal.0-1:RAID.Integrated.1-1", "slot": i,
     "controller": "RAID.Integrated.1-1",
     "size": ("460.00 GB" if i < 2 else "1.09 TB"), "size_gb": (460.0 if i < 2 else 1090.0),
     "media": ("HDD" if i < 4 else "SSD"), "proto": ("SAS" if i < 4 else "SATA"),
     "state": ("Non-RAID" if i == 3 else "Ready"),
     "model": ("SEAGATE ST600MM" if i < 4 else "INTEL SSDSC2KB01")}
    for i in range(6)
]


# =====================================================================
# Build flow (multiple arrays, one reboot for the whole plan)
# =====================================================================
def _accepted(out):
    return bool(re.search(r"(?i)success|successful|JID_|created|pending|committed|already", out))


def _reboot(ip, log):
    log(f"[{ip}] Power-cycling to apply pending config...")
    rc, out = racadm(ip, "serveraction", "powercycle")
    if rc != 0 and not re.search(r"(?i)success", out):
        racadm(ip, "serveraction", "hardreset")
    time.sleep(20)


def _wait_racadm(ip, log, minutes=25):
    log(f"[{ip}] Waiting for iDRAC/RACADM to come back...")
    deadline = time.time() + minutes * 60
    time.sleep(45)
    while time.time() < deadline:
        rc, out = racadm(ip, "getversion", timeout=40)
        if rc == 0 and not re.search(r"(?i)ERROR|Login failed|Unable to connect|timed out|RAC0", out):
            log(f"[{ip}] RACADM ready.")
            return True
        time.sleep(30)
    return False


def _commit_controllers(ip, controllers, reason, log):
    log(f"[{ip}] Committing storage job(s) - {reason}...")
    ok_any = False
    for c in controllers:
        rc, out = racadm(ip, "jobqueue", "create", c, "-s", "TIME_NOW")
        if rc == 0 or _accepted(out):
            ok_any = True
        else:
            log(f"[{ip}] jobqueue create failed on {c}: {out.strip()[:160]}")
    if not ok_any:
        return False
    _reboot(ip, log)
    return _wait_racadm(ip, log)


def _disk_state(ip, fqdd):
    rc, d = racadm(ip, "storage", "get", "pdisks:" + fqdd, "-o")
    return (_val(d, "RaidStatus") or _val(d, "State") or "").lower()


def _verify_vdisk(ip, name):
    rc, out = racadm(ip, "storage", "get", "vdisks", "-o", "-p", "Name,State,Layout")
    for block in re.split(r"(?=Disk\.Virtual\.)", out):
        if re.search(re.escape(name), block):
            return bool(re.search(r"(?i)online|optimal|ready", _val(block, "State")))
    return False


def build_all(ip, arrays, log):
    """arrays = [{name, level, disks:[diskdict...], controller}]. One reboot for
    the convert step (if any) and one for the create step."""
    all_disks = [d for a in arrays for d in a["disks"]]
    controllers = sorted({d["controller"] for d in all_disks})

    nonraid = [d for d in all_disks if "non" in (d["state"] or "").lower()]
    if nonraid:
        log(f"[{ip}] Converting {len(nonraid)} Non-RAID disk(s) to RAID-capable...")
        for d in nonraid:
            rc, out = racadm(ip, "storage", "converttoRAID:" + d["fqdd"])
            if rc != 0 and not _accepted(out):
                log(f"[{ip}] converttoRAID failed for slot {d['slot']}: {out.strip()[:140]}")
                return False
        if not _commit_controllers(ip, controllers, "convert to RAID", log):
            return False
        deadline = time.time() + 45 * 60
        while time.time() < deadline:
            if all("non" not in _disk_state(ip, d["fqdd"]) for d in all_disks):
                log(f"[{ip}] Disks are RAID-capable.")
                break
            time.sleep(30)

    for a in arrays:
        pdkey = ",".join(d["fqdd"] for d in a["disks"])
        log(f"[{ip}] Staging {a['level'].upper()} '{a['name']}' on slots {sorted(d['slot'] for d in a['disks'])}...")
        rc, out = racadm(ip, "storage", "createvd:" + a["controller"], "-rl", a["level"], "-pdkey:" + pdkey, "-name", a["name"])
        if rc != 0 and not _accepted(out):
            log(f"[{ip}] createvd failed for '{a['name']}': {out.strip()[:200]}")
            return False

    log(f"[{ip}] Applying all arrays (job + one reboot)...")
    if not _commit_controllers(ip, controllers, "create virtual disks", log):
        return False

    log(f"[{ip}] Verifying...")
    all_ok = True
    deadline = time.time() + 45 * 60
    pending = {a["name"] for a in arrays}
    while pending and time.time() < deadline:
        for name in list(pending):
            if _verify_vdisk(ip, name):
                log(f"[{ip}] ✓ '{name}' is online.")
                pending.discard(name)
        if pending:
            time.sleep(30)
    if pending:
        all_ok = False
        log(f"[{ip}] Not verified in time: {', '.join(pending)}")
    return all_ok


# =====================================================================
# GUI
# =====================================================================
class App:
    def __init__(self, root, ips):
        self.root = root
        self.ips = ips
        self.disks = []
        self.ticked = set()          # fqdds ticked for the NEXT array
        self.assigned = {}           # fqdd -> array name (locked)
        self.arrays = []             # [{name, level, disks, controller}]
        self.level = tk.StringVar(value="r1")
        self.name = tk.StringVar(value="vDisk1")
        self.q = queue.Queue()
        self.busy = False
        root.title("Configure Raid  -  iDRAC9")
        root.geometry("960x760")

        top = ttk.Frame(root, padding=10); top.pack(fill="x")
        ttk.Label(top, text="Server:").pack(side="left")
        self.server = ttk.Combobox(top, values=ips or ["(SELFTEST)"], state="readonly", width=22)
        self.server.current(0); self.server.pack(side="left", padx=(4, 12))
        self.server.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Button(top, text="Ping + Read disks ↻", command=self.refresh).pack(side="left")
        self.status = ttk.Label(top, text=""); self.status.pack(side="left", padx=14)

        ttk.Label(root, text="Disks  (tick the ones for the next array):", padding=(12, 2)).pack(anchor="w")
        cols = ("sel", "slot", "size", "media", "proto", "state", "assigned", "model")
        heads = {"sel": "✓", "slot": "Slot", "size": "Size", "media": "Media", "proto": "Bus", "state": "State", "assigned": "In array", "model": "Model"}
        widths = {"sel": 34, "slot": 46, "size": 96, "media": 58, "proto": 56, "state": 96, "assigned": 90, "model": 250}
        tv = ttk.Treeview(root, columns=cols, show="headings", height=8, selectmode="none")
        for c in cols:
            tv.heading(c, text=heads[c]); tv.column(c, width=widths[c], anchor=("w" if c == "model" else "center"))
        tv.tag_configure("locked", foreground="#7c8aa0")
        tv.pack(fill="x", padx=12, pady=4); tv.bind("<Button-1>", self.on_click); self.tv = tv

        build = ttk.LabelFrame(root, text="Add an array from the ticked disks", padding=8)
        build.pack(fill="x", padx=12, pady=(6, 2))
        for code, label, _mn, _ev in RAID_LEVELS:
            ttk.Radiobutton(build, text=label, value=code, variable=self.level).pack(side="left", padx=6)
        ttk.Label(build, text="Name:").pack(side="left", padx=(14, 2))
        ttk.Entry(build, textvariable=self.name, width=16).pack(side="left")
        ttk.Button(build, text="+ Add array", command=self.add_array).pack(side="left", padx=10)

        ttk.Label(root, text="Planned arrays:", padding=(12, 2)).pack(anchor="w")
        acols = ("name", "level", "count", "slots")
        aheads = {"name": "Name", "level": "Level", "count": "# Disks", "slots": "Slots"}
        awid = {"name": 160, "level": 80, "count": 70, "slots": 380}
        av = ttk.Treeview(root, columns=acols, show="headings", height=4, selectmode="browse")
        for c in acols:
            av.heading(c, text=aheads[c]); av.column(c, width=awid[c], anchor=("w" if c == "slots" else "center"))
        av.pack(fill="x", padx=12, pady=4); self.av = av
        arow = ttk.Frame(root, padding=(12, 2)); arow.pack(fill="x")
        ttk.Button(arow, text="Remove selected array", command=self.remove_array).pack(side="left")
        self.create_btn = ttk.Button(arow, text="Create all arrays", command=self.on_create)
        self.create_btn.pack(side="left", padx=10)

        self.logbox = tk.Text(root, height=10, bg="#0b1020", fg="#d5dee8", insertbackground="#d5dee8", wrap="word")
        self.logbox.pack(fill="both", expand=True, padx=12, pady=(6, 10))

        self.root.after(150, self._drain)
        self.refresh()

    # ---- UI-thread helpers ----
    def log(self, msg): self.q.put(("log", str(msg)))

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.logbox.insert("end", payload + "\n"); self.logbox.see("end"); print(payload, flush=True)
                elif kind == "disks":
                    self.disks = payload; self._fill_disks()
                elif kind == "status":
                    self.status.config(text=payload)
                elif kind == "busy":
                    self.busy = payload; self.create_btn.config(state="disabled" if payload else "normal")
        except queue.Empty:
            pass
        self.root.after(150, self._drain)

    def current_ip(self):
        return (self.ips[self.server.current()] if self.ips else "")

    # ---- read ----
    def refresh(self):
        if self.busy:
            return
        self.ticked.clear(); self.assigned.clear(); self.arrays = []
        self._fill_arrays()
        self.q.put(("status", "Pinging + reading disks..."))
        threading.Thread(target=self._read_worker, daemon=True).start()

    def _read_worker(self):
        if SELFTEST:
            self.q.put(("disks", [dict(d) for d in SAMPLE_DISKS]))
            self.log("[SELFTEST] Showing sample disks (no server connection).")
            self.q.put(("status", f"{len(SAMPLE_DISKS)} disk(s) - SELFTEST"))
            return
        ip = self.current_ip()
        ok = ping(ip)
        self.log(f"[{ip}] Ping: {'reachable' if ok else 'NO reply (continuing - iDRAC may block ping)'}")
        self.log(f"[{ip}] Reading physical disks via racadm...")
        try:
            disks = read_disks(ip)
        except Exception as e:
            disks = []; self.log(f"[{ip}] ERROR reading disks: {e}")
        self.q.put(("disks", disks))
        self.q.put(("status", f"{len(disks)} disk(s) found"))

    def _fill_disks(self):
        self.tv.delete(*self.tv.get_children())
        for d in self.disks:
            fq = d["fqdd"]
            if fq in self.assigned:
                sel, tag, inarr = "•", ("locked",), self.assigned[fq]
            else:
                sel, tag, inarr = ("☑" if fq in self.ticked else "☐"), (), "-"
            self.tv.insert("", "end", iid=fq, tags=tag,
                           values=(sel, d["slot"], d["size"], d["media"], d["proto"], d["state"], inarr, d["model"]))

    def _fill_arrays(self):
        self.av.delete(*self.av.get_children())
        for a in self.arrays:
            slots = ", ".join(str(d["slot"]) for d in a["disks"])
            self.av.insert("", "end", iid=a["name"], values=(a["name"], a["level"].upper(), len(a["disks"]), slots))

    def on_click(self, event):
        if self.busy:
            return
        row = self.tv.identify_row(event.y)
        if not row:
            return
        if row in self.assigned:
            messagebox.showinfo("Configure Raid", f"Slot is already used by array '{self.assigned[row]}'. Remove that array to free it.")
            return
        if row in self.ticked:
            self.ticked.discard(row)
        else:
            self.ticked.add(row)
        self.tv.set(row, "sel", "☑" if row in self.ticked else "☐")

    # ---- arrays ----
    def add_array(self):
        if self.busy:
            return
        level = self.level.get(); name = self.name.get().strip()
        chosen = [d for d in self.disks if d["fqdd"] in self.ticked]
        if not name:
            messagebox.showwarning("Configure Raid", "Enter a name for the array."); return
        if any(a["name"].lower() == name.lower() for a in self.arrays):
            messagebox.showwarning("Configure Raid", f"An array named '{name}' is already planned."); return
        if len(chosen) < RAID_MIN[level]:
            messagebox.showwarning("Configure Raid", f"{level.upper()} needs at least {RAID_MIN[level]} disks (you ticked {len(chosen)})."); return
        if RAID_EVEN[level] and len(chosen) % 2 != 0:
            messagebox.showwarning("Configure Raid", f"{level.upper()} needs an EVEN number of disks."); return
        if len({d["controller"] for d in chosen}) != 1:
            messagebox.showwarning("Configure Raid", "All disks in one array must be on the SAME controller."); return
        self.arrays.append({"name": name, "level": level, "disks": chosen, "controller": chosen[0]["controller"]})
        for d in chosen:
            self.assigned[d["fqdd"]] = name
        self.ticked.clear()
        # auto-suggest next name
        self.name.set("vDisk" + str(len(self.arrays) + 1))
        self._fill_disks(); self._fill_arrays()
        self.log(f"Planned {level.upper()} '{name}' on slots {sorted(d['slot'] for d in chosen)}.")

    def remove_array(self):
        if self.busy:
            return
        sel = self.av.selection()
        if not sel:
            return
        name = sel[0]
        self.arrays = [a for a in self.arrays if a["name"] != name]
        self.assigned = {fq: n for fq, n in self.assigned.items() if n != name}
        self._fill_disks(); self._fill_arrays()

    # ---- create ----
    def on_create(self):
        if self.busy:
            return
        if not self.arrays:
            messagebox.showwarning("Configure Raid", "Add at least one array first (tick disks -> pick level -> + Add array)."); return
        if SELFTEST:
            plan = "\n".join(f"  {a['level'].upper()} '{a['name']}' -> slots {sorted(d['slot'] for d in a['disks'])}" for a in self.arrays)
            messagebox.showinfo("Configure Raid (SELFTEST)", "Would create:\n" + plan + "\n\n(Selftest - nothing changed.)"); return
        plan = "\n".join(f"  {a['level'].upper()} '{a['name']}' -> slots {sorted(d['slot'] for d in a['disks'])}" for a in self.arrays)
        if not messagebox.askyesno("Confirm - DESTRUCTIVE",
                                    "Create these arrays?\n\n" + plan + "\n\nThis ERASES the disks and reboots the server. Continue?"):
            return
        self.q.put(("busy", True))
        self.q.put(("status", "Building... (reboots the server, can take many minutes)"))
        ip = self.current_ip(); arrays = [dict(a) for a in self.arrays]
        threading.Thread(target=self._build_worker, args=(ip, arrays), daemon=True).start()

    def _build_worker(self, ip, arrays):
        ok = False
        try:
            ok = build_all(ip, arrays, self.log)
        except Exception as e:
            self.log(f"[{ip}] ERROR: {e}")
        self.q.put(("busy", False))
        self.q.put(("status", "Done - success" if ok else "Done - failed / not verified"))
        self.log(f"[SERVER-OK] {ip}" if ok else f"[SERVER-FAIL] {ip}")
        self.refresh()


def main():
    print("=== Configure Raid (RAID 0/1/5/6/10) - iDRAC9 ===", flush=True)
    if not SELFTEST and not shutil.which("racadm"):
        print("[FATAL] racadm was not found in PATH. Install Dell RACADM / OpenManage tools.", flush=True)
        sys.exit(1)
    ips = load_targets()
    if not ips and not SELFTEST:
        print("[FATAL] No target IP. Type an iDRAC IP in the form.", flush=True)
        sys.exit(1)
    print(f"Targets: {', '.join(ips) if ips else '(selftest)'}", flush=True)
    root = tk.Tk()
    App(root, ips)
    root.mainloop()


if __name__ == "__main__":
    main()
