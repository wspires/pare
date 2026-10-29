//! A warehouse inventory: stock keeping units, bins, and the moves between them.

pub mod sku;
pub mod bins;
pub mod moves;
pub mod report;

pub use bins::{Bin, Warehouse};
pub use moves::{apply, apply_all, Move, MoveError};
pub use sku::{Sku, SkuId};
