# Café product animation

A 30-second silent concept film with mock app screens and synthetic data. It does not record a running Melt application.

The job: “I check café profit every week. Make me an app I can keep.”

The Agent creates a separate dashboard window. The user adds controls through conversation, adjusts the scenario, and saves the result as a desktop app. Both windows close. The user opens the saved app from its icon. Its layout, controls, values, and results remain the same. The Agent stays closed.

The final app exposes **Edit with Agent** and **Share app**. The share menu illustrates saving a copy that includes the app, data, and current scenario. The film does not send a copy to another person.

Desktop installation and sharing are product concepts in this film. Their depiction does not claim implemented native integration.

## Storyboard

1. Open Melt Agent from the desktop and describe a recurring task.
2. Watch the Agent create a dashboard in a separate window.
3. Add cost, break-even, and opening-day controls through conversation.
4. Set costs to +30% and close Mondays. Save the app to the desktop.
5. Close the Agent and dashboard. The desktop icon remains.
6. Open Little Hours from its icon. Use the same saved app directly.
7. Open the app's share menu to show the option to save a copy.

## Play and render

Open `index.html` to play the film. English captions are available when served over HTTP. The MP4 also plays independently.

Run `python3 demos/cafe-animation/render.py` from the repository root. The renderer requires Pillow, NumPy, DejaVu fonts, and `ffmpeg` on Linux.

It exports a 1440 × 900 MP4 at 30 frames per second, six screen stills, and a desktop image. The Aqua-inspired wallpaper is original procedural artwork.

Edit `render.py` to change the screens, timing, text, or colors. Update `captions.vtt` when the sequence changes.
