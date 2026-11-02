"""畫出 App 圖示（深色圓角方塊＋白色聲波），輸出 .icns。用法：make_icon.py 輸出路徑.icns"""

import subprocess
import sys
import tempfile
from pathlib import Path

from AppKit import (
    NSBezierPath,
    NSBitmapImageRep,
    NSColor,
    NSCompositingOperationSourceAtop,
    NSGradient,
    NSGraphicsContext,
    NSImage,
    NSImageSymbolConfiguration,
    NSMakeRect,
    NSPNGFileType,
    NSRectFillUsingOperation,
)

SIZE = 1024


def white_symbol(name: str, point_size: float) -> NSImage:
    config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(point_size, 0.4)
    symbol = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    symbol = symbol.imageWithSymbolConfiguration_(config)
    tinted = NSImage.alloc().initWithSize_(symbol.size())
    tinted.lockFocus()
    symbol.drawInRect_(NSMakeRect(0, 0, symbol.size().width, symbol.size().height))
    NSColor.whiteColor().set()
    NSRectFillUsingOperation(
        NSMakeRect(0, 0, symbol.size().width, symbol.size().height), NSCompositingOperationSourceAtop
    )
    tinted.unlockFocus()
    return tinted


def render_png(path: Path) -> None:
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, SIZE, SIZE, 8, 4, True, False, "NSDeviceRGBColorSpace", 0, 0
    )
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))

    inset = 100
    tile = NSMakeRect(inset, inset, SIZE - 2 * inset, SIZE - 2 * inset)
    shape = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(tile, 185, 185)
    gradient = NSGradient.alloc().initWithStartingColor_endingColor_(
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.40, 0.33, 0.98, 1.0),
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.12, 0.10, 0.30, 1.0),
    )
    gradient.drawInBezierPath_angle_(shape, -90)

    glyph = white_symbol("waveform", 380)
    w, h = glyph.size().width, glyph.size().height
    glyph.drawInRect_(NSMakeRect((SIZE - w) / 2, (SIZE - h) / 2, w, h))

    NSGraphicsContext.restoreGraphicsState()
    path.write_bytes(bytes(rep.representationUsingType_properties_(NSPNGFileType, {})))


def main() -> None:
    out = Path(sys.argv[1])
    with tempfile.TemporaryDirectory() as tmp:
        master = Path(tmp) / "icon.png"
        render_png(master)
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                px = str(size * scale)
                subprocess.run(
                    ["sips", "-z", px, px, str(master), "--out", str(iconset / name)],
                    check=True,
                    capture_output=True,
                )
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)


if __name__ == "__main__":
    main()
