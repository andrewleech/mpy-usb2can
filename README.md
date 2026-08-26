# mpy-usb2can

`gs_usb`-compatible USB-CAN adapter firmware, written in MicroPython, targeting the NUCLEO-H563ZI.

The goal is a dual-channel CAN-FD adapter that the mainline Linux `gs_usb` kernel module and
candleLight-compatible hosts drive without modification, on stock hardware and with a fully
permissive dependency tree.

Status: project skeleton only. No CAN or USB device code yet.

## Hardware

- **NUCLEO-H563ZI** (STM32H563ZI, Cortex-M33 @ 250 MHz, 2 MB flash, 640 KB SRAM).
- Two FDCAN instances: FDCAN1 on PD0/PD1, FDCAN2 on PB12 with PB13/PB6.
  Using PB13 requires jumper JP6 to be removed.
- The board has no CAN transceiver. Two external CAN-FD transceivers (e.g. MCP2542) are
  required for dual-channel operation.

Pin assignment is not yet in the board definition; the copied `mpconfigboard.h` has no
`MICROPY_HW_CAN*` defines.

## Layout

```
src/firmware/application  frozen application code
src/firmware/test         unit tests, run on the unix port
src/integration-tests     Robot Framework tests
src/libs                  library submodules
src/micropython           MicroPython (submodule, upstream master)
src/system                board definition and build output
tools                     build helpers
```

## Building

Builds run inside the official MicroPython ARM toolchain container
(`micropython/build-micropython-arm`, the image mpbuild selects for the stm32 port). The
Makefile detects when it is already inside a container or CI and builds directly instead.

```bash
make submodules     # fetch micropython and libraries
make system         # build the application firmware
make mboot          # build the bootloader
make deploy-dfu     # flash over DFU
```

Firmware lands in `src/system/build-USB2CAN_NUCLEO_H563ZI/`.

`mpbuild` cannot drive this layout as-is: it mounts only the MicroPython repository into the
build container, whereas the board definition and frozen sources live outside it under
`src/`. See the build notes in `CLAUDE.md`.

## Testing

```bash
make tests               # unit tests on the unix port
make integration-tests   # Robot Framework tests
make checks              # pre-commit: ruff, mypy, yamllint
```

The unix port carries a minimal `machine` stand-in under `src/unix/simulation` covering only
what the application touches. Anything needing real CAN or USB behaviour belongs in an
on-target test.

## Versioning

[git-versioner](https://pypi.org/project/git-versioner/) derives the version from git. On a
tag it is used as-is; otherwise the semantic version is auto-incremented. Control the
increment with a `CHANGE: major|minor|patch` commit footer, or the `VERSION_INCREMENT`
environment variable.

```python
import version
print(version.firmware_version)
print(version.bootloader_version)
```

## Configuration

Runtime configuration uses
[structured_config](https://gitlab.com/alelec/micropython-structured-config): config classes
with defaults and validation in `src/firmware/application/device_config.py`, overridden by a
JSON file on the device filesystem.

## Provenance

Generated from a Copier template; `.copier-answers.yml` records the answers. The generated
tree has since been reworked (GitHub Actions, upstream MicroPython submodules, no
devcontainer), so `copier update` is not a supported path.
