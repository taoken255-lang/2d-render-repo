from PIL import Image


img = Image.open("../res/img.png")

img = img.resize((512, 512))
img.save("../res/man_small.png")