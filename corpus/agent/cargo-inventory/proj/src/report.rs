use crate::bins::Warehouse;
use std::collections::HashMap;
use std::fmt::Write as _;

/// One line per bin: name, units, free, and the value held, in cents.
pub fn by_bin(w: &Warehouse) -> Vec<String> {
    let mut lines = Vec::new();
    for bin in w.bins.values() {
        let value: u64 = bin
            .stock
            .iter()
            .filter_map(|(id, qty)| w.skus.get(id).map(|s| s.value_of(*qty)))
            .sum();
        lines.push(format!("{:<8} {:>6} units {:>6} free {:>12} cents", bin.name, bin.units(), bin.free(), value));
    }
    lines
}

/// SKUs with fewer than `min` units across the warehouse, by id.
pub fn low_stock(w: &Warehouse, min: u32) -> Vec<(crate::sku::SkuId, u32)> {
    let mut low: Vec<_> = w.skus.keys().map(|id| (*id, w.total(*id))).filter(|(_, n)| *n < min).collect();
    low.sort();
    low
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bins::Bin;
    use crate::sku::{Sku, SkuId};

    fn warehouse() -> Warehouse {
        let mut w = Warehouse::new();
        w.add_sku(Sku::new(1, "bolt", 25, 10));
        w.add_sku(Sku::new(2, "nut", 10, 5));
        w.add_sku(Sku::new(3, "washer", 2, 1));
        let mut a = Bin::new("A", 100);
        a.add(SkuId(1), 40);
        a.add(SkuId(3), 3);
        w.add_bin(a);
        let mut b = Bin::new("B", 50);
        b.add(SkuId(2), 8);
        w.add_bin(b);
        w
    }

    #[test]
    fn by_bin_lines() {
        let lines = by_bin(&warehouse());
        assert_eq!(lines.len(), 2);
        assert!(lines[0].starts_with("A "), "{}", lines[0]);
        assert!(lines[0].contains("43 units"), "{}", lines[0]);
        assert!(lines[0].contains("57 free"), "{}", lines[0]);
        assert!(lines[0].contains("1006 cents"), "{}", lines[0]);
    }

    #[test]
    fn low_stock_sorted() {
        assert_eq!(low_stock(&warehouse(), 10), vec![(SkuId(2), 8), (SkuId(3), 3)]);
        assert_eq!(low_stock(&warehouse(), 1), vec![]);
    }
}
