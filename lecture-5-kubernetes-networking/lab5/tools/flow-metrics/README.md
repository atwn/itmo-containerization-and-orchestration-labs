# Whisker flow metrics for lab5

This helper reads the HTTP API used by Whisker **v3.30.3**:
`/whisker-backend/flows`. It polls every 15 seconds for source namespace
`shop`, with `startTimeGte=now-120` and `startTimeLt=now`.
The API is version-specific; revalidate it when upgrading Calico.

- `shop_flow_denied_packets_window`: packet observations (`packets_in +
  packets_out`) in Deny records over the requested rolling window. A **gauge**,
  not a cumulative counter. Do not use `rate()` or sum successive scrapes.
  Labels retain Src/Dst reporter; values are not unique connection counts.
  Goldmane aggregation and delivery delay make the window approximate.
- `shop_flow_unexpected_denied_packets_window`: the subset for API/worker
  to PostgreSQL TCP 5432 or CoreDNS UDP/TCP 53. These routes should be allowed
  by this lab's matrix. Stray-Pod denials are excluded from this subset.
- `shop_flow_denied_records_window`: number of Deny aggregation records,
  not the number of failed requests or unique connections.
- `shop_flow_collector_success`: API fetch and schema validation succeeded.
  On failure the data gauges are omitted; failure must not become a zero.
- `shop_flow_last_observed_timestamp_seconds`: newest observed flow end,
  including Allow records, for detecting stale telemetry. Ongoing chaos-load
  supplies traffic in this lab; an idle namespace can legitimately be stale.

The helper has no Kubernetes API credentials, host access, or packet capture
capabilities. The chart places it in `obs`, grants access only to Whisker/DNS,
and permits Prometheus to scrape it. The Whisker operator policy is left intact;
a separate additive ingress policy allows this collector.

The existing node-exporter supplies conntrack occupancy (all node traffic, not
just shop or just TCP). The existing CoreDNS ServiceMonitor supplies DNS metrics.
CoreDNS cannot count queries that policy drops before they reach the server.

Build and run tests:

```bash
docker build -f Dockerfile.flow-metrics -t ghcr.io/shop-lab/flow-metrics:v1 .
```

API schema reference:
https://github.com/projectcalico/calico/blob/v3.30.3/whisker-backend/pkg/apis/v1/flows.go
