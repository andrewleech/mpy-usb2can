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

# --- Static analysis ----------------------------------------------------------------------------
#
# The analysers and their driver are not in this repository. They are provided by an image, the same
# way the compiler is: micropython/build-micropython-arm supplies arm-none-eabi-gcc, and
# $(SAST_IMAGE) supplies that toolchain plus cppcheck and the sast-* entry points these targets
# call. Build it from the degraves SAST tree, or pull it, before running any of these.
#
# What this repository does hold is its own analysis policy, under analysis/: which of its paths are
# first-party, which build configurations are agreed, which findings it has judged suppressible and
# which coverage gaps it has accepted. Those describe this project and belong to it.
# The image is declared once, in pyproject.toml's [tool.degraves-sast] table; CI jobs read it back
# through `make -s sast-image`. SAST_IMAGE set on the command line or in the environment wins.
ifeq ($(origin SAST_IMAGE),undefined)
SAST_IMAGE := $(shell awk '/^\[/ { s = ($$0 == "[tool.degraves-sast]") } \
  s && /^image *=/ { sub(/^image *= *"/, ""); sub(/".*/, ""); print }' $(PROJECT_BASE)/pyproject.toml)
endif

# Running a command in the analysis image, or directly when already inside it. The unprefixed form
# is for use inside a shell conditional, where a leading @ would not be a recipe prefix.
ifeq ($(RUN_IN_DOCKER), 1)
SAST_RUN_Q = $(DOCKER_ENV_FILTER) docker run --rm -v "$$(pwd):$$(pwd)" -w "$$(pwd)" --user="$$(id -u):$$(id -g)" $(DOCKER_VERSION) $(SAST_IMAGE)
SAST_IMAGE_CHECK = sast-image-check
else
SAST_RUN_Q =
SAST_IMAGE_CHECK =
endif
SAST_RUN = @$(SAST_RUN_Q)

FW_BUILD = $(PROJECT_BASE)/src/system/build-$(BOARD)
UNIX_BUILD = $(MICROPYTHON_BASE)/ports/unix/build-$(UNIX_VARIANT)
ANALYSIS_POLICY = $(PROJECT_BASE)/analysis

.PHONY: sast-image
sast-image:  ## Print the analysis image this project declares
	@echo $(SAST_IMAGE)

.PHONY: sast-image-check
sast-image-check:
ifeq ($(RUN_IN_DOCKER), 1)
	@docker image inspect $(SAST_IMAGE) >/dev/null 2>&1 || { \
	  echo "The analysis image $(SAST_IMAGE) is not present."; \
	  echo "It carries cppcheck and the sast-* tools; this repository does not."; \
	  echo "Build it from the SAST tree: docker build -t $(SAST_IMAGE) -f images/cppcheck/Dockerfile ."; \
	  exit 1; }
endif

# --- Compilation database
#
# Records the real compiler invocation for every translation unit the build compiles, by interposing
# a shim named for each toolchain binary through CROSS_COMPILE for the firmware builds and CC for
# the unix port. Capture runs inside the image, because the database must describe the compiler the
# firmware actually ships with, and because the shim lives there rather than here.
#
# The artefacts are build output and live in the build directory beside the firmware they describe.
# Nothing here deletes anything: make clean removes them with the rest of the build. The capture
# records are named after the object file each one produces, so recompiling a translation unit
# overwrites its record rather than adding a second, and an incremental build leaves the database
# correct rather than partial. A database is only ever as complete as the build directory it sits
# in, and the completeness check says so by reconciling the two.

.PHONY: compile-commands
compile-commands:  ## Generate compile_commands.json for the firmware build, into its build directory
compile-commands: $(MPY_CROSS) $(SAST_IMAGE_CHECK)
ifeq ($(RUN_IN_DOCKER), 1)
	$(SAST_RUN) make $@ BOARD=$(BOARD) PORT=$(PORT)
else
	@echo "$(BOLD)Capturing compilation database for $(BOARD) on $(PORT)...$(RESET)"
	sast-shim --prefix arm-none-eabi- $(FW_BUILD)/compile-commands/shim
	CC_SHIM_CAPTURE=$(FW_BUILD)/compile-commands/capture \
	  $(MAKE) -C src/system $(FW_OPTIONS) CROSS_COMPILE=$(FW_BUILD)/compile-commands/shim/arm-none-eabi-
	sast-compdb \
	  --capture $(FW_BUILD)/compile-commands/capture \
	  --build-dir $(FW_BUILD) --exclude mboot --exclude compile-commands \
	  --out $(FW_BUILD) --target-root $(PROJECT_BASE) \
	  --policy $(ANALYSIS_POLICY)/ownership.json \
	  --configuration $(PORT)-$(BOARD)
endif

.PHONY: compile-commands-mboot
compile-commands-mboot:  ## Generate compile_commands.json for the bootloader build (stm32 only)
# The bootloader is a separate build of overlapping sources under different flags, notably its own
# MBOOT_VTOR and linker layout, so its translation units are not the firmware's. It is not the only
# build compiling this repository's first-party C: the stm32 port sweeps the board directory with
# $(wildcard $(BOARD_DIR)/*.c) in SRC_C, so board .c files enter the firmware build too.
compile-commands-mboot: submodules $(SAST_IMAGE_CHECK)
	@if [ "$(PORT)" != "stm32" ]; then echo "ERROR: mboot only available on stm32"; exit 1; fi
ifeq ($(RUN_IN_DOCKER), 1)
	$(SAST_RUN) make $@ BOARD=$(BOARD) PORT=$(PORT)
else
	@echo "$(BOLD)Capturing bootloader compilation database for $(BOARD)...$(RESET)"
	sast-shim --prefix arm-none-eabi- $(FW_BUILD)/mboot/compile-commands/shim
	CC_SHIM_CAPTURE=$(FW_BUILD)/mboot/compile-commands/capture \
	  $(MAKE) -C src/system $(MAKE_OPTIONS) BOARD=$(BOARD) PORT=$(PORT) mboot \
	    CROSS_COMPILE=$(FW_BUILD)/mboot/compile-commands/shim/arm-none-eabi-
	sast-compdb \
	  --capture $(FW_BUILD)/mboot/compile-commands/capture \
	  --build-dir $(FW_BUILD)/mboot --exclude compile-commands \
	  --out $(FW_BUILD)/mboot --target-root $(PROJECT_BASE) \
	  --policy $(ANALYSIS_POLICY)/ownership.json \
	  --configuration $(PORT)-$(BOARD)-mboot
endif

.PHONY: compile-commands-unix
compile-commands-unix:  ## Generate compile_commands.json for the unix port, into its build directory
compile-commands-unix: $(MPY_CROSS) $(SAST_IMAGE_CHECK)
ifeq ($(RUN_IN_DOCKER), 1)
	$(SAST_RUN) make $@
else
	@echo "$(BOLD)Capturing compilation database for the unix port...$(RESET)"
	sast-shim $(UNIX_BUILD)/compile-commands/shim
	CC_SHIM_CAPTURE=$(UNIX_BUILD)/compile-commands/capture \
	  $(MAKE) -C $(MICROPYTHON_BASE)/ports/unix $(UNIX_OPTIONS) \
	    CC=$(UNIX_BUILD)/compile-commands/shim/gcc all
	sast-compdb \
	  --capture $(UNIX_BUILD)/compile-commands/capture \
	  --build-dir $(UNIX_BUILD) --exclude compile-commands \
	  --out $(UNIX_BUILD) --target-root $(PROJECT_BASE) \
	  --policy $(ANALYSIS_POLICY)/ownership.json \
	  --configuration unix-$(UNIX_VARIANT)
endif

.PHONY: check-compile-commands
check-compile-commands:  ## Reconcile each generated database against the build output it describes
check-compile-commands: $(SAST_IMAGE_CHECK)
	$(SAST_RUN) sast-compdb --check-db $(FW_BUILD)/compile_commands.json --build-dir $(FW_BUILD)
	@if [ -f $(FW_BUILD)/mboot/compile_commands.json ]; then \
	  $(SAST_RUN_Q) sast-compdb --check-db $(FW_BUILD)/mboot/compile_commands.json --build-dir $(FW_BUILD)/mboot; \
	else echo "no bootloader database for $(BOARD); skipping"; fi
	@if [ -f $(UNIX_BUILD)/compile_commands.json ]; then \
	  $(SAST_RUN_Q) sast-compdb --check-db $(UNIX_BUILD)/compile_commands.json --build-dir $(UNIX_BUILD); \
	else echo "no unix port database; skipping"; fi

# --- Analysis scope from the manifest chain
#
# The manifest chain is the authority for what is declared: frozen Python modules, and user C module
# directories where the pin supports declaring them. The compilation database is the authority for
# what is actually compiled. Neither supersedes the other, so the resolver cross-checks itself
# against the database and reports disagreement rather than reconciling it silently.
#
# Run the compile-commands targets first. Without them the cross-check cannot run and the artefacts
# are weaker; the resolver says so rather than producing them quietly.
SCOPE_DIR ?= $(PROJECT_BASE)/build/scope

.PHONY: scope
scope:  ## Resolve analysis scope from the manifest chain, for every agreed configuration
scope: $(SAST_IMAGE_CHECK)
	$(SAST_RUN) sast-scope --all $(ANALYSIS_POLICY)/configurations.json \
	  --root $(PROJECT_BASE) --policy $(ANALYSIS_POLICY)/ownership.json --out $(SCOPE_DIR)

.PHONY: check-scope
check-scope:  ## Fail if the manifest chain declares anything the recorded scope does not cover
check-scope: $(SAST_IMAGE_CHECK)
	$(SAST_RUN) sast-scope --all $(ANALYSIS_POLICY)/configurations.json \
	  --root $(PROJECT_BASE) --policy $(ANALYSIS_POLICY)/ownership.json --out $(SCOPE_DIR) --check

# --- C static analysis
#
# The analyser reads the compilation database, so it sees the defines and include paths the build
# used rather than inferring them. Artefacts land in <build dir>/cppcheck, beside the firmware they
# describe, and are removed only by make clean. The cache under it is what makes a re-run after a
# one-file edit quick.
CPPCHECK_CHECK_LEVEL ?= normal
CPPCHECK_SYSTEM_INCLUDES ?= none
# "full" analyses the whole compilation database; "gate" restricts it to the ownership buckets
# data/profiles.json marks outcome "gate" (currently the owned in-repo profiles), for a faster run
# over the subset that can actually fail a build. sast-cppcheck stamps which one ran into
# summary.json, so a gated run cannot be mistaken for a full one from the artefact alone.
CPPCHECK_SCOPE ?= full

# $(1) build directory holding compile_commands.json, $(2) configuration name, $(3) extra arguments
define CPPCHECK_RUN
	@test -f $(1)/compile_commands.json || { \
	  echo "$(1)/compile_commands.json is missing. Run the matching compile-commands target first;"; \
	  echo "the analyser takes its scope from the database rather than walking directories."; \
	  exit 1; }
	$(SAST_RUN) sast-cppcheck \
	  --compile-commands $(1)/compile_commands.json \
	  --root $(PROJECT_BASE) --out $(1)/cppcheck --configuration $(2) \
	  --policy-dir $(ANALYSIS_POLICY) \
	  --check-level $(CPPCHECK_CHECK_LEVEL) --system-includes $(CPPCHECK_SYSTEM_INCLUDES) \
	  --scope $(CPPCHECK_SCOPE) $(3)
endef

.PHONY: cppcheck
cppcheck:  ## Analyse the firmware configuration for the current BOARD and PORT
cppcheck: $(SAST_IMAGE_CHECK)
	$(call CPPCHECK_RUN,$(FW_BUILD),$(PORT)-$(BOARD),)

.PHONY: cppcheck-mboot
cppcheck-mboot:  ## Analyse the bootloader configuration (stm32 only)
cppcheck-mboot: $(SAST_IMAGE_CHECK)
	$(call CPPCHECK_RUN,$(FW_BUILD)/mboot,$(PORT)-$(BOARD)-mboot,)

.PHONY: cppcheck-unix
cppcheck-unix:  ## Analyse the unix port configuration
cppcheck-unix: $(SAST_IMAGE_CHECK)
	$(call CPPCHECK_RUN,$(UNIX_BUILD),unix-$(UNIX_VARIANT),)

# Configuration coverage, not findings. It fails when a translation unit the database lists was
# never analysed, or when one ended early at an #error or a parse failure, because either makes a
# low finding count a property of the run rather than of the code.
.PHONY: check-cppcheck
check-cppcheck:  ## Fail if the analysis did not cover every translation unit in the database
check-cppcheck: $(SAST_IMAGE_CHECK)
	$(call CPPCHECK_RUN,$(FW_BUILD),$(PORT)-$(BOARD),--coverage-only)
	@if [ -f $(FW_BUILD)/mboot/compile_commands.json ]; then \
	  $(MAKE) --no-print-directory cppcheck-coverage-mboot; fi
	@if [ -f $(UNIX_BUILD)/compile_commands.json ]; then \
	  $(MAKE) --no-print-directory cppcheck-coverage-unix; fi

.PHONY: cppcheck-coverage-mboot
cppcheck-coverage-mboot:
	$(call CPPCHECK_RUN,$(FW_BUILD)/mboot,$(PORT)-$(BOARD)-mboot,--coverage-only)

.PHONY: cppcheck-coverage-unix
cppcheck-coverage-unix:
	$(call CPPCHECK_RUN,$(UNIX_BUILD),unix-$(UNIX_VARIANT),--coverage-only)

# --- A second free tool: GCC's own -fanalyzer ------------------------------------------------
#
# No new procurement: arm-none-eabi-gcc already builds this firmware, and -fanalyzer is built into
# GCC 10+, which the analysis image already carries as its base layer. This replays the exact
# command line the compilation database recorded, per translation unit, so it is the same
# compilation the firmware ships with rather than a second, independently-derived one.
define FANALYZER_RUN
	@test -f $(1)/compile_commands.json || { \
	  echo "$(1)/compile_commands.json is missing. Run the matching compile-commands target first."; \
	  exit 1; }
	$(SAST_RUN) sast-fanalyzer \
	  --compile-commands $(1)/compile_commands.json \
	  --root $(PROJECT_BASE) --out $(1)/fanalyzer --configuration $(2) \
	  --policy-dir $(ANALYSIS_POLICY)
endef

.PHONY: fanalyzer
fanalyzer:  ## Analyse the firmware configuration for the current BOARD and PORT with GCC -fanalyzer
fanalyzer: $(SAST_IMAGE_CHECK)
	$(call FANALYZER_RUN,$(FW_BUILD),$(PORT)-$(BOARD))

.PHONY: fanalyzer-mboot
fanalyzer-mboot:  ## Analyse the bootloader configuration (stm32 only) with GCC -fanalyzer
fanalyzer-mboot: $(SAST_IMAGE_CHECK)
	$(call FANALYZER_RUN,$(FW_BUILD)/mboot,$(PORT)-$(BOARD)-mboot)

.PHONY: fanalyzer-unix
fanalyzer-unix:  ## Analyse the unix port configuration with GCC -fanalyzer
fanalyzer-unix: $(SAST_IMAGE_CHECK)
	$(call FANALYZER_RUN,$(UNIX_BUILD),unix-$(UNIX_VARIANT))

# Coverage here means every translation unit in the database completed the analyser without timing
# out or crashing it; there is no separate cheap pass the way cppcheck has one, so this is the same
# invocation as the targets above, not a lighter one.
.PHONY: check-fanalyzer
check-fanalyzer:  ## Fail if -fanalyzer did not complete over every translation unit in the database
check-fanalyzer: $(SAST_IMAGE_CHECK)
	$(call FANALYZER_RUN,$(FW_BUILD),$(PORT)-$(BOARD))
	@if [ -f $(FW_BUILD)/mboot/compile_commands.json ]; then \
	  $(MAKE) --no-print-directory fanalyzer-mboot; fi
	@if [ -f $(UNIX_BUILD)/compile_commands.json ]; then \
	  $(MAKE) --no-print-directory fanalyzer-unix; fi


.PHONY: clean
clean:  ## Delete compiled artifacts
clean:
	@echo "$(BOLD)Cleaning MicroPython ...$(RESET)"
	rm -rf $(MICROPYTHON_BASE)/mpy-cross/build
	rm -rf $(MICROPYTHON_BASE)/ports/unix/build-$(UNIX_VARIANT)
	rm -rf $(MICROPYTHON_BASE)/ports/stm32/build-*
	rm -rf src/system/build-$(BOARD)
	rm -rf src/unix/build


