use anyhow::{Context, Result};
use clap::Parser;
use midi_videostitcher_rust::{engine, midi, model::*, timeline};
use std::{collections::BTreeMap, path::PathBuf, sync::Arc, time::Instant};

#[derive(Parser, Debug)]
#[command(version, about = "Rust MIDI 视频拼接器：音名画面、素材分屏与原声混合")]
struct Args {
    #[arg(short = 'f', long, alias = "f")]
    midi: Option<PathBuf>,
    #[arg(long, default_value = "video")]
    video_dir: PathBuf,
    #[arg(short, long, default_value = "output.mp4")]
    output: PathBuf,
    #[arg(long)]
    track: Vec<usize>,
    #[arg(long, conflicts_with = "track")]
    all_tracks: bool,
    #[arg(long, value_parser=clap::value_parser!(u8).range(1..=16))]
    channel: Vec<u8>,
    #[arg(long, default_value="0", value_parser=parse_time)]
    start: f64,
    #[arg(long, value_parser=parse_time)]
    end: Option<f64>,
    #[arg(long, default_value="1280x720", value_parser=parse_size)]
    size: (u32, u32),
    #[arg(long, default_value_t = 30)]
    fps: u32,
    #[arg(long, default_value="labels", value_parser=["labels","clips"])]
    visuals: String,
    #[arg(long, default_value="all", value_parser=["all","highest","lowest","latest"])]
    chord_policy: String,
    #[arg(long)]
    audio: Option<PathBuf>,
    #[arg(long, default_value="0", value_parser=parse_time)]
    audio_offset: f64,
    #[arg(long, conflicts_with = "preview_audio")]
    audio_only: Option<PathBuf>,
    #[arg(long)]
    preview_audio: Option<PathBuf>,
    #[arg(long)]
    ignore_velocity: bool,
    #[arg(long)]
    material_window: Vec<String>,
    #[arg(long, value_parser=parse_pitch)]
    random_material: Vec<u8>,
    #[arg(long)]
    random_all: bool,
    #[arg(long, alias = "seed", default_value_t = 0)]
    random_seed: u64,
    #[arg(long, default_value_t = 0)]
    workers: usize,
    #[arg(long)]
    ffmpeg: Option<PathBuf>,
    #[arg(long)]
    overwrite: bool,
    #[arg(long)]
    check: bool,
    #[arg(long)]
    list_tracks: bool,
    #[arg(long)]
    info_json: Option<PathBuf>,
    #[arg(long)]
    benchmark_json: Option<PathBuf>,
}

fn parse_time(value: &str) -> std::result::Result<f64, String> {
    let parts: Vec<_> = value.trim().split(':').collect();
    if parts.is_empty() || parts.len() > 3 {
        return Err("时间应为秒、分:秒或时:分:秒".into());
    }
    let mut result = 0.0;
    for (index, part) in parts.iter().enumerate() {
        let number: f64 = part.parse().map_err(|_| "时间必须是有效数字".to_owned())?;
        if !number.is_finite()
            || number < 0.0
            || (parts.len() > 1 && index > 0 && number >= 60.0)
            || (index + 1 < parts.len() && number.fract() != 0.0)
        {
            return Err("时间范围或格式无效".into());
        }
        result = result * 60.0 + number;
    }
    Ok(result)
}
fn parse_size(value: &str) -> std::result::Result<(u32, u32), String> {
    let (left, right) = value.split_once('x').ok_or("分辨率格式为 1280x720")?;
    let width: u32 = left.parse().map_err(|_| "宽度无效")?;
    let height: u32 = right.parse().map_err(|_| "高度无效")?;
    if [width, height]
        .into_iter()
        .any(|n| !(16..=7680).contains(&n) || n % 2 != 0)
    {
        return Err("宽高必须是 16–7680 的偶数".into());
    }
    Ok((width, height))
}
fn parse_pitch(value: &str) -> std::result::Result<u8, String> {
    let value = value.trim();
    if let Ok(number) = value.parse::<u8>() {
        if number <= 127 {
            return Ok(number);
        }
    }
    let mut chars = value.chars();
    let letter = chars.next().unwrap_or(' ').to_ascii_uppercase();
    let base = match letter {
        'C' => 0,
        'D' => 2,
        'E' => 4,
        'F' => 5,
        'G' => 7,
        'A' => 9,
        'B' => 11,
        _ => return Err("音高应为 C4、C#4、Db4 或 0–127".into()),
    };
    let rest = chars.as_str();
    let (delta, octave) = if let Some(s) = rest.strip_prefix('#') {
        (1, s)
    } else if let Some(s) = rest.strip_prefix('b') {
        (-1, s)
    } else {
        (0, rest)
    };
    let octave: i32 = octave.parse().map_err(|_| "音高缺少有效八度")?;
    let number = (octave + 1) * 12 + base + delta;
    if !(0..=127).contains(&number) {
        return Err("音高必须在 MIDI 0–127 之间".into());
    }
    Ok(number as u8)
}

fn run(args: Args) -> Result<()> {
    let path = args.midi.context("请指定 --midi，或直接打开桌面窗口。")?;
    let overview = midi::load(&path)?;
    if args.list_tracks {
        println!(
            "时长：{:.6} 秒；{} 个音符",
            overview.duration,
            overview.notes.len()
        );
        for track in &overview.tracks {
            println!(
                "[{}] {} | {} 个音符 | 通道 {:?}",
                track.index,
                track.name,
                track.note_count,
                track.channels.iter().map(|n| n + 1).collect::<Vec<_>>()
            );
        }
        return Ok(());
    }
    let selection = Selection {
        tracks: if args.track.is_empty() {
            None
        } else {
            Some(args.track)
        },
        channels: if args.channel.is_empty() {
            None
        } else {
            Some(args.channel.into_iter().map(|n| n - 1).collect())
        },
        start: args.start,
        end: args.end,
    };
    let data = midi::select(&overview, &selection)?;
    if let Some(path) = args.info_json {
        std::fs::write(path, serde_json::to_vec_pretty(&data)?)?;
        return Ok(());
    }
    let mut materials = BTreeMap::new();
    for pitch in args.random_material {
        materials.insert(
            pitch,
            MaterialChoice {
                random: true,
                ..Default::default()
            },
        );
    }
    if args.random_all {
        for note in &data.notes {
            materials.entry(note.pitch).or_default().random = true;
        }
    }
    for spec in args.material_window {
        let (name, bounds) = spec.split_once('=').context("素材窗口格式为 C4=1,3")?;
        let (start, end) = bounds.split_once(',').context("素材窗口格式为 C4=1,3")?;
        let pitch = parse_pitch(name).map_err(anyhow::Error::msg)?;
        let setting = materials.entry(pitch).or_default();
        setting.start = parse_time(start).map_err(anyhow::Error::msg)?;
        setting.end = if end.trim().is_empty() {
            None
        } else {
            Some(parse_time(end).map_err(anyhow::Error::msg)?)
        };
    }
    let audio_output = args.audio_only.or(args.preview_audio);
    let options = RenderOptions {
        video_dir: args.video_dir,
        output: audio_output.clone().unwrap_or(args.output),
        width: args.size.0,
        height: args.size.1,
        fps: args.fps,
        visuals: if args.visuals == "clips" {
            VisualMode::Clips
        } else {
            VisualMode::Labels
        },
        audio_path: args.audio,
        audio_start: args.start + args.audio_offset,
        velocity: !args.ignore_velocity,
        materials,
        seed: args.random_seed,
        ffmpeg: args.ffmpeg,
        workers: args.workers,
        overwrite: args.overwrite,
        chord: match args.chord_policy.as_str() {
            "highest" => ChordPolicy::Highest,
            "lowest" => ChordPolicy::Lowest,
            "latest" => ChordPolicy::Latest,
            _ => ChordPolicy::All,
        },
    };
    let control = JobControl {
        progress: Arc::new(|event| eprintln!("{:5.1}% {}", event.fraction * 100.0, event.message)),
        ..Default::default()
    };
    if args.check {
        let assets = engine::inspect_materials(&data, &options, &control)?;
        if !assets.is_empty() {
            timeline::plan_materials(&data, &options, &assets)?;
        }
        println!(
            "检查通过：{} 个音符，{:.6} 秒，{} 种素材",
            data.notes.len(),
            data.duration,
            assets.len()
        );
        return Ok(());
    }
    let started = Instant::now();
    let output = if audio_output.is_some() {
        engine::render_audio(&data, &options, &control)?
    } else {
        engine::render(&data, &options, &control)?
    };
    let elapsed = started.elapsed().as_secs_f64();
    println!("已保存：{}；耗时 {:.3} 秒", output.display(), elapsed);
    if let Some(path) = args.benchmark_json {
        let plan = timeline::build(&data, options.fps, options.chord)?;
        let report = serde_json::json!({"engine":"rust","notes":data.notes.len(),"duration_seconds":data.duration,"segments":plan.segments.len(),"max_simultaneous_voices":plan.segments.iter().map(|s|s.voices.len()).max().unwrap_or(0),"width":options.width,"height":options.height,"fps":options.fps,"visuals":options.visuals,"wall_seconds":elapsed,"output_bytes":std::fs::metadata(output)?.len()});
        std::fs::write(path, serde_json::to_vec_pretty(&report)?)?;
    }
    Ok(())
}

fn main() {
    if std::env::args_os().len() == 1 {
        #[cfg(feature = "gui")]
        {
            if let Err(error) = midi_videostitcher_rust::gui::launch() {
                eprintln!("无法打开窗口：{error}");
                std::process::exit(1)
            }
            return;
        }
        #[cfg(not(feature = "gui"))]
        {
            eprintln!("此构建只有命令行功能，请使用 --help。");
            return;
        }
    }
    if let Err(error) = run(Args::parse()) {
        eprintln!("操作失败：{error:#}");
        std::process::exit(1)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn times_and_pitches() {
        assert_eq!(parse_time("1:02.5").unwrap(), 62.5);
        assert_eq!(parse_pitch("C#1").unwrap(), 25);
        assert_eq!(parse_pitch("Db4").unwrap(), 61);
        assert!(parse_time("NaN").is_err());
        assert!(parse_time("01:60").is_err());
        assert!(parse_pitch("C100").is_err());
    }
    #[test]
    fn bad_arguments() {
        assert!(
            Args::try_parse_from(["app", "--audio-only", "a.wav", "--preview-audio", "b.wav"])
                .is_err()
        );
        assert!(parse_size("3x720").is_err());
    }
}
