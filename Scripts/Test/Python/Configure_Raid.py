# -*- coding: utf-8 -*-
"""
Configure Raid - interactive RAID builder for Dell iDRAC9
=========================================================
Flow:
  1. You type the iDRAC IP(s) + username/password in the form and run.
  2. The tool PINGs each target, then reads every physical disk (slot, size,
     media, bus, state).
  3. If any disk is Non-RAID, the RAID-building controls stay LOCKED and a
     "Convert Non-RAID -> Ready" button lights up. Clicking it converts those
     disks, commits + reboots if needed, waits for the iDRAC to come back, and
     automatically RE-READS the disks. Only once every disk shows a ready
     state do the RAID controls unlock - exactly "read -> convert if needed ->
     re-check -> only then build".
  4. You build ONE OR MORE arrays in the same session, e.g.
        disks 1,2 -> RAID 1     disks 3,5,6,7,8,9,10 -> RAID 5
     A disk assigned to one array can NOT be picked again for another
     (it's locked in the table), so there's never any overlap/confusion.
  5. "Create all arrays" builds them. Dell's storage controller supports
     staging MULTIPLE pending virtual-disk-create operations on the SAME
     controller and realizing them all with a SINGLE configuration job/reboot
     (confirmed in Dell's iDRAC9 documentation on "stacked" pending storage
     operations) - so by default every planned array is staged together and
     applied with ONE reboot ("in parallel", server-time-wise). Only if the
     controller genuinely rejects staging one (a real "another job already
     committed" response - not a made-up problem) does that array get
     DEFERRED to the next round: apply what's already staged, wait, then
     retry the deferred one(s) - i.e. sequential, but only when parallel
     truly isn't possible.

RAID levels: RAID 0, RAID 1, RAID 5, RAID 6, RAID 10.
Credentials: PSAUTO_USERNAME/PASSWORD (form) -> PSAUTO_DEFAULT_* (.env) ->
             prompt when standalone. Never hardcoded.
Targets    : the app writes the IP(s) to addresses.txt next to this script;
             the "Server" dropdown switches between them.

Set PSAUTO_RAID_SELFTEST=1 to open the GUI with SAMPLE disks (no connection) -
one of the sample disks starts Non-RAID so you can try the convert step too.

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
import concurrent.futures

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


def _bring_to_front(root):
    """Forces this Tk window to the foreground even though it was launched by a
    background subprocess (the app's runner). A bare lift()/-topmost is often
    NOT enough on Windows: the OS normally blocks a background process from
    stealing focus from whatever window (e.g. the browser) is currently active.
    The reliable fix is the standard AttachThreadInput trick - temporarily
    "borrowing" the foreground window's input thread so SetForegroundWindow is
    allowed to succeed - falling back to the plain Tk calls on any other OS
    or if the Win32 call fails for any reason."""
    try:
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.focus_force()
        root.after(400, lambda: root.attributes("-topmost", False))
    except Exception:
        pass
    if not platform.system().lower().startswith("win"):
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        SW_RESTORE = 9
        user32.ShowWindow(hwnd, SW_RESTORE)
        fg = user32.GetForegroundWindow()
        if fg and fg != hwnd:
            fg_tid = user32.GetWindowThreadProcessId(fg, None)
            cur_tid = kernel32.GetCurrentThreadId()
            user32.AttachThreadInput(cur_tid, fg_tid, True)
            try:
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            finally:
                user32.AttachThreadInput(cur_tid, fg_tid, False)
        else:
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


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
def racadm(ip, *args, timeout=120):
    cmd = ["racadm", "-r", ip, "-u", USER, "-p", PASS, "--nocertwarn"] + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or ""))
    except Exception as e:
        return 1, str(e)


def controller_of(fqdd):
    return fqdd.rsplit(":", 1)[-1] if ":" in fqdd else ""


def slot_of(fqdd):
    m = re.search(r"Disk\.Bay\.(\d+)", fqdd)
    return int(m.group(1)) if m else -1


# Property name is matched loosely (case-insensitive, "Media Type" / "MediaType" /
# "Bus Protocol" / "BusProtocol" all match) because Dell racadm's exact label
# spacing/casing has varied across firmware/OpenManage versions - this is what
# actually failed silently in the field (State/Size showed "?" for every disk).
def _val(block, *keys):
    for key in keys:
        pattern = r"(?im)^\s*" + re.escape(key).replace(r"\ ", r"\s*") + r"\s*=\s*(.+?)\s*$"
        m = re.search(pattern, block)
        if m:
            return m.group(1).strip()
    return ""


_SIZE_KEYS = ("Size", "SizeInBytes", "Size In Bytes", "Capacity", "CapacityInBytes",
              "Capacity In Bytes", "MediaSize", "Media Size", "RaidSize", "Raid Size")


def _extract_size(block):
    """Returns (display_string, size_gb). Tries every known Dell property name
    for a disk's size, in order. A value that already carries a unit (GB/TB/MB)
    is used as-is; a bare large integer (no unit) is treated as a byte count -
    some racadm output reports SizeInBytes/CapacityInBytes with no suffix at
    all, which is what showed as a missing size even though State/Media parsed."""
    for key in _SIZE_KEYS:
        raw = _val(block, key)
        if not raw:
            continue
        if re.search(r"(?i)\b(TB|GB|MB)\b", raw):
            m = re.search(r"([\d.]+)\s*(?i:(TB|GB|MB))", raw)
            if m:
                n, unit = float(m.group(1)), m.group(2).upper()
                gb = n * 1000 if unit == "TB" else (n / 1000 if unit == "MB" else n)
                return raw, gb
            return raw, 0.0
        m = re.search(r"(\d+)", raw)
        if m:
            n = int(m.group(1))
            if n > 10_000_000:   # a bare integer this large is a byte count, not GB
                gb = n / (1000 ** 3)
                return ((f"{gb / 1000:.2f} TB" if gb >= 1000 else f"{gb:.2f} GB"), gb)
            return raw, float(n)   # already a plain GB number with no unit text
    return "", 0.0


def _parse_disk(f, block):
    size, size_gb = _extract_size(block)
    return {
        "fqdd": f, "slot": slot_of(f), "controller": controller_of(f),
        "size": size or "?", "size_gb": size_gb,
        "media": _val(block, "MediaType", "Media Type", "Media") or "",
        "proto": _val(block, "BusProtocol", "Bus Protocol", "Bus") or "",
        "state": _val(block, "RaidStatus", "Raid Status", "State") or "?",
        "model": _val(block, "Model", "ProductID", "DeviceDescription", "Device Description") or "",
        "_raw": block,   # kept only for the one-time diagnostic dump below
    }


_FQDD_RE = re.compile(r"Disk\.Bay\.\d+:[^\s\r\n]+")

# Bounded worker count for CONCURRENT per-disk detail calls - each racadm
# invocation is its own network+SSL session to the iDRAC (not CPU-bound), so
# running several at once is what actually fixes "reading disks looks stuck":
# on a 24-disk server, 24 sequential ~3-8s calls could take minutes; 6 at a
# time cuts that to roughly a sixth. Capped at 6 so the iDRAC's out-of-band
# controller (which only handles a few concurrent racadm sessions) isn't
# overloaded into refusing connections.
_MAX_PARALLEL_READS = 6


def read_disks(ip, log=None):
    """Reads EVERY physical disk, however many there are - never a fixed count.

    Per disk, tries TWO command shapes and keeps whichever actually returned
    properties:
      1) `storage get pdisks:<FQDD>`      - no -o. This is the EXACT call the
         proven, already-working Configure_Raid1.ps1 uses for per-disk detail.
      2) `storage get pdisks:<FQDD> -o`   - the "full output" variant, tried
         only if (1) didn't yield Size/State (some firmware needs it instead).
    Both run concurrently across a small worker pool - correct AND fast.
    If NEITHER shape yields Size/State for some disk, its raw racadm output is
    dumped to the console (once) so the exact real property names/format are
    visible instead of guessing again.
    """
    def _log(m):
        if log:
            log(m)

    rc, lst = racadm(ip, "storage", "get", "pdisks", timeout=90)
    if re.search(r"(?i)error|login failed|unable to connect|authentication|timed out|RAC\d{3,}", lst) and not _FQDD_RE.search(lst):
        _log(f"[{ip}] racadm error while listing disks: {lst.strip()[:220]}")
        return []
    fqdds = sorted(set(_FQDD_RE.findall(lst)), key=lambda f: (controller_of(f), slot_of(f)))
    if not fqdds:
        _log(f"[{ip}] No physical disks reported by racadm.")
        return []
    _log(f"[{ip}] {len(fqdds)} disk(s) listed. Reading details ({min(_MAX_PARALLEL_READS, len(fqdds))} at a time)...")

    results = {}
    lock = threading.Lock()
    done_count = [0]
    diag_done = [False]

    def _one(f):
        rc1, d1 = racadm(ip, "storage", "get", "pdisks:" + f, timeout=90)
        parsed = _parse_disk(f, d1)
        raw_for_diag = d1
        if parsed["size"] == "?" or parsed["state"] == "?":
            rc2, d2 = racadm(ip, "storage", "get", "pdisks:" + f, "-o", timeout=90)
            parsed2 = _parse_disk(f, d2)
            # keep whichever attempt resolved more fields
            better = (int(parsed2["size"] != "?") + int(parsed2["state"] != "?")) > \
                     (int(parsed["size"] != "?") + int(parsed["state"] != "?"))
            if better:
                parsed, raw_for_diag = parsed2, d2
            elif parsed["size"] == "?" and parsed["state"] == "?":
                raw_for_diag = d1 + "\n----- (with -o) -----\n" + d2
        parsed["_raw"] = raw_for_diag
        with lock:
            done_count[0] += 1
            _log(f"[{ip}]   read {done_count[0]}/{len(fqdds)} (slot {slot_of(f)})")
        return f, parsed

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL_READS, len(fqdds))) as pool:
        for f, parsed in pool.map(_one, fqdds):
            results[f] = parsed

    disks = [results[f] for f in fqdds if f in results]
    disks.sort(key=lambda x: (x["controller"], x["slot"]))

    # Diagnostic dump: the FIRST disk where size is still unresolved gets its
    # complete raw racadm output printed line by line (not squashed), so the
    # exact real property names/format are visible - copy these lines back if
    # sizes are still missing so the parser can be fixed for certain.
    unresolved = next((d for d in disks if d["size"] == "?"), None)
    if unresolved and not diag_done[0]:
        diag_done[0] = True
        _log(f"[{ip}] ===== DIAGNOSTIC: raw racadm output for slot {unresolved['slot']} (size not recognized) =====")
        for line in unresolved["_raw"].splitlines():
            if line.strip():
                _log(f"[{ip}]   {line.strip()}")
        _log(f"[{ip}] ===== END DIAGNOSTIC - please copy the lines above if size is still missing =====")
    for d in disks:
        d.pop("_raw", None)

    _log(f"[{ip}] Found {len(disks)} disk(s) total.")
    return disks


SAMPLE_DISKS = [
    {"fqdd": f"Disk.Bay.{i}:Enclosure.Internal.0-1:RAID.Integrated.1-1", "slot": i,
     "controller": "RAID.Integrated.1-1",
     "size": ("460.00 GB" if i < 2 else "1.09 TB"), "size_gb": (460.0 if i < 2 else 1090.0),
     "media": ("HDD" if i < 4 else "SSD"), "proto": ("SAS" if i < 4 else "SATA"),
     "state": ("Non-RAID" if i == 3 else "Ready"),
     "model": ("SEAGATE ST600MM" if i < 4 else "INTEL SSDSC2KB01")}
    for i in range(10)
]


# =====================================================================
# Build flow: convert -> stage (parallel, sequential fallback) -> verify
# =====================================================================
def _accepted(out):
    return bool(re.search(r"(?i)success|successful|JID_|created|pending|committed|already", out))


def _is_job_conflict(out):
    """A REAL blocker (matches Configure_Raid1.ps1's Test-OutputMeansAnotherJobExists):
    a previous storage config job is still committed/pending on the controller,
    so a NEW pending op can't be staged yet. This is the ONLY situation where
    build_all() falls back to sequential (apply what's staged, then retry) -
    every other rejection is a real failure, not "try again later". Deliberately
    does NOT match the bare word "pending" - createvd's normal SUCCESS output
    says "pending" (the vdisk is pending until applied)."""
    return bool(re.search(r"(?i)STOR023|already been committed|A configuration has already been committed|Configuration already committed|already exists", out))


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


def convert_non_raid_to_ready(ip, disks, log):
    """Converts every Non-RAID disk in `disks` to RAID-capable, commits, reboots,
    and waits until they actually show as ready. Returns True once nothing is
    Non-RAID anymore (or there was nothing to convert), False on real failure
    or timeout. This is the explicit, visible "make disks Ready" step the GUI
    button runs - the SAME logic also runs as a defensive fallback at the start
    of build_all(), in case disk state changed since the last read."""
    nonraid = [d for d in disks if "non" in (d["state"] or "").lower()]
    if not nonraid:
        log(f"[{ip}] No Non-RAID disks - nothing to convert.")
        return True
    controllers = sorted({d["controller"] for d in nonraid})
    log(f"[{ip}] Converting {len(nonraid)} Non-RAID disk(s) to RAID-capable: slots {sorted(d['slot'] for d in nonraid)}...")
    for d in nonraid:
        rc, out = racadm(ip, "storage", "converttoRAID:" + d["fqdd"])
        if rc != 0 and not _accepted(out):
            log(f"[{ip}] converttoRAID failed for slot {d['slot']}: {out.strip()[:160]}")
            return False
    if not _commit_controllers(ip, controllers, "convert to RAID", log):
        return False
    log(f"[{ip}] Verifying disks are now RAID-capable...")
    deadline = time.time() + 45 * 60
    while time.time() < deadline:
        if all("non" not in _disk_state(ip, d["fqdd"]) for d in nonraid):
            log(f"[{ip}] All disks are now RAID-capable / Ready.")
            return True
        time.sleep(30)
    log(f"[{ip}] Timed out waiting for disks to become RAID-capable.")
    return False


def build_all(ip, arrays, log):
    """Builds every planned array. Stages ALL createvd operations on a
    controller together and applies them with ONE combined job + reboot
    whenever the controller accepts it - Dell's iDRAC9 documentation confirms
    multiple pending storage operations can be "stacked" and realized by a
    single configuration job, so this is the normal, fast path ("parallel":
    every array gets built in the same reboot cycle). Only if the controller
    genuinely rejects staging one - a real "another job already committed"
    response, detected by _is_job_conflict() - does that array get DEFERRED to
    the next round: apply what's already staged, wait for it, then retry the
    deferred one(s). So arrays only ever get built one-after-another when the
    hardware truly can't take them together; otherwise everything ships with
    a single reboot.
    """
    all_disks = [d for a in arrays for d in a["disks"]]
    if not convert_non_raid_to_ready(ip, all_disks, log):
        log(f"[{ip}] Could not make all disks RAID-capable - aborting.")
        return False

    remaining = list(arrays)
    round_num = 0
    all_ok = True
    while remaining:
        round_num += 1
        label = "" if round_num == 1 else f" (round {round_num}, after previous job applied)"
        staged, deferred = [], []

        for a in remaining:
            pdkey = ",".join(d["fqdd"] for d in a["disks"])
            log(f"[{ip}] Staging {a['level'].upper()} '{a['name']}' on slots {sorted(d['slot'] for d in a['disks'])}{label}...")
            rc, out = racadm(ip, "storage", "createvd:" + a["controller"], "-rl", a["level"], "-pdkey:" + pdkey, "-name", a["name"])
            if _is_job_conflict(out):
                log(f"[{ip}] '{a['name']}' can't be staged yet (another job pending on the controller) - will apply what's ready first, then retry.")
                deferred.append(a)
            elif rc == 0 or _accepted(out):
                staged.append(a)
            else:
                log(f"[{ip}] createvd failed for '{a['name']}': {out.strip()[:200]}")
                return False

        if not staged:
            log(f"[{ip}] No array could be staged this round - stopping.")
            return False

        ctrls = sorted({a["controller"] for a in staged})
        together = f" together ({len(staged)} array(s), one reboot)" if len(staged) > 1 else ""
        log(f"[{ip}] Applying{together}{label}...")
        if not _commit_controllers(ip, ctrls, "create virtual disks", log):
            return False

        pending = {a["name"] for a in staged}
        deadline = time.time() + 45 * 60
        while pending and time.time() < deadline:
            for name in list(pending):
                if _verify_vdisk(ip, name):
                    log(f"[{ip}] {name} is online.")
                    pending.discard(name)
            if pending:
                time.sleep(30)
        if pending:
            all_ok = False
            log(f"[{ip}] Not verified in time: {', '.join(pending)}")

        remaining = deferred

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
        root.geometry("980x800")

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

        # ---- readiness gate: must convert Non-RAID -> Ready before RAID unlocks ----
        gate = ttk.Frame(root, padding=(12, 2)); gate.pack(fill="x")
        self.gate_label = ttk.Label(gate, text="", foreground="#b8860b")
        self.gate_label.pack(side="left")
        self.convert_btn = ttk.Button(gate, text="Convert Non-RAID → Ready", command=self.on_convert)
        self.convert_btn.pack(side="left", padx=10)

        build = ttk.LabelFrame(root, text="Add an array from the ticked disks (locked until disks are Ready)", padding=8)
        build.pack(fill="x", padx=12, pady=(6, 2))
        self.level_radios = []
        for code, label, _mn, _ev in RAID_LEVELS:
            rb = ttk.Radiobutton(build, text=label, value=code, variable=self.level)
            rb.pack(side="left", padx=6)
            self.level_radios.append(rb)
        ttk.Label(build, text="Name:").pack(side="left", padx=(14, 2))
        self.name_entry = ttk.Entry(build, textvariable=self.name, width=16)
        self.name_entry.pack(side="left")
        self.add_array_btn = ttk.Button(build, text="+ Add array", command=self.add_array)
        self.add_array_btn.pack(side="left", padx=10)

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
        ttk.Label(arow, text="(all arrays are built together with one reboot when possible)", foreground="#7c8aa0").pack(side="left", padx=6)

        self.logbox = tk.Text(root, height=10, bg="#0b1020", fg="#d5dee8", insertbackground="#d5dee8", wrap="word")
        self.logbox.pack(fill="both", expand=True, padx=12, pady=(6, 10))

        # Make sure the window actually pops to the front. This is launched as a
        # background subprocess by the app, so a plain lift()/-topmost is often
        # not enough on Windows (a background process is normally blocked from
        # stealing focus from whatever window - e.g. the browser - is active) -
        # see _bring_to_front() for the real fix.
        root.after(80, lambda: _bring_to_front(root))

        self._update_gate()
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
                    self.disks = payload; self._fill_disks(); self._update_gate()
                elif kind == "status":
                    self.status.config(text=payload)
                elif kind == "busy":
                    self.busy = payload; self._update_gate()
                elif kind == "do_refresh":
                    # Routed through the queue (instead of calling self.refresh()
                    # directly from a background thread) so every Tkinter widget
                    # touch happens on the main thread via this polling loop.
                    self.refresh()
        except queue.Empty:
            pass
        self.root.after(150, self._drain)

    def current_ip(self):
        return (self.ips[self.server.current()] if self.ips else "")

    # ---- readiness gate ----
    def _update_gate(self):
        non_raid = [d for d in self.disks if "non" in (d.get("state") or "").lower()]
        ready = bool(self.disks) and not non_raid
        if non_raid:
            self.gate_label.config(text=f"{len(non_raid)} disk(s) are Non-RAID - convert them to Ready before building a RAID array.")
        elif self.disks:
            self.gate_label.config(text="All disks are RAID-capable - ready to build.")
        else:
            self.gate_label.config(text="")
        self.convert_btn.config(state=("normal" if (non_raid and not self.busy) else "disabled"))
        build_state = "normal" if (ready and not self.busy) else "disabled"
        for rb in self.level_radios:
            rb.config(state=build_state)
        self.name_entry.config(state=build_state)
        self.add_array_btn.config(state=build_state)
        self.create_btn.config(state=("normal" if (ready and not self.busy and self.arrays) else "disabled"))

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
            self.log("[SELFTEST] Showing sample disks (one Non-RAID, to try the Convert step).")
            self.q.put(("status", f"{len(SAMPLE_DISKS)} disk(s) - SELFTEST"))
            return
        ip = self.current_ip()
        ok = ping(ip)
        self.log(f"[{ip}] Ping: {'reachable' if ok else 'NO reply (continuing - iDRAC may block ping)'}")
        self.log(f"[{ip}] Reading physical disks via racadm...")
        try:
            disks = read_disks(ip, self.log)
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

    # ---- convert Non-RAID -> Ready (explicit, visible step) ----
    def on_convert(self):
        if self.busy:
            return
        non_raid = [d for d in self.disks if "non" in (d.get("state") or "").lower()]
        if not non_raid:
            return
        slots = sorted(d["slot"] for d in non_raid)
        if not messagebox.askyesno(
            "Convert to Ready",
            f"Convert {len(non_raid)} Non-RAID disk(s) (slots {slots}) to RAID-capable?\n\n"
            "This reboots the server. It does NOT erase data - it only changes the "
            "disk's mode so a RAID can be built on it. Disks are re-read automatically when done."
        ):
            return
        self.q.put(("busy", True))
        self.q.put(("status", "Converting Non-RAID disks to Ready... (reboots the server)"))
        ip = self.current_ip()
        threading.Thread(target=self._convert_worker, args=(ip, non_raid), daemon=True).start()

    def _convert_worker(self, ip, non_raid):
        if SELFTEST:
            self.log("[SELFTEST] Simulating conversion to Ready (no real server contacted)...")
            time.sleep(1)
            for d in self.disks:
                if "non" in (d.get("state") or "").lower():
                    d["state"] = "Ready"
            self.q.put(("disks", list(self.disks)))
            self.q.put(("status", "SELFTEST: disks are now Ready"))
            self.q.put(("busy", False))
            return
        ok = False
        try:
            ok = convert_non_raid_to_ready(ip, non_raid, self.log)
        except Exception as e:
            self.log(f"[{ip}] ERROR: {e}")
        self.q.put(("busy", False))
        self.q.put(("status", "Conversion done - re-reading disks..." if ok else "Conversion failed - see log above"))
        self.q.put(("do_refresh", None))  # per the requested flow: read the disks again automatically

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
        self._fill_disks(); self._fill_arrays(); self._update_gate()
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
        self._fill_disks(); self._fill_arrays(); self._update_gate()

    # ---- create ----
    def on_create(self):
        if self.busy:
            return
        if not self.arrays:
            messagebox.showwarning("Configure Raid", "Add at least one array first (tick disks -> pick level -> + Add array)."); return
        plan = "\n".join(f"  {a['level'].upper()} '{a['name']}' -> slots {sorted(d['slot'] for d in a['disks'])}" for a in self.arrays)
        if SELFTEST:
            messagebox.showinfo("Configure Raid (SELFTEST)", "Would create:\n" + plan + "\n\n(Selftest - nothing changed.)"); return
        if not messagebox.askyesno(
            "Confirm - DESTRUCTIVE",
            "Create these arrays?\n\n" + plan +
            "\n\nAll arrays are staged together and applied with ONE reboot whenever the "
            "controller allows it; only if it genuinely can't, some are built one after another.\n\n"
            "This ERASES the selected disks and reboots the server. Continue?"
        ):
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
        self.q.put(("do_refresh", None))


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
