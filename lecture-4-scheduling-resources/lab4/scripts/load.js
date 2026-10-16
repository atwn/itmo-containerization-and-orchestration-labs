// k6 load test for the shop order path (api -> postgres).
// Used three times in Lab 4: Part 3 (feed krr real usage), Part 8 (prove the
// SLA holds while batch is sacrificed under pressure).
import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:18080';
const VUS = parseInt(__ENV.VUS || '15', 10);
const DURATION = __ENV.DURATION || '3m';

export const options = {
  scenarios: {
    order_path: {
      executor: 'constant-vus',
      vus: VUS,
      duration: DURATION,
    },
  },
  thresholds: {
    http_req_duration: ['p(95)<300'], // the SLA this lab states: p95 < 300ms
  },
};

export default function () {
  const payload = JSON.stringify({ sku: 'book-1', quantity: 1 + (__VU % 3) });
  const headers = { headers: { 'Content-Type': 'application/json' } };

  const orderRes = http.post(`${BASE_URL}/order`, payload, headers);
  check(orderRes, { 'order created (201)': (r) => r.status === 201 });

  const listRes = http.get(`${BASE_URL}/orders`);
  check(listRes, { 'orders listed (200)': (r) => r.status === 200 });

  sleep(0.2);
}
