import { Plus, Star } from 'lucide-react';
import type { ProductSummary } from '@/shared/api/contracts';
import { strings } from '@/shared/i18n/strings';
import { cn } from '@/shared/lib/cn';
import { Button } from '@/shared/ui/Button';
import { Card } from '@/shared/ui/Card';
import { ProductImage } from '@/shared/ui/ProductImage';
import { useSendMessage } from '@/features/chat/useSendMessage';
import { PriceTag } from './PriceTag';

interface ProductCardProps {
  product: ProductSummary;
  /** Compact cards sit in a horizontal carousel; full cards stand alone. */
  layout?: 'compact' | 'full';
}

export function ProductCard({ product, layout = 'compact' }: ProductCardProps) {
  const { send, busy } = useSendMessage();
  const displayName = product.variant_label ? `${product.name} (${product.variant_label})` : product.name;

  return (
    <Card className={cn('flex shrink-0 snap-start flex-col overflow-hidden', layout === 'compact' ? 'w-44' : 'w-full')}>
      <ProductImage
        src={product.image_url}
        alt={product.name}
        className={layout === 'compact' ? 'aspect-square w-full' : 'aspect-[4/3] w-full'}
      />
      <div className="flex flex-1 flex-col gap-1 p-3">
        <p className="line-clamp-2 text-sm leading-snug font-medium text-zinc-900 dark:text-zinc-100">{product.name}</p>
        {product.variant_label && <p className="text-xs text-zinc-500">{product.variant_label}</p>}
        {layout === 'full' && product.description && (
          <p className="mt-1 text-xs leading-relaxed text-zinc-600 dark:text-zinc-400">{product.description}</p>
        )}
        <div className="mt-auto flex items-center justify-between pt-1 text-sm text-zinc-900 dark:text-zinc-100">
          <PriceTag price={product.price} compareAt={product.compare_at_price} />
          {product.rating != null && (
            <span className="flex items-center gap-0.5 text-xs text-zinc-500" aria-label={`Rated ${product.rating} out of 5`}>
              <Star className="size-3 fill-amber-400 text-amber-400" aria-hidden="true" />
              {product.rating.toFixed(1)}
            </span>
          )}
        </div>
        <div className="mt-2 flex gap-2">
          {product.in_stock ? (
            <Button
              size="sm"
              fullWidth
              disabled={busy}
              icon={<Plus className="size-3.5" aria-hidden="true" />}
              onClick={() =>
                send(strings.addToCartMessage(displayName), { type: 'add_to_cart', sku_id: product.sku_id, quantity: 1 })
              }
            >
              {strings.addToCart}
            </Button>
          ) : (
            <span className="flex h-8 w-full items-center justify-center rounded-xl bg-zinc-100 text-xs text-zinc-500 dark:bg-zinc-700/50">
              {strings.outOfStock}
            </span>
          )}
          {layout === 'compact' && (
            <Button
              size="sm"
              variant="secondary"
              disabled={busy}
              onClick={() => send(strings.detailsMessage(product.name), { type: 'view_product', product_id: product.id })}
            >
              {strings.details}
            </Button>
          )}
        </div>
      </div>
    </Card>
  );
}
