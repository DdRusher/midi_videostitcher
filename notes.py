import mido
def time_in_s(delta_ticks, tempo, ticks_per_beat):
    return round((delta_ticks * tempo)/(ticks_per_beat * 1000000),2)
midi_note_to_pitch_name = {
    0: "C-1", 1: "C#-1", 2: "D-1", 3: "D#-1", 4: "E-1", 5: "F-1", 6: "F#-1", 7: "G-1", 8: "G#-1", 9: "A-1", 10: "A#-1", 11: "B-1",
    12: "C0", 13: "C#0", 14: "D0", 15: "D#0", 16: "E0", 17: "F0", 18: "F#0", 19: "G0", 20: "G#0", 21: "A0", 22: "A#0", 23: "B0",
    24: "C1", 25: "C#1", 26: "D1", 27: "D#1", 28: "E1", 29: "F1", 30: "F#1", 31: "G1", 32: "G#1", 33: "A1", 34: "A#1", 35: "B1",
    36: "C2", 37: "C#2", 38: "D2", 39: "D#2", 40: "E2", 41: "F2", 42: "F#2", 43: "G2", 44: "G#2", 45: "A2", 46: "A#2", 47: "B2",
    48: "C3", 49: "C#3", 50: "D3", 51: "D#3", 52: "E3", 53: "F3", 54: "F#3", 55: "G3", 56: "G#3", 57: "A3", 58: "A#3", 59: "B3",
    60: "C4", 61: "C#4", 62: "D4", 63: "D#4", 64: "E4", 65: "F4", 66: "F#4", 67: "G4", 68: "G#4", 69: "A4", 70: "A#4", 71: "B4",
    72: "C5", 73: "C#5", 74: "D5", 75: "D#5", 76: "E5", 77: "F5", 78: "F#5", 79: "G5", 80: "G#5", 81: "A5", 82: "A#5", 83: "B5",
    84: "C6", 85: "C#6", 86: "D6", 87: "D#6", 88: "E6", 89: "F6", 90: "F#6", 91: "G6", 92: "G#6", 93: "A6", 94: "A#6", 95: "B6",
    96: "C7", 97: "C#7", 98: "D7", 99: "D#7", 100: "E7", 101: "F7", 102: "F#7", 103: "G7", 104: "G#7", 105: "A7", 106: "A#7", 107: "B7",
    108: "C8", 109: "C#8", 110: "D8", 111: "D#8", 112: "E8", 113: "F8", 114: "F#8", 115: "G8", 116: "G#8", 117: "A8", 118: "A#8", 119: "B8",
    120: "C9", 121: "C#9", 122: "D9", 123: "D#9", 124: "E9", 125: "F9", 126: "F#9", 127: "G9"
}
def midi_note_to_durations(midfile, selected_track = 0 , chanel = 0):
    try:
        midi = mido.MidiFile(midfile)
    except Exception as e:
        raise ValueError(f"Could not open \"{midfile}\": {e}")
    ticks_per_beat = midi.ticks_per_beat    
    tempo = 500000  
    for track in midi.tracks:
        for msg in track:
            if msg.type == 'set_tempo':
                tempo = msg.tempo
                break    
    note_list = []
    current_tick = 0
    on_notes = {}
    try:
        miditrack = midi.tracks[selected_track]
    except IndexError:
        raise ValueError(f"Selected track {selected_track} does not exist in \"{midfile}\"")
    for note in miditrack:
        current_tick += note.time
        if note.type in ["note_on", "note_off"] and note.channel == chanel:
            note_list.append([current_tick, note])
    result = []                                  #用来存储音符的列表，每一个元素是一个音符号和音符的持续时间   
    remember_note = {}                              #记录音符的索引用的字典
    for current_tickes, msg in note_list:
            if msg.type == 'note_on' and msg.velocity > 0:
                on_notes[msg.note] = current_tickes                 #写入当前的note_on message的音符与‘时间’到字典on_notes中
                result.append([msg.note, -1])                       #把该音符写入result,并将其持续时间写为个-1用来占位，等待note_off消息写入结束时间
                remember_note[msg.note] = len(result) - 1           #记录当前音符的索引
            else:
                current_time = time_in_s(current_tickes - on_notes.pop(msg.note), tempo, ticks_per_beat)   #计算当前音符的持续时间
                result[remember_note[msg.note]][1] = current_time   #通过之前的索引在result里找到之前对应的note_on音符并写入当前音符的真实持续时间
    result = [[midi_note_to_pitch_name[i[0]], i[1]] for i in result]
    return result
if __name__ == '__main__':
    print(midi_note_to_durations(midfile))



        
        
            
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
'''   
    #start_time
    for i, track in enumerate(midi.tracks):
        if i == 0:
            for msg in track:
                if msg.type == 'set_tempo':
                    tempo = msg.tempo
        if i == decided_track:
            for msg in track:
                if msg.type == 'note_on' and msg.channel == decided_chanel:
                    group = []
                    group.append(msg.time)
                    group.append(msg.note)
                    group.append(1)
                    note_list.append(group)
                elif msg.type == 'note_off' and msg.channel == decided_chanel:
                    group = []
                    group.append(msg.time)
                    group.append(msg.note)
                    group.append(0)
                    note_list.append(group)
    note_list_s = []
    for n1,i in enumerate(note_list):                 #i[0] is time, i[1] is note, i[2] is on/off
        new_group = []
        if i[0] != 0 and i[2] == 1:                    
            block = time_in_s(i[0], tempo, ticks_per_beat)
            new_group.append(block)
            new_group.append(0)
            note_list_s.append(new_group)
            new_group = []
        for n2,j in enumerate(note_list):             #find the same note and compute time(s)
            if i[1] == j[1] and n2 > n1 and j[2] == 0 and i[2] == 1:
                sum_time = 0
                for k in range(n1+1,n2+1):             
                    sum_time += note_list[k][0]       #add all time(ticks) of the same note
                new_group.append(time_in_s(sum_time, tempo, ticks_per_beat))
                new_group.append(i[1])
                note_list_s.append(new_group)
                break
    return note_list_s
if __name__ == '__main__':
    print(midi_note_to_s(midfile))

'''