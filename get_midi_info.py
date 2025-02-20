import mido

GM_INSTRUMENTS = [
    "Acoustic Grand Piano", "Bright Acoustic Piano", "Electric Grand Piano", "Honky-tonk Piano", 
    "Electric Piano 1", "Electric Piano 2", "Harpsichord", "Clavinet",
    "Celesta", "Glockenspiel", "Music Box", "Vibraphone", "Marimba", "Xylophone", "Tubular Bells", "Dulcimer",
    "Drawbar Organ", "Percussive Organ", "Rock Organ", "Church Organ", "Reed Organ", "Accordion", "Harmonica", "Tango Accordion",
    "Acoustic Guitar (nylon)", "Acoustic Guitar (steel)", "Electric Guitar (jazz)", "Electric Guitar (clean)", "Electric Guitar (muted)", "Overdriven Guitar", "Distortion Guitar", "Guitar harmonics",
    "Acoustic Bass", "Electric Bass (finger)", "Electric Bass (pick)", "Fretless Bass", "Slap Bass 1", "Slap Bass 2", "Synth Bass 1", "Synth Bass 2",
    "Violin", "Viola", "Cello", "Contrabass", "Tremolo Strings", "Pizzicato Strings", "Orchestral Harp", "Timpani",
    "String Ensemble 1", "String Ensemble 2", "SynthStrings 1", "SynthStrings 2", "Choir Aahs", "Voice Oohs", "Synth Voice", "Orchestra Hit",
    "Trumpet", "Trombone", "Tuba", "Muted Trumpet", "French Horn", "Brass Section", "SynthBrass 1", "SynthBrass 2",
    "Soprano Sax", "Alto Sax", "Tenor Sax", "Baritone Sax", "Oboe", "English Horn", "Bassoon", "Clarinet",
    "Piccolo", "Flute", "Recorder", "Pan Flute", "Blown Bottle", "Shakuhachi", "Whistle", "Ocarina",
    "Lead 1 (square)", "Lead 2 (sawtooth)", "Lead 3 (calliope)", "Lead 4 (chiff)", "Lead 5 (charang)", "Lead 6 (voice)", "Lead 7 (fifths)", "Lead 8 (bass + lead)",
    "Pad 1 (new age)", "Pad 2 (warm)", "Pad 3 (polysynth)", "Pad 4 (choir)", "Pad 5 (bowed)", "Pad 6 (metallic)", "Pad 7 (halo)", "Pad 8 (sweep)",
    "FX 1 (rain)", "FX 2 (soundtrack)", "FX 3 (crystal)", "FX 4 (atmosphere)", "FX 5 (brightness)", "FX 6 (goblins)", "FX 7 (echoes)", "FX 8 (sci-fi)",
    "Sitar", "Banjo", "Shamisen", "Koto", "Kalimba", "Bag pipe", "Fiddle", "Shanai",
    "Tinkle Bell", "Agogo", "Steel Drums", "Woodblock", "Taiko Drum", "Melodic Tom", "Synth Drum", "Reverse Cymbal",
    "Guitar Fret Noise", "Breath Noise", "Seashore", "Bird Tweet", "Telephone Ring", "Helicopter", "Applause", "Gunshot"
]

def analyze_midi(file_path):    
    try:
        mid = mido.MidiFile(file_path)
    except Exception as e:
        print("加载 MIDI 文件失败:", e)
        return
    
    print("MIDI 文件名:", file_path)
    # 尝试从第一个轨道的 meta 消息中获取歌曲名称（可能存在 track_name、sequence_name 或 song_title 消息）
    song_name = None
    for msg in mid.tracks[0]:
        if msg.is_meta and msg.type in ["track_name", "sequence_name", "song_title"]:
            song_name = msg.name
            break
    if song_name:
        print("\n歌曲名称:", song_name)
    else:
        print("\n未在 MIDI 文件中找到歌曲名称。")
    print("Ticks per beat:", mid.ticks_per_beat)
    
    # 尝试获取 BPM（tempo 通常以 set_tempo 消息给出，单位为微秒/拍）
    tempo = None
    for track in mid.tracks:
        for msg in track:
            if msg.type == 'set_tempo':
                tempo = msg.tempo
                break
        if tempo is not None:
            break
    if tempo:
        bpm = mido.tempo2bpm(tempo)
    else:
        bpm = "未知"
    print("BPM:", bpm)
    
    print("轨道数量:", len(mid.tracks))
    
    # 全局统计：所有使用的乐器（program_change）以及每个通道使用的乐器
    all_instruments = set()  # 存储全局 program number
    channel_instruments = {}  # key: channel，value: set of program numbers
    
    # 遍历每个轨道，提取信息
    for idx, track in enumerate(mid.tracks):
        print("\n轨道 {}:".format(idx))
        track_name = None
        track_instruments = set()  # 本轨道使用的乐器 program numbers
        track_channels = set()     # 本轨道使用的通道
        
        for msg in track:
            if msg.is_meta:
                # 获取轨道名称
                if msg.type == "track_name":
                    track_name = msg.name
                # 如果有 instrument_name 消息，也可以在此处理
            else:
                # 如果消息中包含 channel 属性
                if hasattr(msg, 'channel'):
                    track_channels.add(msg.channel)
                    # 记录 program_change 消息中的乐器信息
                    if msg.type == "program_change":
                        track_instruments.add(msg.program)
                        # 更新按通道统计的字典
                        if msg.channel not in channel_instruments:
                            channel_instruments[msg.channel] = set()
                        channel_instruments[msg.channel].add(msg.program)
                        all_instruments.add(msg.program)
        
        if track_name is None:
            track_name = "轨道{}".format(idx)
        print("轨道名称:", track_name)
        print("使用的通道:", sorted(list(track_channels)))
        if track_instruments:
            # 将 program number 转换为乐器名称
            instrument_names_list = [GM_INSTRUMENTS[p] if p < len(GM_INSTRUMENTS) else f"Program {p}"
                                       for p in sorted(list(track_instruments))]
            print("使用的乐器 (program numbers):", sorted(list(track_instruments)))
            print("使用的乐器 (名称):", instrument_names_list)
        else:
            print("该轨道未检测到乐器（可能仅包含打击乐或其他消息）")
    
    # 输出按通道统计的乐器信息
    print("\n各通道的乐器统计:")
    for ch in sorted(channel_instruments.keys()):
        progs = sorted(list(channel_instruments[ch]))
        instrument_names_list = [GM_INSTRUMENTS[p] if p < len(GM_INSTRUMENTS) else f"Program {p}"
                                 for p in progs]
        print("通道 {}: {} -> {}".format(ch, progs, instrument_names_list))
    
    # 全局乐器统计
    if all_instruments:
        progs = sorted(list(all_instruments))
        instrument_names_list = [GM_INSTRUMENTS[p] if p < len(GM_INSTRUMENTS) else f"Program {p}"
                                 for p in progs]
        print("\nMIDI 文件中使用的所有乐器:")
        print(progs, "->", instrument_names_list)
    else:
        print("MIDI 文件中未检测到乐器信息。")
    


if __name__ == "__main__":
    midi_file_path = "Hacking to the Gate.mid"
    analyze_midi(midi_file_path)
