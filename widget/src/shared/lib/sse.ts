import { EventSourceParserStream } from 'eventsource-parser/stream';

export class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly body: unknown,
  ) {
    super(`HTTP ${status}`);
    this.name = 'HttpError';
  }
}

export interface SseEvent {
  event: string;
  data: unknown;
}

interface StreamSseOptions {
  url: string;
  init: RequestInit;
  signal?: AbortSignal;
  onOpen?: () => void;
  onEvent: (event: SseEvent) => void;
}

/**
 * Server-Sent Events over fetch. The native EventSource can't send POST bodies
 * or an Authorization header, and we need both.
 * Resolves when the server closes the stream; rejects on HTTP or network errors.
 */
export async function streamSse({ url, init, signal, onOpen, onEvent }: StreamSseOptions): Promise<void> {
  const response = await fetch(url, {
    ...init,
    signal,
    headers: { Accept: 'text/event-stream', ...init.headers },
  });

  if (!response.ok || !response.body) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      /* non-JSON error body */
    }
    throw new HttpError(response.status, body);
  }

  onOpen?.();
  const events = response.body
    .pipeThrough(new TextDecoderStream())
    .pipeThrough(new EventSourceParserStream());

  const reader = events.getReader();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      let data: unknown = value.data;
      try {
        data = JSON.parse(value.data);
      } catch {
        /* plain-text payload */
      }
      onEvent({ event: value.event ?? 'message', data });
    }
  } finally {
    reader.releaseLock();
  }
}
