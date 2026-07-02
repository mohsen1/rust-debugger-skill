//! Scale the average of the EVEN numbers in `xs` by 10.

pub fn scaled_even_average(xs: &[i64]) -> i64 {
    let evens: Vec<i64> = xs.iter().copied().filter(|x| x % 2 == 1).collect();
    if evens.is_empty() {
        return 0;
    }
    let sum: i64 = evens.iter().sum();
    (sum / evens.len() as i64) * 10
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn averages_the_evens() {
        // evens of [2, 4, 6, 7, 9] are [2, 4, 6]; average 4; * 10 = 40.
        assert_eq!(scaled_even_average(&[2, 4, 6, 7, 9]), 40);
    }
}
