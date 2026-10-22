FROM golang:1.22-alpine AS build

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY main.go ./
COPY internal ./internal
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/worker .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/worker /worker
EXPOSE 8080
ENTRYPOINT ["/worker"]
