export interface Product {
  id: string;
  name: string;
  price: number;
  currency?: "USD" | "EUR";
  tags: string[];
}

export interface CartLine {
  product: Product;
  qty: number;
}

export interface Cart {
  lines: CartLine[];
  coupon?: string;
}

export type ApiResult<T> = { ok: true; data: T } | { ok: false; error: string };
