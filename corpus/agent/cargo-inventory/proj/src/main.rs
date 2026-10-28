use inventory::{apply_all, Bin, Move, Sku, SkuId, Warehouse};

fn main() {
    let mut w = Warehouse::new();
    let mut count = 0;
    w.add_sku(Sku::new(1, "bolt", 25, 10));
    w.add_sku(Sku::new(2, "nut", 10, 5));
    let mut a = Bin::new("A", 100);
    a.add(SkuId(1), 40);
    w.add_bin(a);
    w.add_bin(Bin::new("B", 10));
    let moves = [Move { sku: SkuId(1), qty: 5, from: "A".into(), to: "B".into() }];
    match apply_all(&mut w, &moves) {
        Ok(n) => println!("applied {n} moves"),
        Err((i, e)) => eprintln!("move {i} failed: {e}"),
    }
    for line in inventory::report::by_bin(&w) {
        println!("{line}");
    }
}
