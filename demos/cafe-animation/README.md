# Café product animation

A 48-second silent concept film with realistic mock app screens. The film uses synthetic data. It does not record a running Melt application.

Open `index.html` to play the film with controls and optional English captions. The MP4 also plays independently.

The sequence shows a cost change, an agent request, a new break-even view, a Monday closure scenario, a local run with the model disconnected, and workspace restoration.

## Render the film

Run `python3 demos/cafe-animation/render.py` from the repository root. The renderer requires Pillow, DejaVu fonts, and `ffmpeg` on Linux. It exports a 1440 × 900 MP4 at 24 frames per second and three PNG stills.

Edit `render.py` to change the screens, timing, text, or colors. Edit `captions.vtt` when the sequence changes.
