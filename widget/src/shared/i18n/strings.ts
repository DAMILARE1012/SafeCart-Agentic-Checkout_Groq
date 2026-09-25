/**
 * All user-facing copy lives here, so the widget can be localised by swapping
 * this module per locale without touching components.
 */
export const strings = {
  launcherOpen: 'Open shopping assistant',
  launcherClose: 'Close shopping assistant',
  panelTitle: 'Shopping assistant',
  statusOnline: 'Typically replies instantly',
  statusReconnecting: 'Reconnecting…',
  statusOffline: 'You are offline',
  close: 'Close',
  viewCart: 'View cart',
  backToChat: 'Back to chat',
  composerPlaceholder: 'Ask about products, sizes, or your order…',
  composerLabel: 'Message',
  send: 'Send message',
  retry: 'Retry',
  jumpToLatest: 'Jump to latest',
  greetingTitle: (merchant: string) => `Hi! I'm the ${merchant} assistant.`,
  greetingBody: 'I can help you find products, build your cart and check out securely.',
  starterPrompts: ['Show me running shoes', 'What are your best sellers?', 'Do you have a promo code?'],
  sessionLoading: 'Connecting…',
  sessionError: "We couldn't connect to the assistant.",
  turnError: "Sorry, something went wrong on our side. Your cart is saved.",
  offlineBanner: "You're offline. Messages will send when you reconnect.",
  reconnectingBanner: 'Connection lost. Reconnecting…',
  poweredBy: 'Payments secured by Stripe',

  // catalog
  addToCart: 'Add to cart',
  details: 'Details',
  outOfStock: 'Out of stock',
  addToCartMessage: (name: string) => `Add ${name} to my cart`,
  detailsMessage: (name: string) => `Tell me more about ${name}`,

  // cart
  cartTitle: 'Your cart',
  cartEmpty: 'Your cart is empty.',
  subtotal: 'Subtotal',
  totalAfterDiscounts: 'Total after discounts',
  items: (n: number) => (n === 1 ? '1 item' : `${n} items`),
  checkout: 'Checkout',
  checkoutMessage: "I'm ready to check out",
  taxesAtCheckout: 'Shipping and taxes are calculated at checkout.',
  increaseQty: (name: string) => `Increase quantity of ${name}`,
  decreaseQty: (name: string) => `Decrease quantity of ${name}`,
  removeItem: (name: string) => `Remove ${name}`,
  setQtyMessage: (name: string, qty: number) => `Change ${name} quantity to ${qty}`,
  removeMessage: (name: string) => `Remove ${name} from my cart`,

  // quote / checkout
  orderSummary: 'Order summary',
  shipping: 'Shipping',
  tax: 'Tax',
  taxIncluded: 'Tax (included)',
  total: 'Total',
  free: 'Free',
  confirmAndPay: (amount: string) => `Confirm & pay ${amount}`,
  confirming: 'Confirming…',
  confirmHint: "You'll enter your payment details on Stripe's secure page. You won't be charged until you complete payment there.",
  quoteExpiresIn: (t: string) => `Price held for ${t}`,
  quoteExpired: 'This price has expired.',
  refreshQuote: 'Refresh price',
  refreshQuoteMessage: 'Please refresh my order summary',
  paymentUnavailable: "Secure payment isn't available yet. Your order summary and cart are saved.",
  confirmFailed: "We couldn't start checkout. Please try again.",
  openPaymentPage: 'Open secure payment page',
  paymentPageOpened: 'Secure payment page opened in a new tab.',

  // orders
  orderLabel: (id: string) => `Order ${id}`,
  refundLabel: 'Refund',
} as const;
