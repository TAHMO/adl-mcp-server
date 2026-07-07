#requires -Version 7
<#
.SYNOPSIS
  Register (or update / remove) the adl-mcp-server MCP server with the Claude CLI.
.DESCRIPTION
  adl-mcp-server is a read-only MCP surface over a WMO ADL TimescaleDB. Connection
  settings are stored as the server's env block in the Claude CLI config. Idempotent:
  re-run to update. Use -Remove to unregister. Restart the client after registering —
  MCP servers connect on startup.
.EXAMPLE
  ./Register-AdlMcp.ps1 -DbPassword (Read-Host -AsSecureString | ConvertFrom-SecureString -AsPlainText)
.EXAMPLE
  ./Register-AdlMcp.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$DbHost = "127.0.0.1",
    [int]$DbPort = 5432,
    [string]$DbName = "adl",
    [string]$DbUser = "adl_readonly",
    [string]$DbPassword,
    [string]$ServerPath = (Join-Path $PSScriptRoot "mcp_server.py"),
    [string]$Python = "python",
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$name = "adl-mcp-server"

if ($Remove) {
    claude mcp remove $name
    Write-Host "Removed $name. Restart your MCP client." -ForegroundColor Yellow
    return
}
if (-not $DbPassword) { throw "Provide -DbPassword (or -Remove)." }
if (-not (Test-Path $ServerPath)) { throw "Server not found: $ServerPath" }

# Idempotent: drop any prior entry first (ignore if absent), then add.
try { claude mcp remove $name 2>$null | Out-Null } catch {}

claude mcp add $name `
    -e "ADL_DB_HOST=$DbHost" `
    -e "ADL_DB_PORT=$DbPort" `
    -e "ADL_DB_NAME=$DbName" `
    -e "ADL_DB_USER=$DbUser" `
    -e "ADL_DB_PASSWORD=$DbPassword" `
    -- $Python $ServerPath

# Never echo the password back.
Write-Host "Registered '$name' -> $DbUser@${DbHost}:$DbPort/$DbName" -ForegroundColor Green
Write-Host "Verify:  claude mcp get $name   (then restart the client)" -ForegroundColor Green
