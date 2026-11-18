GOLANGCI := bin/golangci-lint
GOLANGCI_VERSION := v1.61.0

.PHONY: tools build test lint run generate migrate-up db-reset seed

tools:
	GOBIN=$(CURDIR)/bin go install github.com/golangci/golangci-lint/cmd/golangci-lint@$(GOLANGCI_VERSION)
	GOBIN=$(CURDIR)/bin go install github.com/sqlc-dev/sqlc/cmd/sqlc@v1.27.0

build:
	go build -o bin/ferrydesk ./cmd/ferrydesk

lint:
	$(GOLANGCI) run ./...

test:
	go test ./...

run:
	go run ./cmd/ferrydesk

generate:
	sqlc generate

migrate-up:
	migrate -path migrations -database "$$FERRYDESK_DATABASE_URL" up

db-reset:
	./scripts/reset-db.sh

seed:
	psql "$$FERRYDESK_DATABASE_URL" -f scripts/seed.sql
