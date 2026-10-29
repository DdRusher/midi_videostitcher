#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

fn main() {
    if let Err(error) = midi_videostitcher_rust::gui::launch() {
        eprintln!("无法打开窗口：{error}");
        rfd::MessageDialog::new()
            .set_title("无法打开 MIDI 视频拼接器")
            .set_description(format!("{error}\n请确认显卡驱动和桌面会话可用。"))
            .set_level(rfd::MessageLevel::Error)
            .show();
    }
}
