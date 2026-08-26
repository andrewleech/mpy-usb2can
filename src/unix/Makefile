
# External commands
ECHO = echo
RM = rm
MKDIR = mkdir
PYTHON = python3

# Path to the system
MPY_TOP = ../micropython
MPY_LIB_DIR = ../libs/micropython-lib
UNIX_MICROPYTHON = $(MPY_TOP)/ports/unix/build-standard/micropython

# Configuration variables
BUILD = build
MPY_CROSS_FLAGS = -march=x64

all:
	$(MKDIR) -p $(BUILD)/genhdr
	$(ECHO) "QCFG(BYTES_IN_LEN, (1))" > $(BUILD)/genhdr/qstrdefs.preprocessed.h
	$(ECHO) "QCFG(BYTES_IN_HASH, (2))" >> $(BUILD)/genhdr/qstrdefs.preprocessed.h
	$(PYTHON) $(MPY_TOP)/tools/makemanifest.py -o $(BUILD)/frozen_content.c -v "MPY_DIR=$(MPY_TOP)" -v "PORT_DIR=$(MPY_TOP)/ports/stm32" -v "MPY_LIB_DIR=$(MPY_LIB_DIR)" -b "$(BUILD)" -f"$(MPY_CROSS_FLAGS)" manifest.py

clean:
	$(RM) -rf $(BUILD)

repl:
	MICROPYPATH=config:$(BUILD)/frozen_mpy:.frozen $(UNIX_MICROPYTHON)

run:
	MICROPYPATH=config:$(BUILD)/frozen_mpy:.frozen $(UNIX_MICROPYTHON) -c "import main; main.run()"
