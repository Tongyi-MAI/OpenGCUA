"""One immutable, lossless image archive shared by the worker and trajectory."""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import tempfile


def save_image(data, directory: Path) -> Path:
    from PIL import Image

    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if isinstance(data, (bytes, bytearray)):
        with Image.open(io.BytesIO(data)) as decoded:
            decoded.load()
            image = decoded.copy()
    else:
        data.load()
        image = data.copy()
    output = io.BytesIO()
    # Retain PNG for images outside WebP's bit-depth or dimension limits.
    if image.mode == "I" or image.mode.startswith("I;16") or max(image.size) > 16383:
        image.save(output, format="PNG")
        suffix = ".png"
    else:
        alpha = "A" in image.getbands() or "transparency" in image.info
        image = image.convert("RGBA" if alpha else "RGB")
        image.save(output, format="WEBP", lossless=True, exact=True, method=4,
                   icc_profile=image.info.get("icc_profile", b""))
        suffix = ".webp"
    payload = output.getvalue()
    path = directory / ("image_" + hashlib.sha256(payload).hexdigest() + suffix)
    if path.exists():
        return path
    # Publish only complete files, including when concurrent emits share the same pixels.
    fd, temporary = tempfile.mkstemp(prefix=".image-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)
    return path
