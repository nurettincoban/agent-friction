use serde::Serialize;

use crate::error::KilnError;

/// Orton self-supporting cone. `peak_c` is the bending temperature at a
/// 150 °C/h final ramp.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct Cone {
    pub name: &'static str,
    pub peak_c: u16,
}

pub const CONES: &[Cone] = &[
    Cone { name: "022", peak_c: 586 },
    Cone { name: "018", peak_c: 717 },
    Cone { name: "010", peak_c: 887 },
    Cone { name: "06", peak_c: 999 },
    Cone { name: "04", peak_c: 1060 },
    Cone { name: "03", peak_c: 1101 },
    Cone { name: "1", peak_c: 1137 },
    Cone { name: "5", peak_c: 1196 },
    Cone { name: "6", peak_c: 1222 },
    Cone { name: "10", peak_c: 1305 },
];

/// Look a cone up by name. Accepts "6", "c6" and "C6".
pub fn find(name: &str) -> Result<&'static Cone, KilnError> {
    let n = name.trim().trim_start_matches(['c', 'C']);
    CONES
        .iter()
        .find(|c| c.name == n)
        .ok_or_else(|| KilnError::UnknownCone(name.to_string()))
}

/// Peak temperature for a cone, in °C.
pub fn cone_to_celsius(name: &str) -> Result<u16, KilnError> {
    find(name).map(|c| c.peak_c)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_cone_with_or_without_prefix() {
        assert_eq!(cone_to_celsius("6").ok(), Some(1222));
        assert_eq!(cone_to_celsius("C6").ok(), Some(1222));
        assert_eq!(cone_to_celsius("c04").ok(), Some(1060));
    }

    #[test]
    fn unknown_cone_errors() {
        assert!(matches!(cone_to_celsius("7½"), Err(KilnError::UnknownCone(_))));
    }
}
