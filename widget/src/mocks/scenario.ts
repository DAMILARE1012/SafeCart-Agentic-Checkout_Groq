/**
 * A scripted stand-in for agent-svc. It maps a turn to a reply
 * (text + UI blocks + suggestions), mimicking how the real agent calls tools
 * and renders their results.
 */
import type { TurnRequest, UiBlock } from '@/shared/api/contracts';
import { PRODUCTS, PROMO_CODES } from './fixtures';
import { addToCart, cartSnapshot, createQuote, db, findProduct, publicOrder, setQuantity } from './state';

export interface Reply {
  text: string;
  blocks: UiBlock[];
  suggestions: string[];
  error?: { code: string; message: string; retryable: boolean };
}

const reply = (text: string, blocks: UiBlock[] = [], suggestions: string[] = []): Reply => ({ text, blocks, suggestions });

function search(terms: string[]): UiBlock {
  const products = PRODUCTS.filter((p) => terms.some((t) => p.tags.includes(t)));
  return { type: 'product_list', products };
}

function checkoutReply(): Reply {
  const result = createQuote();
  if (!result) return reply('Your cart is empty. Want to see some running shoes?', [], ['Show me running shoes']);
  return reply(
    "Here's your order summary. Shipping and tax are included and the price is held for 10 minutes. Review it and tap Confirm & pay when you're ready.",
    [{ type: 'quote', quote: result.quote, confirmation: result.confirmation }],
  );
}

const failedOnce = new Set<string>();

export function respond(request: TurnRequest): Reply {
  const action = request.action;
  const text = request.text.toLowerCase();

  // -- Structured actions from UI controls (exact IDs, no guessing) ----------
  if (action) {
    switch (action.type) {
      case 'add_to_cart': {
        const product = addToCart(action.sku_id, action.quantity);
        if (!product) return reply("Sorry, that item just went out of stock. Here's what I'd suggest instead:", [search(['run'])]);
        return reply(`Added ${product.name} to your cart.`, [{ type: 'cart', cart: cartSnapshot() }], [
          'Checkout',
          'Add running socks',
          'Do you have a promo code?',
        ]);
      }
      case 'update_cart_item':
      case 'remove_cart_item': {
        const qty = action.type === 'remove_cart_item' ? 0 : action.quantity;
        const product = setQuantity(action.cart_item_id, qty);
        if (!product) return reply("I couldn't find that item in your cart.", [{ type: 'cart', cart: cartSnapshot() }]);
        return reply(qty === 0 ? `Removed ${product.name}.` : `Updated ${product.name} to ${qty}.`, [
          { type: 'cart', cart: cartSnapshot() },
        ]);
      }
      case 'view_product': {
        const product = findProduct(action.product_id);
        if (!product) return reply("I couldn't find that product.");
        return reply(`Here are the details for ${product.name}.`, [{ type: 'product_card', product }], ['Show similar', 'View my cart']);
      }
      case 'start_checkout':
      case 'refresh_quote':
        return checkoutReply();
    }
  }

  // -- Free text ---------------------------------------------------------------
  if (text.includes('simulate error') && !failedOnce.has(request.client_message_id)) {
    failedOnce.add(request.client_message_id); // the retry will succeed
    return {
      ...reply(''),
      error: { code: 'upstream_unavailable', message: 'The assistant is briefly unavailable. Your cart is saved.', retryable: true },
    };
  }

  if (/ignore (all|previous)|100% off|for free|make it free|change the price/.test(text)) {
    return reply(
      "I can't change prices or invent discounts. Prices come straight from the store. I can apply a valid promo code, though: try WELCOME10.",
      [],
      ['Apply WELCOME10'],
    );
  }

  const code = Object.keys(PROMO_CODES).find((c) => text.includes(c.toLowerCase()));
  if (code) {
    db.promo = code;
    return reply(`Applied ${code}: ${PROMO_CODES[code]?.percentOff}% off your order.`, [{ type: 'cart', cart: cartSnapshot() }], ['Checkout']);
  }
  if (/promo|coupon|discount|code/.test(text)) {
    return reply('New customers get 10% off with WELCOME10. Want me to apply it?', [], ['Apply WELCOME10']);
  }

  if (/check ?out|pay|buy|ready/.test(text)) return checkoutReply();
  if (/cart|basket|bag/.test(text)) return reply("Here's your cart.", [{ type: 'cart', cart: cartSnapshot() }], ['Checkout']);

  if (/order|status|track|refund/.test(text)) {
    const order = db.lastOrderId ? publicOrder(db.lastOrderId) : null;
    return order
      ? reply("Here's the latest on your order.", [{ type: 'order_status', order }])
      : reply("I don't see any orders yet in this conversation.");
  }

  if (/best ?sell|popular|recommend|top/.test(text)) {
    const top = [...PRODUCTS].filter((p) => p.in_stock).sort((a, b) => (b.rating ?? 0) - (a.rating ?? 0)).slice(0, 4);
    return reply('These are our best-rated picks right now:', [{ type: 'product_list', products: top }]);
  }
  if (/sock/.test(text)) return reply('Our merino socks are a customer favourite:', [search(['sock'])]);
  if (/jacket|rain|coat/.test(text)) return reply('For wet weather I recommend:', [search(['jacket'])]);
  if (/cap|hat/.test(text)) return reply('Here are our caps:', [search(['cap'])]);
  if (/shoe|run|sneaker|trail|road|similar/.test(text)) {
    return reply('Here are a few running shoes in your size. Tap "Details" for more or "Add to cart" when you find the one.', [search(['shoe'])], [
      'Which is best for trails?',
      'Show me running socks',
    ]);
  }

  return reply(
    'I can help you find products, manage your cart, apply promo codes and check out securely. What are you shopping for today?',
    [],
    ['Show me running shoes', 'What are your best sellers?', 'Track my order'],
  );
}
