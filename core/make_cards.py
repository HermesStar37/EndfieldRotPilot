# -*- coding: utf-8 -*-
"""批量生成干员卡面：cards/<干员>.png

结构：
  白底圆角卡 + 细灰描边
  上方  立绘（70%）
  色条  属性色
  左下  名字（一个）
  右下  职业方框（黑底白图） + 属性方框（属性色底白图）
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app  # noqa: E402
from PIL import Image, ImageDraw, ImageFont, ImageOps  # noqa: E402

CARDS = os.path.join(app.BASE, "cards")
W, H = 300, 390


def font(sz, bold=False):
    fp = "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"
    if not os.path.exists(fp):
        fp = "C:/Windows/Fonts/msyh.ttc"
    return ImageFont.truetype(fp, sz)


def white(img):
    out = Image.new("RGBA", img.size, (255, 255, 255, 255))
    out.putalpha(img.split()[3])
    return out


def make_card(name):
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(card)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=18,
                        fill=(250, 250, 252, 255),
                        outline=(196, 200, 208, 255), width=2)

    # 先定底部行与色条位置，再反推立绘高度（避免中间空隙过大）
    box = 52
    by2 = H - box - 10          # 方框顶部
    bar_y = by2 - 16            # 色条位置
    ph = bar_y - 6 - 8          # 立绘高度：顶部 6，色条上方留 8

    av = Image.open(app.op_file(name, "avatar.png")).convert("RGBA")
    av = ImageOps.fit(av, (W - 12, ph), Image.LANCZOS)
    mask = Image.new("L", av.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, av.width, av.height],
                                           radius=14, fill=255)
    card.paste(av, (6, 6), mask)

    # 属性色
    el = ""
    p = app.op_file(name, "element.txt")
    if p:
        el = io.open(p, encoding="utf-8").read().strip()
    col = app.ELEMENT_COLORS.get(el, (110, 114, 120))

    d.rectangle([10, bar_y, W - 10, bar_y + 6], fill=col + (255,))

    # 底部一行：名字与右侧两个方框同高
    band_mid = by2 + box // 2
    d.text((14, band_mid), name, font=font(32, True),
           fill=(24, 26, 32, 255), anchor="lm")

    # 职业：黑底白图
    cx = W - box - 62
    d.rounded_rectangle([cx, by2, cx + box, by2 + box], radius=6, fill=(28, 30, 36, 255))
    cp = app.op_file(name, "class.txt")
    if cp:
        try:
            cls = io.open(cp, encoding="utf-8").read().strip()
            ip = app.class_icon(cls)
            if ip:
                ci = white(Image.open(ip).convert("RGBA"))
                ci.thumbnail((box - 12, box - 12), Image.LANCZOS)
                card.alpha_composite(ci, (cx + (box - ci.width) // 2,
                                          by2 + (box - ci.height) // 2))
        except Exception as e:
            print("class fail", name, e)

    # 属性：属性色底白图
    ex = W - box - 10
    d.rounded_rectangle([ex, by2, ex + box, by2 + box], radius=6, fill=col + (255,))
    ep = app.elem_icon(el)
    if ep:
        try:
            ei = white(Image.open(ep).convert("RGBA"))
            ei.thumbnail((box - 12, box - 12), Image.LANCZOS)
            card.alpha_composite(ei, (ex + (box - ei.width) // 2,
                                      by2 + (box - ei.height) // 2))
        except Exception as e:
            print("elem fail", name, e)

    return card


def main():
    os.makedirs(CARDS, exist_ok=True)
    names = [n for n in app.OPS if n]
    ok = 0
    for nm in names:
        try:
            make_card(nm).save(os.path.join(CARDS, f"{nm}.png"))
            ok += 1
        except Exception as e:
            print("fail", nm, e)
    print(f"完成 {ok}/{len(names)}")


if __name__ == "__main__":
    main()
