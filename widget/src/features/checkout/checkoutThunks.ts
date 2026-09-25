import { createAppAsyncThunk } from '@/app/hooks';
import { gatewayApi } from '@/shared/api/gatewayApi';
import type { ApiErrorBody } from '@/shared/api/contracts';
import { strings } from '@/shared/i18n/strings';
import { confirmationFailed, confirmationRequested, confirmationSucceeded } from './checkoutSlice';
import { closePaymentWindow, navigatePaymentWindow } from './paymentWindow';

function errorMessage(error: unknown): string {
  const data = (error as { data?: Partial<ApiErrorBody> } | undefined)?.data;
  return data?.message ?? strings.confirmFailed;
}

/**
 * Runs when the user explicitly clicks "Confirm & pay". This is the ONLY place
 * the widget starts a payment. The confirmation token was issued by the gateway
 * and bound to this quote; the idempotency key is stable per quote.
 */
export const confirmAndPay = createAppAsyncThunk(
  'checkout/confirmAndPay',
  async ({ quoteId, confirmationToken }: { quoteId: string; confirmationToken: string }, { dispatch, getState }) => {
    dispatch(confirmationRequested(quoteId));
    const idempotencyKey = getState().checkout.byQuoteId[quoteId]?.idempotencyKey ?? '';

    try {
      const result = await dispatch(
        gatewayApi.endpoints.confirmCheckout.initiate({ confirmationToken, idempotencyKey }),
      ).unwrap();
      const opened = navigatePaymentWindow(quoteId, result.checkout_url);
      dispatch(
        confirmationSucceeded({
          quoteId,
          orderId: result.order_id,
          checkoutUrl: result.checkout_url,
          popupBlocked: !opened,
        }),
      );
    } catch (error) {
      closePaymentWindow(quoteId);
      dispatch(confirmationFailed({ quoteId, error: errorMessage(error) }));
    }
  },
  {
    // Double-click protection: never start a second confirmation while one is running or done.
    condition: ({ quoteId }, { getState }) => {
      const status = getState().checkout.byQuoteId[quoteId]?.status;
      return status !== 'confirming' && status !== 'redirected';
    },
  },
);
