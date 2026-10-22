## Лаба 5 — Zero-trust сеть с доказательством

### Стенд

| Компонент | Конфигурация |
| --- | --- |
| Хост | Debian GNU/Linux 13.7 (trixie), 8 vCPU, 15 GiB RAM, 3.1 GiB swap |
| Кластер | kind v0.27.0, Kubernetes 1.33.1, 1 control-plane + 2 worker-ноды |
| Конфигурация kind | `disableDefaultCNI: true`; pod CIDR `10.244.0.0/16`; service CIDR `10.96.0.0/12` |
| Управление кластером | kubectl v1.32.3, Helm v4.3.0 |
| Container runtime | Docker Engine v26.1.5, используется kind |
| CNI | Calico v3.30.3 (без eBPF dataplane); Felix в качестве агента на нодах; Goldmane и Whisker для визуализации flow logs |
| Сервисы | `api`, `worker`, `postgres` из `shop` Helm chart лабы 3 |
| Мониторинг | Стек Prometheus/Grafana из лабы 2 |

Инициализация кластера:
```bash
kind create cluster \
    --name lab5 \
    --config deploy/kind-cluster.yaml
```

<details>
<summary>Демонстрация</summary>

<img src="./scrs/part0-00.png" width=500>
</details>

---

Конфигурация кластера — [`deploy/kind-cluster.yaml`](deploy/kind-cluster.yaml). Поскольку
`kindnet` отключён, ноды остаются `NotReady` до установки CNI.

### Часть 0 — Выбери CNI

Выбран **Calico**. Для этой лаборатории это осознанный выбор в пользу классического
сетевого dataplane: Calico надёжно реализует Kubernetes `NetworkPolicy`, а правила политики
можно сопоставить с привычными механизмами Linux filtering — iptables/nftables и правилами,
которые устанавливает Felix. Поэтому нам будет проще связать абстракции Kubernetes
(`NetworkPolicy`, pod labels, ingress/egress) с тем, что реально происходит на ноде.

Calico подходит для задачи zero-trust не хуже Cilium: ограничения строятся по labels и портам,
а не по эфемерным pod IP. При этом мы сознательно не включаем Calico eBPF dataplane, чтобы
не смешивать в одной работе два разных механизма доставки и enforcement.

Calico не предоставляет Hubble, поэтому используем его собственный путь наблюдаемости:

- **Goldmane** агрегирует сетевые flow logs, включая verdict и сведения о применённой policy;
- **Whisker** показывает эти flows в веб-интерфейсе и позволяет отфильтровать denied traffic по source,
  destination, namespace и policy.

Установка Calico в качестве CNI:
```bash
kubectl --context kind-lab5 create -f \
    https://raw.githubusercontent.com/projectcalico/calico/v3.30.3/manifests/tigera-operator.yaml
kubectl --context kind-lab5 -n tigera-operator wait \
  --for=condition=Available deployment/tigera-operator --timeout=120s
kubectl --context kind-lab5 apply \
  -f deploy/calico-installation.yaml
```

<details>
<summary>Демонстрация</summary>

<img src="./scrs/part0-01.png" width=100%>
</details>

---

