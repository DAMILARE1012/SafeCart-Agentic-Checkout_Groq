import type { ProductSummary } from '@/shared/api/contracts';
import { ProductCard } from './ProductCard';

/** Horizontally scrollable, snap-aligned product results. Scrolls with keyboard and touch. */
export function ProductCarousel({ products }: { products: ProductSummary[] }) {
  if (products.length === 0) return null;
  return (
    <ul
      aria-label={`${products.length} products`}
      className="cc-carousel -mx-4 flex snap-x snap-mandatory gap-3 overflow-x-auto scroll-px-4 px-4 pb-1"
      tabIndex={0}
    >
      {products.map((product) => (
        <li key={product.id} className="flex">
          <ProductCard product={product} />
        </li>
      ))}
    </ul>
  );
}
