"""名簿の格子の画像（ADR 0014 判断2）。アイコンを大きく丸く、その下に番号と名前を書いた1枚の PNG を作る。

- 横 COLUMNS 人。印のある人は、アイコンのまわりに色の輪を付け、番号の前に記号を付ける
- 日本語の字体は、機械にあるものを順に探す（コンテナは fonts-noto-cjk、Windows は游ゴシックかメイリオ）
- アイコンが取れなかった人は、灰色の丸に名前の1文字目
- 画像は返事に付けるだけで、ファイルには残さない"""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

COLUMNS = 5
CELL_W, CELL_H = 176, 196
AVATAR = 128
RING = 6
PAD = 12
BACKGROUND = (43, 45, 49)
TEXT = (242, 243, 245)
SUBTLE = (181, 186, 193)
BADGE = (30, 31, 34)
PLACEHOLDER = (88, 101, 242)
MARK_COLORS = {"star": (240, 178, 50), "picked": (88, 166, 255), "recent": (87, 202, 120)}
MARK_SIGNS = {"star": "★", "picked": "", "recent": ""}
FONT_PATHS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/YuGothM.ttc",
    "C:/Windows/Fonts/meiryo.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
)


@lru_cache(maxsize=8)
def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_PATHS:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)   # 日本語は出ない（字体が無い機械）


def has_japanese_font() -> bool:
    return any(Path(path).exists() for path in FONT_PATHS)


def fit(text: str, face, width: int) -> str:
    """幅に収まらなければ、後ろを「…」にする。"""
    if face.getlength(text) <= width:
        return text
    while text and face.getlength(text + "…") > width:
        text = text[:-1]
    return text + "…"


def _circle(image: Image.Image) -> Image.Image:
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, image.size[0] - 1, image.size[1] - 1), fill=255)
    out = Image.new("RGBA", image.size, (0, 0, 0, 0))
    out.paste(image, (0, 0), mask)
    return out


def _avatar(data: bytes | None, name: str) -> Image.Image:
    if data:
        try:
            picture = Image.open(io.BytesIO(data)).convert("RGBA").resize((AVATAR, AVATAR), Image.LANCZOS)
            return _circle(picture)
        except Exception:  # noqa: BLE001（壊れた画像は、名前の1文字目で代える）
            pass
    blank = Image.new("RGBA", (AVATAR, AVATAR), PLACEHOLDER)
    draw = ImageDraw.Draw(blank)
    face = font(56)
    letter = (name or "?")[0]
    box = draw.textbbox((0, 0), letter, font=face)
    draw.text(((AVATAR - (box[2] - box[0])) / 2 - box[0], (AVATAR - (box[3] - box[1])) / 2 - box[1]), letter, font=face, fill=TEXT)
    return _circle(blank)


def render(cells: list[dict]) -> bytes:
    """cells は {"number", "name", "mark"（"star"・"picked"・"recent"・""）, "avatar"（PNG などの中身か None）} の並び。"""
    rows = max(1, (len(cells) + COLUMNS - 1) // COLUMNS)
    columns = min(COLUMNS, max(1, len(cells)))
    image = Image.new("RGB", (columns * CELL_W + PAD * 2, rows * CELL_H + PAD * 2), BACKGROUND)
    draw = ImageDraw.Draw(image)
    name_face, badge_face = font(19), font(17)
    for index, cell in enumerate(cells):
        left = PAD + (index % COLUMNS) * CELL_W
        top = PAD + (index // COLUMNS) * CELL_H
        center = left + CELL_W // 2
        x, y = center - AVATAR // 2, top + 8
        color = MARK_COLORS.get(cell.get("mark") or "")
        if color:
            draw.ellipse((x - RING, y - RING, x + AVATAR + RING, y + AVATAR + RING), fill=color)
        picture = _avatar(cell.get("avatar"), cell.get("name") or "")
        image.paste(picture, (x, y), picture)
        badge = f"{MARK_SIGNS.get(cell.get('mark') or '', '')}{cell['number']}"
        width = max(30, int(badge_face.getlength(badge)) + 14)
        draw.rounded_rectangle((x - 4, y - 4, x - 4 + width, y + 24), radius=12, fill=BADGE, outline=color or SUBTLE, width=2)
        draw.text((x - 4 + width / 2, y + 10), badge, font=badge_face, fill=TEXT, anchor="mm")
        name = fit(cell.get("name") or "メンバー", name_face, CELL_W - 12)
        draw.text((center, y + AVATAR + RING + 18), name, font=name_face, fill=TEXT, anchor="mm")
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
