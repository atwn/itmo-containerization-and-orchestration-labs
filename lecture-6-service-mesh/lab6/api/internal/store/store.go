package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Order is persisted so the mesh exercises a real dependency rather than an
// in-memory handler.
type Order struct {
	ID          int64           `json:"id"`
	Payload     json.RawMessage `json:"payload"`
	Processed   bool            `json:"processed"`
	CreatedAt   time.Time       `json:"created_at"`
	ProcessedAt *time.Time      `json:"processed_at,omitempty"`
}

type Store struct {
	pool *pgxpool.Pool
	mu   sync.Mutex
	init bool
}

func Open(ctx context.Context, databaseURL string) (*Store, error) {
	if databaseURL == "" {
		return nil, errors.New("DATABASE_URL is required")
	}
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		return nil, fmt.Errorf("create postgres pool: %w", err)
	}
	return &Store{pool: pool}, nil
}

func (s *Store) Close() { s.pool.Close() }

func (s *Store) ensureSchema(ctx context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.init {
		return nil
	}
	const schema = `
		CREATE TABLE IF NOT EXISTS orders (
			id BIGSERIAL PRIMARY KEY,
			payload JSONB NOT NULL,
			processed BOOLEAN NOT NULL DEFAULT FALSE,
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			processed_at TIMESTAMPTZ
		);
		ALTER TABLE orders
			ADD COLUMN IF NOT EXISTS processed BOOLEAN NOT NULL DEFAULT FALSE;
		ALTER TABLE orders
			ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;
		CREATE INDEX IF NOT EXISTS orders_unprocessed_idx
			ON orders (id) WHERE processed = FALSE;`
	if _, err := s.pool.Exec(ctx, schema); err != nil {
		return fmt.Errorf("initialize database schema: %w", err)
	}
	s.init = true
	return nil
}

func (s *Store) CreateOrder(ctx context.Context, payload json.RawMessage) (Order, error) {
	if err := s.ensureSchema(ctx); err != nil {
		return Order{}, err
	}
	const query = `INSERT INTO orders (payload) VALUES ($1) RETURNING id, payload, processed, created_at, processed_at`
	var order Order
	if err := s.pool.QueryRow(ctx, query, payload).Scan(&order.ID, &order.Payload, &order.Processed, &order.CreatedAt, &order.ProcessedAt); err != nil {
		return Order{}, fmt.Errorf("insert order: %w", err)
	}
	return order, nil
}

func (s *Store) ListOrders(ctx context.Context) ([]Order, error) {
	if err := s.ensureSchema(ctx); err != nil {
		return nil, err
	}
	rows, err := s.pool.Query(ctx, `SELECT id, payload, processed, created_at, processed_at FROM orders ORDER BY id`)
	if err != nil {
		return nil, fmt.Errorf("query orders: %w", err)
	}
	defer rows.Close()
	orders := make([]Order, 0)
	for rows.Next() {
		var order Order
		if err := rows.Scan(&order.ID, &order.Payload, &order.Processed, &order.CreatedAt, &order.ProcessedAt); err != nil {
			return nil, fmt.Errorf("scan order: %w", err)
		}
		orders = append(orders, order)
	}
	return orders, rows.Err()
}
