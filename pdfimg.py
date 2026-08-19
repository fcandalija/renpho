#!/usr/bin/env python3
"""Extract the embedded page image from a RENPHO 'Report Details' PDF and
write (optionally cropped / scaled) PNGs using only the Python stdlib.

The PDFs contain a single RGB image XObject (Flate-compressed raw samples)
plus a grayscale SMask. We decode the RGB, composite the mask onto white,
and can crop/scale regions to read specific sections.
"""
import re, sys, zlib, struct

def _find_obj(data, num):
    m = re.search(rb'\b' + str(num).encode() + rb'\s+0\s+obj', data)
    if not m:
        raise ValueError(f"object {num} not found")
    start = m.end()
    end = data.find(b'endobj', start)
    return data[start:end]

def _stream_bytes(objbody):
    s = objbody.find(b'stream')
    # skip 'stream' + EOL
    i = s + len(b'stream')
    if objbody[i:i+2] == b'\r\n': i += 2
    elif objbody[i:i+1] in (b'\n', b'\r'): i += 1
    e = objbody.rfind(b'endstream')
    return objbody[i:e]

def load_image(pdf_path):
    data = open(pdf_path, 'rb').read()
    # main image = first /Subtype /Image with a /SMask (the big RGB one)
    img_num = smask_num = None
    w = h = None
    for m in re.finditer(rb'(\d+)\s+0\s+obj', data):
        num = int(m.group(1)); body = data[m.end():data.find(b'endobj', m.end())]
        head = body[:body.find(b'stream')] if b'stream' in body else body
        if b'/Subtype /Image' in head and b'/SMask' in head:
            img_num = num
            w = int(re.search(rb'/Width\s+(\d+)', head).group(1))
            h = int(re.search(rb'/Height\s+(\d+)', head).group(1))
            sm = re.search(rb'/SMask\s+(\d+)', head)
            smask_num = int(sm.group(1))
            break
    if img_num is None:
        raise ValueError("no main image found")
    rgb = zlib.decompress(_stream_bytes(_find_obj(data, img_num)))
    mask = zlib.decompress(_stream_bytes(_find_obj(data, smask_num)))
    return w, h, rgb, mask

def _composite_row_white(rgb, mask, w, y):
    """Return bytes for one row composited over white using the alpha mask."""
    out = bytearray(w * 3)
    ri = y * w * 3
    mi = y * w
    for x in range(w):
        a = mask[mi + x]
        if a == 255:
            out[x*3:x*3+3] = rgb[ri+x*3:ri+x*3+3]
        elif a == 0:
            out[x*3] = out[x*3+1] = out[x*3+2] = 255
        else:
            for c in range(3):
                v = rgb[ri + x*3 + c]
                out[x*3+c] = (v * a + 255 * (255 - a)) // 255
    return bytes(out)

def write_png(path, w, h, rgb, mask, box=None, scale=1):
    """box = (x0,y0,x1,y1) crop in source px; scale = integer downscale factor."""
    if box is None:
        x0, y0, x1, y1 = 0, 0, w, h
    else:
        x0, y0, x1, y1 = box
    x0 = max(0, x0); y0 = max(0, y0); x1 = min(w, x1); y1 = min(h, y1)
    cw, ch = x1 - x0, y1 - y0
    ow, oh = cw // scale, ch // scale

    # Precompute composited crop rows lazily with nearest-neighbor downscale.
    raw = bytearray()
    for oy in range(oh):
        sy = y0 + oy * scale
        row = _composite_row_white(rgb, mask, w, sy)
        raw.append(0)  # PNG filter type 0
        line = bytearray(ow * 3)
        for ox in range(ow):
            sx = (x0 + ox * scale) * 3
            line[ox*3:ox*3+3] = row[sx:sx+3]
        raw.extend(line)

    def chunk(typ, payload):
        c = struct.pack('>I', len(payload)) + typ + payload
        return c + struct.pack('>I', zlib.crc32(typ + payload) & 0xffffffff)

    ihdr = struct.pack('>IIBBBBB', ow, oh, 8, 2, 0, 0, 0)
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n')
        f.write(chunk(b'IHDR', ihdr))
        f.write(chunk(b'IDAT', zlib.compress(bytes(raw), 6)))
        f.write(chunk(b'IEND', b''))
    return ow, oh

if __name__ == '__main__':
    pdf = sys.argv[1]
    out = sys.argv[2]
    box = None
    scale = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    if len(sys.argv) > 4:
        box = tuple(int(v) for v in sys.argv[4].split(','))
    w, h, rgb, mask = load_image(pdf)
    ow, oh = write_png(out, w, h, rgb, mask, box=box, scale=scale)
    print(f"src {w}x{h} -> wrote {out} {ow}x{oh}")
