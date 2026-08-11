<#
.SYNOPSIS
    Обратный SSH-туннель: порт на ВМ мониторинга указывает на локальный apollo.

.DESCRIPTION
    Пока apollo работает на рабочей машине, Zabbix и SolarWinds не могут
    достучаться до него напрямую. Туннель разворачивает путь: на ВМ поднимается
    127.0.0.1:<RemotePort>, и всё, что туда приходит, попадает на локальный порт.

    Слушается только петля самой ВМ — наружу ничего не открывается,
    GatewayPorts на сервере включать не требуется.

    Соединение переустанавливается само: обрыв канала не должен означать
    потерю событий на весь оставшийся день.

.EXAMPLE
    .\tools\tunnel.ps1 -VmHost 10.0.0.5 -User ubuntu -KeyFile ~\.ssh\zabbix.pem

.EXAMPLE
    # Второй туннель для второй ВМ — запускается отдельным окном
    .\tools\tunnel.ps1 -VmHost 10.0.0.6 -User admin -RemotePort 18080
#>

[CmdletBinding()]
param(
    # Адрес ВМ, на которой работает Zabbix или SolarWinds
    [Parameter(Mandatory = $true)]
    [string]$VmHost,

    [string]$User = 'ubuntu',

    # Порт на ВМ, куда будет ходить система мониторинга
    [int]$RemotePort = 18080,

    # Локальный порт apollo (nginx фронта отдаёт и /api, и /health)
    [int]$LocalPort = 80,

    # Приватный ключ; если не указан, используется ssh-agent или ключ по умолчанию
    [string]$KeyFile,

    # Пауза перед переподключением после обрыва
    [int]$RetryDelaySeconds = 5
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw 'Клиент ssh не найден. Установите OpenSSH Client: Settings → Optional features.'
}

# Проверяем, что локальный apollo вообще отвечает: туннель до мёртвого порта
# выглядит рабочим, а события молча теряются
try {
    $health = Invoke-WebRequest -Uri "http://127.0.0.1:$LocalPort/health" -TimeoutSec 5 -UseBasicParsing
    Write-Host "Локальный apollo отвечает: $($health.StatusCode)" -ForegroundColor Green
} catch {
    Write-Warning "apollo на 127.0.0.1:$LocalPort не отвечает. Поднимите стенд: docker compose up -d"
}

$sshArgs = @(
    '-N'                                   # только туннель, без интерактивной оболочки
    '-R', "${RemotePort}:127.0.0.1:${LocalPort}"
    '-o', 'ServerAliveInterval=30'         # молчащий канал определяется за минуту
    '-o', 'ServerAliveCountMax=2'
    '-o', 'ExitOnForwardFailure=yes'       # занятый порт на ВМ — это ошибка, а не тишина
    '-o', 'StrictHostKeyChecking=accept-new'
)
if ($KeyFile) { $sshArgs += @('-i', $KeyFile) }
$sshArgs += "$User@$VmHost"

Write-Host ""
Write-Host "Туннель: $VmHost`:$RemotePort  ->  127.0.0.1:$LocalPort (локальный apollo)" -ForegroundColor Cyan
Write-Host "Проверка со стороны ВМ:  curl http://127.0.0.1:$RemotePort/health"
Write-Host "URL для вебхуков:        http://127.0.0.1:$RemotePort/api/v1/ingest/{zabbix|solarwinds}"
Write-Host "Остановка — Ctrl+C"
Write-Host ""

while ($true) {
    $started = Get-Date
    & ssh @sshArgs
    $lived = [int]((Get-Date) - $started).TotalSeconds

    # Мгновенный выход означает отказ до установки канала (неверный ключ,
    # занятый порт) — повторять с той же скоростью бессмысленно
    if ($lived -lt 5) {
        Write-Warning "Соединение оборвалось сразу (код $LASTEXITCODE). Проверьте ключ, пользователя и занятость порта $RemotePort на ВМ."
        Start-Sleep -Seconds ([Math]::Max($RetryDelaySeconds, 15))
    } else {
        Write-Host "$(Get-Date -Format 'HH:mm:ss') туннель прожил $lived с, переподключаюсь…" -ForegroundColor Yellow
        Start-Sleep -Seconds $RetryDelaySeconds
    }
}
