"""픽셀 땃쥐 마스코트로 Windows 아이콘(.ico)을 만든다. (Pillow 필요)

스프라이트는 Claude Design 캔버스 "Shrew AI 판매자 페이지"의 마스코트와 같은 20x11 격자다.
사용: python scripts/make_icon.py [출력경로]   (기본 brand/shrew-office.ico)
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

WALL, FLOOR, SEAM = (47, 111, 154), (154, 90, 31), (91, 58, 46)
F, B, K, W = (91, 58, 46), (154, 90, 31), (30, 42, 51), (244, 247, 250)

# (x, y, w, color) — 한 줄짜리 가로 런
SPRITE = [
    (11, 0, 2, F), (8, 1, 6, F), (6, 2, 10, F),
    (5, 3, 2, F), (7, 3, 1, W), (8, 3, 1, K), (9, 3, 8, F),
    (3, 4, 4, F), (7, 4, 2, K), (9, 4, 9, F),
    (1, 5, 17, F), (0, 6, 1, K), (1, 6, 18, F),
    (3, 7, 4, F), (7, 7, 6, B), (13, 7, 6, F),
    (5, 8, 2, F), (7, 8, 7, B), (14, 8, 6, F),
    (6, 9, 1, F), (7, 9, 6, B), (13, 9, 3, F),
    (7, 10, 2, K), (12, 10, 2, K),
]


def render(size_cells: int = 24) -> Image.Image:
    """24x24 셀 캔버스: 벽 + 바닥 + 가운데 땃쥐."""
    img = Image.new("RGB", (size_cells, size_cells), WALL)
    d = ImageDraw.Draw(img)
    floor_y = 17
    d.rectangle([0, floor_y, size_cells - 1, size_cells - 1], fill=FLOOR)
    d.line([0, floor_y, size_cells - 1, floor_y], fill=SEAM)
    d.line([0, floor_y + 3, size_cells - 1, floor_y + 3], fill=SEAM)
    ox, oy = 2, floor_y - 10
    for x, y, w, c in SPRITE:
        d.line([ox + x, oy + y, ox + x + w - 1, oy + y], fill=c)
    return img


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "brand" / "shrew-office.ico"
    out.parent.mkdir(parents=True, exist_ok=True)
    big = render().resize((256, 256), Image.NEAREST)
    big.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(out.with_suffix(".png"))
    print(f"아이콘 저장: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
