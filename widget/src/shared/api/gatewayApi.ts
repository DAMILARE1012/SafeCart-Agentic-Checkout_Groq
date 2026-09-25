import {
  createApi,
  fetchBaseQuery,
  type BaseQueryFn,
  type FetchArgs,
  type FetchBaseQueryError,
} from '@reduxjs/toolkit/query/react';
import type { RootState } from '@/app/store';
import { renewSession } from '@/features/session/sessionSlice';
import type { ConfirmCheckoutResponse, HistoryMessage, OrderSummary } from './contracts';

const rawBaseQuery: BaseQueryFn<string | FetchArgs, unknown, FetchBaseQueryError> = (args, api, extra) => {
  const state = api.getState() as RootState;
  return fetchBaseQuery({
    baseUrl: state.config.gatewayBaseUrl,
    prepareHeaders: (headers) => {
      const token = (api.getState() as RootState).session.token;
      if (token) headers.set('Authorization', `Bearer ${token}`);
      return headers;
    },
  })(args, api, extra);
};

/** On 401, renew the session once (resuming the same conversation) and replay the request. */
const baseQueryWithReauth: BaseQueryFn<string | FetchArgs, unknown, FetchBaseQueryError> = async (
  args,
  api,
  extra,
) => {
  let result = await rawBaseQuery(args, api, extra);
  if (result.error?.status === 401) {
    try {
      // `renewSession` is a thunk; the cast avoids a circular AppDispatch type.
      await (api.dispatch as (action: unknown) => { unwrap(): Promise<unknown> })(renewSession()).unwrap();
      result = await rawBaseQuery(args, api, extra);
    } catch {
      /* return the original 401 */
    }
  }
  return result;
};

export const gatewayApi = createApi({
  reducerPath: 'gatewayApi',
  baseQuery: baseQueryWithReauth,
  tagTypes: ['Order'],
  endpoints: (build) => ({
    getHistory: build.query<HistoryMessage[], string>({
      query: (conversationId) => `/v1/conversations/${conversationId}/messages`,
      transformResponse: (response: { messages: HistoryMessage[] }) => response.messages,
    }),

    /** The only call that starts a payment. Must carry a stable Idempotency-Key. */
    confirmCheckout: build.mutation<
      ConfirmCheckoutResponse,
      { confirmationToken: string; idempotencyKey: string }
    >({
      query: ({ confirmationToken, idempotencyKey }) => ({
        url: '/v1/checkout/confirm',
        method: 'POST',
        body: { confirmation_token: confirmationToken },
        headers: { 'Idempotency-Key': idempotencyKey },
      }),
    }),

    getOrder: build.query<OrderSummary, string>({
      query: (orderId) => `/v1/orders/${orderId}`,
      providesTags: (_result, _error, orderId) => [{ type: 'Order', id: orderId }],
    }),
  }),
});

export const { useGetOrderQuery } = gatewayApi;
