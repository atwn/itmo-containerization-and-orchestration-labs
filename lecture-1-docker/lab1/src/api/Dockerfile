FROM golang:1.27.1 AS build

WORKDIR /src

COPY go.mod ./
RUN go mod download
COPY . ./

RUN CGO_ENABLED=0 GOOS=linux \
    go build -trimpath -ldflags="-s -w" -o /out/api .

FROM scratch

COPY --from=build /out/api /api

EXPOSE 8080

ENTRYPOINT ["/api"]
