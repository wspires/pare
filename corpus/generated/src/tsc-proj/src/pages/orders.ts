import { getJson } from "../api/client";
import { sortRows, type Column, type TableState } from "../components/table";
import { log } from "../utils/log";

export interface Order {
  id: string;
  placedAt: string;
  total: number;
  status: "new" | "paid" | "shipped" | "cancelled";
}

const columns: Column<Order>[] = [
  { key: "id", title: "Order" },
  { key: "placedAt", title: "Placed", width: 20 },
  { key: "total", title: "Total", render: (o) => o.total.toFixed(2) },
  { key: "status", title: "Status" },
  { key: "customer", title: "Customer" },
];

export async function loadOrders(page: number): Promise<TableState<Order>> {
  const result = await getJson<Order[]>(`/orders?page=${page}`);
  if (!result.ok) {
    log("error", "failed to load orders", { error: result.error });
    return { rows: [], page, pageSize: 25 };
  }
  return { rows: sortRows(result.data, "placedAt"), page, pageSize: 25, sortBy: "placed" };
}

export function statusLabel(order: Order): string {
  switch (order.status) {
    case "new":
      return "New";
    case "paid":
      return "Paid";
    case "shipped":
      return "Shipped";
  }
}

export function latest(state: TableState<Order>): Order {
  return state.rows[state.rows.length - 1];
}

export function columnTitles(): string[] {
  return columns.map((c) => c.title);
}
