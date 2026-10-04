from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from igbot import media


def ratio(image: Image.Image) -> float:
    return image.width / image.height


def test_tall_photo_is_padded_to_4_5() -> None:
    fitted = media.fit_to_ratio(Image.new("RGB", (1080, 1920), "red"), media.clamp_feed_ratio(1080 / 1920))
    assert fitted.size == (1536, 1920)
    assert ratio(fitted) == pytest.approx(0.8)
    assert fitted.getpixel((768, 960)) == (255, 0, 0)  # the original is untouched in the middle


def test_wide_photo_is_padded_to_1_91() -> None:
    fitted = media.fit_to_ratio(Image.new("RGB", (2000, 800), "blue"), media.clamp_feed_ratio(2.5))
    assert ratio(fitted) == pytest.approx(1.91, abs=0.01)
    assert fitted.width == 2000


def test_photo_within_limits_is_unchanged() -> None:
    image = Image.new("RGB", (1080, 1350))
    assert media.fit_to_ratio(image, media.clamp_feed_ratio(ratio(image))) is image


def test_carousel_photos_share_the_first_photos_ratio(tmp_path: Path) -> None:
    paths = []
    for index, size in enumerate([(1080, 1080), (1080, 1920), (1920, 1080)]):
        path = tmp_path / f"{index}.jpg"
        Image.new("RGB", size).save(path)
        paths.append(path)
    fitted = media.prepare_feed_images(paths)
    assert [ratio(image) for image in fitted] == pytest.approx([1.0, 1.0, 1.0], abs=0.01)


def test_normalize_flattens_transparency_and_downscales(tmp_path: Path) -> None:
    source, target = tmp_path / "in.png", tmp_path / "out.jpg"
    Image.new("RGBA", (3000, 1000), (0, 0, 0, 0)).save(source)
    assert media.normalize_image(source, target) == (2048, 683)
    with Image.open(target) as stored:
        assert stored.format == "JPEG" and stored.mode == "RGB"
        assert stored.getpixel((10, 10)) == (255, 255, 255)


def test_unreadable_file_is_reported(tmp_path: Path) -> None:
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not an image")
    with pytest.raises(media.MediaError):
        media.load_image(broken)
