param(
    [int]$InitialCooldownSeconds = 0,
    [int]$CooldownSeconds = 1800,
    [int]$MaxRounds = 20,
    [double]$DelaySeconds = 3.0
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot
New-Item -ItemType Directory -Force -Path "logs" | Out-Null

$ChildLog = Join-Path $RepoRoot "logs\wiki_preview_scrape.child.log"

function Get-StatusCount($Summary, [string]$Name) {
    if (-not $Summary.status_counts) {
        return 0
    }
    $Prop = $Summary.status_counts.PSObject.Properties[$Name]
    if ($Prop) {
        return [int]$Prop.Value
    }
    return 0
}

if ($InitialCooldownSeconds -gt 0) {
    Write-Output "$(Get-Date -Format s) initial cooldown ${InitialCooldownSeconds}s"
    Start-Sleep -Seconds $InitialCooldownSeconds
}

for ($Round = 1; $Round -le $MaxRounds; $Round++) {
    Write-Output "$(Get-Date -Format s) round $Round start, delay=${DelaySeconds}s"
    & python "scripts\scrape_wiki_preview_images.py" `
        --fetch-mode source `
        --workers 1 `
        --delay $DelaySeconds `
        --timeout 25 `
        --stop-on-429 `
        --checkpoint-every 25 2>&1 | Tee-Object -FilePath $ChildLog -Append

    $SummaryPath = Join-Path $RepoRoot "data\wiki_preview_images_summary.json"
    if (-not (Test-Path $SummaryPath)) {
        Write-Output "$(Get-Date -Format s) summary missing; sleeping ${CooldownSeconds}s"
        Start-Sleep -Seconds $CooldownSeconds
        continue
    }

    $Summary = Get-Content -Raw $SummaryPath | ConvertFrom-Json
    $Matched = Get-StatusCount $Summary "matched"
    $WithImages = [int]$Summary.with_images
    $Errors = [int]$Summary.errors
    Write-Output "$(Get-Date -Format s) round $Round summary: with_images=$WithImages matched_pending=$Matched errors=$Errors"

    if ($Matched -le 0) {
        Write-Output "$(Get-Date -Format s) completed"
        break
    }

    Write-Output "$(Get-Date -Format s) cooldown ${CooldownSeconds}s before retry"
    Start-Sleep -Seconds $CooldownSeconds
}
