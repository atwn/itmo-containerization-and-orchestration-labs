# Forwards api, Grafana and Prometheus to localhost so you can click around and
# take the screenshots lab.md asks for. Each forward runs as a background job
# named pf-<name>; stop them all with: Get-Job -Name 'pf-*' | Stop-Job -PassThru | Remove-Job

function Start-PortForward {
    param(
        [string]$Name,
        [string]$Namespace,
        [string]$Target,
        [int]$LocalPort,
        [int]$RemotePort
    )
    Get-Job -Name "pf-$Name" -ErrorAction SilentlyContinue | Stop-Job -PassThru | Remove-Job -ErrorAction SilentlyContinue
    Start-Job -Name "pf-$Name" -ScriptBlock {
        param($ns, $target, $local, $remote)
        while ($true) {
            kubectl -n $ns port-forward $target "${local}:${remote}"
            Start-Sleep -Seconds 2
        }
    } -ArgumentList $Namespace, $Target, $LocalPort, $RemotePort | Out-Null
    Write-Host "$Name -> http://localhost:$LocalPort" -ForegroundColor Green
}

Start-PortForward -Name 'api'        -Namespace 'shop4'      -Target 'svc/shop-api'       -LocalPort 18080 -RemotePort 80
Start-PortForward -Name 'grafana'    -Namespace 'monitoring' -Target 'svc/grafana'        -LocalPort 13000 -RemotePort 80
Start-PortForward -Name 'prometheus' -Namespace 'monitoring' -Target 'svc/kps-prometheus' -LocalPort 19090 -RemotePort 9090

Write-Host ''
Write-Host 'Grafana: http://localhost:13000  (admin / admin)' -ForegroundColor Cyan
Write-Host 'Prometheus: http://localhost:19090' -ForegroundColor Cyan
Write-Host 'api: http://localhost:18080' -ForegroundColor Cyan
Write-Host 'Остановить всё: Get-Job -Name ''pf-*'' | Stop-Job -PassThru | Remove-Job' -ForegroundColor DarkGray
