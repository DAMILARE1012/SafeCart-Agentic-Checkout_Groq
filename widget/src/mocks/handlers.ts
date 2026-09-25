/**
 * Mock Service Worker handlers that emulate gateway-svc's widget API
 * (sessions, streamed turns, server-push events, checkout confirm, orders),
 * so the UI can be built and demoed without the backend.
 */
import { delay, http, HttpResponse } from 'msw';
import type { HistoryMessage, OrderSummary, SessionResponse, TurnRequest } from '@/shared/api/contracts';
import { MERCHANT } from './fixtures';
import { respond, type Reply } from './scenario';
import { confirm, db, publicOrder, transition } from './state';

const encoder = new TextEncoder();
const sse = (event: string, data: unknown) => encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
const sseHeaders = { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' };
const rid = (p: string) => `${p}_${Math.random().toString(36).slice(2, 10)}`;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------------------
// Server push: open /events streams + order lifecycle driven by the mock Stripe page
// ---------------------------------------------------------------------------
const eventStreams = new Set<ReadableStreamDefaultController<Uint8Array>>();

function push(event: string, data: unknown) {
  for (const controller of eventStreams) {
    try {
      controller.enqueue(sse(event, data));
    } catch {
      eventStreams.delete(controller);
    }
  }
}

function update(orderId: string, status: OrderSummary['status'], patch: Partial<OrderSummary> = {}) {
  const order = transition(orderId, status, patch);
  if (order) push('order.updated', order);
}

// The mock checkout page (another tab) reports payment outcomes here, standing in for Stripe webhooks.
if (typeof BroadcastChannel !== 'undefined') {
  const channel = new BroadcastChannel('cc-mock-stripe');
  channel.onmessage = async ({ data }: MessageEvent<{ type: 'paid' | 'canceled'; orderId: string }>) => {
    const order = db.orders.get(data.orderId);
    if (!order || order.status !== 'AWAITING_PAYMENT') return;
    if (data.type === 'canceled') return update(order.id, 'CANCELED');

    update(order.id, 'PAID');
    await sleep(3000);
    if (!order.failsFulfillment) return update(order.id, 'FULFILLED');

    // Compensation saga, as in docs/SYSTEM_DESIGN.md §7.4
    update(order.id, 'FULFILLMENT_FAILED');
    await sleep(1500);
    update(order.id, 'REFUND_PENDING', { refund: { amount: order.total, status: 'pending' } });
    push('notice', {
      level: 'warning',
      text: `We couldn't fulfil order #${order.id.slice(-8).toUpperCase()}, so a full refund is on its way.`,
    });
    await sleep(3000);
    update(order.id, 'REFUNDED', { refund: { amount: order.total, status: 'succeeded' } });
  };
}

// ---------------------------------------------------------------------------
// Turn streaming
// ---------------------------------------------------------------------------
const replayCache = new Map<string, { messageId: string; reply: Reply }>();

function streamReply(messageId: string, reply: Reply): ReadableStream<Uint8Array> {
  return new ReadableStream({
    async start(controller) {
      await sleep(350); // model latency
      controller.enqueue(sse('turn.started', { turn_id: rid('turn'), message_id: messageId }));
      if (reply.error) {
        await sleep(300);
        controller.enqueue(sse('turn.error', { ...reply.error, message_id: messageId }));
        controller.close();
        return;
      }
      for (const word of reply.text.split(/(\s+)/)) {
        controller.enqueue(sse('text.delta', { message_id: messageId, delta: word }));
        await sleep(18);
      }
      for (const block of reply.blocks) {
        await sleep(120);
        controller.enqueue(sse('block', { message_id: messageId, block }));
      }
      if (reply.suggestions.length) controller.enqueue(sse('suggestions', { message_id: messageId, suggestions: reply.suggestions }));
      controller.enqueue(sse('turn.completed', { message_id: messageId }));
      controller.close();
    },
  });
}

function record(request: TurnRequest, messageId: string, reply: Reply) {
  const now = new Date().toISOString();
  db.history.push(
    { id: request.client_message_id, role: 'user', text: request.text, blocks: [], created_at: now },
    { id: messageId, role: 'assistant', text: reply.text, blocks: reply.blocks, suggestions: reply.suggestions, created_at: now } satisfies HistoryMessage,
  );
}

export const handlers = [
  http.post('*/v1/sessions', async ({ request }) => {
    const body = (await request.json()) as { resume_conversation_id?: string };
    await delay(250);
    return HttpResponse.json<SessionResponse>({
      session_token: rid('mock_token'),
      expires_at: new Date(Date.now() + 60 * 60_000).toISOString(),
      conversation_id: body.resume_conversation_id ?? rid('conv'),
      merchant: MERCHANT,
    });
  }),

  http.get('*/v1/conversations/:id/messages', () => HttpResponse.json({ messages: db.history })),

  http.post('*/v1/conversations/:id/turns', async ({ request }) => {
    const turn = (await request.json()) as TurnRequest;
    // Idempotent replay: the same client_message_id returns the same reply without re-running the tools.
    const cached = replayCache.get(turn.client_message_id);
    if (cached) return new HttpResponse(streamReply(cached.messageId, cached.reply), { headers: sseHeaders });

    const messageId = rid('msg');
    const reply = respond(turn);
    if (!reply.error) {
      replayCache.set(turn.client_message_id, { messageId, reply });
      record(turn, messageId, reply);
    }
    return new HttpResponse(streamReply(messageId, reply), { headers: sseHeaders });
  }),

  http.get('*/v1/conversations/:id/events', ({ request }) => {
    let registered: ReadableStreamDefaultController<Uint8Array> | null = null;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        registered = controller;
        eventStreams.add(controller);
        controller.enqueue(encoder.encode(': connected\n\n'));
      },
      cancel() {
        if (registered) eventStreams.delete(registered);
      },
    });
    request.signal.addEventListener('abort', () => {
      if (registered) eventStreams.delete(registered);
    });
    return new HttpResponse(stream, { headers: sseHeaders });
  }),

  http.post('*/v1/checkout/confirm', async ({ request }) => {
    const key = request.headers.get('Idempotency-Key');
    if (!key) return HttpResponse.json({ code: 'idempotency_key_required', message: 'Missing Idempotency-Key', retryable: false }, { status: 400 });
    const { confirmation_token } = (await request.json()) as { confirmation_token: string };
    await delay(700);
    const result = confirm(confirmation_token, key);
    if (!result.ok) {
      const { status, ...error } = result;
      return HttpResponse.json(error, { status });
    }
    return HttpResponse.json({ order_id: result.order_id, checkout_url: result.checkout_url });
  }),

  http.get('*/v1/orders/:id', ({ params }) => {
    const order = publicOrder(String(params.id));
    return order
      ? HttpResponse.json(order)
      : HttpResponse.json({ code: 'not_found', message: 'Order not found', retryable: false }, { status: 404 });
  }),
];
