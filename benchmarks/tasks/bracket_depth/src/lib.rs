//! Maximum nesting depth of parentheses in `s`.

pub fn max_depth(s: &str) -> i32 {
    let mut depth = 0;
    let mut max = 0;
    for c in s.chars() {
        if c == '(' {
            depth += 1;
        }
        if c == ')' {
            depth += 1;
        }
        if depth > max {
            max = depth;
        }
    }
    max
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nested_parens() {
        assert_eq!(max_depth("((()))"), 3);
    }
}
