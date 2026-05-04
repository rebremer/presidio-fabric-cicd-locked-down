<#
.SYNOPSIS
    Bootstrap a Windows VM as an Azure DevOps self-hosted agent for the
    `deploy-presidio-fabric` pipeline.

.DESCRIPTION
    Run this on the agent VM (e.g. `test-fabricjumphost-vm`) in an
    **elevated** PowerShell session. It will:

      1. Install Python 3.11 (system-wide, on PATH).
      2. Install the Azure CLI (required by the AzureCLI@2 pipeline task).
      3. Download and configure the Azure DevOps agent into the chosen pool.
      4. Install the agent as a Windows service so it survives reboots.

    All downloads use HTTPS to public Microsoft / python.org endpoints.
    On a DEP-protected VM with no internet, run the script with
    `-SkipDownloads` and copy the installers + agent zip to the paths below.

.PARAMETER OrgUrl
    Azure DevOps organization URL, e.g. https://dev.azure.com/renebremer

.PARAMETER Pool
    Agent pool name (must exist in the org). Default: 'Default'.

.PARAMETER AgentName
    Display name for this agent. Default: machine hostname.

.PARAMETER Pat
    Personal Access Token with scope: Agent Pools (Read & manage).
    Used only for one-time registration; can be revoked afterwards.

.PARAMETER WorkDir
    Folder to install the agent into. Default: C:\agent.

.PARAMETER SkipDownloads
    Skip Invoke-WebRequest steps. Expects:
      $WorkDir\agent.zip
      $WorkDir\python-installer.exe
      $WorkDir\az-cli.msi
    to be present already.

.EXAMPLE
    .\setup-agent.ps1 -OrgUrl https://dev.azure.com/renebremer -Pat <pat>
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $OrgUrl,
    [Parameter(Mandatory = $true)] [string] $Pat,
    [string] $Pool       = 'Default',
    [string] $AgentName  = $env:COMPUTERNAME,
    [string] $WorkDir    = 'C:\agent',
    [switch] $SkipDownloads
)

$ErrorActionPreference = 'Stop'

# Pinned versions -- bump when needed
$AgentVersion   = '3.243.1'
$PythonVersion  = '3.11.9'

$AgentUrl   = "https://vstsagentpackage.azureedge.net/agent/$AgentVersion/vsts-agent-win-x64-$AgentVersion.zip"
$PythonUrl  = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
$AzCliUrl   = 'https://aka.ms/installazurecliwindowsx64'

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'This script must be run from an elevated PowerShell session.'
}

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
Set-Location $WorkDir

# ---------------------------------------------------------------------------
# 1. Python 3.11
# ---------------------------------------------------------------------------
$pyExe = 'C:\Program Files\Python311\python.exe'
if (Test-Path $pyExe) {
    Write-Host "[1/4] Python already installed at $pyExe"
} else {
    Write-Host '[1/4] Installing Python...'
    $pyInstaller = Join-Path $WorkDir 'python-installer.exe'
    if (-not $SkipDownloads) {
        Invoke-WebRequest -Uri $PythonUrl -OutFile $pyInstaller
    }
    Start-Process -Wait -FilePath $pyInstaller -ArgumentList @(
        '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_pip=1'
    )
}

# ---------------------------------------------------------------------------
# 2. Azure CLI
# ---------------------------------------------------------------------------
if (Get-Command az -ErrorAction SilentlyContinue) {
    Write-Host '[2/4] Azure CLI already installed'
} else {
    Write-Host '[2/4] Installing Azure CLI...'
    $azMsi = Join-Path $WorkDir 'az-cli.msi'
    if (-not $SkipDownloads) {
        Invoke-WebRequest -Uri $AzCliUrl -OutFile $azMsi
    }
    Start-Process msiexec.exe -Wait -ArgumentList "/I `"$azMsi`" /quiet"
}

# Refresh PATH for the current session
$env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
            [System.Environment]::GetEnvironmentVariable('Path', 'User')

# ---------------------------------------------------------------------------
# 3. Download + extract agent
# ---------------------------------------------------------------------------
$agentZip = Join-Path $WorkDir 'agent.zip'
if (-not (Test-Path (Join-Path $WorkDir 'config.cmd'))) {
    Write-Host '[3/4] Downloading agent...'
    if (-not $SkipDownloads) {
        Invoke-WebRequest -Uri $AgentUrl -OutFile $agentZip
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($agentZip, $WorkDir)
} else {
    Write-Host '[3/4] Agent already extracted'
}

# ---------------------------------------------------------------------------
# 4. Configure + install as service
# ---------------------------------------------------------------------------
Write-Host '[4/4] Configuring agent...'
& "$WorkDir\config.cmd" `
    --unattended `
    --url $OrgUrl `
    --auth pat `
    --token $Pat `
    --pool $Pool `
    --agent $AgentName `
    --acceptTeeEula `
    --runAsService `
    --windowsLogonAccount 'NT AUTHORITY\NETWORK SERVICE' `
    --replace

Write-Host ''
Write-Host "Agent '$AgentName' is online in pool '$Pool'."
Write-Host 'You can now revoke the PAT used for registration.'
Write-Host 'Verify in: Org Settings -> Agent pools -> Default -> Agents tab.'
