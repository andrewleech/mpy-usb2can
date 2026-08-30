# Power on initialisation.
import gc
import sys

# Reduce the heap fragmentation, this allows for more RAM utilization.
gc.threshold(4000)  # type: ignore[attr-defined]

# Reorder the path to prioritize the file system.
sys.path.clear()
# If extra directories are added, for example for configuration, that are to
# be imported by the device, add those paths here. They will be preferentially
# imported compared to the root path.
# >> ADD FILE APPEND HERE
sys.path.append(".frozen")

# Development import paths, can remove for production.
sys.path.extend(
    [
        ".",  # Allow import from current working dir (works with mpremote mount)
        "/flash/lib",  # Allow import from mip installed packages.
    ]
)


# `pyb` is the stm32 port's own module: it is where both the legacy USB stack
# and the internal-flash block device live. Ports without it configure USB
# through machine.USBDevice alone and mount their own filesystem at startup,
# so the two blocks below that need it are skipped rather than adapted.
try:
    import pyb
except ImportError:
    pyb = None  # type: ignore[assignment]


def usb_init():
    # VID=0x30C4,
    # PID=0x1100-0x1101 inclusive
    from machine import SOFT_RESET, reset_cause

    # pyb.usb_mode() configures the legacy ST USB stack; it does not exist
    # when the board is built with MICROPY_HW_TINYUSB_STACK. The USB
    # identity is configured through machine.USBDevice instead, done
    # elsewhere as part of the gs_usb runtime device setup.
    if pyb is None or not hasattr(pyb, "usb_mode"):
        return

    # Do not change the USB mode on SOFT_RESET.
    if reset_cause() == SOFT_RESET:
        return

    VID = int(0x30C4)

    PID_VCP = int(0x1100)
    pyb.usb_mode("VCP", vid=VID, pid=PID_VCP)


usb_init()


def filesystem_format():
    """Format the filesystem."""

    import vfs

    vfs.VfsLfs2.mkfs(pyb.Flash(start=0, len=FLASH_LEN))


# Extent of the internal-flash filesystem. The same value must be used to
# format and to mount, or the two disagree about where the filesystem ends.
FLASH_LEN = 64 * 1024


def mount():
    "mount file system"

    import vfs

    flash = pyb.Flash(start=0, len=FLASH_LEN)
    vfs.mount(vfs.VfsLfs2(flash, mtime=False), "/", readonly=False)


def filesystem_init():
    """Check if the filesystem is there, and format it if not and
    flash is blank. Reformat to LFS2 if FAT is detected."""
    import vfs

    # Check for FAT filesystem auto-mounted by MicroPython
    try:
        mounts = vfs.mount()
        for vfs_obj, mount_point in mounts:
            if mount_point in ("/", "/flash"):
                if isinstance(vfs_obj, vfs.VfsFat):
                    # FAT detected - unmount and reformat to LFS2
                    print("FAT filesystem detected - reformatting to LFS2")
                    vfs.umount(mount_point)
                    filesystem_format()
                    mount()
                    return
                elif isinstance(vfs_obj, vfs.VfsLfs2) and mount_point == "/":
                    # LFS2 already mounted correctly
                    return
    except (OSError, AttributeError):
        pass

    # Try to mount LFS2
    try:
        mount()
        return  # Filesystem exists
    except OSError:
        pass

    # Mount failed - reformat and mount
    print("LFS2 mount failed - reformatting flash")
    filesystem_format()
    mount()


try:
    if pyb is not None:
        filesystem_init()
except Exception as exc:  # noqa: BLE001 - boot must continue without storage
    # The adapter's own function needs no filesystem, so a storage failure
    # is reported and stepped over rather than left to abort the rest of
    # boot and everything main.py would have started.
    print("filesystem unavailable:", exc)
