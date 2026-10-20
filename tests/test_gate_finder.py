"""'select next gate' detection on synthetic Overviews built from REAL EVE icons
(tests/fixtures/eve_gate_icons.png: top = yellow next-on-route gate, middle = another
icon on a highlighted row, bottom = normal grey gate)."""
import colorsys, os, random, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    Image = None
import gate_finder as gf

ROW = 20
W = 320


def icons():
    src = Image.open(os.path.join(HERE, "fixtures", "eve_gate_icons.png")).convert("RGB")
    return {"yellow": src.crop((6, 0, 22, 12)), "highlight": src.crop((4, 16, 30, 32)),
            "grey": src.crop((6, 33, 22, 46))}


def hue_shift(img, deg):
    out = img.copy()
    px = out.load()
    for y in range(out.size[1]):
        for x in range(out.size[0]):
            h, s, v = colorsys.rgb_to_hsv(*[c / 255 for c in px[x, y]])
            r, g, b = colorsys.hsv_to_rgb((h + deg / 360) % 1, s, v)
            px[x, y] = (int(r * 255), int(g * 255), int(b * 255))
    return out


def overview(rows, scale=1.0, noise=0, specks=0, seed=1, yellow_text_row=None):
    """rows: list of icon names (or None) top to bottom; each row gets grey text."""
    ic = icons()
    rh = int(ROW * scale)
    img = Image.new("RGB", (W, rh * len(rows)), (9, 8, 9))
    d = ImageDraw.Draw(img)
    for i, name in enumerate(rows):
        y = i * rh
        if i % 2:
            d.rectangle((0, y, W, y + rh - 1), fill=(14, 14, 16))
        if name == "highlight":
            d.rectangle((0, y, W, y + rh - 1), fill=(24, 39, 45))
        if name:
            icon = ic["yellow" if name == "yellow_on_highlight" else name] if name != "orange" else \
                hue_shift(ic["yellow"], -25)
            if name == "yellow_on_highlight":
                d.rectangle((0, y, W, y + rh - 1), fill=(24, 39, 45))
            if scale != 1.0:
                icon = icon.resize((int(icon.size[0] * scale), int(icon.size[1] * scale)), Image.BILINEAR)
            img.paste(icon, (6, y + (rh - icon.size[1]) // 2))
        d.text((int(30 * scale), y + 4), f"Stargate ({'Jita' if i % 2 else 'Perimeter'}) {i}", fill=(170, 170, 170))
        d.text((int(220 * scale) % W, y + 4), f"{12 + i}.3 AU", fill=(200, 200, 200))
        d.rectangle((W - 20, y + 6, W - 12, y + 12), fill=(200, 40, 40))   # red hostile-ish tag
        if yellow_text_row == i:
            d.text((int(30 * scale), y + 4), "Stargate (Amarr)", fill=(224, 238, 64))
    rnd = random.Random(seed)
    if noise:
        px = img.load()
        for y in range(img.size[1]):
            for x in range(W):
                r, g, b = px[x, y]
                n = rnd.randint(-noise, noise)
                px[x, y] = tuple(max(0, min(255, c + n + rnd.randint(-2, 2))) for c in (r, g, b))
    if specks:
        px = img.load()
        for _ in range(specks):
            px[rnd.randrange(W), rnd.randrange(img.size[1])] = (230, 240, 60)
    return img


def target(**kw):
    t = dict(gf.DEFAULT_TARGET)
    t.update(box=[1000, 200, 1000 + W, 600], sampled=True)
    t.update(kw)
    return t


@unittest.skipIf(Image is None, "needs Pillow")
class TestGateFinder(unittest.TestCase):
    def assertRow(self, res, row, rh=ROW):
        self.assertEqual(res.status, "found", res.message)
        x, y = res.point
        self.assertTrue(row * rh <= y < (row + 1) * rh, (res.point, row))
        self.assertTrue(4 <= x <= 6 + 16 * rh / ROW + 2, res.point)    # on the yellow icon

    def test_real_icon_colours(self):
        src = Image.open(os.path.join(HERE, "fixtures", "eve_gate_icons.png")).convert("RGB")
        picked = gf.pick_from_image(src.crop((0, 0, 30, 14)), (14, 6))
        self.assertTrue(60 <= gf.hue_of(picked["rgb"]) <= 66, picked)
        self.assertEqual(picked["icon_size"], [10, 10])
        self.assertTrue(picked["sampled"])
        self.assertTrue(60 <= gf.hue_of(gf.DEFAULT_TARGET["rgb"]) <= 66)
        # the normal grey gate and the highlighted-row icon never match
        for box in ((0, 16, 30, 32), (0, 33, 30, 47)):
            self.assertEqual(gf.find_target(src.crop(box), target()).status, "none")

    def test_yellow_row_at_various_positions(self):
        for row in (0, 1, 4, 9):
            rows = ["grey"] * 10
            rows[row] = "yellow"
            self.assertRow(gf.find_target(overview(rows), target()), row)

    def test_none(self):
        res = gf.find_target(overview(["grey", "highlight", "grey", None, "grey"]), target())
        self.assertEqual((res.status, res.point), ("none", None))
        self.assertIn("no yellow gate found", res.message)

    def test_two_far_apart_is_ambiguous(self):
        res = gf.find_target(overview(["yellow", "grey", "grey", "grey", "yellow", "grey"]), target())
        self.assertEqual((res.status, res.point), ("ambiguous", None))
        self.assertIn("not sure", res.message)

    def test_two_adjacent_takes_top(self):
        self.assertRow(gf.find_target(overview(["grey", "yellow", "yellow", "grey"]), target()), 1)

    def test_noise_and_specks(self):
        rows = ["grey"] * 8
        rows[5] = "yellow"
        self.assertRow(gf.find_target(overview(rows, noise=8, specks=40, seed=3), target()), 5)
        res = gf.find_target(overview(["grey"] * 8, noise=8, specks=40, seed=3), target())
        self.assertEqual(res.status, "none")

    def test_yellow_on_highlighted_row(self):
        self.assertRow(gf.find_target(overview(["grey", "grey", "yellow_on_highlight"]), target()), 2)

    def test_yellow_text_too_still_clicks_icon(self):
        rows = ["grey", "yellow", "grey"]
        self.assertRow(gf.find_target(overview(rows, yellow_text_row=1), target()), 1)

    def test_yellow_text_without_icon_is_not_a_gate(self):
        res = gf.find_target(overview(["grey", "grey", "grey"], yellow_text_row=1), target())
        self.assertEqual(res.status, "none")
        self.assertIn("not icon-shaped", res.message)
        res = gf.find_target(overview(["grey", "grey", "grey"], yellow_text_row=1), target(shape_check=False))
        self.assertEqual(res.status, "found")

    def test_hue_tolerance(self):
        rows = ["grey", "orange", "grey"]          # yellow icon hue-shifted by -25 deg
        self.assertEqual(gf.find_target(overview(rows), target()).status, "none")
        self.assertRow(gf.find_target(overview(rows), target(hue_tolerance=32)), 1)

    def test_brightness_variation_tolerated(self):
        img = overview(["grey", "yellow", "grey"])
        dim = img.point(lambda v: int(v * 0.7))
        self.assertRow(gf.find_target(dim, target()), 1)
        self.assertEqual(gf.find_target(overview(["grey"] * 3).point(lambda v: min(255, int(v * 1.6))),
                                        target()).status, "none")

    def test_ui_scale_with_picked_icon_size(self):
        rows = ["grey", "grey", "yellow", "grey"]
        img = overview(rows, scale=1.5)
        rh = int(ROW * 1.5)
        picked = gf.pick_from_image(img.crop((0, 2 * rh, 40, 3 * rh)), (14, rh // 2))
        res = gf.find_target(img, target(**picked))
        self.assertRow(res, 2, rh)

    def test_click_x_override(self):
        res = gf.find_target(overview(["grey", "yellow"]), target(click_x=150))
        self.assertEqual(res.point[0], 150)

    def test_locate_adds_box_origin_and_grabs_only_the_box(self):
        grabbed = []
        img = overview(["grey", "grey", "yellow"])
        t = target()
        pt, msg = gf.locate(t, grab=lambda box: grabbed.append(list(box)) or img)
        self.assertEqual(grabbed, [t["box"]])
        self.assertTrue(240 <= pt[1] < 260, pt)
        self.assertTrue(1004 <= pt[0] <= 1024)
        self.assertIn("clicking", msg)
        none, msg = gf.locate(t, grab=lambda box: overview(["grey"]))
        self.assertIsNone(none)

    def test_debug_image(self):
        img = overview(["grey", "yellow"])
        out = gf.debug_image(img, gf.find_target(img, target()))
        self.assertEqual(out.size, img.size)


if __name__ == "__main__":
    unittest.main()
