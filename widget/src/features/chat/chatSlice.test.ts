import { describe, expect, it } from 'vitest';
import reducer, {
  blockReceived,
  textDelta,
  turnCompleted,
  turnFailed,
  turnStarted,
  userMessageAdded,
} from './chatSlice';

const request = { client_message_id: 'u1', text: 'show me shoes' };

describe('chatSlice streaming lifecycle', () => {
  it('assembles an assistant message from stream events', () => {
    let state = reducer(undefined, userMessageAdded(request));
    expect(state.turn.status).toBe('streaming');

    state = reducer(state, turnStarted({ userMessageId: 'u1', messageId: 'a1' }));
    state = reducer(state, textDelta({ messageId: 'a1', delta: 'Here are ' }));
    state = reducer(state, textDelta({ messageId: 'a1', delta: 'some options.' }));
    state = reducer(state, blockReceived({ messageId: 'a1', block: { type: 'notice', level: 'info', text: 'x' } }));
    state = reducer(state, turnCompleted({ messageId: 'a1' }));

    expect(state.ids).toEqual(['u1', 'a1']);
    expect(state.entities.a1).toMatchObject({ text: 'Here are some options.', status: 'complete' });
    expect(state.entities.a1?.blocks).toHaveLength(1);
    expect(state.turn.status).toBe('idle');
  });

  it('drops an empty assistant placeholder and marks the user message failed on error', () => {
    let state = reducer(undefined, userMessageAdded(request));
    state = reducer(state, turnStarted({ userMessageId: 'u1', messageId: 'a1' }));
    state = reducer(
      state,
      turnFailed({ userMessageId: 'u1', error: { code: 'x', message: 'boom', retryable: true } }),
    );

    expect(state.ids).toEqual(['u1']);
    expect(state.entities.u1?.status).toBe('failed');
    expect(state.entities.u1?.request).toEqual(request); // kept for idempotent retry
    expect(state.turn.status).toBe('idle');
  });
});
