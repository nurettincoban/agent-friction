//! Temperature conversion. Internally everything is °C.

pub fn c_to_f(c: u16) -> i32 {
    (i32::from(c) * 9 + 2) / 5 + 32
}

pub fn f_to_c(f: i32) -> i32 {
    ((f - 32) * 5 + 4) / 9
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn c_to_f_roundtrip() {
        for c in [0u16, 100, 999, 1222, 1305] {
            assert!((f_to_c(c_to_f(c)) - i32::from(c)).abs() <= 1);
        }
    }
}
