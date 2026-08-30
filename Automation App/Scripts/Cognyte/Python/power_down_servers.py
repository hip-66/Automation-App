# -*- coding: utf-8 -*-
"""
Power down one or more Dell servers by iDRAC IP, using
"racadm serveraction powerdown".

Targets are read from addresses.txt, which PS Automation writes from the IP list
OR the range (start + count) chosen in the form - either way this script just
reads one IP per line. For manual runs from a console it falls back to prompting
for a single IP.

iDRAC credentials: PSAUTO_USERNAME/PASSWORD (explicit override from the app's
UI) wins; otherwise PSAUTO_DEFAULT_USERNAME/PASSWORD (the app's encrypted
.env default) is used; a standalone run with neither set prompts instead.
"""
import os
import sys
import shutil
import subprocess

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
        # Non-interactive run (launched by PS Automation - stdin is a closed
        # pipe): prompting here would hang forever with zero output instead
        # of failing. Fail fast with a clear error instead.
        print("ERROR: No iDRAC credentials available (default credentials are not "
              "configured) and this run is non-interactive, so a password prompt "
              "cannot be shown. Configure the app's default iDRAC credentials and "
              "retry.", flush=True)
        sys.exit(1)


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


def read_targets():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    addresses_file = os.path.join(script_dir, "addresses.txt")
    servers = []
    if os.path.exists(addresses_file):
        with open(addresses_file, "r", encoding="utf-8", errors="ignore") as f:
            servers = [ln.strip() for ln in f if ln.strip()]
    # Manual console fallback only (stdin is a closed pipe when run by the app).
    if not servers and sys.stdin is not None and sys.stdin.isatty():
        ip = input("Enter iDRAC IP to power down: ").strip()
        if ip:
            servers = [ip]
    return servers


# Windows flag: never flash a console window for child processes (ping).
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def is_reachable(ip, timeout_ms=2000, attempts=2):
    """Fast ICMP pre-check: an unreachable/dead iDRAC is detected in ~2-4
    seconds and SKIPPED, instead of letting racadm stall on it for its full
    120s timeout - so one dead server never holds up the rest of the list.
    Checks for 'ttl=' in the reply because Windows ping exits 0 even on a
    'Destination host unreachable' answer from a router - only a real reply
    from the target itself contains a TTL."""
    for _ in range(attempts):
        try:
            proc = subprocess.run(
                ["ping", "-n", "1", "-w", str(timeout_ms), ip],
                capture_output=True, text=True,
                timeout=(timeout_ms / 1000.0) + 3,
                creationflags=CREATE_NO_WINDOW
            )
            if proc.returncode == 0 and "ttl=" in (proc.stdout or "").lower():
                return True
        except Exception:
            pass
    return False


def main():
    print("=== Power Down Servers (racadm serveraction powerdown) ===", flush=True)

    racadm = resolve_racadm()
    if not racadm:
        print("ERROR: racadm.exe not found in PATH or standard Dell install locations.", flush=True)
        print("Install Dell OpenManage / racadm, or add racadm.exe to PATH.", flush=True)
        sys.exit(1)

    servers = read_targets()
    if not servers:
        print("ERROR: No target IP provided (addresses.txt is empty).", flush=True)
        sys.exit(1)

    print(f"racadm       : {racadm}")
    print(f"iDRAC user   : {IDRAC_USER}")
    print(f"Target count : {len(servers)}")
    print(f"Targets      : {', '.join(servers)}\n", flush=True)

    ok = 0
    fail = 0
    skipped = 0
    for ip in servers:
        print("-" * 50, flush=True)

        # Fast reachability gate: no ping reply -> skip THIS server right away
        # and move on. The rest of the list keeps running at full speed.
        print(f"Checking reachability of {ip} ...", flush=True)
        if not is_reachable(ip):
            print(f"SKIPPED: {ip} did not answer ping - moving on to the next server.", flush=True)
            skipped += 1
            continue

        print(f"Powering down {ip} ...", flush=True)
        try:
            proc = subprocess.run(
                [racadm, "-r", ip, "-u", IDRAC_USER, "-p", IDRAC_PASS,
                 "--nocertwarn", "serveraction", "powerdown"],
                capture_output=True, text=True, timeout=120
            )
            for stream in (proc.stdout, proc.stderr):
                if stream and stream.strip():
                    print("  " + stream.strip().replace("\n", "\n  "), flush=True)
            if proc.returncode == 0:
                print(f"OK: power down command sent to {ip}.", flush=True)
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
    print(f" Done. Success: {ok}, Failed: {fail}, Skipped (no ping): {skipped}, Total: {len(servers)}", flush=True)
    print("=" * 50, flush=True)

    if sys.stdin is not None and sys.stdin.isatty():
        input("Press Enter to exit...")

    # Non-zero exit only if NOTHING succeeded (every target failed or was
    # skipped as unreachable) - partial success is still a success for the
    # run as a whole.
    if ok == 0 and (fail + skipped) > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
