# This should be included in mpconfigboard.mk

# Application files (frozen) built into firmware.
FROZEN_MANIFEST = $(BOARD_DIR)/manifest.py

# Use our makeversionhdr.py to manage version number.
$(BUILD)/genhdr/mpversion.h: PY_SRC = $(TOOLS)


# The port this project's board targets. Everything below the mboot section
# marker is stm32-only; a board on another port sets this and gets just the
# portable settings above.
PROJ_PORT ?= stm32

ifeq ($(PROJ_PORT),stm32)

# Compile the application with support / space for bootloader
USE_MBOOT = 1

# Set Bootloader USB descriptors.
# These are both built into mboot and used for "make deploy"
BOOTLOADER_DFU_USB_VID = 0x30C4
BOOTLOADER_DFU_USB_PID = 0x1102


ifeq ($(BUILDING_MBOOT),1)
# Compile mboot version footer
OBJ += $(BUILD)/$(BOARD_DIR)/mboot_footer.o

$(BUILD)/$(BOARD_DIR)/mboot_footer.o: | $(BUILD)/genhdr/mpversion.h
MBOOT_LD_FILES ?= stm32_memory.ld $(BOARD_DIR)/mboot_footer.ld stm32_sections.ld

# rename mboot fw_footer section to text to ensure it's included in bin/dfu files
.PHONY: fw_footer_section
fw_footer_section: $(BUILD)/firmware.elf
	$(Q)$(OBJCOPY) --rename-section .fw_footer=.text $^ $(BUILD)/firmware.1.elf
	@mv $(BUILD)/firmware.1.elf $^

$(BUILD)/firmware.dfu: | fw_footer_section

endif

endif  # PROJ_PORT stm32
