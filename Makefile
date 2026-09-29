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

# Common Unix build options. The variables are kept apart from the job count so the compilation
# database targets can replay the same make line serially.
UNIX_MAKE_VARS = VARIANT=$(UNIX_VARIANT) \
  $(if $(UNIX_VARIANT_DIR),VARIANT_DIR=$(UNIX_VARIANT_DIR)) \
  MPY_LIB_DIR=$(MPY_LIB_DIR) \
  MICROPY_PY_FFI=0 \
  CWARN="-Wall -Wno-error=int-conversion -Wno-error=return-type" \
  CFLAGS_EXTRA="-DMICROPY_ENABLE_SCHEDULER=1 -DMICROPY_PY_UPLATFORM=1"
UNIX_OPTIONS=$(MAKE_OPTIONS) $(UNIX_MAKE_VARS)

# Common Firmware build options, as a function of port and board so that a configuration other
# than the current PORT and BOARD gets the same variables. $(1) port, $(2) board.
fw_make_vars = PORT=$(1) \
  BOARD=$(2) \
  USE_MBOOT=1 \
  USER_C_MODULES=$(USER_C_MODULES)
FW_OPTIONS=$(MAKE_OPTIONS) $(call fw_make_vars,$(PORT),$(BOARD))

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
# Pinned by digest, the same one .github/workflows/*.yml and .gitlab-ci.yml
# run in: the static analysis derives its predefines and type model from this
# image's compilers, so a moving tag would move the analysis with it.
IMAGE ?= micropython/build-micropython-arm@sha256:0d80e3aaa94bcc5d268d1ec92e3d19865e17d68d3bc9328f0051b5fb0dde007b
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
	@echo ""
	@echo "Static analysis per configuration, where <cfg> is one of:"
	@echo "  $(SAST_CONFIGS)"
	@sed -n 's/^#> /  /p' Makefile


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
# The analysers are upstream releases and none of them is in this repository: cppcheck
# $(CPPCHECK_VERSION) built unmodified from its release archive, checked against a pinned SHA-256,
# GCC -fanalyzer from the Arm GNU Toolchain $(ARM_GCC_ANALYZER_VERSION), compiledb for the
# compilation databases, and the REUSE tool for the licence declarations. The degraves-sast package adds only what no accepted tool does: compiler
# predefines for cppcheck, the database completeness check, pull-request scope from the build's own
# dependency files, ownership from the repository's structure, and the accepted coverage gaps
# check. `make sast-tools` installs all of it into $(SAST_TOOLS_DIR), inside the same pinned
# $(IMAGE) that builds the firmware, and every target below runs there, so the database's compiler
# and the analyser's derived type model come from the same compiler.
#
# SAST_TOOLS says where the package comes from, as a pip requirement: a path to a checkout of the
# degraves SAST tree, or a VCS URL pinned to a commit (git+https://...@<40-hex commit>), so the
# tools cannot change between two runs of the same commit here. It has no default because the
# package has no published location yet, and a default pointing somewhere that does not exist
# would fail later and less clearly.
#
# Every translation unit a configuration compiles is analysed, whoever owns it. Ownership, which
# follows Zephyr's convention, decides only where a finding is fixed and whether it can gate: code
# in a submodule is external, the configuration's build directory is generated, and everything
# else tracked here is first-party, third-party code copied in included. REUSE.toml declares the
# licence and origin of that copied code. analysis/ holds the agreed configurations, the cppcheck
# suppressions and the accepted coverage gaps.
#
# Results are the analysers' own SARIF. Nothing here passes or fails a finding: GitHub code
# scanning merge protection does that, from the analyser's own security-severity (see
# .github/workflows/sast.yml). These targets fail on a missing tool, an empty or incomplete
# database, a path that fits no ownership class, and a coverage failure outside
# analysis/coverage-gaps.json, because each of those makes a clean result a property of the run
# rather than of the code.

SAST_TOOLS_DIR ?= $(PROJECT_BASE)/build/sast-tools
CPPCHECK_VERSION ?= 2.22.0
ARM_GCC_ANALYZER_VERSION ?= 15.2.rel1
JOBS ?= $(shell nproc)

CPPCHECK = $(SAST_TOOLS_DIR)/cppcheck/bin/cppcheck
ARM_GCC_ANALYZER = $(SAST_TOOLS_DIR)/arm-gnu-toolchain-$(ARM_GCC_ANALYZER_VERSION)/bin/arm-none-eabi-gcc
SAST_BIN = $(SAST_TOOLS_DIR)/python/bin
SAST_ENV = PATH="$(SAST_BIN):$$PATH" PYTHONPATH="$(SAST_TOOLS_DIR)/python"
ANALYSIS_POLICY = $(PROJECT_BASE)/analysis
CPPCHECK_SUPPRESSIONS = $(ANALYSIS_POLICY)/cppcheck-suppressions.txt

# Setting BASE, the ref a pull request merges into, makes a run a pull-request run: only the
# first-party translation units the diff reaches through their include sets, plus one includer of
# each changed first-party header none of those includes, at CPPCHECK_ENABLE, the severities that
# can gate, split into SAST_SHARDS parts of which this run takes part SAST_SHARD. Unset, a run is a
# full one: every translation unit at every severity, with MISRA.
BASE ?=
SAST_SHARDS ?= 1
SAST_SHARD ?= 0
CPPCHECK_ENABLE ?= warning
CPPCHECK_ENABLE_FULL = warning,style,performance,portability

# The agreed configurations, named as in analysis/configurations.json. For each: its build
# directory, the directory its make line runs from, that line's variables, the target that builds
# it here, and any part of its build directory that is another configuration's.
#
# The database is compiledb over a dry run of that same make line, with the same variables as the
# real build: a flag that differs from the build's is a database describing a different firmware.
# Measured requirements (SAST-01 proving-cycle-compiledb): run from the directory whose make does
# the compiling, never through make -C; -B, or a built tree lists only out-of-date units; -f, or
# compiledb merges into the previous database; and MICROPY_MPYCROSS, or the dry run includes
# mpy-cross's host compile of the same py/*.c files and those entries replace the target's. The job
# count is left out so the dry run's output is not interleaved.
SAST_CONFIGS = stm32-USB2CAN_NUCLEO_H563ZI stm32-USB2CAN_NUCLEO_H563ZI-mboot \
  mimxrt-USB2CAN_SEEED_ARCH_MIX unix-standard mpy-cross

SAST_BUILD_stm32-USB2CAN_NUCLEO_H563ZI = $(PROJECT_BASE)/src/system/build-USB2CAN_NUCLEO_H563ZI
SAST_MAKEDIR_stm32-USB2CAN_NUCLEO_H563ZI = $(PROJECT_BASE)/src/system
SAST_MAKEVARS_stm32-USB2CAN_NUCLEO_H563ZI = $(call fw_make_vars,stm32,USB2CAN_NUCLEO_H563ZI) \
  MICROPY_MPYCROSS=$(MPY_CROSS)
SAST_GOAL_stm32-USB2CAN_NUCLEO_H563ZI = system PORT=stm32 BOARD=USB2CAN_NUCLEO_H563ZI
SAST_EXCLUDE_stm32-USB2CAN_NUCLEO_H563ZI = --exclude mboot

# The bootloader freezes no Python, so its make line has no MICROPY_MPYCROSS to pass.
SAST_BUILD_stm32-USB2CAN_NUCLEO_H563ZI-mboot = $(PROJECT_BASE)/src/system/build-USB2CAN_NUCLEO_H563ZI/mboot
SAST_MAKEDIR_stm32-USB2CAN_NUCLEO_H563ZI-mboot = $(PROJECT_BASE)/src/system
SAST_MAKEVARS_stm32-USB2CAN_NUCLEO_H563ZI-mboot = BOARD=USB2CAN_NUCLEO_H563ZI PORT=stm32 mboot
SAST_GOAL_stm32-USB2CAN_NUCLEO_H563ZI-mboot = mboot PORT=stm32 BOARD=USB2CAN_NUCLEO_H563ZI

SAST_BUILD_mimxrt-USB2CAN_SEEED_ARCH_MIX = $(PROJECT_BASE)/src/system/build-USB2CAN_SEEED_ARCH_MIX
SAST_MAKEDIR_mimxrt-USB2CAN_SEEED_ARCH_MIX = $(PROJECT_BASE)/src/system
SAST_MAKEVARS_mimxrt-USB2CAN_SEEED_ARCH_MIX = $(call fw_make_vars,mimxrt,USB2CAN_SEEED_ARCH_MIX) \
  MICROPY_MPYCROSS=$(MPY_CROSS)
SAST_GOAL_mimxrt-USB2CAN_SEEED_ARCH_MIX = system PORT=mimxrt BOARD=USB2CAN_SEEED_ARCH_MIX

SAST_BUILD_unix-standard = $(MICROPYTHON_BASE)/ports/unix/build-standard
SAST_MAKEDIR_unix-standard = $(MICROPYTHON_BASE)/ports/unix
SAST_MAKEVARS_unix-standard = $(UNIX_MAKE_VARS) MICROPY_MPYCROSS=$(MPY_CROSS) all
SAST_GOAL_unix-standard = unix-port

SAST_BUILD_mpy-cross = $(MICROPYTHON_BASE)/mpy-cross/build
SAST_MAKEDIR_mpy-cross = $(MICROPYTHON_BASE)/mpy-cross
SAST_MAKEVARS_mpy-cross =
SAST_GOAL_mpy-cross = mpy-cross

# GCC -fanalyzer runs over the ARM configurations only, with the pinned Arm GNU Toolchain used for
# analysis alone. The host configurations (unix-standard, mpy-cross) would need a pinned GCC 15
# host compiler, and none exists here: the build container's host gcc is 12.2, which has neither
# SARIF output nor -fdiagnostics-add-output.
SAST_ARM_CONFIGS = stm32-USB2CAN_NUCLEO_H563ZI stm32-USB2CAN_NUCLEO_H563ZI-mboot \
  mimxrt-USB2CAN_SEEED_ARCH_MIX

# The configuration's analysis artefacts, beside the build they describe, so removing the build
# directory removes them too. Expanded in a recipe, where $* is the configuration.
SAST_DIR = $(SAST_BUILD_$*)/sast

# Running inside the build image. The degraves-sast checkout named by SAST_TOOLS is mounted too
# when it is a local directory, since the image otherwise sees only this project. Variables set on
# the command line or in the environment are passed through, because the image starts with neither.
ifeq ($(RUN_IN_DOCKER), 1)
SAST_PASS_VARS = SAST_TOOLS SAST_TOOLS_DIR CPPCHECK_VERSION ARM_GCC_ANALYZER_VERSION \
  SAST_SKIP_ARM_GCC JOBS BASE SAST_SHARDS SAST_SHARD CPPCHECK_ENABLE
SAST_PASS = $(strip $(foreach v,$(SAST_PASS_VARS),\
  $(if $(filter command line environment,$(origin $(v))),$(v)="$($(v))")))
SAST_TOOLS_MOUNT = $(if $(wildcard $(SAST_TOOLS)/pyproject.toml),\
  -v "$(abspath $(SAST_TOOLS)):$(abspath $(SAST_TOOLS))")
SAST_DOCKER = @$(DOCKER_ENV_FILTER) docker run --rm -v "$$(pwd):$$(pwd)" $(SAST_TOOLS_MOUNT) \
  -w "$$(pwd)" --user="$$(id -u):$$(id -g)" $(DOCKER_VERSION) $(IMAGE) make $@ $(SAST_PASS)
endif

# A missing tool fails with the command that installs it, never as an analysis of nothing.
define sast_require
	@for t in $(1); do test -x "$$t" || { echo "$$t is missing."; \
	  echo "Install the analysis tools first: make sast-tools SAST_TOOLS=<pip requirement for degraves-sast>"; \
	  exit 1; }; done
endef

define sast_require_cppcheck
	$(call sast_require,$(CPPCHECK))
	@v="$$($(CPPCHECK) --version)"; test "$$v" = "Cppcheck $(CPPCHECK_VERSION)" || { \
	  echo "$(CPPCHECK) is $$v, not the pinned Cppcheck $(CPPCHECK_VERSION)."; \
	  echo "Run make sast-tools again."; exit 1; }
endef

define sast_require_file
	@test -s $(1) || { echo "$(1) is missing. Run make compile-commands-$* first."; exit 1; }
endef

.PHONY: sast-tools
sast-tools:  ## Install the pinned analysers and the degraves-sast tools, from SAST_TOOLS=<pip requirement>
	@test -n "$(SAST_TOOLS)" || { \
	  echo "SAST_TOOLS is not set. It is the pip requirement for the degraves-sast package, for"; \
	  echo "example a checkout of the degraves SAST tree: make sast-tools SAST_TOOLS=/path/to/sast"; \
	  exit 1; }
	@case "$(SAST_TOOLS)" in *://*) \
	  echo "$(SAST_TOOLS)" | grep -Eq '@[0-9a-f]{40}(#.*)?$$' || { \
	    echo "SAST_TOOLS=$(SAST_TOOLS) names no commit, so the tools could differ between two runs."; \
	    echo "Pin it to a full commit hash: git+https://host/sast.git@<40-hex commit>"; exit 1; };; \
	esac
ifeq ($(RUN_IN_DOCKER), 1)
	$(SAST_DOCKER)
else
	@# --upgrade because pip --target leaves an existing install alone without it.
	python3 -m pip install --quiet --disable-pip-version-check --no-cache-dir --upgrade \
	  --target $(SAST_TOOLS_DIR)/python "$(SAST_TOOLS)"
	$(SAST_ENV) sast-install-tools --prefix $(SAST_TOOLS_DIR) --cppcheck $(CPPCHECK_VERSION) \
	  $(if $(SAST_SKIP_ARM_GCC),--skip-arm-gcc,--arm-gcc $(ARM_GCC_ANALYZER_VERSION))
endif

# Help lists these per configuration; <cfg> is one of $(SAST_CONFIGS).
#> build-<cfg>                   Build the configuration
#> compile-commands-<cfg>        compile_commands.json and includes.json into <build dir>/sast, checked
#> check-compile-commands-<cfg>  Fail if the database does not explain every object the build produced
#> sast-pr-scope-<cfg>           List the units a pull request reaches, BASE=<ref it merges into>
#> cppcheck-<cfg>                cppcheck full run; with BASE=<ref>, the pull-request run
#> check-cppcheck-<cfg>          Fail on a coverage failure outside analysis/coverage-gaps.json
#> misra-<cfg>                   cppcheck's MISRA addon over every unit, report only, into <build dir>/sast/report
#> fanalyzer-<cfg>               GCC -fanalyzer, ARM configurations only
SAST_TARGETS = build compile-commands check-compile-commands sast-pr-scope cppcheck check-cppcheck \
  misra
.PHONY: $(foreach t,$(SAST_TARGETS),$(addprefix $(t)-,$(SAST_CONFIGS))) \
  $(addprefix fanalyzer-,$(SAST_CONFIGS))

$(addprefix build-,$(SAST_CONFIGS)): build-%:
	$(MAKE) $(SAST_GOAL_$*)

# --- Compilation database
#
# One entry per translation unit the build compiles with its C compiler, from the build's own make
# line. The completeness check then reconciles it against the object files the build produced,
# accounting for assembly the assembler compiles, which compiledb does not record, and for objects
# a deleted source left behind; and it writes each unit's include set from the build's dependency
# files, classifying every unit and every included file by ownership, so a path that fits no class
# fails here. Build the configuration first: the check needs its objects, and the include sets
# need its dependency files.
#
# MAKEFLAGS is cleared so nothing from this make's own command line, job server or dry-run flag
# reaches the replayed make line.
$(addprefix compile-commands-,$(SAST_CONFIGS)): compile-commands-%:
ifeq ($(RUN_IN_DOCKER), 1)
	$(SAST_DOCKER)
else
	$(call sast_require,$(SAST_BIN)/compiledb $(SAST_BIN)/sast-compdb)
	@test -d $(SAST_BUILD_$*) || { \
	  echo "$(SAST_BUILD_$*) does not exist. Build the configuration first: make build-$*"; exit 1; }
	@mkdir -p $(SAST_DIR)
	cd $(SAST_MAKEDIR_$*) && MAKEFLAGS= $(SAST_ENV) \
	  compiledb -f -n -o $(SAST_DIR)/compile_commands.json make -B $(SAST_MAKEVARS_$*)
	@grep -q '"file"' $(SAST_DIR)/compile_commands.json || { \
	  echo "$(SAST_DIR)/compile_commands.json has no entries: compiledb recorded nothing."; exit 1; }
	$(SAST_ENV) sast-compdb --check-db $(SAST_DIR)/compile_commands.json \
	  --build-dir $(SAST_BUILD_$*) $(SAST_EXCLUDE_$*) --root $(PROJECT_BASE) \
	  --includes-out $(SAST_DIR)/includes.json --configuration $*
endif

$(addprefix check-compile-commands-,$(SAST_CONFIGS)): check-compile-commands-%:
ifeq ($(RUN_IN_DOCKER), 1)
	$(SAST_DOCKER)
else
	$(call sast_require,$(SAST_BIN)/sast-compdb)
	$(call sast_require_file,$(SAST_DIR)/compile_commands.json)
	$(SAST_ENV) sast-compdb --check-db $(SAST_DIR)/compile_commands.json \
	  --build-dir $(SAST_BUILD_$*) $(SAST_EXCLUDE_$*) --root $(PROJECT_BASE)
endif

# --- Analysis scope from the manifest chain
#
# The manifest chain is the authority for what is declared: frozen Python modules, and user C module
# directories where the pin supports declaring them. The compilation database is the authority for
# what is actually compiled. Neither supersedes the other, so the resolver cross-checks itself
# against the database and reports disagreement rather than reconciling it silently. Ownership of
# what it resolves comes from the same git conventions as the analysers'.
SCOPE_DIR ?= $(PROJECT_BASE)/build/scope

.PHONY: scope check-scope
scope:  ## Resolve analysis scope from the manifest chain, for every agreed configuration
check-scope:  ## Fail if the manifest chain declares anything the recorded scope does not cover
scope check-scope:
ifeq ($(RUN_IN_DOCKER), 1)
	$(SAST_DOCKER)
else
	$(call sast_require,$(SAST_BIN)/sast-scope)
	$(SAST_ENV) sast-scope --all $(ANALYSIS_POLICY)/configurations.json \
	  --root $(PROJECT_BASE) --out $(SCOPE_DIR) $(if $(filter check-scope,$@),--check)
endif

# --- Licence and origin of copied code
#
# Third-party code copied into this repository is first-party here and analysed and gated like the
# rest. REUSE.toml declares its licence and origin, and the REUSE tool checks that every file has
# licensing information and that every licence named has its text under LICENSES/.
.PHONY: check-reuse
check-reuse:  ## Check the licence declarations (REUSE.toml, LICENSES/) with the REUSE tool
ifeq ($(RUN_IN_DOCKER), 1)
	$(SAST_DOCKER)
else
	$(call sast_require,$(SAST_BIN)/reuse)
	$(SAST_ENV) reuse --root $(PROJECT_BASE) lint
endif

# --- cppcheck
#
# One cppcheck invocation per compiler-flag group of the database, each given the predefines and
# platform file sast-cppcheck-inputs derives from that group's own compiler, since cppcheck cannot
# take them from a cross compiler itself. Paths in the SARIF are repository-relative (-rp), because
# code scanning drops absolute ones; for the same reason suppressions name repository-relative
# paths, and the forced predefines header is suppressed that way, since under -rp an absolute
# suppression path silently matches nothing.
#
# $(1) inputs directory, $(2) SARIF file stem, $(3) analysis cache stem, $(4) severities,
# $(5) further arguments. A run that selected nothing still analyses an empty file, so the
# configuration's category receives a valid SARIF from the tool itself rather than none.
define cppcheck_groups
	@set -e; n=0; \
	for db in $(1)/compile_commands-*.json; do \
	  test -e "$$db" || continue; \
	  i=$${db##*/compile_commands-}; i=$${i%.json}; n=$$((n + 1)); \
	  mkdir -p $(3)-$$i; \
	  echo "cppcheck: $$db"; \
	  $(CPPCHECK) --project=$$db --include=$(1)/predefines-$$i.h --platform=$(1)/platform-$$i.xml \
	    --enable=$(4) --inline-suppr -rp=$(PROJECT_BASE) \
	    --suppress="*:$(patsubst $(PROJECT_BASE)/%,%,$(1))/predefines-$$i.h" \
	    --suppressions-list=$(CPPCHECK_SUPPRESSIONS) $(5) \
	    --cppcheck-build-dir=$(3)-$$i -j$(JOBS) \
	    --output-format=sarif --output-file=$(2)-$$i.sarif; \
	done; \
	if [ $$n -eq 0 ]; then \
	  echo "cppcheck: no translation unit selected in $(1); analysing an empty file."; \
	  : > $(1)/empty.c; \
	  $(CPPCHECK) $(1)/empty.c -rp=$(PROJECT_BASE) \
	    --output-format=sarif --output-file=$(2)-empty.sarif; \
	fi
endef

define sast_pr_scope
	@test -n "$(BASE)" || { echo "BASE is not set: name the ref the pull request merges into, e.g. BASE=origin/main"; exit 1; }
	@mkdir -p $(SAST_DIR)/pr
	$(SAST_ENV) sast-pr-scope --db $(SAST_DIR)/compile_commands.json --root $(PROJECT_BASE) \
	  --build-dir $(SAST_BUILD_$*) --base $(BASE) --out $(SAST_DIR)/pr/compile_commands.json \
	  --report $(SAST_DIR)/pr/scope.json --shards $(SAST_SHARDS) --shard $(SAST_SHARD)
endef

$(addprefix sast-pr-scope-,$(SAST_CONFIGS)): sast-pr-scope-%:
ifeq ($(RUN_IN_DOCKER), 1)
	$(SAST_DOCKER)
else
	$(call sast_require,$(SAST_BIN)/sast-pr-scope)
	$(call sast_require_file,$(SAST_DIR)/compile_commands.json)
	$(sast_pr_scope)
endif

# A pull-request run analyses the selected units at CPPCHECK_ENABLE (warning by default, the
# lowest severity cppcheck tags High). A full run analyses every unit of the database, whoever owns
# it, at every severity. Nothing is suppressed by ownership on either run: results in submodule and
# build directory code are uploaded and stay visible, and they cannot gate because merge protection
# acts only on lines a pull request changes, which those never are.
#
# The run's kind is recorded beside its SARIF, so the coverage check judges the run that made the
# SARIF rather than whatever BASE says when it is invoked.
$(addprefix cppcheck-,$(SAST_CONFIGS)): cppcheck-%:
ifeq ($(RUN_IN_DOCKER), 1)
	$(SAST_DOCKER)
else
	$(call sast_require,$(addprefix $(SAST_BIN)/,sast-cppcheck-inputs sast-pr-scope))
	$(sast_require_cppcheck)
	$(call sast_require_file,$(SAST_DIR)/compile_commands.json)
	$(call sast_require_file,$(SAST_DIR)/includes.json)
	@rm -rf $(SAST_DIR)/pr $(SAST_DIR)/full $(SAST_DIR)/cppcheck/*.sarif $(SAST_DIR)/cppcheck/run
	@mkdir -p $(SAST_DIR)/cppcheck
ifneq ($(BASE),)
	$(sast_pr_scope)
	$(SAST_ENV) sast-cppcheck-inputs $(SAST_DIR)/pr/compile_commands.json $(SAST_DIR)/pr/inputs
	$(call cppcheck_groups,$(SAST_DIR)/pr/inputs,$(SAST_DIR)/cppcheck/results,$(SAST_DIR)/cppcheck/cache/pr,$(CPPCHECK_ENABLE),)
	@echo pr > $(SAST_DIR)/cppcheck/run
else
	$(SAST_ENV) sast-cppcheck-inputs $(SAST_DIR)/compile_commands.json $(SAST_DIR)/full/inputs
	$(call cppcheck_groups,$(SAST_DIR)/full/inputs,$(SAST_DIR)/cppcheck/results,$(SAST_DIR)/cppcheck/cache/full,$(CPPCHECK_ENABLE_FULL),)
	@echo full > $(SAST_DIR)/cppcheck/run
endif
endif

# MISRA over every unit of the database, whoever owns it, with cppcheck's MISRA addon at every
# severity, since cppcheck filters addon results by severity and MISRA's style results need the
# full set to get through. Report-only: the results carry no security-severity and never gate, and
# they stay in the configuration's report/ directory, a CI artefact, rather than going to code
# scanning, which rejects a run of more than 25,000 results and displays 5,000; MISRA over every
# unit runs to tens of thousands. Its own target, and its own CI job, because the addon makes it
# several times slower than the analysis that gates. It shares the full run's cppcheck build
# directory, so after cppcheck-<cfg> only the addon's share of the work is left.
$(addprefix misra-,$(SAST_CONFIGS)): misra-%:
ifeq ($(RUN_IN_DOCKER), 1)
	$(SAST_DOCKER)
else
	$(call sast_require,$(SAST_BIN)/sast-cppcheck-inputs)
	$(sast_require_cppcheck)
	$(call sast_require_file,$(SAST_DIR)/compile_commands.json)
	@rm -rf $(SAST_DIR)/report
	@mkdir -p $(SAST_DIR)/report
	$(SAST_ENV) sast-cppcheck-inputs $(SAST_DIR)/compile_commands.json $(SAST_DIR)/full/inputs
	$(call cppcheck_groups,$(SAST_DIR)/full/inputs,$(SAST_DIR)/report/cppcheck,$(SAST_DIR)/cppcheck/cache/full,$(CPPCHECK_ENABLE_FULL),--addon=misra)
endif

# Configuration coverage, not findings. cppcheck's progress output does not show a unit was
# analysed, so coverage is asserted from the results: no coverage-failure result outside the
# accepted gaps. After a full run it also fails on an accepted gap that no longer occurs, so a gap
# fixed by a tool upgrade is removed rather than left asserting a limitation that no longer exists.
$(addprefix check-cppcheck-,$(SAST_CONFIGS)): check-cppcheck-%:
ifeq ($(RUN_IN_DOCKER), 1)
	$(SAST_DOCKER)
else
	$(call sast_require,$(SAST_BIN)/sast-cppcheck-coverage)
	@run="$$(cat $(SAST_DIR)/cppcheck/run 2>/dev/null)"; \
	case "$$run" in full) full=--full;; pr) full=;; *) \
	  echo "$(SAST_DIR)/cppcheck/run is missing, so no cppcheck run finished. Run make cppcheck-$* first."; \
	  exit 1;; esac; \
	echo "checking the $$run run's SARIF"; \
	$(SAST_ENV) sast-cppcheck-coverage --accepted $(ANALYSIS_POLICY)/coverage-gaps.json \
	  --configuration $* $$full $(SAST_DIR)/cppcheck/*.sarif
endif

# --- GCC -fanalyzer
#
# The second analyser. Each database entry is replayed with the pinned GCC 15, which writes its
# own SARIF to a named file per unit. Full runs only, and report-only until its findings are
# triaged.
$(addprefix fanalyzer-,$(SAST_ARM_CONFIGS)): fanalyzer-%:
ifeq ($(RUN_IN_DOCKER), 1)
	$(SAST_DOCKER)
else
	$(call sast_require,$(SAST_BIN)/sast-fanalyzer $(ARM_GCC_ANALYZER))
	$(call sast_require_file,$(SAST_DIR)/compile_commands.json)
	@rm -rf $(SAST_DIR)/fanalyzer
	$(SAST_ENV) sast-fanalyzer --db $(SAST_DIR)/compile_commands.json --gcc $(ARM_GCC_ANALYZER) \
	  --root $(PROJECT_BASE) --out $(SAST_DIR)/fanalyzer --jobs $(JOBS)
endif

$(addprefix fanalyzer-,$(filter-out $(SAST_ARM_CONFIGS),$(SAST_CONFIGS))): fanalyzer-%:
	@echo "$* is a host configuration: -fanalyzer needs a pinned GCC 15 host compiler, which the build"; \
	echo "image does not provide, so it runs over the ARM configurations only."; exit 1

# The unsuffixed names are the firmware configuration for the current PORT and BOARD.
SAST_FW = $(PORT)-$(BOARD)

.PHONY: compile-commands check-compile-commands sast-pr-scope cppcheck check-cppcheck misra \
  fanalyzer
compile-commands:  ## Generate the firmware configuration's compilation database (build it first)
compile-commands: compile-commands-$(SAST_FW)
check-compile-commands:  ## Reconcile the firmware configuration's database against its build output
check-compile-commands: check-compile-commands-$(SAST_FW)
sast-pr-scope:  ## List the firmware configuration's units a pull request reaches (BASE=<ref>)
sast-pr-scope: sast-pr-scope-$(SAST_FW)
cppcheck:  ## Analyse the firmware configuration with cppcheck (BASE=<ref> for a pull-request run)
cppcheck: cppcheck-$(SAST_FW)
check-cppcheck:  ## Fail on a coverage failure in the firmware configuration outside the accepted gaps
check-cppcheck: check-cppcheck-$(SAST_FW)
misra:  ## MISRA over the firmware configuration's every unit, report only
misra: misra-$(SAST_FW)
fanalyzer:  ## Analyse the firmware configuration with GCC -fanalyzer
fanalyzer: fanalyzer-$(SAST_FW)


.PHONY: clean
clean:  ## Delete compiled artifacts
clean:
	@echo "$(BOLD)Cleaning MicroPython ...$(RESET)"
	rm -rf $(MICROPYTHON_BASE)/mpy-cross/build
	rm -rf $(MICROPYTHON_BASE)/ports/unix/build-$(UNIX_VARIANT)
	rm -rf $(MICROPYTHON_BASE)/ports/stm32/build-*
	rm -rf src/system/build-$(BOARD)
	rm -rf src/unix/build


