#!/usr/bin/env python3
"""
Cross-platform camera check — discovers cameras on Linux, macOS, and Windows.

Works with USB webcams, built-in cameras, and Jetson CSI cameras.
Replaces the old Jetson-only CSI test.
"""

import sys

def main():
    from repryntt.hardware.camera import discover_cameras, capture_frame

    print("=== Camera Discovery ===")

    try:
        import cv2
        print(f"OpenCV version: {cv2.__version__}")
        gst = "Yes" if "GStreamer" in cv2.getBuildInformation() else "No"
        print(f"GStreamer support: {gst}")
    except ImportError:
        print("OpenCV not installed — install with: pip install opencv-python")
        sys.exit(1)

    print()
    cameras = discover_cameras(force_refresh=True)

    if not cameras:
        print("No cameras found.")
        print()
        print("Troubleshooting:")
        print("  Linux:   Check USB connection, try 'ls /dev/video*'")
        print("  macOS:   Grant camera permission in System Settings > Privacy")
        print("  Windows: Check Device Manager > Imaging devices")
        sys.exit(1)

    print(f"\nFound {len(cameras)} camera(s):\n")
    for c in cameras:
        csi = " [CSI]" if c.is_csi else ""
        print(f"  [{c.index}] {c.name}{csi}")
        print(f"      {c.width}x{c.height} @ {c.fps:.0f}fps  ({c.backend})")
        if c.device_path:
            print(f"      Device: {c.device_path}")
        print()

    # Test capture from each camera
    print("=== Capture Test ===\n")
    success = 0
    for c in cameras:
        frame = capture_frame(c.index)
        if frame is not None:
            h, w = frame.shape[:2]
            print(f"  [{c.index}] ✓ Captured {w}x{h} frame")
            success += 1
        else:
            print(f"  [{c.index}] ✗ Capture failed")

    print(f"\n=== Results: {success}/{len(cameras)} cameras working ===")
    if success == len(cameras):
        print("✓ All cameras working!")
    else:
        print("⚠ Some cameras failed. Check connections and try again.")
        sys.exit(1)


if __name__ == "__main__":
    main()

