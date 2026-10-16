# Run with: python -m mai_ui_repl --api 0.1 --serial 127.0.0.1:27666 --cells examples/repl_cells.py
# This file is one cell, so top-level await is intentional.
import io
from PIL import Image, ImageStat

obs = await ui.observe(screenshot=True)
raw = await mu.images.read(obs.image.frame)
picture = Image.open(io.BytesIO(raw)).convert("RGB")
for size in [(180, 400), (90, 200)]:
    thumbnail = picture.resize(size)
    output = io.BytesIO()
    thumbnail.save(output, format="PNG")
    await mu.emit(output.getvalue(), caption=f"Analysis thumbnail {size}")
await mu.emit({"pixels": picture.size, "mean_rgb": ImageStat.Stat(picture).mean})
