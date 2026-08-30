# -*- coding: utf-8 -*-
"""
Change the IPv4 address of one or more Dell iDRACs, using racadm.

Modeled on set_idrac_hostname.py, but instead of a hostname the second list is
the NEW iDRAC IP for each server ("iDRAC IP by NDD"):

  addresses.txt  = the CURRENT iDRAC IP(s) we connect to (line N)
  newips.txt     = the NEW iDRAC IP to assign            (line N)

Line N of newips.txt is applied to line N of addresses.txt (first -> first, and
so on), so many iDRACs can be re-addressed in one run. Both lists are already
range-expanded by the app (the form supports "192.168.0.1-192.168.0.3").

netmask.txt / gateway.txt (optional) hold a SINGLE value each, applied to every
server (they share the same subnet), written first so they take effect before
the address change drops our connection.

Every server runs in its OWN parallel session (thread), so N iDRACs are
re-addressed together, not one after another.

Credentials: PSAUTO_USERNAME/PASSWORD (explicit override from the app's UI)
wins; otherwise PSAUTO_DEFAULT_USERNAME/PASSWORD (the app's encrypted .env
default). A non-interactive run with neither fails fast instead of hanging.
"""
import os
import sys
import shutil
import subprocess
import threading
import concurrent.futures

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
MAX_PARALLEL = 15          # cap concurrent sessions so a big fleet can't overwhelm the box
SETTLE_SECONDS = 12        # give the iDRAC a moment to start applying the new address
VERIFY_TIMEOUT_S = 90      # AFTER the settle wait, how long to keep polling the NEW IP

# --- Credentials (never hardcoded; fail fast when non-interactive) ----------
IDRAC_USER = os.environ.get("PSAUTO_USERNAME", "").strip() or os.environ.get("PSAUTO_DEFAULT_USERNAME", "").strip()
IDRAC_PASS = os.environ.get("PSAUTO_PASSWORD", "") or os.environ.get("PSAUTO_DEFAULT_PASSWORD", "")
if not IDRAC_USER or not IDRAC_PASS:
    if sys.stdin is not None and sys.stdin.isatty():
        import getpass
        if not IDRAC_USER:
            IDRAC_USER = input("iDRAC username: ").strip()
        if not IDRAC_PASS:
            IDRAC_PASS = getpass.getpass("iDRAC password: ")
    else:
        print("ERROR: No iDRAC credentials available (defaults not configured) and this run "
              "is non-interactive, so a prompt cannot be shown. Configure the app's default "
              "iDRAC credentials and retry.", flush=True)
        sys.exit(1)

_print_lock = threading.Lock()


def log(ip, msg):
    # Every line is tagged with the (current) server IP so the app can show a
    # per-server view; the lock keeps parallel threads' lines from interleaving
    # mid-line.
    with _print_lock:
        print(f"[{ip}] {msg}", flush=True)


def marker(text):
    # App status markers must NOT be IP-prefixed (parsed at line start).
    with _print_lock:
        print(text, flush=True)


def resolve_racadm():
    exe = shutil.which("racadm")
    if exe:
        return exe
    for candidate in (
        r"C:\Program Files\Dell\SysMgt\rac5\racadm.exe",
        r"C:\Program Files\Dell\SysMgt\iDRAC\racadm.exe",
        r"C:\Program Files (x86)\Dell\SysMgt\rac5\racadm.exe",
    ):
        if os.path.exists(candidate):
            return candidate
    return None


def read_lines(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return [ln.strip() for ln in f if ln.strip()]
    return []


def read_single(path):
    lines = read_lines(path)
    return lines[0] if lines else ""


def racadm_set(racadm, ip, attr, value, timeout=60):
    """Run 'racadm -r <ip> set <attr> <value>'. Returns (ok, output)."""
    try:
        proc = subprocess.run(
            [racadm, "-r", ip, "-u", IDRAC_USER, "-p", IDRAC_PASS,
             "--nocertwarn", "set", attr, value],
            capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return proc.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "racadm timed out"
    except Exception as e:
        return False, str(e)


def new_ip_reachable(racadm, new_ip):
    """The real success signal: after the address change, can we reach the
    iDRAC at its NEW IP? (The 'set address' command drops our old connection,
    so its own exit code is unreliable - this is what actually confirms it.)"""
    try:
        proc = subprocess.run(
            [racadm, "-r", new_ip, "-u", IDRAC_USER, "-p", IDRAC_PASS,
             "--nocertwarn", "getsysinfo"],
            capture_output=True, text=True, timeout=25,
            creationflags=CREATE_NO_WINDOW
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).lower()
        return proc.returncode == 0 and "error" not in out and "unable to connect" not in out
    except Exception:
        return False


def change_one(racadm, current_ip, new_ip, netmask, gateway):
    """Full flow for ONE iDRAC. Returns a result dict."""
    import time
    marker(f"[SERVER-START] {current_ip}")
    result = {"current": current_ip, "new": new_ip, "ok": False, "detail": ""}
    try:
        log(current_ip, f"Changing iDRAC IP  {current_ip}  ->  {new_ip}")

        # 1) Netmask + Gateway FIRST (they don't drop the current connection),
        #    so they're already in place when the address flips.
        if netmask:
            ok, out = racadm_set(racadm, current_ip, "iDRAC.IPv4.Netmask", netmask)
            log(current_ip, f"set Netmask {netmask}: {'OK' if ok else 'FAILED'} {out}")
        if gateway:
            ok, out = racadm_set(racadm, current_ip, "iDRAC.IPv4.Gateway", gateway)
            log(current_ip, f"set Gateway {gateway}: {'OK' if ok else 'FAILED'} {out}")

        # 2) Address LAST - this is what drops the connection to the old IP.
        set_ok, set_out = racadm_set(racadm, current_ip, "iDRAC.IPv4.Address", new_ip)
        log(current_ip, f"set Address {new_ip}: {set_out}")

        # 3) Verify by reaching the NEW IP (retry while the iDRAC re-homes its
        #    NIC). Success is defined by the new IP answering - NOT by the set
        #    command's exit code, which is unreliable once the link drops.
        #    A short settle wait first, THEN the full poll window (deadline set
        #    after the settle so the timeout budget isn't eaten by it).
        log(current_ip, f"Waiting for {new_ip} to come up...")
        time.sleep(SETTLE_SECONDS)
        deadline = time.time() + VERIFY_TIMEOUT_S
        reached = False
        while True:
            if new_ip_reachable(racadm, new_ip):
                reached = True
                break
            if time.time() >= deadline:
                break
            time.sleep(8)

        if reached:
            log(current_ip, f"OK: iDRAC is now reachable at {new_ip}.")
            result["ok"] = True
            result["detail"] = "OK"
            marker(f"[SERVER-OK] {current_ip}")
        else:
            msg = (f"new IP {new_ip} did not respond within {VERIFY_TIMEOUT_S}s "
                   f"(set command said: {set_out or 'no output'})")
            log(current_ip, f"FAILED: {msg}")
            result["detail"] = msg
            marker(f"[SERVER-FAIL] {current_ip}|{msg}")
    except Exception as e:
        result["detail"] = str(e)
        log(current_ip, f"ERROR: {e}")
        marker(f"[SERVER-FAIL] {current_ip}|{e}")
    return result


def main():
    print("=== Change iDRAC IP (racadm set iDRAC.IPv4.Address) ===", flush=True)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    current_ips = read_lines(os.path.join(script_dir, "addresses.txt"))
    new_ips = read_lines(os.path.join(script_dir, "newips.txt"))
    netmask = read_single(os.path.join(script_dir, "netmask.txt"))
    gateway = read_single(os.path.join(script_dir, "gateway.txt"))

    if not current_ips:
        print("ERROR: No current iDRAC IPs provided (addresses.txt is empty).", flush=True)
        sys.exit(1)
    if not new_ips:
        print("ERROR: No new iDRAC IPs provided (newips.txt is empty).", flush=True)
        sys.exit(1)
    if len(current_ips) != len(new_ips):
        print(f"ERROR: Count mismatch - {len(current_ips)} current IP(s) but {len(new_ips)} new IP(s).", flush=True)
        print("The current-IP list and the new-IP list must have the same number of lines.", flush=True)
        sys.exit(1)

    racadm = resolve_racadm()
    if not racadm:
        print("ERROR: racadm.exe not found in PATH or standard Dell install locations.", flush=True)
        print("Install Dell OpenManage / racadm, or add racadm.exe to PATH.", flush=True)
        sys.exit(1)

    print(f"racadm       : {racadm}")
    print(f"iDRAC user   : {IDRAC_USER}")
    print(f"Netmask      : {netmask or '(unchanged)'}")
    print(f"Gateway      : {gateway or '(unchanged)'}")
    print(f"Pairs        : {len(current_ips)}   (each runs in its own parallel session)\n", flush=True)
    print(f"[TOTAL-SERVERS] {len(current_ips)}", flush=True)

    results = []
    workers = min(MAX_PARALLEL, len(current_ips))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [
            ex.submit(change_one, racadm, cur, new, netmask, gateway)
            for cur, new in zip(current_ips, new_ips)
        ]
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())

    ok = sum(1 for r in results if r["ok"])
    fail = len(results) - ok
    print("\n" + "=" * 60, flush=True)
    print(" FINAL SUMMARY (all servers)", flush=True)
    print("=" * 60, flush=True)
    # keep the original input order in the printed summary
    order = {ip: i for i, ip in enumerate(current_ips)}
    for r in sorted(results, key=lambda r: order.get(r["current"], 0)):
        status = "OK  " if r["ok"] else "FAIL"
        print(f"  {status}  {r['current']}  ->  {r['new']}   {'' if r['ok'] else '- ' + r['detail']}", flush=True)
    print("=" * 60, flush=True)
    print(f" Done. Success: {ok}, Failed: {fail}, Total: {len(results)}", flush=True)

    if sys.stdin is not None and sys.stdin.isatty():
        input("Press Enter to exit...")

    # Non-zero exit only if EVERY server failed.
    if ok == 0 and fail > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
