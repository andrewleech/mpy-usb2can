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

### Static analysis

C static analysis runs per build configuration. The agreed configurations are
`stm32-USB2CAN_NUCLEO_H563ZI` (firmware), `stm32-USB2CAN_NUCLEO_H563ZI-mboot` (bootloader),
`mimxrt-USB2CAN_SEEED_ARCH_MIX` (second board), `unix-standard` and `mpy-cross`; `make help` lists
them and the targets below.

```bash
make sast-tools SAST_TOOLS=/path/to/sast   # install the analysers once, into build/sast-tools

make build-<cfg>                    # build the configuration
make compile-commands-<cfg>         # compilation database and header sets, checked against the build
make check-compile-commands-<cfg>   # re-run that completeness check alone
make cppcheck-<cfg>                 # full run: every unit, every severity, MISRA on first-party
make cppcheck-<cfg> BASE=origin/main   # pull-request run: the units a diff against BASE reaches
make check-cppcheck-<cfg>           # fail on a coverage failure outside analysis/coverage-gaps.json
make fanalyzer-<cfg>                # GCC -fanalyzer, ARM configurations only
make scope                          # resolve analysis scope from the manifest chain
make check-scope                    # fail if the manifest declares what the scope does not cover
```

`compile-commands`, `cppcheck`, `check-cppcheck`, `fanalyzer` and the others without a suffix are
the firmware configuration for the current `BOARD` and `PORT`.

The analysers and the tools these targets call are **not in this repository**. `make sast-tools`
installs them into `build/sast-tools`, inside `micropython/build-micropython-arm` like every other
target here: cppcheck 2.22.0 built unmodified from its release tag, the Arm GNU Toolchain
15.2.Rel1 for `-fanalyzer`, compiledb, and the `degraves-sast` package from the degraves SAST
tree. `SAST_TOOLS` is a pip requirement for that package, a path to a checkout or a VCS URL, and
has no default. Every analysis target fails with the install command when a tool is missing, and
cppcheck is checked against its pinned version.

Output lands in each configuration's build directory under `sast/` (`compile_commands.json`,
`includes.json`, `cppcheck/*.sarif`, `fanalyzer/`), and `make clean` removes it with the build. The
exception is `make scope`, whose artefacts span configurations and go to `build/scope`.

#### Ownership

Every path a configuration compiles or includes is in exactly one class, from conventions git and
GitHub already use:

| Class | Declared by | Findings |
|---|---|---|
| External | Inside a git submodule (`src/micropython`, `src/libs/*`) | Uploaded and visible; never gate here |
| Vendored | `.gitattributes` `linguist-vendored` | Uploaded and visible; never gate |
| Generated | `.gitattributes` `linguist-generated` (the build directories) | Uploaded and visible; never gate |
| First-party | Tracked in this repository and none of the above | Gate |

Anything else fails the run rather than falling into a class: a compiled file that is untracked,
outside every submodule and not marked generated, or an ownership attribute with a value other
than set or unset. Today the first-party C is `src/system/USB2CAN_NUCLEO_H563ZI/mboot_footer.c`
and the board headers.

To add a path:
- A new first-party directory or file needs nothing: tracked and not in a submodule is
  first-party.
- A new submodule needs nothing: it is external. Its findings gate in its own repository.
- Third-party code copied into this repository gets a `.gitattributes` line such as
  `src/libs/foo/** linguist-vendored`.
- A new build output location gets a `linguist-generated` line, like the `src/system/build-*`
  line already there.
- A new configuration gets its variables in the Makefile's static analysis section, an entry in
  `analysis/configurations.json`, and a matrix entry in `.github/workflows/sast.yml` and
  `.gitlab-ci.yml`.

Moving a first-party path into a class that never gates takes a reason in the commit, since it
takes that code out of the gate.

#### Pull-request and full runs

A pull-request run (`BASE` set) analyses the first-party translation units the diff reaches
through their include sets, plus one unit that includes each changed first-party header none of
those does, at cppcheck's `warning` severity and above, with results in vendored and generated
paths suppressed. A change to a board header reaches almost every unit, because the port's
configuration includes it, so such a pull request costs about a full run. A full run analyses
every unit of the configuration at every severity, in two passes over disjoint sets of units: the
units that are not first-party without MISRA, and the first-party units with cppcheck's MISRA
C:2012 addon, keeping only first-party MISRA results. The ARM configurations also run GCC
`-fanalyzer`. CI runs pull-request runs on pull requests and full runs on pushes to `main` and
`sast`, weekly and on demand.

#### What gates

Nothing in this repository decides whether a finding blocks a merge. CI uploads each
configuration's SARIF to GitHub code scanning under the categories `cppcheck/<cfg>` and
`gcc-analyzer/<cfg>`, and a repository ruleset fails a pull request on a new cppcheck alert whose
security severity is High or higher: cppcheck's CWE-tagged `warning` (8.5) and `error` (9.9)
findings. Alerts already on the base branch do not block, and dismissing one records a reason.
Merge protection only acts on alerts whose lines are all in the pull request's diff, which is what
keeps submodule findings out of the gate. MISRA and `-fanalyzer` results carry no security
severity and never gate.

The ruleset is a repository setting. It is created once, by a repository admin:

```bash
gh api --method POST repos/{owner}/{repo}/rulesets --input - <<'EOF'
{
  "name": "Code scanning: cppcheck",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
  "rules": [{
    "type": "code_scanning",
    "parameters": {"code_scanning_tools": [
      {"tool": "Cppcheck", "security_alerts_threshold": "high_or_higher", "alerts_threshold": "none"}
    ]}
  }]
}
EOF
```

CI also fails, independently of any finding, when a tool is missing, a compilation database is
empty or does not explain every object the build produced, or cppcheck reports a coverage failure
(a unit it could not read) outside `analysis/coverage-gaps.json`.

GitLab runs the same targets (`.gitlab-ci.yml`) and keeps the SARIF and databases as artefacts,
but its Vulnerability Report and merge request approval policies read GitLab's own report schema,
not SARIF, so on GitLab the findings neither appear there nor block a merge request.

#### Known limitations

- A pull-request run uploads a subset of units to the same categories as a full run, so alerts on
  the base branch in units the pull request did not analyse are listed as fixed in its code
  scanning summary. They are not fixed, and they do not affect the merge check.
- A pull request from a fork gets no `security-events: write` token, so its results cannot be
  uploaded and the merge protection check waits on them.
- The upload job runs only when every analysis job passed. One failing configuration uploads
  nothing for any configuration, because a partial upload would mark the unanalysed units'
  alerts fixed.
- The ruleset above covers the default branch only; pull requests into other branches, `sast`
  included, are analysed and uploaded but not gated.

#### Policy files

`.gitattributes` holds the ownership lines above. `analysis/` holds the rest of this project's
own policy: `configurations.json` for the agreed build configurations,
`cppcheck-suppressions.txt` for findings judged suppressible, with a reason per entry, and
`coverage-gaps.json` for the coverage gaps accepted.

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
