import { fetchProducts } from "./api/client";
import { login } from "./api/auth";
import { addLine, describe, total } from "./store";
import { log, setLevel } from "./utils/log";
import type { Cart } from "./types";

async function main(argv: string[]): Promise<number> {
  setLevel(argv.includes("--verbose") ? "debug" : "info");
  const session = await login(process.env.SHOP_USER, process.env.SHOP_PASS ?? "");
  log("info", "logged in", { user: session.user });

  const products = await fetchProducts(["p-1", "p-2", "p-3"]);
  let cart: Cart = { lines: [] };
  for (const p of products) {
    cart = addLine(cart, p, 1);
  }
  log("info", describe(cart));
  if (total(cart) > 100) {
    log("warn", "large order", { total: total(cart) });
    return 2;
  }
}

main(process.argv.slice(2)).then((code) => process.exit(code));
