<#
Run the isolated Docker release regressions from PowerShell. This wrapper does
not load .env files, connect to a server, or use existing compose volumes.

Examples:
  .\scripts\run-release-regression.ps1
  .\scripts\run-release-regression.ps1 -Documents
  .\scripts\run-release-regression.ps1 -Postgres
  .\scripts\run-release-regression.ps1 -Image greatsellai-hr-ci:local
#>
[CmdletBinding()]
param(
    [switch]$Documents,
    [switch]$Postgres,
    [string]$Image
)

$ErrorActionPreference = 'Stop'

if ($Documents -and $Postgres) {
    throw 'Choose either -Documents or -Postgres, or omit both to run all checks.'
}

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$arguments = @((Join-Path $PSScriptRoot 'run_release_regression.py'))
if ($Documents) {
    $arguments += '--documents'
} elseif ($Postgres) {
    $arguments += '--postgres'
} else {
    $arguments += '--all'
}
if ($Image) {
    $arguments += '--image'
    $arguments += $Image
}

Push-Location $repositoryRoot
try {
    & python @arguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}
