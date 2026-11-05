<#
  Publishes dist\NextPull-<version>.zip (+ .sha256) as a GitHub release tagged v<version>, with the notes from CHANGELOG.md.
      powershell -ExecutionPolicy Bypass -File build\publish.ps1
  Needs the GitHub CLI (gh), signed in. It shows what it is about to publish and waits for you to type the version to confirm.
  Run build\build.ps1 first. Nothing is uploaded until you confirm.
#>
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$version = (Select-String -Path (Join-Path $root "nextpull.py") -Pattern '^__version__ = "([^"]+)"').Matches[0].Groups[1].Value
$zip = Join-Path $root "dist\NextPull-$version.zip"
$sha = "$zip.sha256"
foreach ($file in @($zip, $sha)) { if (-not (Test-Path $file)) { throw "$file is missing. Run build\build.ps1 first." } }
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw "The GitHub CLI (gh) is not installed or not on PATH." }

# release notes = the "## <version>" section of CHANGELOG.md
$lines = Get-Content (Join-Path $root "CHANGELOG.md") -Encoding UTF8
$start = ($lines | Select-String -Pattern "^## $([regex]::Escape($version))\b" | Select-Object -First 1).LineNumber
if (-not $start) { throw "CHANGELOG.md has no '## $version' section." }
$notes = @()
foreach ($line in $lines[$start..($lines.Count - 1)]) { if ($line -match '^## ') { break }; $notes += $line }
$notesFile = Join-Path $env:TEMP "nextpull-release-notes.md"
Set-Content -Path $notesFile -Value ($notes -join "`n") -Encoding UTF8

Write-Host "About to publish NextPull v$version to GitHub:"
Write-Host "  $zip  ($([math]::Round((Get-Item $zip).Length / 1MB, 1)) MB)"
Write-Host "  $sha  ($((Get-Content $sha -Raw).Trim()))"
Write-Host "  notes: $($notes.Count) lines from CHANGELOG.md"
$answer = Read-Host "Type the version ($version) to publish, anything else to cancel"
if ($answer.Trim() -ne $version) { Write-Host "Cancelled. Nothing was uploaded."; exit 1 }
gh release create "v$version" $zip $sha --title "NextPull $version" --notes-file $notesFile
if ($LASTEXITCODE -ne 0) { throw "gh release create failed" }
Write-Host "Published v$version."
