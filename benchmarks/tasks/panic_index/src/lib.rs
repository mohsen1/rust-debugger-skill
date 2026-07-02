//! Return the third comma-separated field of `line`.

pub fn third_field(line: &str) -> &str {
    let parts: Vec<&str> = line.split(',').collect();
    parts[3]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gets_the_third_field() {
        assert_eq!(third_field("x,y,z"), "z");
    }
}
