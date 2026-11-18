mod cli;
mod commands;

use clap::Parser;

fn main() -> anyhow::Result<()> {
    let cli = cli::Cli::parse();
    match cli.command {
        cli::Command::Plan(args) => commands::plan::run(args),
        cli::Command::Cone(args) => commands::cone::run(args),
    }
}
