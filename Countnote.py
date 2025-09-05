import mido

file =  mido.MidiFile("C:\\Users\\q1760\\Downloads\\鸟之诗-AIR.mid")
for i, track in enumerate(file.tracks):
    channel_counts = {}
    for msg in track:
        if msg.type == 'note_on' or msg.type == 'note_off' :
            channel = msg.channel
            if channel in channel_counts:
                channel_counts[channel] += 1
            else:
                channel_counts[channel] = 1
    print(f"轨道 {i} 共计: {len(channel_counts)} 个通道")
    
    if channel_counts == {}:
        print("没有音符") 
    else:
        print("通道音符数量详情：")
    for channel, count in sorted(channel_counts.items()):
        print(f"通道 {channel}: {count} 个音符")
