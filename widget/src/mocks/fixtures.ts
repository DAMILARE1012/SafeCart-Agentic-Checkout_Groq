/**
 * Demo catalog for the dev playground. Images are generated SVGs, so the
 * playground works fully offline.
 */
import type { MerchantInfo, ProductSummary } from '@/shared/api/contracts';

export const MERCHANT: MerchantInfo = {
  id: 'merchant_demo',
  name: 'Northwind Outfitters',
  logo_url: null,
  currency: 'USD',
};

function art(label: string, hue: number): string {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
    <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="hsl(${hue} 70% 62%)"/><stop offset="1" stop-color="hsl(${(hue + 40) % 360} 70% 42%)"/>
    </linearGradient></defs>
    <rect width="200" height="200" fill="url(#g)"/>
    <text x="100" y="118" font-family="system-ui,sans-serif" font-size="56" font-weight="700" fill="white" text-anchor="middle">${label}</text>
  </svg>`;
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}

const usd = (amount_minor: number) => ({ amount_minor, currency: 'USD' });

export interface MockProduct extends ProductSummary {
  tags: string[];
  /** Demo switch: orders containing this product fail fulfilment → automatic refund. */
  failsFulfillment?: boolean;
}

export const PRODUCTS: MockProduct[] = [
  {
    id: 'prod_trail_gtx',
    sku_id: 'sku_trail_gtx_10_blk',
    name: 'Trail Runner GTX',
    variant_label: "Men's 10 · Black",
    description: 'Waterproof Gore-Tex trail shoe with a grippy lug outsole and rock plate. 290 g.',
    image_url: art('TR', 210),
    price: usd(12900),
    compare_at_price: usd(14900),
    in_stock: true,
    rating: 4.7,
    tags: ['shoe', 'run', 'trail', 'waterproof', 'sneaker'],
  },
  {
    id: 'prod_road_air',
    sku_id: 'sku_road_air_10_wht',
    name: 'Road Glide Air',
    variant_label: "Men's 10 · White",
    description: 'Lightweight, cushioned daily trainer for road miles. 245 g, 8 mm drop.',
    image_url: art('RG', 160),
    price: usd(11000),
    in_stock: true,
    rating: 4.5,
    tags: ['shoe', 'run', 'road', 'sneaker'],
  },
  {
    id: 'prod_summit_ltd',
    sku_id: 'sku_summit_ltd_10_org',
    name: 'Summit Pro Limited',
    variant_label: "Men's 10 · Orange",
    description: 'Limited-run carbon-plated racer. (Demo: its orders fail fulfilment, triggering an automatic refund.)',
    image_url: art('SP', 20),
    price: usd(18900),
    in_stock: true,
    rating: 4.9,
    tags: ['shoe', 'run', 'race', 'limited', 'sneaker'],
    failsFulfillment: true,
  },
  {
    id: 'prod_merino_socks',
    sku_id: 'sku_merino_socks_m',
    name: 'Merino Run Socks (2-pack)',
    variant_label: 'Size M',
    description: 'Breathable merino blend with blister-resistant seams.',
    image_url: art('MS', 280),
    price: usd(2400),
    in_stock: true,
    rating: 4.8,
    tags: ['sock', 'socks', 'run', 'accessory'],
  },
  {
    id: 'prod_storm_shell',
    sku_id: 'sku_storm_shell_m_navy',
    name: 'Storm Shell Jacket',
    variant_label: 'M · Navy',
    description: 'Packable 2.5-layer waterproof shell with taped seams.',
    image_url: art('SS', 230),
    price: usd(16500),
    in_stock: true,
    rating: 4.6,
    tags: ['jacket', 'rain', 'coat', 'waterproof'],
  },
  {
    id: 'prod_feather_cap',
    sku_id: 'sku_feather_cap_os',
    name: 'Featherlight Cap',
    variant_label: 'One size',
    image_url: art('FC', 45),
    price: usd(3200),
    in_stock: false,
    rating: 4.3,
    tags: ['cap', 'hat', 'accessory'],
  },
];

export const PROMO_CODES: Record<string, { label: string; percentOff: number }> = {
  WELCOME10: { label: 'Welcome offer', percentOff: 10 },
};

export const TAX_RATE = 0.0825;
export const FREE_SHIPPING_FROM = 10000;
export const FLAT_SHIPPING = 800;
