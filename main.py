import argparse
import notes
from moviepy import VideoFileClip, concatenate_videoclips
midfile = argparse.ArgumentParser()
midfile.add_argument("--f", help="path to midi file")
notes_list = notes.midi_note_to_durations(midfile)
clips = []
def main(notes_list):
    notes_list = [[f"video/{note}.mp4", duration] for note, duration in notes_list]
    for file, duration in notes_list: 
        clip = VideoFileClip(file)
        clip = clip.subclipped(0, duration)
        clips.append(clip)
    final_clip = concatenate_videoclips(clips, method = 'compose')
    final_clip.write_videofile("output.mp4")
if __name__ == '__main__':
    main(notes_list)
        



        
    
