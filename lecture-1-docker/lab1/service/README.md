## Lab 1. API

Standalone Go HTTP service for Part 0. Requires Go 1.20 or newer; uses only the standard library and the HTTP server that is part of the standard library.

### Running
From this directory:

```sh
go build -o /tmp/api .
/tmp/api
```

The server listens on `:8080` (all interfaces). In another terminal:

```sh
curl http://localhost:8080/health
curl 'http://localhost:8080/eat?mb=1'
curl http://localhost:8080/burn
```

- `/health` returns `ok`.
- `/eat?mb=N` retains N MiB (1 MiB = 1,048,576 bytes) per request. Every page is written so the allocation commits memory, and references are retained so garbage collection cannot reclaim it. Requests accumulate; there is intentionally no memory cap. Large requests can exhaust memory or trigger a cgroup OOM kill. Missing, invalid, nonpositive, and overflowing values return 400.
- `/burn` немедленно отвечает и запускает одну занятую горутину на каждый запрос, потребляя примерно одно ядро CPU на горутину. Повторные запросы увеличивают нагрузку. Планировщик Go сохраняет отзывчивость HTTP-сервера; ограничения CPU могут уменьшить наблюдаемое потребление.

Stop with Ctrl+C to release memory and stop CPU load. Unsupported methods return 405; unknown paths return 404.

### Validation

```sh
gofmt -w main.go main_test.go
go test ./...
go test -race ./...
```
