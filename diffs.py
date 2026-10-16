"""diffs.py - compare two versions: text diff and screenshot diff."""
import difflib

from PIL import Image, ImageChops, ImageDraw


def text_diff_html(old_text, new_text):
    """Side-by-side HTML table made with Python's built-in difflib."""
    differ = difflib.HtmlDiff(wrapcolumn=70)
    return differ.make_table(
        old_text.splitlines(),
        new_text.splitlines(),
        "Old version",
        "New version",
        context=True,   # only show changed lines plus a little context
        numlines=2,
    )


def screenshot_diff(old_path, new_path, out_path):
    """Paint the changed pixels red on top of the new screenshot."""
    old = Image.open(old_path).convert("RGB")
    new = Image.open(new_path).convert("RGB")
    if old.size != new.size:
        new = new.resize(old.size)

    # difference() gives black where pixels are equal, bright where they differ
    difference = ImageChops.difference(old, new).convert("L")
    # turn it into a black/white mask (white = changed)
    mask = difference.point(lambda value: 255 if value > 30 else 0)

    red = Image.new("RGB", new.size, (255, 0, 0))
    tinted = Image.blend(new, red, 0.5)                 # half-red copy of new
    result = Image.composite(tinted, new, mask)         # use red copy where changed

    box = mask.getbbox()                                # box around all changes
    if box:
        ImageDraw.Draw(result).rectangle(box, outline=(255, 0, 0), width=3)

    result.save(out_path)
    return box is not None
