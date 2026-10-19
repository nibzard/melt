"""Render the Melt café concept film. Requires Pillow and ffmpeg."""
from pathlib import Path
import math
import subprocess
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent
W, H, FPS, DURATION = 1440, 900, 24, 48
BG = '#e9e7e1'
INK = '#252b28'
MUTED = '#7a817b'
GREEN = '#25775b'
LINE = '#e7e9e3'
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
SERIF = '/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf'
fonts = {}


def font(size, weight='normal'):
    key = size, weight
    if key not in fonts:
        fonts[key] = ImageFont.truetype({'normal': FONT, 'bold': BOLD, 'serif': SERIF}[weight], size)
    return fonts[key]


def ease(t):
    t = max(0, min(1, t))
    return t*t*(3-2*t)


def frame(t):
    im = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(im)

    def box(x, y, w, h, fill='white', radius=12, outline=None):
        d.rounded_rectangle((x, y, x+w, y+h), radius, fill, outline, width=1)

    def txt(x, y, s, size=14, color=INK, weight='normal'):
        d.text((x, y), s, fill=color, font=font(size, weight))

    def line(points, color=LINE, width=1):
        d.line(points, fill=color, width=width)

    def pill(x, y, s, fill='#edf3ed', color=GREEN):
        width = d.textlength(s, font=font(11)) + 22
        box(x, y, width, 25, fill, 12)
        txt(x+11, y+5, s, 11, color)

    evolved = t >= 20
    closed = t >= 29
    offline = t >= 35
    cost = 30 * ease((t-6)/1.2)
    profit = round(3280 - 864 * cost/30 + (420 if closed else 0))
    title, subtitle = ('A question becomes a workspace.', 'MELT  /  THE CAFÉ DEMO')
    if t >= 5:
        title, subtitle = 'Change an assumption. See the impact.', '01  /  EXPLORE'
    if t >= 12:
        title, subtitle = 'Ask for the tool you need next.', '02  /  EVOLVE'
    if t >= 25:
        title, subtitle = 'Keep the tool. Explore the decision.', '03  /  OPERATE'
    if t >= 35:
        title, subtitle = 'The model can leave. Your tools stay.', '04  /  OWN'
    if t >= 40:
        title, subtitle = 'Pick up exactly where you left off.', '05  /  RETURN'
    txt(60, 25, subtitle, 12, GREEN, 'bold')
    txt(60, 49, title, 31, INK, 'serif')
    txt(1195, 53, 'melt', 31, INK, 'bold')
    for i in range(4):
        box(1290+i*12, 59+i*3, 7, 25-i*3, GREEN, 3)

    # Desktop window and title bar.
    box(44, 111, 1352, 718, '#d9d8d1', 18)
    box(40, 105, 1360, 716, '#fafbf8', 16, '#d1d6cc')
    box(41, 106, 1358, 43, '#f1f2ed', 15)
    d.rectangle((41, 133, 1399, 149), fill='#f1f2ed')
    for x, c in [(62, '#e68f82'), (82, '#e4c16e'), (102, '#87b68b')]:
        d.ellipse((x, 122, x+10, 132), fill=c)
    txt(153, 118, 'Melt', 14, INK, 'bold')
    txt(204, 119, '/   Little Hours Café', 13, MUTED)
    txt(1080, 120, 'Saved locally  ·  ' + ('10:43' if closed else '10:42'), 11, MUTED)
    pill(1260, 114, 'Workspace', '#e4e9df', '#54644d')
    line((40, 149, 1400, 149), '#dce0d6')

    # Navigation.
    d.rectangle((41, 150, 219, 793), fill='#f1f3ed')
    txt(59, 173, 'LITTLE HOURS', 11, MUTED, 'bold')
    txt(59, 194, 'Café workspace', 16, INK, 'bold')
    box(51, 235, 157, 36, '#e0e8da', 7)
    txt(65, 244, '▦  Overview', 13, GREEN, 'bold')
    txt(65, 287, '◷  Run history', 13, MUTED)
    txt(65, 327, '▤  Sources', 13, MUTED)
    txt(59, 393, 'DATA SOURCES', 10, MUTED, 'bold')
    for y, a, b in [(423, 'sales.csv', '842 rows'), (476, 'costs.csv', '18 ingredients'), (529, 'staffing.csv', '7 days')]:
        txt(61, y, a, 12)
        txt(61, y+19, b, 10, MUTED)
    box(54, 684, 151, 87, '#e8ece2', 8)
    txt(66, 697, 'Your workspace', 11, GREEN, 'bold')
    txt(66, 718, 'Stored on this device', 10, MUTED)
    txt(66, 739, 'All changes saved', 10, GREEN)

    # Workspace header.
    txt(243, 170, 'Can this café survive?', 25, INK, 'bold')
    txt(244, 207, 'A small café. A few assumptions. A clearer decision.', 12, MUTED)
    pill(909, 174, 'Scenario model')
    for x, label, value, detail in [
        (243, 'MONTHLY REVENUE', '€18,600' if not closed else '€17,400', 'Typical month · 30 days'),
        (507, 'MONTHLY PROFIT', f'€{profit:,}', 'After operating costs'),
        (771, 'PROFIT MARGIN', f'{profit/(17400 if closed else 18600)*100:.1f}%', 'Based on current assumptions')]:
        box(x, 243, 250, 108, 'white', 10, LINE)
        txt(x+16, 257, label, 10, MUTED, 'bold')
        txt(x+16, 279, value, 29, GREEN if label == 'MONTHLY PROFIT' else INK, 'bold')
        txt(x+16, 325, detail, 10, MUTED)

    # Main chart.
    chartw = 493 if evolved else 778
    box(243, 365, chartw, 233, 'white', 10, LINE)
    txt(260, 381, 'Profit by day', 15, INK, 'bold')
    txt(260, 406, 'Weekly pattern · current scenario', 10, MUTED)
    plotx, ploty, plotw = 288, 445, chartw-76
    for k, label in enumerate(['€200', '€100', '€0']):
        yy = ploty + k*54
        txt(255, yy-6, label, 9, MUTED)
        line((plotx, yy, plotx+plotw, yy))
    values = [28, 61, 77, 89, 142, 180, 119]
    gap = plotw/7
    for i, v in enumerate(values):
        reduced = v - cost*.65
        if i == 0 and closed:
            reduced = 0
        bh = max(2, reduced*.54)
        xx = plotx + i*gap+gap*.18
        box(xx, 553-bh, gap*.48, bh, '#abc6a9' if i < 4 else GREEN, 4)
        txt(xx-1, 564, ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][i], 10, MUTED)

    if evolved:
        box(750, 365, 271, 233, '#edf3e8', 10, '#d0dfc6')
        txt(769, 381, 'Break-even', 15, INK, 'bold')
        pill(931, 378, 'New', '#dae9cf')
        txt(770, 425, '€539' if not closed else '€560', 34, GREEN, 'bold')
        txt(770, 470, 'daily revenue needed', 12, MUTED)
        box(770, 503, 230, 8, '#d4e0ca', 4)
        box(770, 503, 183 if not closed else 192, 8, GREEN, 4)
        txt(770, 529, 'Current: €620 / day' if not closed else 'Current: €669 / open day', 11)
        txt(770, 565, 'Calculated with your cost assumptions', 9, MUTED)
        glow = 1-ease((t-20)/2)
        if glow:
            d.rounded_rectangle((748, 363, 1023, 600), 12, outline=GREEN, width=2)

    # Bottom assumptions and table.
    box(243, 612, 370, 163, 'white', 10, LINE)
    txt(260, 626, 'Assumptions', 14, INK, 'bold')
    txt(260, 661, 'Coffee bean costs', 12)
    box(482, 650, 113, 33, '#f5f6f2', 6, LINE)
    txt(496, 658, f'+{round(cost)}%', 14, GREEN, 'bold')
    if evolved:
        txt(260, 702, 'Open on Mondays', 12)
        box(554, 697, 40, 22, '#d7dcd4' if closed else GREEN, 11)
        d.ellipse((557 if closed else 575, 700, 573 if closed else 591, 716), fill='white')
    else:
        txt(260, 704, 'Changes apply to this scenario only.', 10, MUTED)
    box(260, 738, 335, 25, GREEN, 6)
    txt(379, 743, 'Run scenario', 11, 'white', 'bold')
    box(627, 612, 394, 163, 'white', 10, LINE)
    txt(643, 626, 'Scenario results', 14, INK, 'bold')
    txt(643, 660, 'SCENARIO', 9, MUTED, 'bold')
    txt(853, 660, 'PROFIT / MO.', 9, MUTED, 'bold')
    for y, label, val, active in [(686, 'Original costs', '€3,280', False), (714, 'Beans +30%', '€2,416', not closed), (742, 'Closed Mondays', '€2,836', closed)]:
        if y == 714 and t < 7.2:
            continue
        if y == 742 and not closed:
            continue
        if active:
            box(637, y-2, 374, 25, '#edf3e8', 4)
        txt(645, y+2, label, 11)
        txt(868, y+2, val, 11, GREEN if active else MUTED, 'bold' if active else 'normal')

    # Agent panel, with a believable conversation and live patch receipt.
    d.rectangle((1040, 150, 1399, 793), fill='#f6f7f3')
    line((1040, 150, 1040, 793), '#dce0d6')
    txt(1062, 171, '✦  Workspace agent', 15, INK, 'bold')
    pill(1062, 203, 'Model disconnected' if offline else 'Connected', '#eee8dd' if offline else '#e7eedf', '#8b7450' if offline else GREEN)
    txt(1062, 253, 'YOU', 10, MUTED, 'bold')
    txt(1062, 276, 'Can this café survive?', 15)
    txt(1062, 319, 'MELT', 10, GREEN, 'bold')
    for i, s in enumerate(['I built a workspace from your', 'sales, costs, and staffing data.', '', 'Change an assumption to explore', 'what keeps the café profitable.']):
        txt(1062, 342+i*22, s, 12, '#555e55')
    if t >= 13:
        box(1056, 466, 326, 84, '#e6ecdf', 9)
        prompt = 'Add a break-even view and let me adjust opening hours.'
        shown = prompt[:int(max(0, t-13)*24)]
        parts = [shown[:30], shown[30:]]
        txt(1070, 478, 'YOU', 9, MUTED, 'bold')
        for i, s in enumerate(parts):
            txt(1070, 500+i*21, s, 12)
    if 16 <= t < 20:
        txt(1065, 571, '✦  Updating your workspace' + '.'*(int(t*2)%4), 12, GREEN)
        txt(1065, 597, 'Adding a calculation and a new view', 10, MUTED)
    if t >= 20:
        txt(1062, 570, '✓  Workspace updated', 13, GREEN, 'bold')
        for i, s in enumerate(['Added a break-even calculation.', 'Your assumptions and views are kept.', '', 'Try closing on Mondays to compare', 'the monthly profit.']):
            txt(1062, 599+i*21, s, 11, '#555e55')
    box(1056, 727, 326, 48, '#ffffff', 9, LINE)
    txt(1070, 743, 'Reconnect to ask for changes' if offline else 'Ask to change this workspace…', 11, MUTED)
    line((40, 793, 1400, 793), '#dce0d6')
    txt(58, 801, '●  Local workspace', 10, GREEN)
    txt(246, 801, 'Local run complete · results saved' if t >= 37 else ('Scenario saved' if closed else 'All results saved'), 10, MUTED)
    txt(1059, 801, 'Python actions available locally', 10, MUTED)

    # Cursor choreography.
    if 5 <= t < 11:
        cx, cy = 545, 669
    elif 26 <= t < 29:
        cx, cy = 576, 709
    elif 29 <= t < 32 or 35.5 <= t < 37:
        cx, cy = 446, 750
    else:
        cx, cy = -50, -50
    if cx > 0:
        if int(t*2)%2 == 0:
            d.ellipse((cx-15, cy-15, cx+15, cy+15), outline='#85aa79', width=2)
        d.polygon([(cx, cy), (cx+3, cy+22), (cx+9, cy+15), (cx+17, cy+16)], fill=INK)

    # Restore interlude, followed by the identical saved state.
    if 40 <= t < 42:
        overlay = Image.new('RGB', (W, H), BG)
        od = ImageDraw.Draw(overlay)
        od.text((625, 330), 'melt', font=font(58, 'bold'), fill=INK)
        od.text((533, 430), 'Opening Little Hours Café', font=font(23), fill=INK)
        od.text((551, 478), 'Restoring your saved workspace', font=font(16), fill=MUTED)
        od.rounded_rectangle((560, 526, 880, 531), 2, fill='#d7dcd2')
        od.rounded_rectangle((560, 526, 560+320*ease((t-40)/1.8), 531), 2, fill=GREEN)
        im = overlay
        d = ImageDraw.Draw(im)
    if 42 <= t < 45:
        box(555, 154, 340, 38, '#e1ecda', 9, '#bbd0ae')
        txt(574, 165, '✓  Workspace restored. Ready to use.', 13, GREEN)

    # Footer and progress serve as the film's quiet captions.
    foot = 'Synthetic café data  ·  Product concept animation'
    txt(60, 851, foot, 11, MUTED)
    txt(1003, 849, 'Ask. Adjust. Make it yours.', 16, GREEN, 'serif')
    d.rectangle((0, 895, W*t/DURATION, 899), fill=GREEN)
    return im


def render():
    for sec, name in [(3, '01-workspace'), (22, '02-evolved'), (36, '03-offline')]:
        frame(sec).save(ROOT / f'{name}.png')
    process = subprocess.Popen([
        'ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-vcodec', 'rawvideo',
        '-pix_fmt', 'rgb24', '-s', f'{W}x{H}', '-r', str(FPS), '-i', '-',
        '-an', '-c:v', 'libx264', '-preset', 'fast', '-crf', '19',
        '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(ROOT / 'melt-cafe-demo.mp4'),
    ], stdin=subprocess.PIPE)
    for i in range(FPS*DURATION):
        process.stdin.write(frame(i/FPS).tobytes())
        if i % (FPS*8) == 0:
            print(f'Rendered {i//FPS}s / {DURATION}s', flush=True)
    process.stdin.close()
    if process.wait():
        raise RuntimeError('ffmpeg failed')


if __name__ == '__main__':
    render()
