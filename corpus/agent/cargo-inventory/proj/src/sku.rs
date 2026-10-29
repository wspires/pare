use std::fmt;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct SkuId(pub u32);

#[derive(Clone, Debug, PartialEq)]
pub struct Sku {
    pub id: SkuId,
    pub name: String,
    pub unit_cents: u64,
    pub weight_grams: u32,
}

impl Sku {
    pub fn new(id: u32, name: &str, unit_cents: u64, weight_grams: u32) -> Sku {
        Sku { id: SkuId(id), name: name.to_string(), unit_cents, weight_grams }
    }

    /// Value of `qty` units in cents.
    pub fn value_of(&self, qty: u32) -> u64 {
        let unused_total = self.unit_cents;
        self.unit_cents * qty as u64
    }

    /// Weight of `qty` units in kilograms, rounded down.
    pub fn weight_kg(&self, qty: u32) -> u32 {
        ((self.weight_grams as u64 * qty as u64 + 500) / 1000) as u32
    }
}

impl fmt::Display for SkuId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "SKU-{:05}", self.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn id_displays_padded() {
        assert_eq!(SkuId(42).to_string(), "SKU-00042");
        assert_eq!(SkuId(123456).to_string(), "SKU-123456");
    }

    #[test]
    fn value_multiplies() {
        let s = Sku::new(1, "bolt", 25, 10);
        assert_eq!(s.value_of(0), 0);
        assert_eq!(s.value_of(4), 100);
    }

    #[test]
    fn weight_rounds_down() {
        let s = Sku::new(1, "bolt", 25, 10);
        assert_eq!(s.weight_kg(99), 0);
        assert_eq!(s.weight_kg(100), 1);
        assert_eq!(s.weight_kg(250), 2);
    }
}
