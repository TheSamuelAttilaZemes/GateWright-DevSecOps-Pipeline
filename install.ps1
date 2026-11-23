# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
<#
.SYNOPSIS
    Gatewright installer for Windows (the counterpart of install.sh).

.DESCRIPTION
    1. finds Python 3.10 or newer
    2. installs Gatewright into its own virtual environment, so your Python is never touched
    3. puts gatewright.exe in a folder on your PATH (or tells you what to add)
    4. checks that the command works

    Needs no administrator rights and changes no system settings. Running it again upgrades.

.PARAMETER Prefix
    Where Gatewright lives (default: ~\.gatewright-venv).
.PARAMETER BinDir
    Where gatewright.exe goes (default: ~\.local\bin).
.PARAMETER Python
    Use this Python interpreter.
.PARAMETER NoTools
    Don't install the starter scanners (ruff, bandit, pytest, coverage, pip-audit).
.PARAMETER Offline
    Install Python packages from this folder only (no internet, see docs/tips.md).
.PARAMETER DryRun
    Print what would be done, change nothing.
.PARAMETER Uninstall
    Remove Gatewright (keeps your projects' .gatewright folders).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
#>
[CmdletBinding()]
param(
    [string]$Prefix = (Join-Path $env:USERPROFILE '.gatewright-venv'),
    [string]$BinDir = (Join-Path $env:USERPROFILE '.local\bin'),
    [string]$Python = '',
    [string]$Offline = '',
    [switch]$NoTools,
    [switch]$DryRun,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$Source = $PSScriptRoot
$VenvPython = Join-Path $Prefix 'Scripts\python.exe'
$VenvExe = Join-Path $Prefix 'Scripts\gatewright.exe'
$Command = Join-Path $BinDir 'gatewright.exe'

function Write-Step([string]$Text) { Write-Host "==> $Text" }

function Invoke-Step([string]$Exe, [string[]]$Arguments) {
    Write-Host "  + $Exe $($Arguments -join ' ')"
    if ($DryRun) { return }
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed with exit code ${LASTEXITCODE}: $Exe" }
}

function Test-Python([string]$Exe) {
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe)) { return $false }
    $ver = & $Exe -c 'import sys; print(sys.version_info[0] * 100 + sys.version_info[1])'
    return ($LASTEXITCODE -eq 0) -and ([int]$ver -ge 310)
}

function Find-Python {
    if ($Python) {
        if (Test-Python $Python) { return $Python }
        throw "$Python is not Python 3.10 or newer."
    }
    $found = @()
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        # the newest Python the launcher knows
        $exe = & py.exe -3 -c 'import sys; print(sys.executable)'
        if ($LASTEXITCODE -eq 0) { $found += $exe }
    }
    # WindowsApps\python.exe only opens the Microsoft Store when Python isn't installed from there
    $found += @(Get-Command python.exe -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notlike '*\WindowsApps\*' } | ForEach-Object { $_.Source })
    foreach ($exe in $found) { if (Test-Python $exe) { return $exe } }
    throw 'Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/windows/ and run this again.'
}

if ($Uninstall) {
    Write-Step "Removing Gatewright from $Prefix and $Command"
    if (Test-Path -LiteralPath $Prefix) {
        # only ever delete a folder that really is a Gatewright environment
        if (-not (Test-Path -LiteralPath (Join-Path $Prefix 'pyvenv.cfg')) -or -not (Test-Path -LiteralPath $VenvExe)) {
            throw "$Prefix is not a Gatewright environment; nothing was removed."
        }
        Write-Host "  + remove $Prefix"
        if (-not $DryRun) { Remove-Item -LiteralPath $Prefix -Recurse -Force -Confirm:$false }
    }
    if (Test-Path -LiteralPath $Command) {
        Write-Host "  + remove $Command"
        if (-not $DryRun) { Remove-Item -LiteralPath $Command -Force -Confirm:$false }
    }
    Write-Step 'Gatewright removed. Project folders (.gatewright\) were left untouched.'
    exit 0
}

if (-not (Test-Path -LiteralPath (Join-Path $Source 'pyproject.toml'))) {
    throw 'Run install.ps1 from the Gatewright source folder (pyproject.toml is missing).'
}

if ($Offline) {
    $env:PIP_NO_INDEX = '1'
    $env:PIP_FIND_LINKS = (Resolve-Path -LiteralPath $Offline).Path -replace ' ', '%20'
    $env:PIP_FIND_LINKS = 'file:///' + ($env:PIP_FIND_LINKS -replace '\\', '/')
}
$extras = if ($NoTools) { '' } else { '[tools]' }
if (Test-Path -LiteralPath $VenvPython) {
    Write-Step "Upgrading the environment in $Prefix"
} elseif ((Test-Path -LiteralPath $Prefix) -and (Get-ChildItem -LiteralPath $Prefix -Force | Select-Object -First 1)) {
    throw "$Prefix exists and is not a virtual environment. Choose another folder with -Prefix."
} else {
    $base = Find-Python
    Write-Step "Creating an environment in $Prefix (Python: $base)"
    Invoke-Step $base @('-m', 'venv', $Prefix)
}
Invoke-Step $VenvPython @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '--upgrade', "$Source$extras")
# the version may be unchanged: make sure the code is this copy of it
Invoke-Step $VenvPython @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '--force-reinstall', '--no-deps', "$Source")

Write-Step "Installing the command: $Command"
if (-not $DryRun) {
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    try { Copy-Item -LiteralPath $VenvExe -Destination $Command -Force }
    catch { throw "Could not write $Command. Close running Gatewright windows and run this again. ($_)" }
    $version = & $Command --version
    if ($LASTEXITCODE -ne 0) { throw "$Command does not work." }
    Write-Step "Installed: $version"
}
$onPath = ($env:PATH -split ';' | ForEach-Object { $_.TrimEnd('\') }) -contains $BinDir.TrimEnd('\')
if (-not $onPath) {
    Write-Host "  ! $BinDir is not on your PATH. Add it under Settings > Edit environment variables for your account."
}
if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
    Write-Host '  ! Git for Windows was not found. Jobs need its Git Bash: https://git-scm.com/download/win'
}
Write-Step 'Next: run "gatewright doctor" in your project.'
exit 0
