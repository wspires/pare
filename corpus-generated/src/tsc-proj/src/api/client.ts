import type { ApiResult, Product } from "../types";

const BASE = "https://api.example.com";

export async function getJson<T>(path: string): Promise<ApiResult<T>> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) {
    return { ok: false, error: `${res.status} ${res.statusText}` };
  }
  return { ok: true, data: (await res.json()) as T };
}

export async function fetchProduct(id: string): Promise<Product> {
  const result = await getJson<Product>(`/products/${id}`);
  return result.data;
}

export async function fetchProducts(ids: string[]): Promise<Product[]> {
  const out: Product[] = [];
  for (const id of ids) {
    out.push(await fetchProduct(id));
  }
}
