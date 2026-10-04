"""Image helpers: normalising uploads to JPEG, previews for Claude, and fitting feed photos into
Instagram's allowed aspect ratios."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

FEED_MIN_RATIO = 4 / 5  # tallest feed photo Instagram accepts (portrait 4:5)
FEED_MAX_RATIO = 1.91  # widest feed photo Instagram accepts (landscape 1.91:1)
MAX_SIDE = 2048  # keeps uploads far below Instagram's 8 MB image limit
PREVIEW_SIDE = 1024  # what Claude looks at
JPEG_QUALITY = 90


class MediaError(Exception):
    """A file can't be used as Instagram media; the message is shown to the admin."""


def load_image(path: Path) -> Image.Image:
    """Open an image upright (EXIF rotation applied) in RGB, with transparency flattened onto white."""
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                return flat
            return image.convert("RGB")
    except (OSError, Image.DecompressionBombError) as exc:
        raise MediaError("Rasmni o'qib bo'lmadi — uni oddiy foto (JPEG/PNG) sifatida yuboring.") from exc


def downscale(image: Image.Image, max_side: int) -> Image.Image:
    if max(image.size) <= max_side:
        return image
    smaller = image.copy()
    smaller.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return smaller


def save_jpeg(image: Image.Image, path: Path, quality: int = JPEG_QUALITY) -> None:
    image.save(path, "JPEG", quality=quality, optimize=True)


def normalize_image(source: Path, target: Path) -> tuple[int, int]:
    """Store an uploaded image as an upright RGB JPEG of reasonable size; returns its (width, height)."""
    image = downscale(load_image(source), MAX_SIDE)
    save_jpeg(image, target)
    return image.size


def make_preview(source: Path, target: Path) -> None:
    save_jpeg(downscale(load_image(source), PREVIEW_SIDE), target, quality=85)


def clamp_feed_ratio(ratio: float) -> float:
    return min(max(ratio, FEED_MIN_RATIO), FEED_MAX_RATIO)


def fit_to_ratio(image: Image.Image, ratio: float) -> Image.Image:
    """Pad an image to width/height == ratio without cropping it: the original sits in the middle of
    a blurred, enlarged copy of itself. Returns the image unchanged if it is already close enough."""
    width, height = image.size
    if abs(width / height - ratio) / ratio < 0.01:
        return image
    if width / height < ratio:  # too tall: widen the canvas
        canvas_size = (round(height * ratio), height)
    else:  # too wide: make the canvas taller
        canvas_size = (width, round(width / ratio))
    scale = max(canvas_size[0] / width, canvas_size[1] / height)
    background = image.resize((round(width * scale), round(height * scale)), Image.Resampling.LANCZOS)
    left = (background.width - canvas_size[0]) // 2
    top = (background.height - canvas_size[1]) // 2
    background = background.crop((left, top, left + canvas_size[0], top + canvas_size[1]))
    background = background.filter(ImageFilter.GaussianBlur(radius=max(canvas_size) / 30))
    background.paste(image, ((canvas_size[0] - width) // 2, (canvas_size[1] - height) // 2))
    return background


def prepare_feed_images(paths: list[Path]) -> list[Image.Image]:
    """Fit feed photos into Instagram's ratio limits. All photos of a carousel get the (clamped) ratio
    of the first one, because Instagram shows every carousel item in the first item's frame."""
    images = [load_image(path) for path in paths]
    target = clamp_feed_ratio(images[0].width / images[0].height)
    return [fit_to_ratio(image, target) for image in images]
