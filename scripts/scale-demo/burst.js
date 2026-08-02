import http from 'k6/http';
import { sleep, check } from 'k6';

// Bursty AI profile per the phase doc: few concurrent users, LONG
// request durations (LLM generations), small think-time between —
// not the classic short-request ramp.
export const options = {
  scenarios: {
    burst: {
      executor: 'constant-vus',
      vus: 4,
      duration: '4m',
    },
  },
};

const url = 'http://ai-gateway/v1/chat/completions';
const prompts = [
  'Explain what a Kubernetes Deployment does, in about four sentences.',
  'Describe how DNS resolution works inside a Kubernetes cluster, briefly.',
  'What is the difference between a Service and an Ingress? A short paragraph.',
  'Summarize what Prometheus does in a monitoring stack, in a few sentences.',
];

export default function () {
  const payload = JSON.stringify({
    model: __ENV.MODEL || 'qwen3.5:9b',
    messages: [
      { role: 'user', content: prompts[Math.floor(Math.random() * prompts.length)] },
    ],
    max_tokens: 700,
  });
  const res = http.post(url, payload, {
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${__ENV.API_KEY}`,
    },
    timeout: '240s',
  });
  check(res, { 'status 200': (r) => r.status === 200 });
  sleep(1 + Math.random() * 2);
}
