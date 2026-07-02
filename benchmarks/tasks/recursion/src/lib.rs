//! nth Fibonacci number, with fib(0) = 0 and fib(1) = 1.

pub fn fib(n: u32) -> u64 {
    if n <= 1 {
        return 1;
    }
    fib(n - 1) + fib(n - 2)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tenth_fibonacci() {
        assert_eq!(fib(10), 55);
    }
}
