# PS Automation - local bootstrap launcher.
# Meant to be run from a Desktop shortcut (see "Start PS Automation.bat" next
# to this file) - it clones (or updates) a local copy at $TargetDir and
# launches it, so every automation (Selenium, ChromeDriver, racadm, SSH) runs
# on THIS machine, using this machine's own network and its own local disk
# for output.
#
# $TargetDir matches the project's own canonical folder name ("Automation
# App", with the space) on purpose: on a machine that already has a manually
# set-up working copy there, this makes the script UPDATE that same folder
# in place instead of cloning a second, differently-named copy next to it.
#
# Deliberately lives OUTSIDE the folder it manages: on a brand-new machine
# that folder doesn't exist yet, so the launcher can't be inside it. Keep
# this script (+ its .bat) in a stable spot such as C:\Scripts\, and point
# the Desktop shortcut there - not into the managed folder itself.

$ErrorActionPreference = "Stop"

$RepoUrl   = "https://github.com/hip-66/Automation-App.git"
$Branch    = "main"
$TargetDir = "C:\Scripts\Automation App"

function Fail($msg) {
    Write-Host ""
    Write-Host "[ERROR] $msg" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "Git is not installed (or not on PATH). Install it from https://git-scm.com/download/win, then run this shortcut again."
}
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Fail "Python is not installed (or not on PATH). Run 'Install Adons\Install Adons.bat' from a working copy first, or install Python 3.x, then run this shortcut again."
}

$didPull = $false

if (-not (Test-Path $TargetDir)) {
    Write-Host "PS Automation not found locally - cloning into $TargetDir ..."
    $parent = Split-Path $TargetDir -Parent
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    git clone --branch $Branch $RepoUrl "$TargetDir"
    if ($LASTEXITCODE -ne 0) { Fail "git clone failed - check your network connection and repo access." }
    $didPull = $true
} elseif (-not (Test-Path (Join-Path $TargetDir ".git"))) {
    Fail "$TargetDir exists but isn't a git repository (no .git folder). Rename it aside, or turn it into a real clone manually (git init + git remote add origin $RepoUrl + git fetch), then run this shortcut again."
} else {
    Write-Host "Checking for updates..."
    Push-Location $TargetDir
    try {
        # --quiet, NOT *>&1 redirection: git writes its normal progress lines
        # to stderr, and merging stderr into the success stream via `*>&1`/
        # `2>&1` makes PowerShell 5.1 wrap each line as a NativeCommandError
        # - which $ErrorActionPreference = "Stop" then escalates into a fatal
        # script-terminating exception even though the fetch itself succeeded.
        git fetch origin $Branch --quiet
        if ($LASTEXITCODE -ne 0) { Fail "git fetch failed - check your network connection." }

        # Local edits in this working copy would need a REAL merge against
        # the incoming update, which can conflict (renames, deletes, etc.)
        # - not something a one-click launcher should attempt silently.
        # Stop with a clear message instead of leaving the folder mid-merge.
        $statusOutput = git status --porcelain
        if (-not [string]::IsNullOrWhiteSpace(($statusOutput -join "`n"))) {
            Fail "There are local changes in $TargetDir that would block an automatic update. Either commit them (git add -A; git commit) or discard them (git reset --hard; git clean -fd) in that folder, then run this shortcut again."
        }

        $localRev  = git rev-parse HEAD
        $remoteRev = git rev-parse "origin/$Branch"

        if ($localRev -ne $remoteRev) {
            Write-Host "Update available - pulling the latest version..."
            git pull origin $Branch --ff-only
            if ($LASTEXITCODE -ne 0) {
                Fail "git pull failed - resolve manually (git status) in $TargetDir, then run this shortcut again."
            }
            $didPull = $true
        } else {
            Write-Host "Already up to date."
        }
    } finally {
        Pop-Location
    }
}

Push-Location $TargetDir
try {
    # A pull may have changed requirements.txt - force run_ui.bat to
    # re-check dependencies instead of trusting a now possibly-stale marker.
    if ($didPull -and (Test-Path ".deps_ok")) {
        Remove-Item ".deps_ok" -Force
    }

    # First run on this machine: no .env yet (it's git-ignored, never
    # cloned). generate_env.py is safe to run with no arguments - it fills
    # in safe built-in defaults non-interactively.
    if (-not (Test-Path ".env")) {
        Write-Host "First run on this machine - generating local .env ..."
        python generate_env.py
        if ($LASTEXITCODE -ne 0) { Fail "generate_env.py failed - see the error above." }
    }

    Write-Host "Starting PS Automation..."
    & ".\run_ui.bat"
} finally {
    Pop-Location
}
