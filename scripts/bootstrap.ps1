[CmdletBinding()]
param(
    [switch]$InstallComfyUI,
    [switch]$SkipComfyUI,
    [switch]$SkipFrontend,
    [string]$PythonExecutable
)

$ErrorActionPreference = 'Stop'
if ($InstallComfyUI -and $SkipComfyUI) { throw 'Choose either -InstallComfyUI or -SkipComfyUI.' }
function Invoke-Checked([scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "Command failed (exit $LASTEXITCODE): $Command" }
}
$projectRoot = Split-Path -Parent $PSScriptRoot
$lockPath = Join-Path $projectRoot 'dependencies.lock.json'
$lock = Get-Content -LiteralPath $lockPath -Raw -Encoding UTF8 | ConvertFrom-Json
$comfyRoot = Join-Path $projectRoot 'runtime\ComfyUI'
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

if ($InstallComfyUI -and -not (Test-Path -LiteralPath (Join-Path $comfyRoot 'main.py'))) {
    if ((Test-Path -LiteralPath $comfyRoot) -and (Get-ChildItem -LiteralPath $comfyRoot -Force | Select-Object -First 1)) {
        throw "ComfyUI installation directory is not empty: $comfyRoot. Preserve its contents and select a fresh installation directory."
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $comfyRoot) -Force | Out-Null
    Invoke-Checked { git clone --filter=blob:none --no-checkout $lock.comfyui.repository $comfyRoot }
    Invoke-Checked { git -C $comfyRoot checkout $lock.comfyui.commit }
    foreach ($relativePatch in $lock.comfyui.patches) {
        Invoke-Checked { git -C $comfyRoot apply (Join-Path $projectRoot $relativePatch) }
    }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    if ($PythonExecutable) {
        Invoke-Checked { & $PythonExecutable -m venv (Join-Path $projectRoot '.venv') }
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $pythonVersion = @('3.13', '3.12') | Where-Object {
            $probePreference = $ErrorActionPreference
            try {
                $ErrorActionPreference = 'Continue'
                & py "-$_" -c 'import sys' 2>$null
                $LASTEXITCODE -eq 0
            } finally { $ErrorActionPreference = $probePreference }
        } | Select-Object -First 1
        if (-not $pythonVersion) { throw 'Python 3.12 or 3.13 was not found. Use -PythonExecutable to select Python.' }
        Invoke-Checked { py "-$pythonVersion" -m venv (Join-Path $projectRoot '.venv') }
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        Invoke-Checked { python -m venv (Join-Path $projectRoot '.venv') }
    } else {
        throw 'Python was not found. Use -PythonExecutable to select Python 3.12 or 3.13.'
    }
}

Invoke-Checked { & $venvPython -m pip install --upgrade pip }
if ($InstallComfyUI -and (Test-Path -LiteralPath (Join-Path $comfyRoot 'requirements.txt'))) {
    Invoke-Checked { & $venvPython -m pip install -r (Join-Path $comfyRoot 'requirements.txt') }
}
Invoke-Checked { & $venvPython -m pip install -r (Join-Path $projectRoot 'mobile_server\requirements.txt') }

if (-not $SkipFrontend) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        throw 'Node.js 22 and npm are required to build the frontend.'
    }
    Push-Location $projectRoot
    try {
        Invoke-Checked { npm ci }
        Invoke-Checked { npm run build }
    } finally {
        Pop-Location
    }
}

$localConfig = Join-Path $projectRoot 'config.toml'
if (-not (Test-Path -LiteralPath $localConfig)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot 'config.example.toml') -Destination $localConfig
}

Write-Host ''
Write-Host 'Base environment is ready.'
Write-Host 'Configure paths.comfyui and comfyui.url to connect your existing ComfyUI installation.'
Write-Host 'Read docs\dependencies.md and install the nodes and models required by enabled workflows.'
Write-Host 'Then run the Comfy Canvas batch launcher in the repository root.'
Push-Location $projectRoot
try { Invoke-Checked { & $venvPython -m mobile_server.doctor --source-only } }
finally { Pop-Location }
Write-Host 'After configuring models and custom nodes, run: .venv\Scripts\python.exe -m mobile_server.doctor'
