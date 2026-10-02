<#
  Downloads the official rclone for Windows (amd64) into tools\rclone\rclone.exe and verifies its SHA-256 against the
  checksums rclone publishes. Run once:
      powershell -ExecutionPolicy Bypass -File tools\Fetch-rclone.ps1
  Source: https://downloads.rclone.org (the official site). Nothing else is downloaded or installed.
#>
$ErrorActionPreference = "Stop"
$base = "https://downloads.rclone.org"
$target = Join-Path $PSScriptRoot "rclone"
New-Item -ItemType Directory -Force -Path $target | Out-Null
$temp = Join-Path ([IO.Path]::GetTempPath()) ("nextpull-rclone-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temp | Out-Null
try {
    $version = ((Invoke-WebRequest -UseBasicParsing "$base/version.txt").Content).Trim() -replace "^rclone\s+", ""
    $name = "rclone-$version-windows-amd64.zip"
    Write-Host "Downloading $name ..."
    $zip = Join-Path $temp $name
    Invoke-WebRequest -UseBasicParsing "$base/$version/$name" -OutFile $zip
    $sums = (Invoke-WebRequest -UseBasicParsing "$base/$version/SHA256SUMS").Content
    if ($sums -is [byte[]]) { $sums = [Text.Encoding]::UTF8.GetString($sums) }   # served as application/octet-stream
    $line = ($sums -split "`n") | Where-Object { $_ -match "\s$([regex]::Escape($name))\s*$" } | Select-Object -First 1
    if (-not $line) { throw "No checksum for $name was found in SHA256SUMS" }
    $expected = ($line -split "\s+")[0].ToLower()
    $actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if ($expected -ne $actual) { throw "Checksum mismatch for $name (expected $expected, got $actual). Not installing." }
    Write-Host "Checksum OK ($actual)"
    Expand-Archive $zip -DestinationPath $temp -Force
    $exe = Get-ChildItem $temp -Recurse -Filter rclone.exe | Select-Object -First 1
    Copy-Item $exe.FullName (Join-Path $target "rclone.exe") -Force
    Write-Host "Installed: $(Join-Path $target 'rclone.exe')"
    & (Join-Path $target "rclone.exe") version | Select-Object -First 1
} finally {
    Remove-Item $temp -Recurse -Force -ErrorAction SilentlyContinue
}
