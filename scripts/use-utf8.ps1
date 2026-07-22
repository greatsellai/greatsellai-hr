Set-StrictMode -Version Latest

# Dot-source this file in Windows PowerShell 5.1 before passing non-ASCII
# text to Git, GitHub CLI, Python, or another native command:
#
#   . .\scripts\use-utf8.ps1
#
# `chcp` changes the console code page, while `$OutputEncoding` controls the
# bytes emitted by PowerShell pipelines. Both are required to avoid replacing
# Chinese text with question marks before the receiving program sees it.
if ($env:OS -eq "Windows_NT") {
    & "$env:SystemRoot\System32\chcp.com" 65001 | Out-Null
}

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$global:OutputEncoding = $utf8NoBom

Write-Output "UTF-8 console and pipeline encoding enabled for this PowerShell session."
