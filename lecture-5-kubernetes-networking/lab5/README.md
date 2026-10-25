## Лаба 5 — Zero-trust сеть с доказательством

### Стенд

| Компонент | Конфигурация  |
|:---------:|:--------------|
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

### Часть 1 — Разверни shop и исследуй сеть пода

Развернули сервисы приложения "shop" из 3-й лабораторной на двух worker-нодах в кластере kind.

<details>
<summary>Предустановка</summary>

```bash
# (optional) local-path as default storageclass (unless already provided by kind):
kubectl --context kind-lab5 apply -f \
    https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.37/deploy/local-path-storage.yaml
kubectl --context kind-lab5 patch storageclass local-path --type merge \
    -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'

# CloudNativePG
helm repo add cnpg https://cloudnative-pg.github.io/charts
helm repo update
helm --kube-context kind-lab5 upgrade --install cnpg cnpg/cloudnative-pg \
    --version 0.29.1 \
    --namespace cnpg-system \
    --create-namespace \
    -f platform/cnpg-values.yaml \
    --wait \
    --timeout 10m

# (optional) re-initialize CNI if needed (e.g. when token is expired):
kubectl --context kind-lab5 -n calico-system \
  rollout restart daemonset/calico-node
kubectl --context kind-lab5 -n calico-system \
  rollout status daemonset/calico-node --timeout=180s

# Observability stack (Prometheus + Grafana):
helm --kube-context kind-lab5 upgrade --install kps \
  prometheus-community/kube-prometheus-stack \
  --version 90.1.1 \
  --namespace obs \
  --create-namespace \
  -f deploy/kube-prometheus-stack.yaml \
  --wait --timeout 15m

```
</details>

---

```bash
# build and load images into the kind cluster:
docker build -f Dockerfile.api -t ghcr.io/shop-lab/api:v3 .
docker build -f Dockerfile.worker -t ghcr.io/shop-lab/worker:v2 .

kind load docker-image ghcr.io/shop-lab/api:v3 --name lab5
kind load docker-image ghcr.io/shop-lab/worker:v2 --name lab5

# bring up 'shop' services via Helm:
helm --kube-context kind-lab5 upgrade \
  --install shop charts/shop \
  --namespace shop --create-namespace \
  --wait --timeout 15m
```

<details>
<summary>Демонстрация</summary>

<img src="./scrs/part1-00-b.png" width=100%>
</details>

---

#### Инспекция сети

##### 1. Сеть внутри pod'а

Приняли решение использовать эфемерный debug-контейнер для работы с сетью внутри pod'а,
так как минимальный базовый образ `api` и `worker` не содержит необходимых утилит
(ситуация вполне типичная и для продуктового окружения).

Внутри `api` pod'а на ноде `lab5-worker`:
* `if19` - veth на ноде, к которому "подключен" сетевой интерфейс pod'а
* `10.244.219.204` - ip адрес виртуального NIC pod'а из пула адресов Calico, совпадает с адресом pod'а на ноде
* `/32` - маска подсети, означает что адрес один, а значит маршрут проходит через виртуальный next-hop
* `169.254.1.1` - link-local адрес, означает что маршрутизация осуществляется через Calica контроллер на ноде
* `10.96.0.10` - ClusterIP CoreDNS кластера, т.е. разрешение имён происходит внутри кластера

<details>
<summary>Демонстрация</summary>

<img src="./scrs/part1-01-api.png" width=100%>
</details>

---

Внутри `worker` pod'а на ноде `lab5-worker-2`:
* `if16` - veth на ноде, к которому "подключен" сетевой интерфейс pod'а
* `10.244.124.73` - ip адрес виртуального NIC pod'а из пула адресов Calico, совпадает с адресом pod'а на ноде
* `/32` - маска подсети, означает что адрес один, а значит маршрут проходит через виртуальный next-hop
* `169.254.1.1` - link-local адрес, означает что маршрутизация осуществляется через Calica контроллер на ноде
* `10.96.0.10` - ClusterIP CoreDNS кластера, т.е. разрешение имён происходит внутри кластера

<details>
<summary>Демонстрация</summary>

<img src="./scrs/part1-01-worker.png" width=100%>
</details>

---

##### 2. Связь pod-интерфейса с veth на ноде

ifindex 19 `api` pod'a соответствует veth интерфейсу под соответствующим номером на `lab5-worker` ноде.

<details>
<summary>Демонстрация</summary>

<img src="./scrs/part1-01-node-w1.png" width=100%>
</details>

---

ifindex 16 `worker` pod'a соответствует veth интерфейсу под соответствующим номером на `lab5-worker2` ноде.

<details>
<summary>Демонстрация</summary>

<img src="./scrs/part1-01-node-w2.png" width=100%>
</details>

---

##### 3. Межнодовый трафик

Поробовали достучаться из pod'a `api` в pod `worker` по IP и по DNS-имени.
Для демонстрации выбрали pod'ы, расположенные на двух разных нодах.

<details>
<summary>Демонстрация</summary>

<img src="./scrs/part1-01-connection.png" width=100%>
</details>

### Часть 2 — Докажи непрерывность через хаос подов

Заменили ClusterIP на NodePort в конфигурации `api` сервиса в [values.yaml](./charts/shop/values.yaml) и обновили приложение.

```bash
yq -y -i '.api.service.type = "NodePort"' charts/shop/values.yaml

helm --kube-context kind-lab5 --namespace shop \
    upgrade shop charts/shop --reset-values \
    --wait --timeout 5m
```

<details>
<summary>Демонстрация</summary>

<img src="./scrs/part2-00.png" width=100%>
</details>

---

Выводы:
* при наличии NodePort сервис становится доступным извне по IP-адресу ноды
* NodePort сам по себе не обеспечивает балансировку

#### Запуск хаос-теста

Для проверки непрерывности сервиса подготовили три вспомогательных скрипта:

- [`scripts/load.sh`](./scripts/load.sh) — генерирует запросы к `/health` с заданным
  количеством запросов в секунду и выводит результат, HTTP-статус и latency;
- [`scripts/watch.sh`](./scripts/watch.sh) — показывает состояние Pod'ов либо `EndpointSlice` в режиме обновления раз в секунду;
- [`scripts/chaos.sh`](./scripts/chaos.sh) — каждые 5–10 секунд выбирает случайный `api` Pod и удаляет его.
  После удаления Deployment или другой контроллер восстанавливает нужное количество реплик.

Для начального сравнения записали контрольный прогон без удаления Pod'ов. В качестве
базовых показателей использовали число успешных и неуспешных запросов, а также
latency. В хаос-тесте дополнительно наблюдали удаления Pod'ов, переходы Pod'ов в
состояния `Terminating`/`Running`, изменение IP-адресов и состава `EndpointSlice`.

В моменты удаления Pod'ов незначительно менялaсь latency, но ни один запрос не упал.

<details>
<summary>Демонстрация</summary>

<img src="./scrs/part2-01-chaos.png" width=100%>
</details>

---

Для визуального сравнения показателей latency в моменты удаления подов решили
построить граф при помощи Grafana + Alloy + Loki.
Для этого поместили генератор трафика в дополнительный под на кластере,
чтобы Alloy смог спарсить логи (см. [load-generator.yaml](./deploy/load-generator.yaml)).
Это также позволило нам задействовать CoreDNS внутри кластера,
а не дёргать `api`-сервис по IP ноды и NodePort, как мы это делали в предыдущем эксперименте.
При этом входящий трафик теперь распределяется равномерно между обеими нодами за счёт name resolution в CoreDNS кластера.

**(to be continued...)**
```bash
# install Loki
helm --kube-context kind-lab5 upgrade --install loki grafana/loki \
  --namespace obs --create-namespace \
  -f deploy/loki.yaml \
  --wait --timeout 15m

# install Alloy
```

#### Ответы на вопросы части 2

**Чем `NodePort` отличается от `LoadBalancer` и headless-сервиса?**

`NodePort` открывает один и тот же порт на каждой ноде кластера. Запрос к
`NodeIP:NodePort` попадает в Service и затем направляется на один из готовых
backend'ов. Это простой способ предоставить сервис наружу без отдельного
балансировщика. В нашем kind-кластере внешний `load.sh` обращается к
`172.18.0.3:32590` именно через `NodePort`.

`LoadBalancer` — это декларация, что сервису нужен внешний балансировщик. В
облачном Kubernetes соответствующий provider обычно создаёт внешний LB и выдаёт
`EXTERNAL-IP`. В обычном kind-кластере Kubernetes сам по себе не создаёт облачный
балансировщик, поэтому `EXTERNAL-IP` может остаться пустым. Для такой среды
потребовалась бы дополнительная реализация, например MetalLB. Внешний LB затем
может передавать трафик в `NodePort` или напрямую в dataplane — это зависит от
реализации provider'а.

Headless-сервис создаётся с `clusterIP: None`. У него нет виртуального ClusterIP
и обычного Service-level load balancing через kube-proxy. DNS возвращает адреса
самих Pod'ов, а выбор backend'а выполняет клиент или библиотека service discovery.
Такой сервис удобен, когда клиенту нужны индивидуальные адреса реплик, например
для StatefulSet, но он не предназначен для предоставления сервиса наружу
кластера.

**Что происходит при удалении Pod'а?**

Service не является самим Pod'ом. Это стабильная абстракция адресации: ClusterIP и
набор backend'ов, выбранных по label selector. Реальные адреса backend'ов находятся
в `EndpointSlice`.

Когда Pod становится неготовым или удаляется, контроллер EndpointSlice изменяет
состояние соответствующего endpoint. В частности, `conditions.ready` становится
`false` либо endpoint исключается из списка готовых backend'ов. `kube-proxy` следит
за Service и EndpointSlice и обновляет dataplane ноды — в нашем случае правила
Service-маршрутизации в iptables. Поэтому новые запросы не должны направляться на
неготовый Pod.

После этого Deployment создаёт новый Pod. Новый Pod обычно получает другой IP-адрес,
поэтому EndpointSlice изменяется дважды: старый endpoint удаляется или помечается
как недоступный, а новый добавляется после прохождения readiness probe. Именно
поэтому в ходе эксперимента важно было наблюдать не только список Pod'ов, но и
`EndpointSlice`.

**(to be continued...)**