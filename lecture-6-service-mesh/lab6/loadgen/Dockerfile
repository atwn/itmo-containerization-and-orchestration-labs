FROM golang:1.22-alpine AS build

WORKDIR /src
COPY go.mod ./
COPY main.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/loadgen .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/loadgen /loadgen
EXPOSE 8080
ENTRYPOINT ["/loadgen"]
