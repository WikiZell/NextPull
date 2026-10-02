<#
  Builds a self-contained NextPull folder (dist\NextPull\NextPull.exe) with PyInstaller. The target PC needs no Python.
      powershell -ExecutionPolicy Bypass -File build\build.ps1
  Afterwards copy the whole dist\NextPull folder to the target PC (for example to C:\NextPull). If tools\rclone\rclone.exe
  exists here it is included; otherwise run tools\Fetch-rclone.ps1 first, or add rclone.exe to dist\NextPull\tools\rclone\.
#>
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
py -3 -m pip install --quiet -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# the exe icon is drawn by the same code as the tray icon
$icon = Join-Path $PSScriptRoot "NextPull.ico"
py -3 -c "import sys; sys.path.insert(0,'.'); from tray import make_icon; make_icon(256).save(r'$icon', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"

Remove-Item (Join-Path $root "dist\NextPull") -Recurse -Force -ErrorAction SilentlyContinue
py -3 -m PyInstaller --noconfirm --clean --onedir --windowed --name NextPull --icon $icon `
    --distpath (Join-Path $root "dist") --workpath (Join-Path $root "build\work") --specpath (Join-Path $root "build\work") `
    --add-data "$root\web;web" --hidden-import pystray._win32 --collect-submodules webview.platforms `
    (Join-Path $root "nextpull.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$out = Join-Path $root "dist\NextPull"
$rclone = Join-Path $root "tools\rclone\rclone.exe"
if (Test-Path $rclone) {
    New-Item -ItemType Directory -Force -Path (Join-Path $out "tools\rclone") | Out-Null
    Copy-Item $rclone (Join-Path $out "tools\rclone\rclone.exe") -Force
    Write-Host "rclone.exe included."
} else {
    Write-Warning "tools\rclone\rclone.exe not found: the build does not include rclone (see tools\Fetch-rclone.ps1)."
}
Copy-Item (Join-Path $root "README.md") $out -Force
Write-Host "Built: $out\NextPull.exe"
