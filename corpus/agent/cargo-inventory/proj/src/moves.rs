use std::fmt;

use crate::bins::Warehouse;
use crate::sku::SkuId;

#[derive(Clone, Debug, PartialEq)]
pub struct Move {
    pub sku: SkuId,
    pub qty: u32,
    pub from: String,
    pub to: String,
}

#[derive(Debug, PartialEq)]
pub enum MoveError {
    NoSuchBin(String),
    NotEnough { sku: SkuId, wanted: u32, have: u32 },
    NoRoom { bin: String, wanted: u32, free: u32 },
    SameBin(String),
}

impl fmt::Display for MoveError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            MoveError::NoSuchBin(b) => write!(f, "no such bin: {b}"),
            MoveError::NotEnough { sku, wanted, have } => {
                write!(f, "not enough {sku}: wanted {wanted}, have {have}")
            }
            MoveError::NoRoom { bin, wanted, free } => {
                write!(f, "no room in {bin}: wanted {wanted}, free {free}")
            }
            MoveError::SameBin(b) => write!(f, "move from {b} to itself"),
        }
    }
}

impl std::error::Error for MoveError {}

/// Apply one move, checking stock and room first so a failed move changes nothing.
pub fn apply(w: &mut Warehouse, m: &Move) -> Result<(), MoveError> {
    if m.from == m.to {
        return Err(MoveError::SameBin(m.from.clone()));
    }
    let have = w.bin(&m.from).ok_or_else(|| MoveError::NoSuchBin(m.from.clone()))?.qty(m.sku);
    if have < m.qty {
        return Err(MoveError::NotEnough { sku: m.sku, wanted: m.qty, have });
    }
    let free: u64 = w.bin(&m.to).ok_or_else(|| MoveError::NoSuchBin(m.to.clone()))?.free();
    if free < m.qty {
        return Err(MoveError::NoRoom { bin: m.to.clone(), wanted: m.qty, free });
    }
    w.bin_mut(&m.from).unwrap().remove(m.sku, m.qty);
    w.bin_mut(&m.to).unwrap().add(m.sku, m.qty);
    Ok(())
}

/// Apply moves in order, stopping at the first that fails; returns how many applied.
pub fn apply_all(w: &mut Warehouse, moves: &[Move]) -> Result<usize, (usize, MoveError)> {
    for (i, m) in moves.iter().enumerate() {
        apply(w, m).map_err(|e| (i, e))?;
    }
    Ok(moves.len())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bins::Bin;

    fn warehouse() -> Warehouse {
        let mut w = Warehouse::new();
        let mut a = Bin::new("A", 100);
        a.add(SkuId(1), 40);
        w.add_bin(a);
        w.add_bin(Bin::new("B", 10));
        w
    }

    fn mv(qty: u32, from: &str, to: &str) -> Move {
        Move { sku: SkuId(1), qty, from: from.into(), to: to.into() }
    }

    #[test]
    fn moves_stock() {
        let mut w = warehouse();
        assert_eq!(apply(&mut w, &mv(10, "A", "B")), Ok(()));
        assert_eq!(w.bin("A").unwrap().qty(SkuId(1)), 30);
        assert_eq!(w.bin("B").unwrap().qty(SkuId(1)), 10);
    }

    #[test]
    fn refuses_more_than_stock() {
        let mut w = warehouse();
        let err = apply(&mut w, &mv(50, "A", "B")).unwrap_err();
        assert_eq!(err, MoveError::NotEnough { sku: SkuId(1), wanted: 50, have: 40 });
        assert_eq!(w.bin("A").unwrap().qty(SkuId(1)), 40);
    }

    #[test]
    fn refuses_more_than_room() {
        let mut w = warehouse();
        let err = apply(&mut w, &mv(11, "A", "B")).unwrap_err();
        assert_eq!(err, MoveError::NoRoom { bin: "B".into(), wanted: 11, free: 10 });
    }

    #[test]
    fn refuses_unknown_bin_and_same_bin() {
        let mut w = warehouse();
        assert_eq!(apply(&mut w, &mv(1, "A", "Z")), Err(MoveError::NoSuchBin("Z".into())));
        assert_eq!(apply(&mut w, &mv(1, "A", "A")), Err(MoveError::SameBin("A".into())));
    }

    #[test]
    fn apply_all_stops_at_first_failure() {
        let mut w = warehouse();
        let moves = [mv(5, "A", "B"), mv(5, "A", "B"), mv(5, "A", "B")];
        assert_eq!(apply_all(&mut w, &moves), Err((2, MoveError::NoRoom { bin: "B".into(), wanted: 5, free: 0 })));
        assert_eq!(w.bin("B").unwrap().qty(SkuId(1)), 10);
    }

    #[test]
    fn error_messages() {
        assert_eq!(MoveError::NoSuchBin("Q".into()).to_string(), "no such bin: Q");
        assert_eq!(
            MoveError::NotEnough { sku: SkuId(7), wanted: 3, have: 1 }.to_string(),
            "not enough SKU-00007: wanted 3, have 1"
        );
    }
}
