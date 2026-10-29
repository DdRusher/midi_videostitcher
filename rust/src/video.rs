use std::collections::{BTreeMap, HashMap};
use std::ffi::OsString;
use std::fs::{self, File};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};

use ab_glyph::{point, Font, FontArc, PxScale, ScaleFont};
use anyhow::{bail, Context, Result};
use font8x8::{UnicodeFonts, BASIC_FONTS};
use image::codecs::png::{CompressionType, FilterType, PngEncoder};
use image::{ImageEncoder, Rgb, RgbImage};
use memmap2::Mmap;
use rayon::prelude::*;

use crate::model::{
    frequency, note_name, JobControl, MediaInfo, RenderOptions, SourceWindow, Timeline, VisualMode,
};
use crate::{media, process};

fn push(arguments: &mut Vec<OsString>, values: &[&str]) {
    arguments.extend(values.iter().map(OsString::from));
}
fn push_path(arguments: &mut Vec<OsString>, path: &Path) {
    arguments.push(path.as_os_str().into());
}

fn encode_output(
    arguments: &mut Vec<OsString>,
    options: &RenderOptions,
    seconds: f64,
    output: &Path,
) {
    push(
        arguments,
        &[
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-bf",
            "0",
            "-threads",
            "2",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            "-frames:v",
        ],
    );
    arguments.push((seconds * options.fps as f64).round().to_string().into());
    arguments.push("-t".into());
    arguments.push(format!("{seconds:.12}").into());
    push_path(arguments, output);
}

fn geometry(options: &RenderOptions, count: usize) -> Result<(usize, usize, u32, u32)> {
    let columns = (count.max(1) as f64).sqrt().ceil() as usize;
    let rows = count.max(1).div_ceil(columns);
    let width = options.width / columns as u32 / 2 * 2;
    let height = options.height / rows as u32 / 2 * 2;
    if width < 2 || height < 2 {
        bail!("分辨率不足以同时显示 {count} 个音符，请提高输出分辨率。");
    }
    Ok((columns, rows, width, height))
}

fn hsv(h: f32, s: f32, v: f32) -> Rgb<u8> {
    let h = h.rem_euclid(1.0) * 6.0;
    let c = v * s;
    let x = c * (1.0 - ((h % 2.0) - 1.0).abs());
    let m = v - c;
    let (r, g, b) = match h as u8 {
        0 => (c, x, 0.0),
        1 => (x, c, 0.0),
        2 => (0.0, c, x),
        3 => (0.0, x, c),
        4 => (x, 0.0, c),
        _ => (c, 0.0, x),
    };
    Rgb([
        ((r + m) * 255.0).round() as u8,
        ((g + m) * 255.0).round() as u8,
        ((b + m) * 255.0).round() as u8,
    ])
}
fn rectangle(image: &mut RgbImage, x: u32, y: u32, width: u32, height: u32, color: Rgb<u8>) {
    for row in y..y.saturating_add(height).min(image.height()) {
        for column in x..x.saturating_add(width).min(image.width()) {
            image.put_pixel(column, row, color);
        }
    }
}
static FONT: OnceLock<Option<FontArc>> = OnceLock::new();
fn system_font() -> &'static Option<FontArc> {
    FONT.get_or_init(|| {
        let windows = std::env::var_os("WINDIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| "C:/Windows".into());
        let candidates = [
            windows.join("Fonts/consola.ttf"),
            windows.join("Fonts/arial.ttf"),
            PathBuf::from("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            PathBuf::from("/System/Library/Fonts/Supplemental/Arial.ttf"),
        ];
        candidates.into_iter().find_map(|path| {
            fs::read(path)
                .ok()
                .and_then(|bytes| FontArc::try_from_vec(bytes).ok())
        })
    })
}
fn text(
    image: &mut RgbImage,
    bounds: (u32, u32, u32, u32),
    value: &str,
    requested: f32,
    color: Rgb<u8>,
) {
    let (x, y, width, height) = bounds;
    if let Some(font) = system_font() {
        let mut scale = PxScale::from(requested.max(8.0));
        let mut scaled = font.as_scaled(scale);
        let mut extent: f32 = value
            .chars()
            .map(|c| scaled.h_advance(font.glyph_id(c)))
            .sum();
        if extent > width as f32 && extent > 0.0 {
            scale = PxScale::from(scale.x * width as f32 / extent);
            scaled = font.as_scaled(scale);
            extent = value
                .chars()
                .map(|c| scaled.h_advance(font.glyph_id(c)))
                .sum();
        }
        // Rasterize in the tile's coordinate system. Adding an integer screen
        // offset before outlining can change f32 rounding and font coverage on
        // some platforms, making the same tile depend on its grid position.
        let mut pen = (width as f32 - extent) / 2.0;
        let baseline = (height as f32 - scaled.height()) / 2.0 + scaled.ascent();
        for character in value.chars() {
            let id = font.glyph_id(character);
            let glyph = id.with_scale_and_position(scale, point(pen, baseline));
            if let Some(outline) = font.outline_glyph(glyph) {
                let rect = outline.px_bounds();
                outline.draw(|px, py, coverage| {
                    let gx = x as i32 + rect.min.x as i32 + px as i32;
                    let gy = y as i32 + rect.min.y as i32 + py as i32;
                    if gx >= 0 && gy >= 0 && gx < image.width() as i32 && gy < image.height() as i32
                    {
                        let pixel = image.get_pixel_mut(gx as u32, gy as u32);
                        for component in 0..3 {
                            pixel[component] = (pixel[component] as f32 * (1.0 - coverage)
                                + color[component] as f32 * coverage)
                                .round() as u8;
                        }
                    }
                });
            }
            pen += scaled.h_advance(id);
        }
    } else {
        let scale = ((requested as u32 / 8).max(1))
            .min((width / (value.chars().count().max(1) as u32 * 8)).max(1));
        let x = x + width.saturating_sub(value.len() as u32 * 8 * scale) / 2;
        let y = y + height.saturating_sub(8 * scale) / 2;
        for (index, character) in value.chars().enumerate() {
            if let Some(glyph) = BASIC_FONTS.get(character) {
                for (row, bits) in glyph.iter().enumerate() {
                    for column in 0..8 {
                        if bits & (1 << column) != 0 {
                            rectangle(
                                image,
                                x + index as u32 * 8 * scale + column * scale,
                                y + row as u32 * scale,
                                scale,
                                scale,
                                color,
                            );
                        }
                    }
                }
            }
        }
    }
}

fn card(options: &RenderOptions, voices: &[(u8, u8)]) -> Result<RgbImage> {
    let mut image = RgbImage::new(options.width, options.height);
    if voices.is_empty() {
        return Ok(image);
    }
    let (columns, _, width, height) = geometry(options, voices.len())?;
    for (index, &(pitch, velocity)) in voices.iter().enumerate() {
        let x = (index % columns) as u32 * width;
        let y = (index / columns) as u32 * height;
        if width <= 4 || height <= 4 {
            rectangle(
                &mut image,
                x,
                y,
                width,
                height,
                hsv((pitch % 12) as f32 / 12.0 + 0.58, 0.48, 0.94),
            );
            continue;
        }
        let margin = (width.min(height) / 28).clamp(1, 18);
        let hue = (pitch % 12) as f32 / 12.0 + 0.58;
        let accent = hsv(hue, 0.48, 0.94);
        let background = hsv(hue, 0.48, 0.18);
        rectangle(
            &mut image,
            x + margin,
            y + margin,
            width - 2 * margin,
            height - 2 * margin,
            accent,
        );
        let edge = (margin / 3).max(1);
        rectangle(
            &mut image,
            x + margin + edge,
            y + margin + edge,
            width - 2 * (margin + edge),
            height - 2 * (margin + edge),
            background,
        );
        let inset = margin * 3;
        let usable = width.saturating_sub(2 * inset);
        text(
            &mut image,
            (x + inset, y + height * 13 / 100, usable, height * 31 / 100),
            &note_name(pitch),
            (width as f32 * 0.3).min(height as f32 * 0.3),
            Rgb([255, 255, 255]),
        );
        if width >= 80 && height >= 75 {
            let small = (width as f32 * 0.055).min(height as f32 * 0.075).max(8.0);
            text(
                &mut image,
                (x + inset, y + height * 49 / 100, usable, height * 13 / 100),
                &format!("{:.3} Hz", frequency(pitch)),
                small,
                accent,
            );
            text(
                &mut image,
                (x + inset, y + height * 66 / 100, usable, height * 13 / 100),
                &format!("Velocity {velocity}/127"),
                small,
                Rgb([255, 255, 255]),
            );
            rectangle(
                &mut image,
                x + inset,
                y + height * 86 / 100,
                usable,
                (height / 40).max(2),
                Rgb([31, 38, 49]),
            );
            rectangle(
                &mut image,
                x + inset,
                y + height * 86 / 100,
                (usable as f64 * velocity as f64 / 127.0).round() as u32,
                (height / 40).max(2),
                accent,
            );
        }
    }
    Ok(image)
}

#[derive(Default)]
struct CardTiles {
    entries: HashMap<(u8, u8, u32, u32), Arc<RgbImage>>,
    bytes: usize,
}
fn cached_card(
    options: &RenderOptions,
    voices: &[(u8, u8)],
    tiles: &Mutex<CardTiles>,
    control: &JobControl,
) -> Result<RgbImage> {
    let mut output = RgbImage::new(options.width, options.height);
    if voices.is_empty() {
        return Ok(output);
    }
    let (columns, _, width, height) = geometry(options, voices.len())?;
    for (index, &(pitch, velocity)) in voices.iter().enumerate() {
        control.check()?;
        let key = (pitch, velocity, width, height);
        let found = tiles.lock().unwrap().entries.get(&key).cloned();
        let tile = if let Some(tile) = found {
            tile
        } else {
            let options = RenderOptions {
                width,
                height,
                ..Default::default()
            };
            let tile = Arc::new(card(&options, &[(pitch, velocity)])?);
            let mut tiles = tiles.lock().unwrap();
            if tiles.bytes + tile.as_raw().len() <= 256 * 1024 * 1024 {
                if !tiles.entries.contains_key(&key) {
                    tiles.bytes += tile.as_raw().len();
                    tiles.entries.insert(key, tile.clone());
                }
            }
            tile
        };
        let x = index % columns * width as usize;
        let y = index / columns * height as usize;
        let row_bytes = width as usize * 3;
        for row in 0..height as usize {
            let target = ((y + row) * options.width as usize + x) * 3;
            let pixels: &mut [u8] = output.as_mut();
            pixels[target..target + row_bytes]
                .copy_from_slice(&tile.as_raw()[row * row_bytes..(row + 1) * row_bytes]);
        }
    }
    Ok(output)
}

fn labels(
    timeline: &Timeline,
    options: &RenderOptions,
    ffmpeg: &Path,
    work: &Path,
    audio: &Path,
    control: &JobControl,
) -> Result<PathBuf> {
    let directory = work.join("cards");
    fs::create_dir(&directory)?;
    let mut indices: HashMap<Vec<(u8, u8)>, usize> = HashMap::new();
    let mut pictures = Vec::new();
    let mut concat = String::from("ffconcat version 1.0\n");
    let mut last_index = 0;
    for segment in &timeline.segments {
        control.check()?;
        let key: Vec<_> = segment
            .voices
            .iter()
            .map(|voice| (voice.pitch, voice.velocity))
            .collect();
        let index = if let Some(&index) = indices.get(&key) {
            index
        } else {
            let index = pictures.len();
            indices.insert(key.clone(), index);
            pictures.push(key);
            index
        };
        concat.push_str(&format!(
            "file 'card-{index:06}.png'\noption framerate {}\nduration {:.12}\n",
            options.fps,
            segment.frames() as f64 / options.fps as f64
        ));
        last_index = index;
    }
    concat.push_str(&format!(
        "file 'card-{last_index:06}.png'\noption framerate {}\n",
        options.fps
    ));
    let tiles = Mutex::new(CardTiles::default());
    let completed = Mutex::new(0_usize);
    let workers = if options.workers == 0 {
        std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(2)
            .min(4)
    } else {
        options.workers.min(16)
    };
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(workers.max(1))
        .build()?;
    pool.install(|| {
        pictures
            .par_iter()
            .enumerate()
            .map(|(index, key)| -> Result<()> {
                control.check()?;
                let image = cached_card(options, key, &tiles, control)?;
                let file = std::io::BufWriter::new(File::create(
                    directory.join(format!("card-{index:06}.png")),
                )?);
                let encoder =
                    PngEncoder::new_with_quality(file, CompressionType::Fast, FilterType::Sub);
                encoder.write_image(
                    image.as_raw(),
                    options.width,
                    options.height,
                    image::ExtendedColorType::Rgb8,
                )?;
                let mut completed = completed.lock().unwrap();
                *completed += 1;
                control.report(
                    0.35 * (*completed as f64 / pictures.len().max(1) as f64),
                    format!("已生成音名画面 {}/{}", *completed, pictures.len()),
                );
                Ok(())
            })
            .collect::<Result<Vec<_>>>()
    })?;
    let manifest = directory.join("cards.ffconcat");
    fs::write(&manifest, concat)?;
    let output = work.join("finished.mp4");
    let mut arguments = process::ffmpeg_args();
    // Give concat a relative manifest URL. FFmpeg otherwise resolves its
    // entries incorrectly when a Windows canonical path starts with \\?\.
    push(
        &mut arguments,
        &["-f", "concat", "-safe", "0", "-i", "cards.ffconcat"],
    );
    arguments.push("-i".into());
    push_path(&mut arguments, audio);
    push(
        &mut arguments,
        &["-vf", &format!("fps={}:round=near", options.fps)],
    );
    encode_output(&mut arguments, options, timeline.seconds(), &output);
    process::encode(
        ffmpeg,
        &arguments,
        Some(&directory),
        &control.scoped(0.35, 1.0, ""),
        timeline.seconds(),
    )?;
    Ok(output)
}
#[derive(Clone, Debug)]
struct CacheSpec {
    pitch: u8,
    width: u32,
    height: u32,
    border: u32,
    base: f64,
    end: f64,
    period: f64,
}
type TileKey = (u8, u32, u32);
struct Tile {
    map: Mmap,
    frames: usize,
    base: f64,
    period: f64,
    frame_bytes: usize,
    width: u32,
    height: u32,
}
static RGB_CACHE: OnceLock<PathBuf> = OnceLock::new();
fn cache_directory() -> Result<&'static Path> {
    let path =
        RGB_CACHE.get_or_init(|| std::env::temp_dir().join("midi-videostitcher-rgb-cache-v1"));
    fs::create_dir_all(path)?;
    Ok(path.as_path())
}

fn prune_cache(reserved: u64) -> Result<()> {
    let directory = cache_directory()?;
    let mut entries = Vec::new();
    let mut bytes = 0_u64;
    for item in fs::read_dir(directory)? {
        let path = item?.path();
        let metadata = match path.metadata() {
            Ok(value) => value,
            Err(_) => continue,
        };
        if path.extension().and_then(|value| value.to_str()) == Some("rgb") {
            bytes = bytes.saturating_add(metadata.len());
            entries.push((metadata.modified().ok(), path, metadata.len()));
        } else if metadata
            .modified()
            .ok()
            .and_then(|time| time.elapsed().ok())
            .map_or(false, |age| age.as_secs() > 3600)
        {
            let _ = fs::remove_file(path);
        }
    }
    entries.sort_by_key(|entry| entry.0);
    let target = (1024_u64 * 1024 * 1024).saturating_sub(reserved);
    for (_, path, length) in entries {
        if bytes <= target {
            break;
        }
        if fs::remove_file(path).is_ok() {
            bytes = bytes.saturating_sub(length);
        }
    }
    Ok(())
}
fn source_specs(
    timeline: &Timeline,
    assets: &BTreeMap<u8, MediaInfo>,
    windows: &BTreeMap<u8, SourceWindow>,
    options: &RenderOptions,
) -> Result<BTreeMap<TileKey, CacheSpec>> {
    let mut specs: BTreeMap<TileKey, CacheSpec> = BTreeMap::new();
    for segment in &timeline.segments {
        if segment.voices.is_empty() {
            continue;
        }
        let (_, _, width, height) = geometry(options, segment.voices.len())?;
        for voice in &segment.voices {
            let period = windows
                .get(&voice.pitch)
                .map(SourceWindow::duration)
                .unwrap_or(assets[&voice.pitch].duration);
            let mut start = voice.source_offset.rem_euclid(period);
            let mut end = start + segment.frames() as f64 / options.fps as f64;
            if end > period + 1e-8 {
                start = 0.0;
                end = period;
            }
            let entry = specs
                .entry((voice.pitch, width, height))
                .or_insert(CacheSpec {
                    pitch: voice.pitch,
                    width,
                    height,
                    border: if segment.voices.len() > 1 && width.min(height) >= 16 {
                        2
                    } else {
                        0
                    },
                    base: start,
                    end,
                    period,
                });
            entry.base = entry.base.min(start);
            entry.end = entry.end.max(end.min(period));
        }
    }
    for spec in specs.values_mut() {
        spec.base = (spec.base * options.fps as f64).floor() / options.fps as f64;
        spec.end = ((spec.end * options.fps as f64).ceil() / options.fps as f64).min(spec.period);
    }
    Ok(specs)
}

fn decode_tile(
    spec: &CacheSpec,
    asset: &MediaInfo,
    window: &SourceWindow,
    options: &RenderOptions,
    ffmpeg: &Path,
    control: &JobControl,
) -> Result<(TileKey, Tile)> {
    control.check()?;
    let cache = cache_directory()?;
    let fingerprint = format!(
        "{}|{:.12}|{:.12}|{}|{}|{}|{}|{:.12}|{:.12}",
        media::fingerprint(&asset.path)?,
        window.start,
        window.end,
        spec.width,
        spec.height,
        spec.border,
        options.fps,
        spec.base,
        spec.end
    );
    let hash = blake3::hash(fingerprint.as_bytes()).to_hex().to_string();
    let path = cache.join(format!("{hash}.rgb"));
    let frame_bytes = spec.width as usize * spec.height as usize * 3;
    if !path.is_file()
        || path.metadata()?.len() < (frame_bytes as u64)
        || path.metadata()?.len() % frame_bytes as u64 != 0
    {
        let partial = tempfile::Builder::new()
            .prefix("tile-")
            .tempfile_in(cache)?;
        let mut arguments = process::ffmpeg_args();
        push(
            &mut arguments,
            &[
                "-ss",
                &format!("{:.12}", window.start + spec.base),
                "-threads",
                "1",
                "-i",
            ],
        );
        push_path(&mut arguments, &asset.path);
        let border = spec.border;
        let filters=format!("fps={},scale={}:{}:force_original_aspect_ratio=decrease,pad={}:{}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1",options.fps,spec.width-2*border,spec.height-2*border,spec.width,spec.height);
        push(
            &mut arguments,
            &[
                "-t",
                &format!("{:.12}", spec.end - spec.base),
                "-map",
                "0:v:0",
                "-an",
                "-vf",
                &filters,
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "-threads",
                "1",
            ],
        );
        push_path(&mut arguments, partial.path());
        process::encode(
            ffmpeg,
            &arguments,
            None,
            &JobControl {
                cancel: control.cancel.clone(),
                ..Default::default()
            },
            spec.end - spec.base,
        )?;
        if partial.as_file().metadata()?.len() < frame_bytes as u64 {
            bail!(
                "素材 {} 的所选区间没有可见视频帧，请扩大范围。",
                note_name(spec.pitch)
            );
        }
        match partial.persist_noclobber(&path) {
            Ok(_) => {}
            Err(error) if path.is_file() => {
                drop(error);
            }
            Err(error) => return Err(error.error.into()),
        }
    }
    let file = File::open(&path)?;
    // The immutable cache is only published after the decoder has closed it.
    let map = unsafe { Mmap::map(&file) }?;
    let frames = map.len() / frame_bytes;
    Ok((
        (spec.pitch, spec.width, spec.height),
        Tile {
            map,
            frames,
            base: spec.base,
            period: spec.period,
            frame_bytes,
            width: spec.width,
            height: spec.height,
        },
    ))
}

fn native_clips(
    timeline: &Timeline,
    assets: &BTreeMap<u8, MediaInfo>,
    windows: &BTreeMap<u8, SourceWindow>,
    options: &RenderOptions,
    ffmpeg: &Path,
    work: &Path,
    audio: &Path,
    control: &JobControl,
    specs: BTreeMap<TileKey, CacheSpec>,
) -> Result<PathBuf> {
    let jobs: Vec<_> = specs.into_values().collect();
    let workers = if options.workers == 0 {
        std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(2)
            .min(4)
    } else {
        options.workers.min(16)
    };
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(workers.max(1))
        .build()?;
    let decoded: Vec<_> = pool.install(|| {
        jobs.par_iter()
            .map(|spec| {
                decode_tile(
                    spec,
                    &assets[&spec.pitch],
                    &windows[&spec.pitch],
                    options,
                    ffmpeg,
                    control,
                )
            })
            .collect::<Result<Vec<_>>>()
    })?;
    let tiles: BTreeMap<_, _> = decoded.into_iter().collect();
    control.report(0.15, "素材帧缓存已就绪，正在连续编码…");
    let output = work.join("finished.mp4");
    let mut arguments = process::ffmpeg_args();
    push(
        &mut arguments,
        &[
            "-f",
            "rawvideo",
            "-pixel_format",
            "rgb24",
            "-video_size",
            &format!("{}x{}", options.width, options.height),
            "-framerate",
            &options.fps.to_string(),
            "-i",
            "pipe:0",
            "-i",
        ],
    );
    push_path(&mut arguments, audio);
    encode_output(&mut arguments, options, timeline.seconds(), &output);
    let frame_bytes = options.width as usize * options.height as usize * 3;
    process::encode_frames(
        ffmpeg,
        &arguments,
        None,
        &control.scoped(0.15, 1.0, ""),
        timeline.seconds(),
        |pipe| {
            let mut frame = vec![0_u8; frame_bytes];
            for segment in &timeline.segments {
                let (columns, _, width, height) = geometry(options, segment.voices.len())?;
                for local in 0..segment.frames() {
                    control.check()?;
                    frame.fill(0);
                    for (index, voice) in segment.voices.iter().enumerate() {
                        let tile = &tiles[&(voice.pitch, width, height)];
                        let source_time = (voice.source_offset + local as f64 / options.fps as f64)
                            .rem_euclid(tile.period);
                        let source_frame =
                            (((source_time - tile.base).max(0.0) * options.fps as f64 + 1e-7)
                                .floor() as usize)
                                .min(tile.frames - 1);
                        let input = &tile.map[source_frame * tile.frame_bytes
                            ..(source_frame + 1) * tile.frame_bytes];
                        let x = index % columns * tile.width as usize;
                        let y = index / columns * tile.height as usize;
                        let row_bytes = tile.width as usize * 3;
                        for row in 0..tile.height as usize {
                            let target = ((y + row) * options.width as usize + x) * 3;
                            frame[target..target + row_bytes]
                                .copy_from_slice(&input[row * row_bytes..(row + 1) * row_bytes]);
                        }
                    }
                    pipe.write_all(&frame).context("写入连续视频帧失败")?;
                }
            }
            Ok(())
        },
    )?;
    Ok(output)
}

fn legacy_clips(
    timeline: &Timeline,
    assets: &BTreeMap<u8, MediaInfo>,
    windows: &BTreeMap<u8, SourceWindow>,
    options: &RenderOptions,
    ffmpeg: &Path,
    work: &Path,
    audio: &Path,
    control: &JobControl,
) -> Result<PathBuf> {
    let mut sources = assets.clone();
    let mut periods: BTreeMap<u8, f64> = assets
        .iter()
        .map(|(&pitch, info)| (pitch, info.duration))
        .collect();
    for (&pitch, window) in windows {
        if window.start <= 1e-9 && (window.end - assets[&pitch].duration).abs() < 1e-9 {
            continue;
        }
        let frames = (window.duration() * options.fps as f64 + 1e-8).floor() as u64;
        if frames < 1 {
            bail!("{} 的素材区间不足一帧，请扩大范围。", note_name(pitch));
        }
        let destination = work.join(format!("window-{pitch}.mp4"));
        let rate = frames as f64 / window.duration();
        let mut arguments = process::ffmpeg_args();
        push(
            &mut arguments,
            &[
                "-ss",
                &format!("{:.12}", window.start),
                "-threads",
                "1",
                "-i",
            ],
        );
        push_path(&mut arguments, &assets[&pitch].path);
        push(
            &mut arguments,
            &[
                "-map",
                "0:v:0",
                "-an",
                "-vf",
                &format!(
                    "fps={},trim=end_frame={frames},setpts=N/({rate:.12}*TB)",
                    options.fps
                ),
                "-frames:v",
                &frames.to_string(),
                "-r",
                &format!("{rate:.12}"),
                "-fps_mode",
                "cfr",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "18",
                "-bf",
                "0",
                "-pix_fmt",
                "yuv420p",
                "-video_track_timescale",
                "48000",
            ],
        );
        push_path(&mut arguments, &destination);
        process::encode(
            ffmpeg,
            &arguments,
            None,
            &JobControl {
                cancel: control.cancel.clone(),
                ..Default::default()
            },
            window.duration(),
        )?;
        sources.get_mut(&pitch).unwrap().path = destination;
        periods.insert(pitch, window.duration());
    }
    let workers = if options.workers == 0 {
        4
    } else {
        options.workers.min(16)
    };
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(workers.max(1))
        .build()?;
    let quiet = JobControl {
        cancel: control.cancel.clone(),
        ..Default::default()
    };
    let completed = Mutex::new(0_usize);
    pool.install(||timeline.segments.par_iter().enumerate().map(|(number,segment)|->Result<()> {
        quiet.check()?;let destination=work.join(format!("segment-{number:06}.mkv"));let mut arguments=process::ffmpeg_args();push(&mut arguments,&["-filter_complex_threads","1"]);
        if segment.voices.is_empty() {push(&mut arguments,&["-f","lavfi","-i",&format!("color=c=black:s={}x{}:r={}",options.width,options.height,options.fps),"-map","0:v:0"]);}
        else {
            let (columns,_,width,height)=geometry(options,segment.voices.len())?;let border=if segment.voices.len()>1 && width.min(height)>=16 {2}else{0};let mut filters=Vec::new();
            for (index,voice) in segment.voices.iter().enumerate() {
                push(&mut arguments,&["-stream_loop","-1","-ss",&format!("{:.12}",voice.source_offset.rem_euclid(periods[&voice.pitch])),"-threads","1","-i"]);push_path(&mut arguments,&sources[&voice.pitch].path);
                filters.push(format!("[{index}:v:0]setpts=PTS-STARTPTS,fps={},scale={}:{}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p[tile{index}]",options.fps,width-2*border,height-2*border));
            }
            if segment.voices.len()==1 {filters.push("[tile0]null[grid]".into());}
            else {let labels=(0..segment.voices.len()).map(|i|format!("[tile{i}]")).collect::<String>();let layout=(0..segment.voices.len()).map(|i|format!("{}_{}",i%columns*width as usize,i/columns*height as usize)).collect::<Vec<_>>().join("|");filters.push(format!("{labels}xstack=inputs={}:layout={layout}:fill=black[grid]",segment.voices.len()));}
            filters.push(format!("[grid]pad={}:{}:(ow-iw)/2:(oh-ih)/2:color=black[vout]",options.width,options.height));let script=work.join(format!("filters-{number:06}.txt"));fs::write(&script,filters.join(";\n"))?;
            arguments.push("-filter_complex_script".into());push_path(&mut arguments,&script);push(&mut arguments,&["-map","[vout]"]);
        }
        push(&mut arguments,&["-an","-frames:v",&segment.frames().to_string(),"-c:v","libx264","-preset","ultrafast","-crf","18","-bf","0","-pix_fmt","yuv420p","-threads","2"]);push_path(&mut arguments,&destination);
        process::encode(ffmpeg,&arguments,None,&quiet,segment.frames() as f64/options.fps as f64)?;
        let mut done=completed.lock().unwrap();*done+=1;control.report(0.1+0.8*(*done as f64/timeline.segments.len() as f64),"正在并行编码素材片段…");Ok(())
    }).collect::<Result<Vec<_>>>())?;
    let mut concat = String::new();
    for (index, segment) in timeline.segments.iter().enumerate() {
        concat.push_str(&format!(
            "file 'segment-{index:06}.mkv'\nduration {:.12}\n",
            segment.frames() as f64 / options.fps as f64
        ));
    }
    let manifest = work.join("segments.ffconcat");
    fs::write(&manifest, concat)?;
    let output = work.join("finished.mp4");
    let mut arguments = process::ffmpeg_args();
    // Resolve segment filenames against this directory, not a \\?\ URL.
    push(
        &mut arguments,
        &["-f", "concat", "-safe", "0", "-i", "segments.ffconcat"],
    );
    arguments.push("-i".into());
    push_path(&mut arguments, audio);
    push(
        &mut arguments,
        &[
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-t",
            &format!("{:.12}", timeline.seconds()),
            "-movflags",
            "+faststart",
        ],
    );
    push_path(&mut arguments, &output);
    process::encode(
        ffmpeg,
        &arguments,
        Some(work),
        &control.scoped(0.9, 1.0, ""),
        timeline.seconds(),
    )?;
    Ok(output)
}

pub fn render(
    timeline: &Timeline,
    assets: &BTreeMap<u8, MediaInfo>,
    windows: &BTreeMap<u8, SourceWindow>,
    options: &RenderOptions,
    ffmpeg: &Path,
    work: &Path,
    audio: &Path,
    control: &JobControl,
) -> Result<PathBuf> {
    if options.visuals == VisualMode::Labels {
        return labels(timeline, options, ffmpeg, work, audio, control);
    }
    let specs = source_specs(timeline, assets, windows, options)?;
    let bytes = specs
        .values()
        .map(|spec| {
            ((spec.end - spec.base) * options.fps as f64)
                .ceil()
                .max(1.0) as u64
                * spec.width as u64
                * spec.height as u64
                * 3
        })
        .sum::<u64>();
    if bytes <= 768 * 1024 * 1024 {
        prune_cache(bytes)?;
        native_clips(
            timeline, assets, windows, options, ffmpeg, work, audio, control, specs,
        )
    } else {
        control.report(0.0, "素材帧较多，使用低缓存并行模式…");
        legacy_clips(
            timeline, assets, windows, options, ffmpeg, work, audio, control,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn geometry_keeps_all_five_voices() {
        let options = RenderOptions {
            width: 640,
            height: 360,
            ..Default::default()
        };
        assert_eq!(geometry(&options, 5).unwrap(), (3, 2, 212, 180));
    }
    #[test]
    fn cached_tiles_match_direct_layout_and_handle_tiny_cells() {
        let options = RenderOptions {
            width: 320,
            height: 180,
            ..Default::default()
        };
        let voices = vec![(60, 32), (64, 64), (67, 127), (60, 90), (71, 100)];
        let cached = cached_card(
            &options,
            &voices,
            &Mutex::new(CardTiles::default()),
            &JobControl::default(),
        )
        .unwrap();
        let direct = card(&options, &voices).unwrap();
        assert!(
            cached.as_raw() == direct.as_raw(),
            "cached and direct card pixels differ at byte {:?}",
            cached
                .as_raw()
                .iter()
                .zip(direct.as_raw())
                .position(|(a, b)| a != b)
        );
        let tiny = RenderOptions {
            width: 64,
            height: 64,
            ..Default::default()
        };
        let dense: Vec<_> = (0..1000)
            .map(|index| (60 + (index % 12) as u8, 64))
            .collect();
        assert!(card(&tiny, &dense).is_ok());
    }
    #[test]
    fn cards_and_rests_have_visible_strength() {
        let options = RenderOptions {
            width: 320,
            height: 180,
            ..Default::default()
        };
        let rest = card(&options, &[]).unwrap();
        assert!(rest.as_raw().iter().all(|&value| value == 0));
        let strong = card(&options, &[(60, 127)]).unwrap();
        let weak = card(&options, &[(60, 32)]).unwrap();
        assert_ne!(strong.as_raw(), weak.as_raw());
    }
    #[test]
    fn low_cache_fallback_handles_canonical_unicode_paths_and_windowed_overlaps() {
        let Ok(ffmpeg) = media::find_ffmpeg(None) else {
            return;
        };
        let directory = tempfile::tempdir().unwrap();
        let work = directory.path().join("原视频 拼接");
        fs::create_dir(&work).unwrap();
        // On Windows this deliberately exercises extended-length \\?\ paths.
        let work = work.canonicalize().unwrap();
        let source = work.join("C4.mkv");
        let mut arguments = process::args(&[
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=64x64:r=20:d=1",
            "-an",
            "-c:v",
            "ffv1",
        ]);
        push_path(&mut arguments, &source);
        process::encode(&ffmpeg, &arguments, None, &JobControl::default(), 0.0).unwrap();
        let data = crate::model::MidiData {
            notes: vec![
                crate::model::Note {
                    pitch: 60,
                    velocity: 127,
                    start: 0.0,
                    end: 1.0,
                    track: 0,
                    channel: 0,
                    source_offset: 0.0,
                },
                crate::model::Note {
                    pitch: 60,
                    velocity: 100,
                    start: 0.4,
                    end: 0.8,
                    track: 0,
                    channel: 0,
                    source_offset: 0.0,
                },
            ],
            duration: 1.0,
            tracks: vec![],
            warnings: vec![],
        };
        let options = RenderOptions {
            width: 64,
            height: 64,
            fps: 20,
            visuals: VisualMode::Clips,
            workers: 2,
            ..Default::default()
        };
        let timeline = crate::timeline::build(&data, 20, options.chord).unwrap();
        let infos = media::probe_many(&ffmpeg, &[source.clone()], &JobControl::default()).unwrap();
        let assets = BTreeMap::from([(60, infos[&source].clone())]);
        let windows = BTreeMap::from([(
            60,
            SourceWindow {
                start: 0.2,
                end: 0.8,
            },
        )]);
        let audio = work.join("mix.wav");
        let spec = hound::WavSpec {
            channels: 2,
            sample_rate: 48000,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut writer = hound::WavWriter::create(&audio, spec).unwrap();
        for _ in 0..96000 {
            writer.write_sample(0_i16).unwrap();
        }
        writer.finalize().unwrap();
        let result = legacy_clips(
            &timeline,
            &assets,
            &windows,
            &options,
            &ffmpeg,
            &work,
            &audio,
            &JobControl::default(),
        )
        .unwrap();
        let verify = |result: &Path| {
            let mut arguments = process::args(&["-v", "error", "-i"]);
            push_path(&mut arguments, result);
            push(
                &mut arguments,
                &[
                    "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
                ],
            );
            let decoded =
                process::run(&ffmpeg, &arguments, None, &JobControl::default(), 0.0).unwrap();
            assert_eq!(
                decoded.code,
                0,
                "{}",
                String::from_utf8_lossy(&decoded.stderr)
            );
            assert_eq!(decoded.stdout.len(), 20 * 64 * 64 * 3);
        };
        verify(&result);
        // The image-card renderer uses another relative ffconcat manifest and
        // must remain safe when overwriting output in a canonical directory.
        let result = labels(
            &timeline,
            &options,
            &ffmpeg,
            &work,
            &audio,
            &JobControl::default(),
        )
        .unwrap();
        verify(&result);
    }
}
