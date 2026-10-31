$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw 'Missing venv. First run python -m venv .venv and install requirements.txt'
}
Push-Location $root
try {
    & $python tests.py
    if ($LASTEXITCODE -ne 0) { throw 'Unit tests failed' }
    & $python tests_auto_meso.py
    if ($LASTEXITCODE -ne 0) { throw 'Automatic meso OCR tests failed' }
    & $python tests_fast_polling.py
    if ($LASTEXITCODE -ne 0) { throw 'Fast EXP OCR regression tests failed' }
    & $python tests_meso_forecast.py
    if ($LASTEXITCODE -ne 0) { throw 'Meso cumulative forecasting regression tests failed' }
    # PyInstaller writes INFO/WARNING progress to stderr on Windows. In
    # PowerShell 5 this must not become a terminating NativeCommandError.
    # The actual process exit code still determines build success.
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $python -m PyInstaller --noconfirm --clean --onefile --windowed --name MapleHuntTracker_v1_5 --collect-all customtkinter --collect-all winocr --collect-all windows_capture --hidden-import windows_capture --hidden-import winrt.windows.media.ocr --hidden-import winrt.windows.globalization --hidden-import winrt.windows.graphics.imaging --hidden-import winrt.windows.storage.streams --hidden-import winrt.windows.foundation --hidden-import winrt.runtime app.py
        $buildExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($buildExitCode -ne 0) { throw ('PyInstaller build failed: ' + $buildExitCode) }
    Write-Output ('BUILT_EXE=' + (Join-Path $root 'dist\MapleHuntTracker_v1_5.exe'))
} finally {
    Pop-Location
}
