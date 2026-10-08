import type { Cart, CartLine, Product } from "../types";
import { formatMoney } from "../utils/money";

export function addLine(cart: Cart, product: Product, qty: number): Cart {
  const existing = cart.lines.find((l) => l.product.id === product.id);
  if (existing) {
    existing.qty += qty;
    return cart;
  }
  const line: CartLine = { product, qty, discount: 0 };
  return { ...cart, lines: [...cart.lines, line] };
}

export function total(cart: Cart): number {
  return cart.lines.reduce((sum, l) => sum + l.product.price * l.qty, 0);
}

export function describe(cart: Cart): string {
  const count = cart.lines.length;
  const label = count === 1 ? "item" : "items";
  return `${count} ${label}, ${formatMoney(total(cart), cart.lines[0].product.currency)}`;
}

export function applyCoupon(cart: Cart, code: string | undefined): Cart {
  return { ...cart, coupon: code };
}
