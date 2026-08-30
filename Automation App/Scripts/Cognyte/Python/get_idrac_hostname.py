# -*- coding: utf-8 -*-
"""
Read the CURRENT iDRAC.NIC.DNSRacName (the "DNS iDRAC Name" field) from one or
more Dell iDRACs - a quick way to confirm a hostname change (set_idrac_hostname.py
/ Set-iDRAC-Hostname.ps1) actually took effect, without opening the iDRAC web UI.

Reads addresses.txt (one IP per line, written by PS Automation from the IP list
typed in the form). iDRAC credentials use the app's configured default
(IDRAC_USER / IDRAC_PASS are resolved below - never hardcoded in this file).

Note: this reads iDRAC.NIC.DNSRacName specifically - the iDRAC's own DNS
registration name. It is NOT the same field as the hostname banner shown in the
iDRAC web GUI/browser tab title, which is reported by the server's operating
system (System.ServerOS.HostName) and cannot be set remotely via racadm.
"""
import os
import sys
import shutil
import subprocess

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


def parse_dnsracname(racadm_output):
    """racadm's "get" output looks like:
        [Key=iDRAC.Embedded.1#NIC.1]
        DNSRacName=kafka18
    Pull out the value after "DNSRacName=" (case-insensitive, tolerant of
    trailing whitespace/CR)."""
    for line in racadm_output.splitlines():
        line = line.strip()
        if line.lower().startswith("dnsracname="):
            return line.split("=", 1)[1].strip()
    return None


def main():
    print("=== Get iDRAC Hostname (iDRAC.NIC.DNSRacName) ===", flush=True)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    servers = read_lines(os.path.join(script_dir, "addresses.txt"))

    if not servers:
        print("ERROR: No IPs provided (addresses.txt is empty).", flush=True)
        sys.exit(1)

    racadm = resolve_racadm()
    if not racadm:
        print("ERROR: racadm.exe not found in PATH or standard Dell install locations.", flush=True)
        print("Install Dell OpenManage / racadm, or add racadm.exe to PATH.", flush=True)
        sys.exit(1)

    print(f"racadm       : {racadm}")
    print(f"iDRAC user   : {IDRAC_USER}")
    print(f"Targets      : {len(servers)}\n", flush=True)

    results = []  # (ip, name_or_None, error_or_None)
    for ip in servers:
        print("-" * 50, flush=True)
        print(f"Reading hostname on {ip} ...", flush=True)
        try:
            proc = subprocess.run(
                [racadm, "-r", ip, "-u", IDRAC_USER, "-p", IDRAC_PASS,
                 "--nocertwarn", "get", "iDRAC.NIC.DNSRacName"],
                capture_output=True, text=True, timeout=120
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            if output.strip():
                print("  " + output.strip().replace("\n", "\n  "), flush=True)

            if proc.returncode != 0:
                print(f"ERROR: racadm exited with code {proc.returncode} for {ip}.", flush=True)
                results.append((ip, None, f"racadm exit code {proc.returncode}"))
                continue

            name = parse_dnsracname(output)
            if name is None:
                print(f"ERROR: Could not find DNSRacName in racadm output for {ip}.", flush=True)
                results.append((ip, None, "DNSRacName not found in output"))
            else:
                print(f"OK: {ip} current hostname is '{name}'.", flush=True)
                results.append((ip, name, None))
        except subprocess.TimeoutExpired:
            print(f"ERROR: racadm timed out for {ip}.", flush=True)
            results.append((ip, None, "timed out"))
        except Exception as e:
            print(f"ERROR on {ip}: {e}", flush=True)
            results.append((ip, None, str(e)))

    ok = sum(1 for _, name, _ in results if name is not None)
    fail = len(results) - ok

    print("\n" + "=" * 70, flush=True)
    print(" CURRENT iDRAC HOSTNAMES", flush=True)
    print("=" * 70, flush=True)
    print(f"{'IP Address':<20} | {'Hostname (DNSRacName)':<30} | Status", flush=True)
    print("-" * 70, flush=True)
    for ip, name, error in results:
        if name is not None:
            print(f"{ip:<20} | {name:<30} | OK", flush=True)
        else:
            print(f"{ip:<20} | {'-':<30} | Failed ({error})", flush=True)
    print("=" * 70, flush=True)
    print(f" Done. Success: {ok}, Failed: {fail}, Total: {len(results)}", flush=True)
    print("=" * 70, flush=True)

    if sys.stdin is not None and sys.stdin.isatty():
        input("Press Enter to exit...")

    # Non-zero exit only if EVERY target failed (partial success is still a
    # success for the run as a whole).
    if fail > 0 and ok == 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
