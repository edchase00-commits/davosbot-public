param(
    [Parameter(Mandatory = $true)]
    [string]$Message,
    [string[]]$Path = @(),
    [switch]$All,
    [switch]$Push,
    [string]$Remote = "origin",
    [string]$Branch = ""
)

$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param(
        [string]$Description,
        [scriptblock]$Command
    )

    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
Push-Location $repo
try {
    if ($All) {
        Invoke-Checked "git add" { git add -A }
    } elseif ($Path.Count -gt 0) {
        Invoke-Checked "git add" { git add -- $Path }
    } else {
        throw "Pass -All or one or more -Path values so staging stays intentional."
    }

    git diff --cached --quiet
    if ($LASTEXITCODE -eq 0) {
        Write-Host "No staged changes to commit."
        return
    }

    Invoke-Checked "git commit" { git commit -m $Message }

    if ($Push) {
        if (-not $Branch) {
            $Branch = (git branch --show-current).Trim()
        }
        if (-not $Branch) {
            throw "Could not determine current branch for push."
        }
        Invoke-Checked "git push" { git push $Remote $Branch }
    }
} finally {
    Pop-Location
}
