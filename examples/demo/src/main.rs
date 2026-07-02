//! A tiny program to try rdbg on. Break at line 12 and inspect `items`,
//! or run with `--panic` and `rdbg launch ... --panic` to catch the panic.

#[derive(Debug)]
struct Item { name: String, qty: u32 }

fn total(items: &[Item]) -> u32 {
    let mut sum = 0;
    for it in items {
        sum += it.qty;                    // loop body: break/trace here
    }
    if sum == 999 { panic!("unlucky total"); }
    sum
}

fn main() {
    let items = vec![
        Item { name: "apple".into(), qty: 3 },
        Item { name: "pear".into(),  qty: 0 },
        Item { name: "kiwi".into(),  qty: 7 },
    ];
    println!("total = {}", total(&items));
    if std::env::args().any(|a| a == "--panic") { total(&[Item { name: "x".into(), qty: 999 }]); }
}
