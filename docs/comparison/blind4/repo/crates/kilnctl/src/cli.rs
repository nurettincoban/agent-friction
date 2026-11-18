use clap::{Args, Parser, Subcommand, ValueEnum};

#[derive(Debug, Parser)]
#[command(name = "kilnctl", version, about = "Plan and inspect kiln firings")]
pub struct Cli {
    #[command(subcommand)]
    pub command: Command,
}

#[derive(Debug, Subcommand)]
pub enum Command {
    /// Print a firing schedule with its ETA
    Plan(PlanArgs),
    /// Show the peak temperature for a cone
    Cone(ConeArgs),
}

#[derive(Debug, Args)]
pub struct PlanArgs {
    #[arg(long, value_enum, default_value_t = PresetArg::FastBisque)]
    pub preset: PresetArg,
    /// Fire to this cone instead of the preset's default peak
    #[arg(long)]
    pub cone: Option<String>,
    /// Start time as HH:MM today (defaults to now)
    #[arg(long)]
    pub start: Option<String>,
    /// Print temperatures in °F
    #[arg(long)]
    pub fahrenheit: bool,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
pub enum PresetArg {
    FastBisque,
    SlowBisque,
    Glaze6,
}

#[derive(Debug, Args)]
pub struct ConeArgs {
    /// Cone name, e.g. 6, C6, 04
    pub name: String,
    /// Print °F instead of °C
    #[arg(long)]
    pub fahrenheit: bool,
}
