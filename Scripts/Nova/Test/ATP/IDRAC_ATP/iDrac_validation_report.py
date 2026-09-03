import subprocess
import sys

def run_scripts():
    print(">>> Starting iDRAC 10 Automation...")
    try:
        # Runs the first script and waits for it to finish
        subprocess.run([sys.executable, "iDRAC10_Report.py"], check=True)
        
        print("\n>>> iDRAC 10 finished. Moving to iDRAC 9 Automation...")
        
        # Runs the second script and waits for it to finish
        subprocess.run([sys.executable, "iDRAC9_Report.py"], check=True)
        
        print("\n>>> ALL AUTOMATIONS COMPLETED SUCCESSFULLY!")
        
    except subprocess.CalledProcessError as e:
        print(f"\n[ERROR] An error occurred during script execution. Code: {e.returncode}")

if __name__ == "__main__":
    run_scripts()