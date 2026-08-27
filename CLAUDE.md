# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`gs_usb`-compatible USB-CAN adapter firmware written in MicroPython, targeting the
NUCLEO-H563ZI (STM32H563ZI). The intent is that the mainline Linux `gs_usb` kernel module and
candleLight-compatible hosts drive the device unmodified, with a fully permissive dependency
tree (MicroPython and TinyUSB are both MIT).

Currently a project skeleton: build system, board definition and test harness are in place;
no CAN or USB device code has been written yet.

## Essential Commands

### Building
```bash
make submodules     # fetch micropython and libraries (shallow)
make system         # build application firmware
make mboot          # build the bootloader
make libs-only      # build with frozen libraries only, no application
make deploy-dfu     # flash over DFU
make clean          # remove build output
```

Output lands in `src/system/build-USB2CAN_NUCLEO_H563ZI/`.

### Testing
```bash
make tests               # unit tests on the unix port
make test=test_version tests
make integration-tests   # Robot Framework
make checks              # pre-commit: ruff, mypy, yamllint
```

## Build Environment

Builds run inside `micropython/build-micropython-arm`, the official MicroPython ARM toolchain
container and the image mpbuild selects for the stm32 port. The Makefile sets
`RUN_IN_DOCKER=0` when `/.dockerenv` exists or `CI` is set, so the same targets work inside a
container and in GitHub Actions. Override the image with `make IMAGE=... system`.

**mpbuild does not currently drive this project.** It mounts only the MicroPython repository
root into the build container and discovers boards by globbing
`ports/*/boards/*/board.json`. This project keeps the board definition
(`src/system/USB2CAN_NUCLEO_H563ZI`) and the frozen sources (`src/firmware`, `src/libs`)
outside `src/micropython`, so neither the board nor its sources are visible to a mpbuild
build. Making mpbuild work here needs a change to mpbuild itself: mount the git superproject
when MicroPython is a submodule, and accept board directories outside the tree.

## Architecture

### Source structure
- **src/firmware/application/** - frozen application code
  - `boot.py` - filesystem (LFS2) and USB mode setup, runs before `main.py`
  - `main.py` - entry point; initialises logging and config, calls `device.run()`
  - `device.py` - task setup and the asyncio event loop
  - `hardware.py` - hardware objects, kept as the single place hardware is touched
  - `device_config.py` - runtime configuration structure and defaults
  - `repl.py` - namespace exposed to `aiorepl`
  - `version.py` - version, generated into the build by `tools/makeversionhdr.py`
- **src/firmware/test/** - unit tests, executed on the unix port
- **src/system/** - board definition, `mpconfigproj.mk`, build output
- **src/libs/** - library submodules frozen into firmware
- **src/unix/simulation/** - minimal stand-ins so the application can run on the unix port
- **src/integration-tests/** - Robot Framework tests

### Manifests
`src/manifest.py` is the root; it pulls in `libs/manifest.py` and, unless `EXCLUDE_APP=1`,
`firmware/manifest.py`. The board's `manifest.py` includes the root with
`platform_baremetal=True`; `src/unix/manifest.py` includes it with `platform_baremetal=False`
and adds the simulation modules. Test code and the unittest library are frozen only when not
baremetal.

## Hardware Notes

- FDCAN1 is on PD1 (TX) and PD0 (RX); FDCAN2 is on PB13 (TX) and PB12 (RX), all AF9, per
  `boards/stm32h573_af.csv`. Board-level solder bridge and jumper configuration of these pins
  is unverified; check the board user manual before wiring a transceiver.
- The Nucleo has no onboard CAN transceiver. Dual-channel needs two external CAN-FD
  transceivers.
- FDCAN message RAM on the H5 has a fixed per-instance SRAMCAN layout, set by compile-time
  constants in the HAL rather than configured at init: 28 standard filters, 3 elements per Rx
  FIFO, 3 in the Tx FIFO/queue. There is no `MessageRAMOffset` field as there is on H7, so
  there is nothing to partition and nothing to assert on. The binding constraint is the depth:
  3 elements per Rx FIFO leaves very little slack for a frame-forwarding adapter.
- H5 FDCAN is not enabled in the port. `MICROPY_HW_ENABLE_FDCAN` is defined only for
  STM32G0/G4/H7/N6 (`mpconfigboard_common.h:746`), and the Makefile's CAN HAL selection
  (`Makefile:426-431`) matches `f0 f4 f7`, `g0 g4 h7 n6` and `l4` but not `h5`, so no CAN HAL
  is compiled at all. Enabling it is a port change, and an upstreamable one.
- Upstream marks the H5 mboot linker configuration as untested. Keep ST-LINK flashing
  available as a fallback.

## MicroPython Specifics

- Frozen modules are used throughout for memory efficiency.
- Zero-allocation steady state matters here: a `gs_usb` device that drops frames during a GC
  sweep is worse than one that is merely slow. Pre-allocate buffers and use `memoryview` on
  any hot path.
- Type hints are enabled via the `micropython-stm32-stubs` package (dev dependency group).

## Dependency Management

`pyproject.toml` uses PEP 735 dependency groups. Install with `uv sync --group dev`.

## Working with Submodules

`src/micropython` tracks upstream MicroPython. Changes to core belong upstream: fork with
`gh repo fork`, branch, and open a PR against `micropython/micropython`. Reference
`CLAUDE_micropython.md` for MicroPython-specific guidance. The same applies to the other
library submodules; update the pointer here only after upstream accepts.
