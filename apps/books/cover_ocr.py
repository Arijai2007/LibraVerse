import re
from functools import cache
from io import BytesIO
from unicodedata import normalize

from PIL import Image, ImageOps


@cache
def _reader():
    from easyocr import Reader

    return Reader(["en"], gpu=False, download_enabled=False, verbose=False)


def read_cover_text(cover):
    """Read cover text without changing the uploaded file that Django will save."""
    try:
        cover.seek(0)
        with Image.open(cover) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((1400, 1400))
            with BytesIO() as buffer:
                image.save(buffer, format="PNG")
                lines = _reader().readtext(buffer.getvalue(), detail=0, paragraph=True)
    finally:
        cover.seek(0)
    return " ".join(lines)


def cover_contains_title(text, title):
    def words(value):
        return " ".join(re.findall(r"[^\W_]+", normalize("NFKC", value).casefold()))

    normalized_title = words(title)
    return bool(normalized_title) and f" {normalized_title} " in f" {words(text)} "
