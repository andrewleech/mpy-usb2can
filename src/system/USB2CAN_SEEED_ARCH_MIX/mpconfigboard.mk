# MCU settings
MCU_SERIES = MIMXRT1052
MCU_VARIANT = MIMXRT1052DVL6B

MICROPY_FLOAT_IMPL = double
MICROPY_HW_FLASH_TYPE = qspi_nor_flash
MICROPY_HW_FLASH_SIZE = 0x800000  # 8MB
MICROPY_HW_FLASH_CLK = kFlexSpiSerialClk_133MHz
MICROPY_HW_FLASH_QE_CMD = 0x01
MICROPY_HW_FLASH_QE_ARG = 0x40
MICROPY_HW_ROMFS_BYTES = 0x80000  # 512kB

MICROPY_HW_SDRAM_AVAIL = 1
MICROPY_HW_SDRAM_SIZE  = 0x2000000  # 32MB

# The board header declares an ENET PHY, which makes the port register a
# network.LAN type; that only links with lwip present.
MICROPY_PY_LWIP = 1
MICROPY_PY_SSL = 1
MICROPY_SSL_MBEDTLS = 1

# Firmware is deployed through the resident tinyuf2 bootloader over the OTG
# port, which reserves the first 48kB of flash for itself.
USE_UF2_BOOTLOADER = 1

# Include local project build settings.
PROJ_PORT = mimxrt
include $(BOARD_DIR)/../mpconfigproj.mk
