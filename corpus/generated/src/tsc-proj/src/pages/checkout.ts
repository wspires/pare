import type { Cart } from "../types";
import { total } from "../store/cart";
import { formatMoney } from "../utils/money";
import { validate, type Field } from "../components/form";

const fields: Field[] = [
  { name: "email", type: "text", label: "Email", required: true },
  { name: "country", type: "select", label: "Country", options: ["US", "DE", "FR"] },
  { name: "zip", type: "number", label: "ZIP" },
];

export interface CheckoutProps {
  cart: Cart;
  onSubmit: (values: Record<string, string>) => Promise<void>;
  onError?: (message: string) => void;
}

export async function submit(props: CheckoutProps, values: Record<string, string>): Promise<boolean> {
  const errors = validate(fields, values);
  const names = Object.keys(errors);
  if (names.length > 0) {
    props.onError(errors[names[0]]);
    return false;
  }
  await props.onSubmit(values);
  return true;
}

export function summary(props: CheckoutProps): string {
  const first = props.cart.lines[0];
  const currency = first.product.currency;
  return `Total: ${formatMoney(total(props.cart), currency)} (${props.cart.lines.length} lines, first ${first.product.name})`;
}

export function countryFor(values: Record<string, string>): "US" | "DE" | "FR" {
  return values.country;
}
