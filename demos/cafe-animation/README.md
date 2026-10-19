# Café product animation

A 24-second silent concept film with realistic mock app screens and synthetic data. It does not record a running Melt application.

The film starts with an empty workspace on a classic Aqua-style desktop. Three short agent prompts build the interface. The cursor adjusts costs, closes Mondays, and runs the tool with the agent disconnected.

Open `index.html` to play the film. English captions are available when served over HTTP. The MP4 also plays independently.

## Render the film

Run `python3 demos/cafe-animation/render.py` from the repository root. The renderer requires Pillow, NumPy, DejaVu fonts, and `ffmpeg` on Linux.

It exports a 1440 × 900 MP4 at 30 frames per second, four screen stills, and a desktop image. The Aqua-inspired wallpaper is original procedural artwork.

Edit `render.py` to change the screens, timing, text, or colors. Update `captions.vtt` when the sequence changes.
