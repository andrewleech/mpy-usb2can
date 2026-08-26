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


def usb_init():
    # VID=0x30C4,
    # PID=0x1100-0x1101 inclusive
    import pyb
    from machine import SOFT_RESET, reset_cause

    # Do not change the USB mode on SOFT_RESET.
    if reset_cause() == SOFT_RESET:
        return

    VID = int(0x30C4)

    PID_VCP = int(0x1100)
    pyb.usb_mode("VCP", vid=VID, pid=PID_VCP)


usb_init()


def filesystem_format():
    """Format the filesystem."""

    import pyb
    import vfs

    vfs.VfsLfs2.mkfs(pyb.Flash(start=0))


def mount():
    "mount file system"

    import pyb
    import vfs

    flash = pyb.Flash(start=0, len=(64 * 1024))
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


filesystem_init()
