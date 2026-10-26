$ErrorActionPreference = "Stop"
$companionPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $companionPython)) {
    throw "Create the Python environment and install requirements first; see README.md."
}
& $companionPython (Join-Path $PSScriptRoot "run.py")
exit $LASTEXITCODE
