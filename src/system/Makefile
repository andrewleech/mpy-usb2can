BOARD ?=

BUILD = $(abspath build-$(BOARD))

MICROPY_TOP = $(abspath ../micropython)
PORT ?= stm32

# Try to locate board directory
# First check local directory, then upstream MicroPython boards
BOARD_DIR_LOCAL = $(abspath $(BOARD))
BOARD_DIR_UPSTREAM = $(MICROPY_TOP)/ports/$(PORT)/boards/$(BOARD)

ifeq ($(wildcard $(BOARD_DIR_LOCAL)/mpconfigboard.mk),)
  # Local board doesn't exist, try upstream
  ifeq ($(wildcard $(BOARD_DIR_UPSTREAM)/mpconfigboard.mk),)
    $(error Board '$(BOARD)' not found. Searched in: $(BOARD_DIR_LOCAL) and $(BOARD_DIR_UPSTREAM). Please ensure the board directory exists or specify a valid BOARD name)
  else
    BOARD_DIR = $(BOARD_DIR_UPSTREAM)
  endif
else
  BOARD_DIR = $(BOARD_DIR_LOCAL)
endif

include $(MICROPY_TOP)/py/mkenv.mk
include $(MICROPY_TOP)/py/mkrules.mk
include $(BOARD_DIR)/mpconfigboard.mk

MPY_LIB_DIR = $(abspath ../libs/micropython-lib)
MAKE_ARGS = BOARD=$(BOARD) BOARD_DIR=$(BOARD_DIR) BUILD=$(BUILD)

export TOOLS = $(abspath ../../tools)

export MPY_GIT_VERSIONER=1

MBOOT_TEXT0_ADDR ?= 0x08000000


submodules_:
	$(MAKE) -C $(MICROPY_TOP)/ports/$(PORT) $(MAKE_ARGS) submodules

all: submodules_
	$(MAKE) -C $(MICROPY_TOP)/ports/$(PORT) $(MAKE_ARGS) MPY_LIB_DIR=$(MPY_LIB_DIR) USER_C_MODULES=$(USER_C_MODULES)
	@echo "firmware available in: $(BUILD)"
	@sed -En 's/.*MICROPY_GIT_TAG "(.*)"/version: \1/p' "$(BUILD)/genhdr/mpversion.h" > $(BUILD)/version.txt
	@cat $(BUILD)/version.txt
	@date


mboot: submodules_
	@# Generate mpversion.h using our custom makeversionhdr.py before mboot build
	@mkdir -p $(BUILD)/mboot/genhdr
	$(PYTHON) $(TOOLS)/makeversionhdr.py $(BUILD)/mboot/genhdr/mpversion.h
	$(MAKE) -C $(MICROPY_TOP)/ports/$(PORT)/mboot $(MAKE_ARGS)/mboot
	cd $(BUILD)/mboot; for f in firmware*.*; do cp $$f ../bootloader.$${f##*.}; done
	@sed -En 's/.*MICROPY_GIT_TAG "(.*)"/version: \1/p' "$(BUILD)/mboot/genhdr/mpversion.h" > $(BUILD)/version.txt
	@cat $(BUILD)/version.txt
	@echo "bootloader available in: $(BUILD)/bootloader.(bin|hex|dfu)"

deploy:
	$(MAKE) -C $(MICROPY_TOP)/ports/$(PORT) $(MAKE_ARGS) deploy

