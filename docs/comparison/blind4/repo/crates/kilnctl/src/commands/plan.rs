use anyhow::Context;
use chrono::{Local, NaiveTime};
use kiln_core::{cone_to_celsius, units, Preset, Schedule};

use crate::cli::{PlanArgs, PresetArg};

pub fn run(args: PlanArgs) -> anyhow::Result<()> {
    let preset = match args.preset {
        PresetArg::FastBisque => Preset::FastBisque,
        PresetArg::SlowBisque => Preset::SlowBisque,
        PresetArg::Glaze6 => Preset::Glaze6,
    };
    let mut schedule = Schedule::preset(preset);
    if let Some(cone) = &args.cone {
        schedule = schedule.with_peak(cone_to_celsius(cone)?);
    }
    schedule.validate()?;

    let start = match &args.start {
        Some(s) => {
            let t = NaiveTime::parse_from_str(s, "%H:%M")
                .with_context(|| format!("bad --start `{s}`, expected HH:MM"))?;
            Local::now()
                .with_time(t)
                .single()
                .context("start time is ambiguous in local time")?
        }
        None => Local::now(),
    };

    let unit = if args.fahrenheit { "°F" } else { "°C" };
    let show = |c: u16| if args.fahrenheit { units::c_to_f(c) } else { i32::from(c) };

    println!("{}  (start {})", schedule.name, start.format("%a %H:%M"));
    for (i, s) in schedule.segments.iter().enumerate() {
        println!(
            "  {:>2}. {:>5} {}/h -> {:>5} {}  hold {:>3} min",
            i + 1,
            s.rate_c_per_h,
            unit,
            show(s.target_c),
            unit,
            s.hold_min
        );
    }
    let total = schedule.duration();
    println!(
        "ETA {}  (total {}h{:02}m)",
        schedule.eta_from(start).format("%a %H:%M"),
        total.num_hours(),
        total.num_minutes() % 60
    );
    Ok(())
}
