[CmdletBinding()]
param(
    [ValidateSet('main', 'all')]
    [string]$Profile = 'all'
)

$ErrorActionPreference = 'Stop'
function Invoke-Checked([scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "Command failed (exit $LASTEXITCODE): $Command" }
}
$projectRoot = Split-Path -Parent $PSScriptRoot
$lock = Get-Content -LiteralPath (Join-Path $projectRoot 'manifests/dependencies.lock.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$nodeRoot = Join-Path $projectRoot 'runtime\ComfyUI\custom_nodes'
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $nodeRoot)) {
    throw 'ComfyUI is missing. Run scripts\bootstrap.ps1 first.'
}
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw 'Python environment is missing. Run scripts\bootstrap.ps1 first.'
}

$nodes = @($lock.verified_public_custom_nodes | Where-Object {
    $Profile -eq 'all' -or $_.install_profile -eq 'main'
})
foreach ($node in $nodes) {
    $destination = Join-Path $nodeRoot $node.name
    if (-not (Test-Path -LiteralPath $destination)) {
        Invoke-Checked { git clone $node.repository $destination }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $destination '.git'))) {
        throw "Existing custom node is not a Git checkout: $destination"
    }
    $origin = (git -C $destination remote get-url origin).Trim()
    if ($origin.TrimEnd('/') -ne $node.repository.TrimEnd('/')) {
        throw "Unexpected origin for $($node.name): $origin"
    }
    if (git -C $destination status --porcelain) { throw "Custom node has local changes; leaving it unchanged: $destination" }
    Invoke-Checked { git -C $destination fetch --depth 1 origin $node.commit }
    Invoke-Checked { git -C $destination checkout --detach $node.commit }
    $requirements = Join-Path $destination 'requirements.txt'
    if (Test-Path -LiteralPath $requirements) {
        Invoke-Checked { & $venvPython -m pip install -r $requirements }
    }
}

Write-Host "Verified public custom nodes are installed (profile: $Profile)."
Write-Host 'Excluded local components were not copied. See docs\dependencies.md.'
