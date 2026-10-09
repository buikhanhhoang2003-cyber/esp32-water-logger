<#
.SYNOPSIS
    Build + nạp firmware cầu nối RS485 cho ESP32 và mở app DDSU666.

.DESCRIPTION
    ddsu666.bat [lệnh] [-Port COMx]

      all      (mặc định) build firmware, nạp vào ESP32, mở app và kết nối luôn
      build    chỉ build firmware
      flash    build (nếu cần) và nạp firmware
      run      chỉ mở app (cài pyserial nếu thiếu)
      monitor  xem cổng serial của ESP32 (gõ PING + Enter để thử, Ctrl+] để thoát)
      clean    xóa thư mục build của firmware
      test     chạy bộ test của app
      ports    liệt kê các cổng COM

    Không truyền -Port thì script tự tìm cổng USB-serial (CH340/CP210x...); có nhiều cổng thì hỏi.
    ESP-IDF: dùng môi trường đang mở nếu có idf.py, nếu không thì tìm -IdfPath, $env:IDF_PATH,
    rồi C:\Espressif\frameworks\esp-idf-v5.* (mới nhất).

.EXAMPLE
    .\ddsu666.bat
    .\ddsu666.bat all -Port COM16
    .\ddsu666.bat flash -Port COM16 -FlashBaud 115200
    .\ddsu666.bat run
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("all", "build", "flash", "run", "monitor", "clean", "test", "ports")]
    [string]$Action = "all",
    [string]$Port = "",
    [int]$FlashBaud = 460800,
    [string]$IdfPath = ""
)

$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root = $PSScriptRoot
$Firmware = Join-Path $Root "firmware"
$Gui = Join-Path $Root "gui"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Stop-WithError([string]$Text) {
    Write-Host ""
    Write-Host "LỖI: $Text" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- COM ports

function Get-SerialPorts {
    Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '\(COM\d+\)' } |
        ForEach-Object {
            [pscustomobject]@{ Port = ($_.Name -replace '^.*\((COM\d+)\).*$', '$1'); Name = $_.Name }
        } |
        Sort-Object { [int]($_.Port -replace '\D', '') }
}

function Resolve-Port {
    if ($Port) { return $Port.ToUpper() }
    $ports = @(Get-SerialPorts)
    if ($ports.Count -eq 0) {
        Stop-WithError "Không thấy cổng COM nào. Cắm ESP32 vào USB (cần driver CH340/CP210x) rồi chạy lại."
    }
    $usb = @($ports | Where-Object { $_.Name -match 'CH34|CH91|CP210|USB.?SERIAL|USB.?UART|FTDI|Silicon Labs|JTAG' })
    if ($usb.Count -eq 1) { $pick = $usb[0] }
    elseif ($ports.Count -eq 1) { $pick = $ports[0] }
    else {
        Write-Host "Có nhiều cổng COM:"
        for ($i = 0; $i -lt $ports.Count; $i++) { Write-Host ("  [{0}] {1}" -f ($i + 1), $ports[$i].Name) }
        $answer = Read-Host "Chọn số thứ tự cổng của ESP32"
        $index = 0
        if (-not [int]::TryParse($answer, [ref]$index) -or $index -lt 1 -or $index -gt $ports.Count) {
            Stop-WithError "Lựa chọn không hợp lệ: '$answer'"
        }
        $pick = $ports[$index - 1]
    }
    Write-Host "Dùng cổng $($pick.Name)"
    return $pick.Port
}

# ---------------------------------------------------------------- Python (for the app)

function Find-Python {
    # Resolve before activating ESP-IDF: its export script puts its own venv Python first in PATH.
    $candidates = @()
    foreach ($command in @(Get-Command python -All -ErrorAction SilentlyContinue)) {
        if ($command.Source -and $command.Source -notmatch 'WindowsApps') { $candidates += $command.Source }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $fromLauncher = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $fromLauncher) { $candidates += $fromLauncher.Trim() }
    }
    foreach ($exe in $candidates) {
        & $exe -c "import tkinter" 2>$null
        if ($LASTEXITCODE -eq 0) { return $exe }
    }
    Stop-WithError "Không tìm thấy Python 3 có Tkinter. Cài Python từ https://www.python.org (chọn 'tcl/tk and IDLE')."
}

function Install-AppRequirements([string]$Python) {
    & $Python -c "import serial" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Step "Cài thư viện pyserial"
        & $Python -m pip install -r (Join-Path $Gui "requirements.txt")
        if ($LASTEXITCODE -ne 0) { Stop-WithError "Không cài được pyserial (kiểm tra mạng / pip)." }
    }
}

function Start-App([string]$Python, [string[]]$AppArgs) {
    Install-AppRequirements $Python
    Write-Step "Mở app DDSU666 $($AppArgs -join ' ')"
    Push-Location $Gui
    try { & $Python (Join-Path $Gui "ddsu666_gui.py") @AppArgs } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { Stop-WithError "App thoát với mã lỗi $LASTEXITCODE." }
}

# ---------------------------------------------------------------- ESP-IDF

function Find-IdfExport {
    $roots = @()
    if ($IdfPath) { $roots += $IdfPath }
    elseif ($env:IDF_PATH) { $roots += $env:IDF_PATH }
    else {
        $frameworks = "C:\Espressif\frameworks"
        if (Test-Path $frameworks) {
            $roots += Get-ChildItem $frameworks -Directory -Filter "esp-idf-v5*" |
                Sort-Object Name -Descending | ForEach-Object { $_.FullName }
        }
    }
    foreach ($candidate in $roots) {
        $export = Join-Path $candidate "export.ps1"
        if (Test-Path $export) { return $export }
    }
    Stop-WithError ("Không tìm thấy ESP-IDF. Mở 'ESP-IDF 5.3 PowerShell' rồi chạy lại, " +
                    "hoặc truyền -IdfPath C:\duong\dan\esp-idf.")
}

function Invoke-Idf([string[]]$IdfArgs) {
    idf.py -C $Firmware @IdfArgs
    if ($LASTEXITCODE -ne 0) { Stop-WithError "idf.py $($IdfArgs -join ' ') thất bại (mã $LASTEXITCODE)." }
}

# ---------------------------------------------------------------- main

if ($Action -eq "ports") {
    $ports = @(Get-SerialPorts)
    if ($ports.Count -eq 0) { Write-Host "Không có cổng COM nào." } else { $ports | ForEach-Object { Write-Host $_.Name } }
    exit 0
}

$Python = $null
if ($Action -in @("all", "run", "test")) { $Python = Find-Python }

if ($Action -in @("all", "build", "flash", "monitor", "clean")) {
    if (Get-Command idf.py -ErrorAction SilentlyContinue) {
        Write-Host "Dùng ESP-IDF đang mở: $env:IDF_PATH"
    } else {
        $export = Find-IdfExport
        if (-not $env:IDF_TOOLS_PATH -and (Test-Path "C:\Espressif\python_env")) { $env:IDF_TOOLS_PATH = "C:\Espressif" }
        $exportLog = Join-Path $env:TEMP "ddsu666_idf_export.log"
        Write-Step "Kích hoạt ESP-IDF: $(Split-Path $export -Parent)"
        # Dot-sourced at script scope so that the idf.py function it defines stays available.
        . $export *> $exportLog
        if (-not (Get-Command idf.py -ErrorAction SilentlyContinue)) {
            Stop-WithError "Kích hoạt ESP-IDF thất bại, xem $exportLog"
        }
    }
}

switch ($Action) {
    "build" {
        Write-Step "Build firmware"
        Invoke-Idf @("build")
    }
    "flash" {
        $comPort = Resolve-Port
        Write-Step "Build và nạp firmware vào $comPort"
        Invoke-Idf @("-p", $comPort, "-b", "$FlashBaud", "flash")
        Write-Host "Nạp xong. Mở app: .\ddsu666.bat run" -ForegroundColor Green
    }
    "monitor" {
        $comPort = Resolve-Port
        Write-Step "Monitor $comPort (gõ PING + Enter để thử, Ctrl+] để thoát)"
        Invoke-Idf @("-p", $comPort, "monitor")
    }
    "clean" {
        Write-Step "Xóa thư mục build"
        Invoke-Idf @("fullclean")
    }
    "test" {
        Write-Step "Chạy test của app"
        Install-AppRequirements $Python
        Push-Location $Gui
        try { & $Python -m unittest discover -s tests } finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { Stop-WithError "Có test thất bại." }
    }
    "run" {
        $appArgs = @()
        if ($Port) { $appArgs = @("--port", $Port.ToUpper(), "--mode", "bridge") }
        Start-App $Python $appArgs
    }
    "all" {
        $comPort = Resolve-Port
        Write-Step "Build firmware"
        Invoke-Idf @("build")
        Write-Step "Nạp firmware vào $comPort"
        Invoke-Idf @("-p", $comPort, "-b", "$FlashBaud", "flash")
        Start-App $Python @("--port", $comPort, "--mode", "bridge", "--connect")
    }
}
exit 0
