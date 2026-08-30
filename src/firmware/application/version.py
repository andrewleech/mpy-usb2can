"""
Provide information about the application and its version.
"""
import os
import sys
import uctypes
import logging

log = logging.getLogger("version")


application_name = "USB2CAN"

unix = sys.platform == "linux"

# Detect USB_NET (NCM) support
usb_net = False
if not unix:
    try:
        import network

        usb_net = hasattr(network, "USB_NCM")
    except ImportError:
        pass

try:
    # embedded firmware version
    firmware_version = build_tag = os.uname().version
except AttributeError:
    try:
        # unix port support
        import platform

        firmware_version = build_tag = platform.platform()
    except ImportError:
        firmware_version = build_tag = "unknown"


# Extract the bootloader version from a known location in flash.
#
# mboot is part of the stm32 port and MBOOT_VERS_ADDR is an address in that
# port's flash map, so the read below is only meaningful where `pyb` - the
# module that port carries - exists. Elsewhere it would dereference an address
# that belongs to something else entirely.
try:
    import pyb  # noqa: F401 - imported for its presence, not its contents

    mboot = True
except ImportError:
    mboot = False

MBOOT_VERS_ADDR: int = 0x08000280  # Matches address in $(BOARD)/mboot_footer.ld
MBOOT_FOOTER_VERSION = 1
MBOOT_FOOTER_LENGTH = 64
MBOOT_VERS_HDR = b"VER:"


def _bootloader_version():
    BOOTLOADER_FOOTER = {
        "footer_version": 0 | uctypes.UINT8,
        "footer_length": 1 | uctypes.UINT16,
        "vershdr": (3 | uctypes.ARRAY, 4 | uctypes.UINT8),
        "version": (7 | uctypes.ARRAY, 57 | uctypes.UINT8),
    }

    vers = "UNKNOWN"

    footer = uctypes.struct(MBOOT_VERS_ADDR, BOOTLOADER_FOOTER, uctypes.LITTLE_ENDIAN)

    if footer.footer_version != MBOOT_FOOTER_VERSION:
        log.warning("bootloader error: footer version mismatch")
        return vers

    if footer.footer_length != MBOOT_FOOTER_LENGTH:
        log.warning("bootloader error: footer length mismatch")
        return vers

    if footer.vershdr != MBOOT_VERS_HDR:
        log.warning("bootloader error: version header mismatch")
        return vers

    try:
        vers = str(footer.version, "utf-8").strip("\x00")
    except UnicodeDecodeError:
        vers = "UNKNOWN"

    return vers


if unix or not mboot:
    bootloader_version = "N/A"
else:
    bootloader_version = _bootloader_version()
