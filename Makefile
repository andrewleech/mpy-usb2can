#   @Makefile for the Master project
MAKE_OPTIONS := -j$(shell nproc)
PROJECT_BASE := $(strip $(shell dirname $(realpath $(lastword $(MAKEFILE_LIST)))))

# Check for spaces in PROJECT_BASE path
ifneq ($(words $(PROJECT_BASE)),1)
$(error PROJECT_BASE contains spaces: "$(PROJECT_BASE)". Build paths with spaces are not supported by Make.)
endif

MICROPYTHON_BASE := $(PROJECT_BASE)/src/micropython
MPY_LIB_DIR := $(PROJECT_BASE)/src/libs/micropython-lib

# Include project-wide configuration
include $(PROJECT_BASE)/src/system/mpconfigproj.mk

# Unix port configuration
ifeq ($(ENABLE_OPENMV), 1)
  UNIX_VARIANT = openmv
  UNIX_VARIANT_DIR = $(PROJECT_BASE)/src/system/UNIX_OPENMV
else
  UNIX_VARIANT = standard
  UNIX_VARIANT_DIR =
endif

# Common Unix build options
UNIX_OPTIONS=$(MAKE_OPTIONS) VARIANT=$(UNIX_VARIANT) \
  $(if $(UNIX_VARIANT_DIR),VARIANT_DIR=$(UNIX_VARIANT_DIR)) \
  MPY_LIB_DIR=$(MPY_LIB_DIR) \
  MICROPY_PY_FFI=0 \
  CWARN="-Wall -Wno-error=int-conversion -Wno-error=return-type" \
  CFLAGS_EXTRA="-DMICROPY_ENABLE_SCHEDULER=1 -DMICROPY_PY_UPLATFORM=1"

# Common Firmware build options
FW_OPTIONS=$(MAKE_OPTIONS) \
  PORT=$(PORT) \
  BOARD=$(BOARD) \
  USE_MBOOT=1 \
  USER_C_MODULES=$(USER_C_MODULES)

MPY_CROSS = $(MICROPYTHON_BASE)/mpy-cross/build/mpy-cross

# Resolve the version on the host and hand it to the build as environment
# variables, which makeversionhdr.py prefers over its own lookups. This keeps
# git-versioner out of the build container, which carries only the toolchain.
# Left unset when uv is unavailable (inside the build container, where the
# values arrive through the environment instead); makeversionhdr.py treats an
# empty MICROPY_GIT_TAG as a failure rather than falling back.
MPY_VERSION := $(shell PROJECT_ROOT="$(PROJECT_BASE)" uv run --quiet --with git-versioner \
  python -c 'import __version__; print(__version__.version, __version__.git_hash)' 2>/dev/null)
ifneq ($(strip $(MPY_VERSION)),)
export MICROPY_GIT_TAG := $(word 1,$(MPY_VERSION))
export MICROPY_GIT_HASH := $(word 2,$(MPY_VERSION))
endif

# Board and Port definition
BOARD ?= USB2CAN_NUCLEO_H563ZI

PORT ?= stm32


# Integration Tests
MP_CMD ?= $(MICROPYTHON_BASE)/ports/unix/build-$(UNIX_VARIANT)/micropython
MP_PATH ?= $(PROJECT_BASE)/src/unix/build/frozen_mpy:$(PROJECT_BASE)/src/integration-tests/tests/drivers:.frozen

# Run commands inside ephemeral docker if needed
ifneq ("$(wildcard /.dockerenv)$(CI)","")
RUN_IN_DOCKER=0
else
RUN_IN_DOCKER=1
# Official MicroPython ARM toolchain container; this is the image mpbuild
# selects for the stm32 port (see BUILD_CONTAINERS in mpbuild/build.py).
IMAGE ?= micropython/build-micropython-arm
# Filter environment variables to avoid polluting build with host-specific vars
DOCKER_ENV_FILTER = env -i HOME="$$HOME" USER="$$USER" PATH="$$PATH" TERM="$$TERM" SHELL="$$SHELL" \
  MICROPY_GIT_TAG="$(MICROPY_GIT_TAG)" MICROPY_GIT_HASH="$(MICROPY_GIT_HASH)"
DOCKER_VERSION = -e MICROPY_GIT_TAG -e MICROPY_GIT_HASH
DOCKER = @$(DOCKER_ENV_FILTER) docker run --rm -v "$$(pwd):$$(pwd)" -w "$$(pwd)" --user="$$(id -u):$$(id -g)" $(DOCKER_VERSION) $(IMAGE)
# Minimal environment for reproducible builds (e.g., mpy-cross)
DOCKER_CLEAN_ENV = env -i PATH="$$PATH" LANG=C.UTF-8 LC_ALL=C.UTF-8
DOCKER_CLEAN = @$(DOCKER_CLEAN_ENV) docker run --rm -v "$$(pwd):$$(pwd)" -w "$$(pwd)" --user="$$(id -u):$$(id -g)" $(IMAGE)
endif

BOLD :=
RESET :=

default: help

.PHONY : help
help:
	@echo "$(BOLD)$(PROJECT_NAME) Makefile$(RESET)"
	@echo "Please use 'make $(BOLD)target$(RESET)' where $(BOLD)target$(RESET) is one of:"
	@grep -h ':\s\+##' Makefile | column -t -s# | awk -F ":" '{ print "  $(BOLD)" $$1 "$(RESET)" $$2 }'


# Static analysis runs on the host; the toolchain image carries no python tooling.
.PHONY: checks
checks:  ## Run static analysis (ruff, mypy, yamllint)
checks:
	@echo "$(BOLD)Running static analysis$(RESET)"
	@uvx pre-commit run --all-files

.PHONY: lint
lint:  ## Run the linting tool
lint:
	@echo "$(BOLD)Running ruff linter$(RESET)"
	@uvx pre-commit run --all-files ruff

.PHONY: format
format:  ## Check formatting
format:
	@echo "$(BOLD)Checking formatting$(RESET)"
	@uvx pre-commit run --all-files ruff-format

## Checkout the required submodules.
# This target block uses the src/libs/micropython-lib/README.md
# as a target so that `make submodules` is skipped if micropython-lib
# is already checked out. This can be overridden with
# `make submodules-force` to unconditionally run the full task.
.PHONY: submodules submodules-force
define SUBMODULE_SYNC_AND_UPDATE
	@echo "$(BOLD)Cloning submodules$(RESET)"
	## Shallow clone micropython submodule
	git submodule update --init --depth 1 src/micropython
	## Recursive clone all other library submodules
	git -c submodule."src/micropython".update=none submodule update --init --recursive
	## Clone micropython port dependencies
	make -C src/system $(FW_OPTIONS) submodules
	make -C $(MICROPYTHON_BASE)/ports/unix $(UNIX_OPTIONS) submodules
endef

# 1. Standard target: Depends on the file.
# Runs only if README.md is missing.
submodules: $(MPY_LIB_DIR)/README.md

# 2. File target: Runs the target if README.md is missing.
$(MPY_LIB_DIR)/README.md:
	$(SUBMODULE_SYNC_AND_UPDATE)

# 3. Force target: Runs the target unconditionally.
submodules-force:
	$(SUBMODULE_SYNC_AND_UPDATE)

.PHONY: mpy-cross
mpy-cross: $(MPY_CROSS)  ## Build the MicroPython cross-compiler
$(MPY_CROSS): submodules
ifeq ($(RUN_IN_DOCKER), 1)
	$(DOCKER_CLEAN) make mpy-cross
else
	@echo "$(BOLD)Building mpy-cross...$(RESET)"
	make -C $(MICROPYTHON_BASE)/mpy-cross $(MAKE_OPTIONS)
endif

.PHONY: system
system:  ## Build MicroPython including frozen firmware
system: $(MPY_CROSS)
ifeq ($(RUN_IN_DOCKER), 1)
	$(DOCKER) make $@ BOARD=$(BOARD) PORT=$(PORT)
else
	@echo "-----------------------------------"
	@echo "$(BOLD)Building MicroPython ...$(RESET)"
	make -C src/system $(FW_OPTIONS)
endif

.PHONY: libs-only
libs-only:  ## Build MicroPython with frozen libraries only (no application code)
libs-only: $(MPY_CROSS)
ifeq ($(RUN_IN_DOCKER), 1)
	$(DOCKER) make $@ BOARD=$(BOARD) PORT=$(PORT)
else
	@echo "-----------------------------------"
	@echo "$(BOLD)Building MicroPython with libraries only...$(RESET)"
	rm -rf src/system/build-$(BOARD)/frozen_*
	make -C src/system $(FW_OPTIONS) EXCLUDE_APP=1
endif

# Generate a compilation database (compile_commands.json) from the real firmware
# build. This is the SAST scope authority for what is actually compiled, consumed
# by cppcheck, GitLab Advanced SAST and Coverity. The database is written into
# each port's build directory (src/system/build-<BOARD>/ and the unix port's
# build dir), so the build tree's existing .gitignore covers it. The capture
# tooling is part of the SAST programme and lives outside this repository, since
# it spans every target; this target delegates to it. Point SAST_HARNESS at the
# harness directory, or run from within the SAST planning tree where it is found
# automatically at ../../tools/sast.
SAST_HARNESS ?= $(wildcard $(PROJECT_BASE)/../../tools/sast)
.PHONY: compile-commands
compile-commands:  ## Generate compile_commands.json for SAST (needs the SAST harness)
compile-commands:
	@if [ -z "$(SAST_HARNESS)" ] || [ ! -x "$(SAST_HARNESS)/compile-db.sh" ]; then \
		echo "$(BOLD)compile-commands needs the SAST harness.$(RESET)"; \
		echo "Set SAST_HARNESS=/path/to/tools/sast, or run inside the SAST planning tree."; \
		exit 1; \
	fi
	@echo "$(BOLD)Generating compilation database via the SAST harness...$(RESET)"
	SAST_TARGET_REPO=$(notdir $(PROJECT_BASE)) $(SAST_HARNESS)/compile-db.sh $(CDBARGS)


.PHONY: mboot bootloader
bootloader: mboot
mboot:  ## Build MicroPython bootloader
mboot: submodules
ifeq ($(RUN_IN_DOCKER), 1)
	$(DOCKER) make $@ BOARD=$(BOARD) PORT=$(PORT)
else
	@echo "-----------------------------------"
	@echo "$(BOLD)Building MicroPython mBoot ...$(RESET)"
	@if [ "$(PORT)" != "stm32" ]; then echo "ERROR: mBoot only available on stm32"; exit 1; fi
	make -C src/system $(MAKE_OPTIONS) BOARD=$(BOARD) PORT=$(PORT) mboot
endif

.PHONY: deploy-dfu
deploy-dfu:  ## Flash application
deploy-dfu:
	@echo "-----------------------------------"
	@echo "$(BOLD)Deploying Application ...$(RESET)"
	make -C src/system $(MAKE_OPTIONS) BOARD=$(BOARD) PORT=$(PORT) deploy


.PHONY: unix-port
unix-port:  ## Build the MicroPython unix port (used for unit tests)
unix-port: $(MPY_CROSS)
ifeq ($(RUN_IN_DOCKER), 1)
	$(DOCKER) make $@
else
	@echo "-----------------------------------"
	@echo "$(BOLD)Building the MicroPython unix port...$(RESET)"
	make -C $(MICROPYTHON_BASE)/ports/unix $(UNIX_OPTIONS) all
endif

.PHONY: unix-frozen-code
unix-frozen-code:  ## Build the frozen code for the MicroPython unix port
unix-frozen-code: $(MPY_CROSS)
ifeq ($(RUN_IN_DOCKER), 1)
	$(DOCKER) make $@
else
	@echo "$(BOLD)Freezing code for MicroPython unix port...$(RESET)"
	MICROPY_MPYCROSS=$(MPY_CROSS) make -C src/unix
endif

.PHONY: repl-unix
repl-unix:  ## Get a REPL on the MicroPython unix port (with fake hardware)
repl-unix: unix-port unix-frozen-code
	@echo "$(BOLD)REPL on the MicroPython unix port...$(RESET)"
	make -C $(PROJECT_BASE)/src/unix $(MAKE_OPTIONS) repl

.PHONY: run-unix
run-unix:  ## Run the firmware on the MicroPython unix port (with fake hardware)
run-unix: unix-port unix-frozen-code
	@echo "$(BOLD)Running on the MicroPython unix port...$(RESET)"
	make -C $(PROJECT_BASE)/src/unix $(MAKE_OPTIONS) run

.PHONY: tests
tests:  ## Execute unit tests (inside the unix port). Set 'test=test_one' to run a single unit test suite.
tests: unix-port unix-frozen-code
ifeq ($(RUN_IN_DOCKER), 1)
	$(DOCKER) make $@
else
	@echo "-----------------------------------"
	@echo "$(BOLD)Execute unit tests...$(RESET)"
	make -f custom.mk custom-tests
ifdef test
	cd src/unix/build/frozen_mpy; $(MP_CMD) -c 'import unittest_junit; unittest_junit.main("$(test)")'
else
	cd src/unix/build/frozen_mpy; $(MP_CMD) -m unittest_junit discover -p 'test_*.*py'
endif
	rm -rf $(PROJECT_BASE)/results 2> /dev/null
	mv src/unix/build/frozen_mpy/results $(PROJECT_BASE)/
endif

.PHONY: integration-tests
integration-tests:  ## Execute integration tests (inside the unix port). Run as `ROBOTARGS='-i PreMerge' make integration-tests` to pass arguments to robot.
# Robot runs on the host, not in the build container: it drives the already
# built unix port binary and needs uv, which the toolchain image does not carry.
integration-tests: unix-port unix-frozen-code
	@echo "-----------------------------------"
	@echo "$(BOLD)Execute integration tests...$(RESET)"
	make -f custom.mk custom-integration-tests
	cd src/integration-tests; MICROPYTHON_CMD=$(MP_CMD) MICROPYPATH=$(MP_PATH) uv run robot --outputdir $(PROJECT_BASE)/src/unix/integration-tests --variable USE_UNIX_PORT:True -L TRACE --xunit=xunit.xml --exclude NotImplemented --exclude ToolValidation --exclude RequiresHardware $(ROBOTARGS) tests

STUBS_PACKAGE = micropython-stm32-stubs

.PHONY: typings
typings:  ## Install MicroPython type stubs into tools/typings
typings: tools/typings/VERSIONS
tools/typings/VERSIONS:
	@echo "$(BOLD)Installing $(STUBS_PACKAGE)$(RESET)"
	uv pip install --quiet $(STUBS_PACKAGE) --target tools/typings
	@# mypy requires a VERSIONS file at both levels of a custom typeshed.
	@mkdir -p tools/typings/stdlib/os
	@printf 'from posixpath import __all__ as __all__\n' > tools/typings/stdlib/os/path.pyi
	@printf 'os: 3.0-\nsys: 3.0-\nio: 3.0-\n' > tools/typings/stdlib/VERSIONS
	@printf '# Stub versions for MicroPython stubs\n' > tools/typings/VERSIONS

.PHONY: clean
clean:  ## Delete compiled artifacts
clean:
	@echo "$(BOLD)Cleaning MicroPython ...$(RESET)"
	rm -rf $(MICROPYTHON_BASE)/mpy-cross/build
	rm -rf $(MICROPYTHON_BASE)/ports/unix/build-$(UNIX_VARIANT)
	rm -rf $(MICROPYTHON_BASE)/ports/stm32/build-*
	rm -rf src/system/build-$(BOARD)
	rm -rf src/unix/build


