#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Automated background removal and replacement for a single-subject video.
- Segments the person with rembg (U2Net human segmentation).
- Generates a stylized studio drum background.
- Composites subject over background per frame.
- Exports MP4; attempts to keep original audio via ffmpeg if available.

Usage:
    python scripts/video_bg_replace.py --input data/incoming/input.mp4 --output data/outputs/output.mp4

Notes:
- For best results, input should be a relatively static camera with a clear subject.
- This script runs on CPU; a short clip is recommended.
"""

import argparse
import os
import shutil
import subprocess
from typing import Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

try:
    from rembg import new_session, remove
except Exception as e:
    raise SystemExit("rembg is required. Install with: pip install rembg Pillow opencv-python numpy")


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def has_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False


def pil_from_bgr(frame: np.ndarray) -> Image.Image:
    # OpenCV BGR to PIL RGB
    return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))


def bgr_from_pil(img: Image.Image) -> np.ndarray:
    # PIL RGB/RGBA to OpenCV BGR
    if img.mode != "RGB":
        img = img.convert("RGB")
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def refine_alpha(alpha: Image.Image, feather: int = 2, close: int = 3, open_: int = 0, erode: int = 0, dilate: int = 0) -> Image.Image:
    # alpha: L mode
    a = np.array(alpha)
    # Morphological close to fill small holes
    if close > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close, close))
        a = cv2.morphologyEx(a, cv2.MORPH_CLOSE, kernel)
    # Optional open to remove small specks
    if open_ > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (open_, open_))
        a = cv2.morphologyEx(a, cv2.MORPH_OPEN, kernel)
    # Erode/dilate to control edge shrink/grow
    if erode > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (erode, erode))
        a = cv2.erode(a, kernel, iterations=1)
    if dilate > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilate, dilate))
        a = cv2.dilate(a, kernel, iterations=1)
    # Feather edges
    if feather > 0:
        a = cv2.GaussianBlur(a, (0, 0), sigmaX=feather, sigmaY=feather)
    return Image.fromarray(a)


def draw_drum_silhouette(size: Tuple[int, int]) -> Image.Image:
    """Draw a stylized drum kit silhouette as an RGBA layer."""
    w, h = size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    base_y = int(h * 0.72)
    color = (20, 20, 24, 255)

    # Bass drum
    r = int(min(w, h) * 0.12)
    cx = int(w * 0.48)
    cy = base_y - r
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)

    # Floor tom
    r2 = int(r * 0.75)
    cx2 = int(w * 0.62)
    cy2 = base_y - int(r2 * 0.9)
    d.ellipse([cx2 - r2, cy2 - r2, cx2 + r2, cy2 + r2], fill=color)

    # Rack tom
    r3 = int(r * 0.55)
    cx3 = int(w * 0.39)
    cy3 = base_y - int(r3 * 1.8)
    d.ellipse([cx3 - r3, cy3 - r3, cx3 + r3, cy3 + r3], fill=color)

    # Snare
    r4 = int(r * 0.5)
    cx4 = int(w * 0.52)
    cy4 = base_y - int(r4 * 1.9)
    d.ellipse([cx4 - r4, cy4 - r4, cx4 + r4, cy4 + r4], fill=color)

    # Stands and cymbals
    cym_color = (26, 26, 30, 255)
    # Hi-hat stand
    d.rectangle([int(w*0.33)-3, base_y - int(h*0.25), int(w*0.33)+3, base_y], fill=cym_color)
    # Hi-hat cymbal
    d.polygon([
        (int(w*0.28), base_y - int(h*0.25)),
        (int(w*0.33), base_y - int(h*0.27)),
        (int(w*0.38), base_y - int(h*0.25)),
        (int(w*0.33), base_y - int(h*0.23)),
    ], fill=cym_color)

    # Ride stand
    d.rectangle([int(w*0.72)-3, base_y - int(h*0.32), int(w*0.72)+3, base_y], fill=cym_color)
    # Ride cymbal
    d.polygon([
        (int(w*0.66), base_y - int(h*0.32)),
        (int(w*0.72), base_y - int(h*0.35)),
        (int(w*0.78), base_y - int(h*0.32)),
        (int(w*0.72), base_y - int(h*0.29)),
    ], fill=cym_color)

    # Kick pedal shadow
    d.rectangle([cx - int(r*0.2), base_y - 6, cx + int(r*0.2), base_y], fill=(0, 0, 0, 120))

    # Ground shadow bar
    d.rectangle([0, base_y, w, base_y + max(6, h//200)], fill=(0, 0, 0, 160))

    # Slight blur for realism
    return img.filter(ImageFilter.GaussianBlur(radius=max(1, int(min(w, h) * 0.002))))


def make_stage_background(size: Tuple[int, int]) -> Image.Image:
    """Create a stylized dark studio background with colored stage lights and fog."""
    w, h = size
    bg = Image.new("RGBA", (w, h), (14, 16, 22, 255))

    # Radial lights
    lights = [
        (int(w*0.25), int(h*0.2), (70, 120, 255, 120), int(min(w, h)*0.35)),
        (int(w*0.75), int(h*0.22), (255, 90, 140, 110), int(min(w, h)*0.35)),
        (int(w*0.5), int(h*0.05), (120, 180, 255, 90), int(min(w, h)*0.4)),
    ]
    for cx, cy, col, rad in lights:
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        lx = ImageDraw.Draw(layer)
        for r in range(rad, 0, -6):
            a = max(0, int(col[3] * (r / rad) ** 2))
            c = (col[0], col[1], col[2], a)
            lx.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c)
        bg = Image.alpha_composite(bg, layer)

    # Fog pass
    fog = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    fx = ImageDraw.Draw(fog)
    rng = np.random.default_rng(42)
    for _ in range(90):
        x = int(rng.uniform(0, w))
        y = int(rng.uniform(h*0.3, h*0.9))
        rw = int(rng.uniform(w*0.05, w*0.25))
        rh = int(rw * rng.uniform(0.2, 0.5))
        a = int(rng.uniform(8, 24))
        fx.ellipse([x - rw, y - rh, x + rw, y + rh], fill=(220, 230, 255, a))
    fog = fog.filter(ImageFilter.GaussianBlur(radius=max(6, int(min(w, h)*0.02))))
    bg = Image.alpha_composite(bg, fog)

    # Add drum silhouette
    drums = draw_drum_silhouette((w, h))
    bg = Image.alpha_composite(bg, drums)

    # Add realistic hardware stands (tripods) for hi-hat and ride, plus snare stand
    hardware = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    hw = ImageDraw.Draw(hardware)
    base_y = int(h * 0.72)
    pole = (175, 178, 185, 255)  # lighter metallic grey for visibility

    def tripod(x, y, leg= int(h*0.065), spread=int(w*0.055), width=5):
        # center pole
        hw.line([(x, y - int(h*0.26)), (x, y)], fill=pole, width=width)
        # left leg
        hw.line([(x, y), (x - spread, y + leg)], fill=pole, width=width)
        # right leg
        hw.line([(x, y), (x + spread, y + leg)], fill=pole, width=width)
        # rear leg (shorter, vertical hint)
        hw.line([(x, y), (x, y + int(leg*0.75))], fill=pole, width=max(2, width-1))

    # Hi-hat stand tripod
    x_hh = int(w*0.33)
    tripod(x_hh, base_y)
    # Hi-hat rod and cymbals (thin rod already in silhouette; reinforce)
    hw.line([(x_hh, base_y - int(h*0.25) - 4), (x_hh, base_y)], fill=pole, width=3)

    # Ride stand tripod with a boom arm
    x_ride = int(w*0.72)
    tripod(x_ride, base_y)
    # Boom arm
    hw.line([(x_ride, base_y - int(h*0.22)), (x_ride - int(w*0.06), base_y - int(h*0.30))], fill=pole, width=3)

    # Snare stand (shorter)
    x_sn = int(w*0.52)
    y_sn = base_y - int(h*0.16)
    hw.line([(x_sn, y_sn - int(h*0.08)), (x_sn, y_sn + int(h*0.02))], fill=pole, width=3)
    hw.line([(x_sn, y_sn + int(h*0.02)), (x_sn - int(w*0.035), y_sn + int(h*0.07))], fill=pole, width=3)
    hw.line([(x_sn, y_sn + int(h*0.02)), (x_sn + int(w*0.035), y_sn + int(h*0.07))], fill=pole, width=3)

    # Optional drum throne (stool) hint
    x_stool = int(w*0.57)
    seat_w = int(w*0.06); seat_h = int(h*0.02)
    hw.rounded_rectangle([x_stool - seat_w//2, base_y - int(h*0.22) - seat_h, x_stool + seat_w//2, base_y - int(h*0.22)], radius=6, fill=pole)
    hw.line([(x_stool, base_y - int(h*0.22)), (x_stool, base_y)], fill=pole, width=4)
    hw.line([(x_stool, base_y), (x_stool - int(w*0.04), base_y + int(h*0.06))], fill=pole, width=4)
    hw.line([(x_stool, base_y), (x_stool + int(w*0.04), base_y + int(h*0.06))], fill=pole, width=4)

    bg = Image.alpha_composite(bg, hardware)

    return bg


def process_video(input_path: str, output_path: str, work_dir: str = "data/work"):
    ensure_dir(work_dir)
    frames_dir = os.path.join(work_dir, "frames")
    cut_dir = os.path.join(work_dir, "cut")
    out_dir = os.path.join(work_dir, "out")
    for d in (frames_dir, cut_dir, out_dir):
        if os.path.exists(d):
            shutil.rmtree(d)
        ensure_dir(d)

    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        raise SystemExit(f"Failed to open input: {input_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    print(f"Input: {w}x{h} @ {fps:.2f} fps")

    session = new_session("isnet-general-use")

    # Pre-generate background
    stage_bg = make_stage_background((w, h))

    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        pil_frame = pil_from_bgr(frame)
        # Remove background -> RGBA with alpha
        rgba = remove(
            pil_frame,
            session=session,
            alpha_matting=True,
            alpha_matting_foreground_threshold=240,
            alpha_matting_background_threshold=10,
            alpha_matting_erode_size=10,
        )
        if rgba.mode != "RGBA":
            rgba = rgba.convert("RGBA")
        # Refine alpha
        alpha = rgba.split()[3]
        alpha = refine_alpha(alpha, feather=3, close=5)
        rgba = Image.merge("RGBA", (*rgba.split()[:3], alpha))
        # Composite onto stage background
        comp = Image.alpha_composite(stage_bg, rgba)
        comp.save(os.path.join(out_dir, f"{idx:06d}.png"))
        idx += 1
        if idx % 30 == 0:
            print(f"Processed {idx} frames…")

    cap.release()

    if idx == 0:
        raise SystemExit("No frames processed.")

    # Assemble video
    temp_video = os.path.join(work_dir, "video_noaudio.mp4")
    # Use ffmpeg for efficient encoding if available
    if has_ffmpeg():
        print("Encoding video with ffmpeg…")
        cmd = [
            "ffmpeg", "-y",
            "-framerate", f"{fps}",
            "-i", os.path.join(out_dir, "%06d.png"),
            "-c:v", "libx264", "-crf", "20", "-preset", "slow", "-pix_fmt", "yuv420p",
            temp_video,
        ]
        subprocess.run(cmd, check=True)
        # Mux original audio
        print("Muxing original audio…")
        cmd2 = [
            "ffmpeg", "-y",
            "-i", temp_video,
            "-i", input_path,
            "-c:v", "copy", "-c:a", "aac", "-map", "0:v:0", "-map", "1:a:0?",
            output_path,
        ]
        subprocess.run(cmd2, check=True)
    else:
        print("ffmpeg not found — writing silent MP4 via OpenCV (no audio).")
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        out = cv2.VideoWriter(temp_video, fourcc, fps, (w, h))
        for i in range(idx):
            img = Image.open(os.path.join(out_dir, f"{i:06d}.png")).convert("RGB")
            out.write(bgr_from_pil(img))
        out.release()
        shutil.move(temp_video, output_path)

    print(f"Done → {output_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="Path to input video (MP4)")
    ap.add_argument("--output", required=True, help="Path to output MP4")
    ap.add_argument("--work", default="data/work", help="Working directory for frames")
    args = ap.parse_args()

    process_video(args.input, args.output, args.work)
