# -*- coding: utf-8 -*-
"""
Rename one or more Dell iDRACs by setting iDRAC.NIC.DNSRacName from a parallel
list of IPs and hostnames.

Reads addresses.txt (IPs) and hostnames.txt (names), both written by PS
Automation from the two lists typed in the form. Line N in hostnames.txt is
applied to line N in addresses.txt (first name -> first IP, and so on), so many
servers can be renamed in one run.

This sets the iDRAC DNS name (iDRAC.NIC.DNSRacName) - the name that shows up as
the iDRAC hostname (e.g. in the iDRAC report). iDRAC credentials use the app's
configured default (IDRAC_USER / IDRAC_PASS are resolved below - never
hardcoded in this file).
"""
import os
import sys
import shutil
import subprocess

# Never hardcoded: PSAUTO_USERNAME/PASSWORD (explicit override from the app's
# UI) wins; otherwise PSAUTO_DEFAULT_USERNAME/PASSWORD (the app's encrypted
# .env default) is used; a standalone run with neither set prompts instead.
IDRAC_USER = os.environ.get("PSAUTO_USERNAME", "").strip() or os.environ.get("PSAUTO_DEFAULT_USERNAME", "").strip()
IDRAC_PASS = os.environ.get("PSAUTO_PASSWORD", "") or os.environ.get("PSAUTO_DEFAULT_PASSWORD", "")
if not IDRAC_USER or not IDRAC_PASS:
    import getpass
    if not IDRAC_USER:
        IDRAC_USER = input("iDRAC username: ").strip()
    if not IDRAC_PASS:
        IDRAC_PASS = getpass.getpass("iDRAC password: ")


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


def main():
    print("=== Set iDRAC Hostname (iDRAC.NIC.DNSRacName) ===", flush=True)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    servers = read_lines(os.path.join(script_dir, "addresses.txt"))
    names = read_lines(os.path.join(script_dir, "hostnames.txt"))

    if not servers:
        print("ERROR: No IPs provided (addresses.txt is empty).", flush=True)
        sys.exit(1)
    if not names:
        print("ERROR: No hostnames provided (hostnames.txt is empty).", flush=True)
        sys.exit(1)
    if len(servers) != len(names):
        print(f"ERROR: Count mismatch - {len(servers)} IP(s) but {len(names)} hostname(s).", flush=True)
        print("The IP list and the hostname list must have the same number of lines.", flush=True)
        sys.exit(1)

    racadm = resolve_racadm()
    if not racadm:
        print("ERROR: racadm.exe not found in PATH or standard Dell install locations.", flush=True)
        print("Install Dell OpenManage / racadm, or add racadm.exe to PATH.", flush=True)
        sys.exit(1)

    print(f"racadm       : {racadm}")
    print(f"iDRAC user   : {IDRAC_USER}")
    print(f"Pairs        : {len(servers)}\n", flush=True)

    ok = 0
    fail = 0
    for ip, name in zip(servers, names):
        print("-" * 50, flush=True)
        print(f"Setting {ip}  ->  hostname '{name}' ...", flush=True)
        try:
            proc = subprocess.run(
                [racadm, "-r", ip, "-u", IDRAC_USER, "-p", IDRAC_PASS,
                 "--nocertwarn", "set", "iDRAC.NIC.DNSRacName", name],
                capture_output=True, text=True, timeout=120
            )
            for stream in (proc.stdout, proc.stderr):
                if stream and stream.strip():
                    print("  " + stream.strip().replace("\n", "\n  "), flush=True)
            if proc.returncode == 0:
                print(f"OK: {ip} hostname set to '{name}'.", flush=True)
                ok += 1
            else:
                print(f"ERROR: racadm exited with code {proc.returncode} for {ip}.", flush=True)
                fail += 1
        except subprocess.TimeoutExpired:
            print(f"ERROR: racadm timed out for {ip}.", flush=True)
            fail += 1
        except Exception as e:
            print(f"ERROR on {ip}: {e}", flush=True)
            fail += 1

    print("\n" + "=" * 50, flush=True)
    print(f" Done. Success: {ok}, Failed: {fail}, Total: {len(servers)}", flush=True)
    print("=" * 50, flush=True)

    if sys.stdin is not None and sys.stdin.isatty():
        input("Press Enter to exit...")

    if fail > 0 and ok == 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
