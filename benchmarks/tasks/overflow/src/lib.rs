//! True sum of the bytes in `data`.

pub fn byte_sum(data: &[u8]) -> u32 {
    let mut sum: u8 = 0;
    for &b in data {
        sum = sum.wrapping_add(b);
    }
    sum as u32
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sums_without_overflow() {
        assert_eq!(byte_sum(&[200, 100, 50]), 350);
    }
}
