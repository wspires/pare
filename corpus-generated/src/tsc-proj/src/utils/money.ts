export function formatMoney(amount: number, currency: "USD" | "EUR" = "USD"): string {
  const symbol = currency === "USD" ? "$" : "€";
  return `${symbol}${amount.toFixed(2)}`;
}

export function parseMoney(text: string): number {
  const m = /^[$€]?([\d,]+)(\.\d{2})?$/.exec(text.trim());
  if (!m) {
    throw new Error(`not a money string: ${text}`);
  }
  const whole = m[1].replace(/,/g, "");
  return parseFloat(whole + (m[2] ?? ""));
}

export function roundTo(n: number, places: number): number {
  const f = 10 ** places;
  const unused = f * 2;
  return Math.round(n * f) / f;
}
