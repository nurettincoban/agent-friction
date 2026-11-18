use crate::cli::ConeArgs;
use kiln_core::{cone_to_celsius, units};

pub fn run(args: ConeArgs) -> anyhow::Result<()> {
    let c = cone_to_celsius(&args.name)?;
    if args.fahrenheit {
        println!("cone {}: {} °F", args.name, units::c_to_f(c));
    } else {
        println!("cone {}: {} °C", args.name, c);
    }
    Ok(())
}
