"""Show a little cat on Cozmo's face for 4 seconds - the cat-safe 'doorman' reaction."""
import pycozmo, time
from PIL import Image, ImageDraw

im = Image.new("1", (128, 32), 0)
d = ImageDraw.Draw(im)
# cat head: face + two ears
d.ellipse((44, 8, 76, 31), outline=1, width=2)
d.polygon([(46, 14), (50, 2), (57, 10)], outline=1, fill=1)
d.polygon([(74, 14), (70, 2), (63, 10)], outline=1, fill=1)
d.ellipse((52, 16, 56, 20), fill=1)        # eyes
d.ellipse((64, 16, 68, 20), fill=1)
d.line((58, 24, 62, 24), fill=1)           # nose
for y in (22, 26):                          # whiskers
    d.line((36, y, 50, 23), fill=1)
    d.line((70, 23, 84, y), fill=1)
d.text((90, 12), "MEOW", fill=1)

with pycozmo.connect() as cli:
    time.sleep(1)
    cli.display_image(im, duration=4.0)
    time.sleep(5)
print("done")
