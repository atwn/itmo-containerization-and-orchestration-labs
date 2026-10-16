$ErrorActionPreference = 'Stop'

function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Command
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Command
        if ($LASTEXITCODE -ne 0) {
            throw "$Name — код выхода $LASTEXITCODE"
        }
    } finally {
        $ErrorActionPreference = $previous
    }
}

$root = Split-Path -Parent $PSScriptRoot
$deploy = Join-Path $root 'deploy'
$cluster = 'shop4'
$ns = 'shop4'

Write-Host '==> kind cluster' -ForegroundColor Cyan
$previous = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$existing = kind get clusters 2>$null
$ErrorActionPreference = $previous
if ($existing -notcontains $cluster) {
    Invoke-Native 'kind create cluster' { kind create cluster --config (Join-Path $deploy 'kind-cluster.yaml') }
} else {
    Write-Host "кластер $cluster уже есть"
}
Invoke-Native 'kubectl config' { kubectl config use-context "kind-$cluster" }

Write-Host '==> ограничение ресурсов нод (делаем кластер тесным умышленно)' -ForegroundColor Cyan
Invoke-Native 'docker update worker' { docker update --memory=3g --memory-swap=3g --cpus=2 "$cluster-worker" }
Invoke-Native 'docker update worker2' { docker update --memory=3g --memory-swap=3g --cpus=2 "$cluster-worker2" }
Invoke-Native 'docker update control-plane' { docker update --memory=1.5g --memory-swap=1.5g --cpus=1 "$cluster-control-plane" }

Write-Host '==> namespace' -ForegroundColor Cyan
Invoke-Native 'kubectl create ns' {
    kubectl get ns $ns 2>$null
    if ($LASTEXITCODE -ne 0) { kubectl create namespace $ns }
}

Write-Host '==> сборка образов api/worker/batch' -ForegroundColor Cyan
Invoke-Native 'docker build api' { docker build -f (Join-Path $root 'Dockerfile.api') -t lab4/api:0.1.0 $root }
Invoke-Native 'docker build worker' { docker build -f (Join-Path $root 'Dockerfile.worker') -t lab4/worker:0.1.0 $root }
Invoke-Native 'docker build batch' { docker build -f (Join-Path $root 'Dockerfile.batch') -t lab4/batch:0.1.0 $root }
Invoke-Native 'kind load' { kind load docker-image lab4/api:0.1.0 lab4/worker:0.1.0 lab4/batch:0.1.0 --name $cluster }

Write-Host '==> helm repos' -ForegroundColor Cyan
Invoke-Native 'helm repo add cnpg' { helm repo add cnpg https://cloudnative-pg.github.io/charts }
Invoke-Native 'helm repo add prometheus-community' { helm repo add prometheus-community https://prometheus-community.github.io/helm-charts }
Invoke-Native 'helm repo update' { helm repo update }

Write-Host '==> CloudNativePG operator' -ForegroundColor Cyan
Invoke-Native 'helm cnpg' {
    helm upgrade --install cnpg cnpg/cloudnative-pg `
        --version 0.29.1 `
        --namespace cnpg-system --create-namespace `
        -f (Join-Path $root 'platform\cnpg-values.yaml') `
        --wait --timeout 5m
}

Write-Host '==> kube-prometheus-stack (Prometheus + Alertmanager + Grafana)' -ForegroundColor Cyan
Invoke-Native 'helm kps' {
    helm upgrade --install kps prometheus-community/kube-prometheus-stack `
        --version 90.1.1 `
        --namespace monitoring --create-namespace `
        -f (Join-Path $deploy 'values\kube-prometheus-stack.yaml') `
        --wait --timeout 10m
}

Write-Host '==> shop (api, worker, batch, postgres)' -ForegroundColor Cyan
Invoke-Native 'helm shop' {
    helm upgrade --install shop (Join-Path $root 'charts\shop') --namespace $ns --wait --timeout 5m
}

Write-Host ''
Write-Host 'Готово. Дальше: .\scripts\port-forward.ps1' -ForegroundColor Green
kubectl -n $ns get pods -o wide
