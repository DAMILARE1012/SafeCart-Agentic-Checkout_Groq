import type { UiBlock } from '@/shared/api/contracts';
import { CartBlock } from '@/features/cart/CartBlock';
import { ProductCard } from '@/features/catalog/ProductCard';
import { ProductCarousel } from '@/features/catalog/ProductCarousel';
import { QuoteCard } from '@/features/checkout/QuoteCard';
import { OrderTracker } from '@/features/orders/OrderTracker';
import { NoticeBlock } from './NoticeBlock';

/**
 * Maps a structured block from the agent to its feature component.
 * Prices on screen always come from these blocks, never from the LLM's text.
 */
export function BlockRenderer({ block }: { block: UiBlock }) {
  switch (block.type) {
    case 'product_list':
      return <ProductCarousel products={block.products} />;
    case 'product_card':
      return <ProductCard product={block.product} layout="full" />;
    case 'cart':
      return <CartBlock cart={block.cart} />;
    case 'quote':
      return <QuoteCard quote={block.quote} confirmation={block.confirmation} />;
    case 'order_status':
      return <OrderTracker orderId={block.order.id} initial={block.order} />;
    case 'notice':
      return <NoticeBlock level={block.level} text={block.text} />;
    default: {
      // Unknown block types from a newer backend are skipped instead of crashing the widget.
      const _exhaustive: never = block;
      void _exhaustive;
      return null;
    }
  }
}
