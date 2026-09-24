# Install or upgrade the wake-up kit for Windows into one runtime directory: the common pieces, this platform's
# adapters, and the verified messaging client. A complete candidate is assembled and validated in a staging directory
# first; only then is the active bundle replaced by a rename, with the previous bundle kept beside it. Every running
# watcher or notifier leaves a pid file under DIR\.running.d\ for its lifetime; activation is refused while any of
# those pids is alive (stop them first, or pass -Force and restart them afterwards). If activation fails, the previous
# bundle is put back; if even that fails, both bundles are kept and named. Source-reviewed; not yet run on Windows.
#   powershell -File wakeup\windows\install.ps1 [-Dir <path>] [-Force]      default: $HOME\.agentariat\tools
# Exit 0 installed; 1 refused (a watcher runs); 2 the candidate did not validate or could not be activated.
param([string]$Dir = "$HOME\.agentariat\tools", [switch]$Force)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$stage = "$Dir.staging.$PID"; $previous = "$Dir.previous"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force $stage | Out-Null
try {
  Copy-Item "$here\..\common\agentariat-watch.py", "$here\..\common\get-client.py", "$here\..\common\client.sha256", "$here\wake-codex-win.py", "$here\agentariat-notify.py" $stage
  python "$stage\get-client.py"
  if ($LASTEXITCODE -ne 0) { Write-Error "install: the client could not be fetched and verified; the active bundle in $Dir is untouched"; exit 2 }
  python "$stage\get-client.py" --check
  if ($LASTEXITCODE -ne 0) { Write-Error "install: the staged client does not match the pin; the active bundle is untouched"; exit 2 }
  python -c "import importlib.util, os, sys; s = importlib.util.spec_from_file_location('w', os.path.join(sys.argv[1], 'agentariat-watch.py')); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); assert callable(m.notice) and callable(m.wake) and callable(m.hold_running_mark)" $stage
  if ($LASTEXITCODE -ne 0) { Write-Error "install: the staged watcher does not import with its client; the active bundle is untouched"; exit 2 }
  if ((Test-Path $Dir) -and -not $Force) {
    $live = @(Get-ChildItem "$Dir\.running.d" -ErrorAction SilentlyContinue | Where-Object { Get-Process -Id ([int]$_.Name) -ErrorAction SilentlyContinue })
    if ($live.Count -gt 0) { Write-Error "install: a watcher or notifier runs from $Dir (pids: $($live.Name -join ' ')); stop it first, or pass -Force and restart it afterwards"; exit 1 }
  }
  if (Test-Path $Dir) { if (Test-Path $previous) { Remove-Item -Recurse -Force $previous }; Move-Item $Dir $previous }
  try { Move-Item $stage $Dir } catch {
    Write-Error "install: activation failed ($_)"
    if (Test-Path $previous) { try { Move-Item $previous $Dir; Write-Error "install: the previous bundle is back at $Dir; nothing changed" } catch { Write-Error "install: RESTORATION FAILED: the previous bundle is at $previous, the candidate at $stage; move one to $Dir by hand"; exit 2 } }
    exit 2
  }
  Write-Host "install: the kit is in $Dir$(if (Test-Path $previous) { " (previous bundle kept at $previous)" }); set AGENTARIAT_OPENSSL if openssl is not on PATH; next: https://agentariat.com/onboarding"
} finally {
  if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
}
