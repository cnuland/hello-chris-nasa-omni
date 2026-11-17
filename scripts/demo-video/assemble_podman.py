"""Assemble the demo: trim each recorded segment, add the captioned ISS tour clip, fade, concatenate to one MP4."""
import json, subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

V = Path("/tmp/nasa-video"); B = V / "build-podman"; B.mkdir(exist_ok=True)
TOUR = Path.home() / "nasa-media/iss-tour/tour-medium.mp4"
OUTFILE = V / "NASA-Omni-Inventory-POC-Demo-Podman.mp4"
seg = json.loads((V / "rec-podman/segments.json").read_text())
FONT = "/System/Library/Fonts/Helvetica.ttc"


def sh(*a):
    subprocess.run(a, check=True)


def caption_png(path, title, sub):
    im = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    ft = ImageFont.truetype(FONT, 27, index=1); fs = ImageFont.truetype(FONT, 21, index=0)
    w = max(d.textlength(title, font=ft), d.textlength(sub, font=fs)) + 52
    h = 14 + 34 + 30 + 14
    x0 = (1920 - w) / 2; y0 = 40
    d.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=10, fill=(8, 14, 28, 225))
    d.rectangle([x0, y0, x0 + 6, y0 + h], fill=(238, 0, 0, 255))
    d.text((x0 + 26, y0 + 12), title, font=ft, fill="white")
    d.text((x0 + 26, y0 + 12 + 36), sub, font=fs, fill=(201, 211, 227))
    im.save(path)


FPS = 30
COMMON = ["-r", str(FPS), "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
          "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]


def browser_segment(name, out):
    s = seg[name]; dur = s["end"] - s["start"] - 0.15
    vf = f"fps={FPS},scale=1920:1080,fade=t=in:st=0:d=0.35,fade=t=out:st={dur - 0.35:.2f}:d=0.35"
    sh("ffmpeg", "-v", "error", "-y", "-ss", f"{s['start']:.2f}", "-t", f"{dur:.2f}", "-i", s["file"],
       "-f", "lavfi", "-t", f"{dur:.2f}", "-i", "anullsrc=r=48000:cl=stereo",
       "-vf", vf, "-map", "0:v", "-map", "1:a", "-shortest", *COMMON, str(out))


def iss_clip(out, start=44, dur=28):
    caps = [(0, 9.5, "Input: NASA's public-domain tour video \"Step Inside the International Space Station\"",
             "Video and audio together, the way a station camera hears and sees it"),
            (9.5, 19, "Perception samples 6 frames plus the audio for each 30-second window",
             "The omni model (Gemma 4) returns one structured observation: objects, people, events and location"),
            (19, dur, "Watch the orange sphere in Kibo",
             "It is not in the catalog, so it becomes an unknown for NASA to classify. We follow it to the chatbot")]
    inputs, chain, last = [], [], "[v0]"
    for i, (a, b, t, sub) in enumerate(caps):
        p = B / f"isscap{i}.png"; caption_png(p, t, sub); inputs += ["-i", str(p)]
    fc = [f"[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps={FPS}[v0]"]
    for i, (a, b, *_ ) in enumerate(caps):
        nxt = f"[v{i + 1}]"
        fc.append(f"{last}[{i + 1}:v]overlay=0:0:enable='between(t,{a},{b})'{nxt}"); last = nxt
    fc.append(f"{last}fade=t=in:st=0:d=0.5,fade=t=out:st={dur - 0.6}:d=0.6[vout]")
    fc.append(f"[0:a]afade=t=in:st=0:d=0.8,afade=t=out:st={dur - 1.2}:d=1.2,volume=0.8[aout]")
    sh("ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(dur), "-i", str(TOUR), *inputs,
       "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]", *COMMON, str(out))


order = ["s0_title", "iss", "s2_terminal", "s4_dashboard", "s5_review", "s6_add", "s7_chat", "s9_close"]
parts = []
for name in order:
    out = B / f"{name}.mp4"
    iss_clip(out) if name == "iss" else browser_segment(name, out)
    parts.append(out)
(B / "list.txt").write_text("".join(f"file '{p}'\n" for p in parts))
sh("ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(B / "list.txt"), "-c", "copy",
   "-movflags", "+faststart", str(OUTFILE))
print(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,size", "-of", "compact", str(OUTFILE)],
                     capture_output=True, text=True).stdout)
