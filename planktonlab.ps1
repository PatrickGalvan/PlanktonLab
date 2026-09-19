$ErrorActionPreference = "Stop"

$AppRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$UvVersion = if ($env:PLANKTONLAB_UV_VERSION) { $env:PLANKTONLAB_UV_VERSION } else { "0.12.11" }
$RuntimeDirectory = Join-Path $AppRoot ".planktonlab-runtime"
$UvDirectory = Join-Path $RuntimeDirectory "bin"
$UvExecutable = Join-Path $UvDirectory "uv.exe"
$VirtualEnvironment = if ($env:PLANKTONLAB_VENV_DIR) { $env:PLANKTONLAB_VENV_DIR } else { Join-Path $AppRoot ".venv" }
$Accelerator = if ($env:PLANKTONLAB_ACCELERATOR) { $env:PLANKTONLAB_ACCELERATOR.ToLowerInvariant() } else { "cpu" }

function Write-PlanktonLabInfo([string]$Message) {
    Write-Host "[PlanktonLab] $Message" -ForegroundColor Blue
}

try {
    Set-Location $AppRoot
    if (-not (Test-Path (Join-Path $AppRoot "pyproject.toml")) -or -not (Test-Path (Join-Path $AppRoot "uv.lock"))) {
        throw "pyproject.toml or uv.lock is missing. Restore the complete Git checkout."
    }
    if ($Accelerator -notin @("cpu", "cuda")) {
        throw "PLANKTONLAB_ACCELERATOR must be either 'cpu' or 'cuda'."
    }

    if (-not (Test-Path $UvExecutable)) {
        Write-PlanktonLabInfo "Installing the portable environment manager (one-time download)."
        New-Item -ItemType Directory -Force -Path $UvDirectory | Out-Null
        $env:UV_UNMANAGED_INSTALL = $UvDirectory
        Invoke-RestMethod "https://astral.sh/uv/$UvVersion/install.ps1" | Invoke-Expression
    }
    if (-not (Test-Path $UvExecutable)) {
        throw "The environment manager could not be installed."
    }

    $env:UV_PROJECT_ENVIRONMENT = $VirtualEnvironment
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $RuntimeDirectory "python"
    Write-PlanktonLabInfo "Preparing Python 3.12 and the locked $Accelerator dependencies."
    & $UvExecutable --system-certs sync --frozen --extra $Accelerator
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

    $PythonExecutable = Join-Path $VirtualEnvironment "Scripts\python.exe"
    if (-not (Test-Path $PythonExecutable)) {
        throw "The Python environment was not created successfully."
    }
    & $PythonExecutable -c "import cv2, fastai, flask, sqlalchemy, waitress"
    if ($LASTEXITCODE -ne 0) { throw "A required dependency could not be imported after installation." }

    Write-PlanktonLabInfo "Starting PlanktonLab. Press Ctrl+C to stop it."
    & $PythonExecutable (Join-Path $AppRoot "run.py") @args
    exit $LASTEXITCODE
}
catch {
    Write-Host "[PlanktonLab] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
