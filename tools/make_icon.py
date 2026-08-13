import struct

SIZES = (16, 32, 48, 64, 256)


def rounded_rect_mask(size, x0, y0, w, h, radius):
    mask = set()
    for y in range(y0, y0 + h):
        for x in range(x0, x0 + w):
            cx = min(max(x, x0 + radius), x0 + w - radius - 1)
            cy = min(max(y, y0 + radius), y0 + h - radius - 1)
            dx, dy = x - cx, y - cy
            if dx * dx + dy * dy <= radius * radius:
                mask.add((x, y))
    return mask


def shield_points(s):
    return [
        (s * 0.50, s * 0.14),
        (s * 0.86, s * 0.30),
        (s * 0.86, s * 0.62),
        (s * 0.50, s * 0.86),
        (s * 0.14, s * 0.62),
        (s * 0.14, s * 0.30),
    ]


def point_in_polygon(x, y, points):
    inside = False
    n = len(points)
    j = n - 1
    for i in range(n):
        xi, yi = points[i]
        xj, yj = points[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (
            (yj - yi) or 1e-9
        ) + xi:
            inside = not inside
        j = i
    return inside


def render_pixels(size):
    pixels = bytearray()
    corner = max(1, round(size * 0.22))
    inset = max(1, round(size * 0.06))
    outer = rounded_rect_mask(size, 0, 0, size, size, corner)
    inner = rounded_rect_mask(
        size, inset, inset, size - 2 * inset, size - 2 * inset, max(1, corner - 1)
    )
    shield = [tuple(size * coord for coord in p) for p in shield_points(size)]
    radius = size * 0.14
    for y in range(size):
        for x in range(size):
            color = (0, 0, 0, 0)
            if (x, y) in outer:
                if (x, y) in inner:
                    color = (255, 255, 255, 255)
                else:
                    color = (10, 10, 10, 255)
            if point_in_polygon(x, y, shield):
                color = (10, 10, 10, 255)
            dx, dy = x - size * 0.5, y - size * 0.5
            if dx * dx + dy * dy <= radius * radius:
                color = (255, 255, 255, 255)
            pixels.extend(color)
    return pixels


def bmp_data(size, px):
    header = struct.pack(
        "<IiiHHIIiiII",
        40,
        size,
        size * 2,
        1,
        32,
        0,
        len(px) * 4,
        0,
        0,
        0,
        0,
    )
    rows = []
    for y in range(size - 1, -1, -1):
        row = b""
        for x in range(size):
            i = (y * size + x) * 4
            b, g, r, a = px[i], px[i + 1], px[i + 2], px[i + 3]
            row += struct.pack("<BBBB", b, g, r, a)
        rows.append(row)
    return header + b"".join(rows)


def write_ico(path):
    images = []
    for size in SIZES:
        px = render_pixels(size)
        data = bmp_data(size, px)
        images.append((size, data))
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = b""
    offset = 6 + 16 * len(images)
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII",
            size & 0xFF,
            size & 0xFF,
            0,
            0,
            1,
            32,
            len(data),
            offset,
        )
        offset += len(data)
    with open(path, "wb") as f:
        f.write(header + entries)
        for _, data in images:
            f.write(data)


if __name__ == "__main__":
    write_ico("flint.ico")
    print("wrote flint.ico")