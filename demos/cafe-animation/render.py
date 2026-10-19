"""Render the Melt café concept film with Pillow, NumPy, and ffmpeg."""
from pathlib import Path
import subprocess
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT = Path(__file__).parent
W, H, FPS, DURATION = 1440, 900, 30, 24
INK, MUTED, GREEN, LINE = '#262b30', '#81858b', '#247460', '#e5e7e9'
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
fonts = {}


def font(size, bold=False):
    key = size, bold
    if key not in fonts:
        fonts[key] = ImageFont.truetype(BOLD if bold else FONT, size)
    return fonts[key]


def ease(t):
    t = max(0, min(1, t))
    return t*t*(3-2*t)


class Canvas:
    def __init__(self, image):
        self.image = image
        self.d = ImageDraw.Draw(image)

    def box(self, x, y, w, h, fill='white', radius=9, outline=None):
        self.d.rounded_rectangle((x, y, x+w, y+h), radius, fill, outline)

    def text(self, x, y, s, size=13, color=INK, bold=False):
        self.d.text((x, y), s, fill=color, font=font(size, bold))

    def line(self, points, color=LINE, width=1):
        self.d.line(points, fill=color, width=width)

    def dot(self, x, y, r, color):
        self.d.ellipse((x-r, y-r, x+r, y+r), fill=color)


def desktop():
    # Original Aqua-inspired wallpaper: soft blue ribbons and luminous folds.
    y, x = np.mgrid[0:H, 0:W]
    x, y = x/W, y/H
    fold = y - (.65 - .3*np.sin(x*3.8-1))
    ribbon = np.exp(-(fold/.14)**2)
    edge = np.exp(-((fold+.12)/.025)**2)
    upper = np.exp(-((y-.18-.14*np.sin(x*5))/.19)**2)
    field = np.stack([
        12 + 24*y + 35*ribbon + 22*edge + 18*upper,
        55 + 35*y + 88*ribbon + 45*edge + 25*upper,
        111 + 39*y + 68*ribbon + 24*edge + 34*upper,
    ], axis=-1)
    image = Image.fromarray(np.uint8(np.clip(field, 0, 255))).convert('RGBA')
    c = Canvas(image)
    # Menu bar.
    c.box(0, 0, W, 27, '#cee0ef', 0)
    c.dot(23, 13, 6, '#182c3d')
    c.d.ellipse((24, 3, 28, 8), fill='#182c3d')
    c.dot(29, 10, 3, '#cee0ef')
    for xx, label, bold in [(43, 'Melt', True), (95, 'File', False), (139, 'Edit', False), (185, 'View', False), (241, 'Workspace', False), (337, 'Window', False), (414, 'Help', False)]:
        c.text(xx, 5, label, 12, '#1b3042', bold)
    c.text(1230, 5, 'Mon 19 Oct   10:42', 12, '#1b3042')
    c.box(1188, 8, 20, 10, None, 2, '#1b3042')
    c.box(1190, 10, 14, 6, '#1b3042', 1)
    c.line((1210, 11, 1210, 15), '#1b3042', 2)
    for r in [5, 9, 13]:
        c.d.arc((1157-r, 21-r, 1157+r, 21+r), 225, 315, fill='#1b3042', width=2)
    # Dock with restrained app icons.
    dock = Image.new('RGBA', (W, H))
    dc = Canvas(dock)
    dc.box(498, 828, 444, 65, (221, 235, 247, 150), 17, (255, 255, 255, 110))
    for xx, color, label in [(512, '#58adf1', ''), (573, '#f5f6f8', ''), (634, '#eee9e3', '19'), (695, '#f9c54c', '≡'), (756, '#237a65', 'm'), (817, '#29313a', '>_'), (878, '#d8e0e8', '▥')]:
        dc.box(xx, 837, 48, 47, color, 10)
        dc.text(xx+11, 843, label, 25, 'white' if xx in [512, 756, 817] else '#46545d', True)
    dc.box(536, 839, 22, 43, '#c1e7ff', 6)
    dc.line((526, 848, 526, 855), '#234d6c', 2)
    dc.line((547, 848, 547, 855), '#234d6c', 2)
    dc.d.arc((521, 850, 550, 874), 15, 165, fill='#234d6c', width=2)
    dc.dot(597, 861, 19, '#37a7e5')
    dc.d.polygon([(605, 847), (600, 864), (589, 875), (594, 858)], fill='white')
    dc.d.polygon([(605, 847), (600, 864), (594, 858)], fill='#e46d6d')
    dc.dot(780, 889, 2, '#24374a')
    image = Image.alpha_composite(image, dock)
    shadow = Image.new('RGBA', (W, H))
    ImageDraw.Draw(shadow).rounded_rectangle((61, 82, 1379, 803), 14, fill=(0, 13, 35, 130))
    image = Image.alpha_composite(image, shadow.filter(ImageFilter.GaussianBlur(20)))
    return image


DESKTOP = desktop()


def frame(t):
    im = DESKTOP.copy()
    c = Canvas(im)
    box, txt, line = c.box, c.text, c.line
    built = t >= 3.1
    cost_ready = t >= 8.7
    evolved = t >= 14.8
    offline = t >= 20.3
    cost = 30*ease((t-10)/.65)
    closure = ease((t-17.6)/.45)
    closed = t >= 17.6
    profit = round(3280-864*cost/30+420*closure)
    revenue = round(18600-1200*closure)
    box(62, 70, 1316, 731, '#fafafa', 12, '#c9d0d8')
    box(63, 71, 1314, 48, '#f0f0f2', 11)
    c.d.rectangle((63, 100, 1377, 119), fill='#f0f0f2')
    for x, color in [(84, '#ff6058'), (104, '#ffbd2e'), (124, '#28c840')]:
        c.dot(x, 95, 6, color)
    txt(165, 84, '▥', 20, MUTED)
    txt(222, 87, 'Little Hours Café' if built else 'Untitled workspace', 13, INK, True)
    txt(1085, 88, '✓ Saved' if built else 'Local workspace', 11, MUTED)
    txt(1253, 86, '⊞    ···', 18, MUTED)
    line((63, 119, 1377, 119), '#d8dade')
    # Slim native sidebar.
    c.d.rectangle((63, 120, 223, 777), fill='#eff0f3')
    line((223, 120, 223, 777), '#dfe1e6')
    txt(80, 143, 'WORKSPACE', 9, MUTED, True)
    box(72, 166, 141, 32, '#dee2e9', 5)
    txt(84, 175, '▦  Overview', 12, INK, True)
    txt(84, 213, '◷  History', 12, '#5d6570')
    txt(80, 272, 'SOURCES', 9, MUTED, True)
    if t >= 2.8:
        for yy, name in [(302, 'sales.csv'), (333, 'costs.csv'), (364, 'staffing.csv')]:
            txt(84, yy, '▤  '+name, 11, '#5d6570')
    else:
        txt(84, 303, 'No sources yet', 11, '#a0a5ad')
    txt(82, 742, '●  On this Mac', 10, '#718172')
    # Agent panel.
    c.d.rectangle((1003, 120, 1377, 777), fill='#f7f7f9')
    line((1003, 120, 1003, 777), '#dfe1e6')
    txt(1025, 140, '✦  Agent', 14, INK, True)
    txt(1308, 143, '···', 16, MUTED)
    line((1003, 176, 1377, 176))
    # Initial empty canvas: no pre-built app.
    if not built:
        txt(551, 372, '✦', 35, '#c2c8cd')
        txt(470, 430, 'What are we building?', 20, '#656d76')
        txt(451, 464, 'Start with a question and your data.', 12, '#9298a0')
    else:
        txt(246, 144, 'Little Hours Café', 22, INK, True)
        txt(246, 179, 'Overview', 11, MUTED)
        txt(850, 151, 'This month  ⌄', 11, MUTED)

    def reveal(start, draw):
        if t < start:
            return
        layer = Image.new('RGBA', (W, H))
        draw(Canvas(layer))
        p = ease((t-start)/.45)
        layer.putalpha(layer.getchannel('A').point(lambda a: round(a*p)))
        im.alpha_composite(layer, (0, round(12*(1-p))))

    def metrics(g):
        for xx, label, value, color in [(246, 'Revenue', f'€{revenue:,}', INK), (493, 'Net profit', f'€{profit:,}', GREEN if cost < 1 or closed else '#b37c2c'), (740, 'Margin', f'{profit/revenue*100:.1f}%', INK)]:
            g.box(xx, 212, 232, 102, 'white', 8, LINE)
            g.text(xx+15, 226, label, 11, MUTED)
            g.text(xx+15, 250, value, 29, color, True)
            g.text(xx+15, 290, 'Monthly estimate', 9, MUTED)
    reveal(3.1, metrics)

    def chart(g):
        squeeze = ease((t-14.8)/.45)
        cw = 726-256*squeeze
        g.box(246, 329, cw, 242, 'white', 8, LINE)
        g.text(262, 345, 'Daily profit', 13, INK, True)
        g.text(262, 369, 'A typical week', 10, MUTED)
        px, pw = 291, cw-72
        for yy, label in [(410, '€200'), (463, '€100'), (516, '€0')]:
            g.text(257, yy-5, label, 9, MUTED)
            g.line((px, yy, px+pw, yy))
        # A 30-day month has four Mondays and 26 other days.
        # Closing Mondays removes their €105 loss in the high-cost case.
        values = [-76.2-cost*.96, 82-cost*.96, 105-cost*.96, 128-cost*.96, 170-cost*.96, 197-cost*.96, 141.076923-cost*.96]
        values[0] *= 1-closure
        for i, val in enumerate(values):
            x = px+i*pw/7+9
            val *= ease((t-3.55-i*.055)/.55)
            top = 516-max(0, val)*.49
            bottom = 516-min(0, val)*.22
            g.box(x, top, pw/7-18, max(2, bottom-top), '#bf9384' if val < 0 else '#6da490', 3)
            g.text(x, 549, ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][i], 9, MUTED)
    reveal(3.45, chart)

    def break_even(g):
        g.box(730, 329, 242, 242, '#edf4ef', 8, '#dce7dd')
        g.text(746, 345, 'Break-even', 13, INK, True)
        g.text(746, 390, '€560' if closed else '€539', 35, GREEN, True)
        g.text(746, 438, 'per open day', 12, MUTED)
        g.box(746, 477, 208, 6, '#d4e2d7', 3)
        g.box(746, 477, 174, 6, GREEN, 3)
        g.text(746, 500, '€669 daily revenue' if closed else '€620 daily revenue', 11, GREEN)
        g.text(746, 544, 'Based on your current costs', 9, MUTED)
    reveal(15, break_even)

    def lower(g):
        g.box(246, 586, 350, 171, 'white', 8, LINE)
        g.text(262, 601, 'Scenario', 13, INK, True)
        if cost_ready:
            g.text(262, 636, 'Bean costs', 11)
            g.text(521, 633, f'+{round(cost)}%', 15, GREEN, True)
            g.line((263, 671, 564, 671), '#e0e5e3', 4)
            g.line((263, 671, 263+cost/50*301, 671), GREEN, 4)
            g.dot(263+cost/50*301, 671, 7, GREEN)
        else:
            g.text(262, 638, 'Original costs', 12, '#5e6771')
            g.text(262, 664, 'Ask the agent to add a scenario.', 11, MUTED)
        if evolved:
            g.text(262, 704, 'Open Mondays', 11)
            g.box(395, 701, 34, 19, '#cbd1d5' if closed else GREEN, 10)
            g.dot(420-16*closure, 710, 7, 'white')
        g.box(473, 698, 106, 30, GREEN, 6)
        g.text(490, 706, 'Run  ↵', 12, 'white', True)
        g.box(611, 586, 361, 171, 'white', 8, LINE)
        g.text(627, 601, 'Saved scenarios', 13, INK, True)
        for yy, label, value, show in [(637, 'Original', '€3,280', True), (674, 'Beans +30%', '€2,416', cost >= 29.9), (711, 'Mondays off', '€2,836', closed)]:
            if show:
                if (closed and yy == 711) or (not closed and cost >= 29.9 and yy == 674):
                    g.box(621, yy-3, 340, 29, '#eaf2ed', 4)
                g.text(630, yy+3, label, 11)
                g.text(885, yy+3, value, 11, GREEN, True)
    reveal(3.85, lower)

    # Conversation drives every new part of the interface.
    def message(y, role, lines, user=False):
        if user:
            box(1020, y, 339, len(lines)*21+34, '#e7eaf0', 9)
        txt(1033, y+10, role, 9, '#747d88' if user else GREEN, True)
        for i, s in enumerate(lines):
            txt(1033, y+30+i*21, s, 12, INK)

    if t >= 2.3:
        message(192, 'YOU', ['Can this café survive?', 'Build me a tool from these files.'], True)
        for xx, label in [(1033, 'sales.csv'), (1143, 'costs.csv'), (1245, 'staffing.csv')]:
            box(xx, 280, 99, 24, '#f0f1f4', 5, '#dfe2e7')
            txt(xx+8, 286, label, 10, '#68727c')
    if 2.6 <= t < 4.5:
        txt(1033, 326, '✦  Building your workspace' + '.'*(int(t*3)%4), 12, GREEN)
    elif t >= 4.5:
        message(316, 'MELT', ['€3,280 profit per month.', 'Mondays are losing money.'])
    if t >= 8.1:
        message(410, 'YOU', ['What if beans cost 30% more?'], True)
    if t >= 9:
        message(482, 'MELT', ['Added a cost control. Try it.'])
    if t >= 14.2:
        message(550, 'YOU', ['Add break-even. Let me close', 'on Mondays.'], True)
    if t >= 15.6:
        txt(1033, 644, '✓  Added both. Your values are kept.', 11, GREEN)

    # Live typing happens in the input, then becomes a sent message.
    box(1019, 690, 341, 69, 'white', 9, '#d9dde3')
    prompt, begin, finish = '', 0, 0
    for a, b, text in [(.25, 2.3, 'Can this café survive?\nBuild me a tool from these files.'), (6.35, 8.1, 'What if beans cost 30% more?'), (12.2, 14.2, 'Add break-even. Let me close\non Mondays.')]:
        if a <= t < b:
            prompt, begin, finish = text, a, b
    if prompt:
        shown = prompt[:int(len(prompt)*min(1, (t-begin)/(finish-begin-.22)))]
        for i, row in enumerate(shown.split('\n')):
            txt(1032, 703+i*19, row + ('│' if i == len(shown.split('\n'))-1 else ''), 12)
    else:
        txt(1032, 705, 'Ask for a change…' if not offline else 'Agent offline · tools still available', 11, MUTED)
    box(1320, 724, 25, 25, '#2b7661' if prompt else '#e4e7eb', 6)
    txt(1327, 726, '↑', 16, 'white' if prompt else MUTED)
    # Status stays inside the app.
    line((63, 777, 1377, 777), '#e0e2e6')
    txt(80, 784, 'Local workspace', 9, MUTED)
    txt(247, 784, '✓ Local run complete' if t >= 21.6 else ('✓ All changes saved' if built else ''), 9, GREEN)
    txt(1194, 784, '● Agent offline' if offline else '● Agent connected', 9, '#9a855d' if offline else GREEN)
    # Cursor moves between the conversation and the actual tools.
    points = [(0, 1155, 724), (2, 1331, 736), (2.5, 1331, 736), (5, 1144, 716), (7.8, 1331, 736), (8.3, 1331, 736), (9.6, 263, 671), (10, 263, 671), (10.65, 444, 671), (11.1, 521, 712), (11.6, 521, 712), (12, 1166, 720), (14, 1331, 736), (14.4, 1331, 736), (16.6, 414, 712), (17.6, 414, 712), (18.1, 521, 712), (18.7, 521, 712), (19.5, 1270, 787), (20.4, 1270, 787), (21.1, 521, 712), (21.7, 521, 712), (22.3, 986, 750), (24, 986, 750)]
    for a, b in zip(points, points[1:]):
        if a[0] <= t <= b[0]:
            p = ease((t-a[0])/(b[0]-a[0]))
            mx, my = a[1]+(b[1]-a[1])*p, a[2]+(b[2]-a[2])*p
            break
    else:
        mx, my = 986, 750
    for click in [2.23, 8.03, 11.25, 14.13, 17.6, 18.3, 20.25, 21.4]:
        if 0 <= t-click < .28:
            r = 5+17*(t-click)/.28
            c.d.ellipse((mx-r, my-r, mx+r, my+r), outline='#7aa8c7', width=2)
    c.d.polygon([(mx, my), (mx+1, my+22), (mx+6, my+17), (mx+11, my+26), (mx+15, my+24), (mx+10, my+15), (mx+18, my+14)], fill='#24282d', outline='white', width=1)
    return im.convert('RGB')


def render():
    DESKTOP.convert('RGB').save(ROOT/'desktop-wallpaper.png')
    for sec, name in [(.1, '00-empty'), (5, '01-workspace'), (18.9, '02-evolved'), (22.8, '03-offline')]:
        frame(sec).save(ROOT/f'{name}.png')
    process = subprocess.Popen([
        'ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-vcodec', 'rawvideo',
        '-pix_fmt', 'rgb24', '-s', f'{W}x{H}', '-r', str(FPS), '-i', '-',
        '-an', '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
        '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(ROOT/'melt-cafe-demo.mp4'),
    ], stdin=subprocess.PIPE)
    for i in range(FPS*DURATION):
        process.stdin.write(frame(i/FPS).tobytes())
        if i % (FPS*6) == 0:
            print(f'Rendered {i//FPS}s / {DURATION}s', flush=True)
    process.stdin.close()
    if process.wait():
        raise RuntimeError('ffmpeg failed')


if __name__ == '__main__':
    render()
