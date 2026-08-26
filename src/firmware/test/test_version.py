import struct
import uctypes
import unittest

import version


class TestVersion(unittest.TestCase):
    def test_application_name_exists(self):
        """Verify application name is defined and non-empty."""
        self.assertIsInstance(version.application_name, str)
        self.assertTrue(len(version.application_name) > 0)

    def test_firmware_version_exists(self):
        """Verify firmware version is defined and non-empty."""
        self.assertIsInstance(version.firmware_version, str)
        self.assertTrue(len(version.firmware_version) > 0)

    def test_build_tag_exists(self):
        """Verify build tag is defined and non-empty."""
        self.assertIsInstance(version.build_tag, str)
        self.assertTrue(len(version.build_tag) > 0)

    def test_firmware_version_matches_build_tag(self):
        """Verify firmware_version and build_tag are consistent."""
        self.assertEqual(version.firmware_version, version.build_tag)

    @staticmethod
    def create_foooter(version, crc):
        # Refer to src/system/SONIC_NUCLEO_WB55/mboot_footer.c
        """
        typedef struct __attribute__ ((__packed__)) _fw_footer {
            uint8_t footer_version;
            uint16_t footer_length;
            char vershdr[4];
            char version[57];
        } fw_footer_t;
        """
        footer_fmt_v1 = "<BH4s57s"
        FOOTER_VERSION = 1
        FOOTER_LENGTH = 64
        MBOOT_VERS_HDR = b"VER:"

        vers = bytes(version[0:57], "utf8")

        header = struct.pack(
            footer_fmt_v1,
            FOOTER_VERSION,
            FOOTER_LENGTH,
            MBOOT_VERS_HDR,
            vers,
        )
        return header

    def test_bootloader_version_exists(self):
        """Verify bootloader version attribute exists."""
        self.assertIsInstance(version.bootloader_version, str)
        self.assertTrue(len(version.bootloader_version) > 0)

    def test_bootloader_version_parsing(self):
        """Verify bootloader version can be extracted from footer structure."""
        footer = self.create_foooter("1.2.3", 0xA5)

        bin_addr = uctypes.addressof(footer)

        version.MBOOT_VERS_ADDR = bin_addr

        vers = version._bootloader_version()

        self.assertEqual(vers, "1.2.3")
