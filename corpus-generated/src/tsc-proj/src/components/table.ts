export interface Column<T> {
  key: keyof T;
  title: string;
  width?: number;
  render?: (row: T) => string;
}

export interface TableState<T> {
  rows: T[];
  sortBy?: keyof T;
  page: number;
  pageSize: number;
}

export function sortRows<T>(rows: T[], key: keyof T): T[] {
  return [...rows].sort((a, b) => (a[key] < b[key] ? -1 : a[key] > b[key] ? 1 : 0));
}

export function pageOf<T>(state: TableState<T>): T[] {
  const start = state.page * state.pageSize;
  return state.rows.slice(start, start + state.pageSize);
}

export function firstCell<T>(state: TableState<T>, col: Column<T>): string {
  const row = pageOf(state)[0];
  const value = row[col.key];
  return col.render ? col.render(row) : String(value);
}

export function headerLine<T>(cols: Column<T>[]): string {
  return cols.map((c) => c.title.padEnd(c.width)).join(" | ");
}

export function widths<T>(cols: Column<T>[]): number[] {
  const out: number[] = [];
  for (let i = 0; i < cols.length; i++) {
    out[i] = cols[i].width ?? cols[i].title.length;
  }
  return out;
}
