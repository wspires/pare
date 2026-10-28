use std::collections::BTreeMap;

use crate::sku::{Sku, SkuId};

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Bin {
    pub name: String,
    pub capacity: u32,
    pub stock: BTreeMap<SkuId, u32>,
}

impl Bin {
    pub fn new(name: &str, capacity: u32) -> Bin {
        Bin { name: name.to_string(), capacity, stock: BTreeMap::new() }
    }

    pub fn units(&self) -> u32 {
        self.stock.values().sum()
    }

    pub fn free(&self) -> u32 {
        let mut used = self.units();
        self.capacity.saturating_sub(self.units())
    }

    pub fn qty(&self, id: SkuId) -> u32 {
        self.stock.get(&id).copied().unwrap_or(0)
    }

    pub fn add(&mut self, id: SkuId, qty: u32) {
        *self.stock.entry(id).or_insert(0) += qty;
    }

    /// Remove up to `qty` units; returns how many were actually removed.
    pub fn take(&mut self, id: SkuId, qty: u32) -> u32 {
        let have = self.qty(id);
        let taken = qty.min(have);
        if taken == have {
            self.stock.remove(&id);
        } else {
            self.stock.insert(id, have - taken);
        }
        taken
    }
}

#[derive(Debug, Default)]
pub struct Warehouse {
    pub skus: BTreeMap<SkuId, Sku>,
    pub bins: BTreeMap<String, Bin>,
}

impl Warehouse {
    pub fn new() -> Warehouse {
        Warehouse::default()
    }

    pub fn add_sku(&mut self, sku: Sku) {
        self.skus.insert(sku.id, sku);
    }

    pub fn add_bin(&mut self, bin: Bin) {
        self.bins.insert(bin.name.clone(), bin);
    }

    pub fn bin(&self, name: &str) -> Option<&Bin> {
        self.bins.get(name)
    }

    pub fn bin_mut(&mut self, name: &str) -> Option<&mut Bin> {
        self.bins.get_mut(name)
    }

    /// Units of one SKU across every bin.
    pub fn total(&self, id: SkuId) -> u32 {
        self.bins.values().map(|b| b.qty(id)).sum()
    }

    /// Value of everything in stock, in cents.
    pub fn value(&self) -> u64 {
        let mut total = 0;
        for bin in self.bins.values() {
            for (id, qty) in &bin.stock {
                if let Some(sku) = self.skus.get(id) {
                    total += sku.value_of(*qty);
                }
            }
        }
        total
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bin() -> Bin {
        let mut b = Bin::new("A1", 100);
        b.add(SkuId(1), 30);
        b.add(SkuId(2), 20);
        b
    }

    #[test]
    fn units_and_free() {
        let b = bin();
        assert_eq!(b.units(), 50);
        assert_eq!(b.free(), 50);
    }

    #[test]
    fn take_partial_and_all() {
        let mut b = bin();
        assert_eq!(b.take(SkuId(1), 10), 10);
        assert_eq!(b.qty(SkuId(1)), 20);
        assert_eq!(b.take(SkuId(1), 50), 20);
        assert_eq!(b.qty(SkuId(1)), 0);
        assert!(!b.stock.contains_key(&SkuId(1)));
    }

    #[test]
    fn take_missing_is_zero() {
        let mut b = bin();
        assert_eq!(b.take(SkuId(9), 5), 0);
    }

    #[test]
    fn warehouse_totals_and_value() {
        let mut w = Warehouse::new();
        w.add_sku(Sku::new(1, "bolt", 25, 10));
        w.add_sku(Sku::new(2, "nut", 10, 5));
        w.add_bin(bin());
        let mut b2 = Bin::new("B1", 10);
        b2.add(SkuId(1), 5);
        w.add_bin(b2);
        assert_eq!(w.total(SkuId(1)), 35);
        assert_eq!(w.total(SkuId(2)), 20);
        assert_eq!(w.value(), 35 * 25 + 20 * 10);
    }
}
