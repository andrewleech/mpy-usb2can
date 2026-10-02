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
make sast-tools                    # install the analysers once, into build/sast-tools

make build-<cfg>                    # build the configuration
make compile-commands-<cfg>         # compilation database, checked against the build
make check-compile-commands-<cfg>   # re-run that completeness check alone
make cppcheck-<cfg>                 # cppcheck over every unit
make check-cppcheck-<cfg>           # fail on a coverage failure outside analysis/coverage-gaps.json
make codechecker-<cfg>              # CodeChecker's Clang Static Analyzer over every unit
make check-codechecker-<cfg>        # fail unless it analysed every unit, outside the accepted gaps
make misra-<cfg>                    # MISRA over every unit, report only, into sast/report/
make fanalyzer-<cfg>                # GCC -fanalyzer, ARM configurations only
make check-reuse                    # licence and origin declarations, with the REUSE tool
```

`compile-commands`, `cppcheck`, `check-cppcheck`, `codechecker`, `fanalyzer` and the others without
a suffix are the firmware configuration for the current `BOARD` and `PORT`.

The analysers and the tools these targets call are **not in this repository**. `make sast-tools`
installs them into `build/sast-tools`, inside the pinned build image like every other target here:
cppcheck 2.22.0 built unmodified from its release archive, checked against a pinned SHA-256; the
Arm GNU Toolchain 15.2.Rel1 for `-fanalyzer`, checked against Arm's published SHA-256; CodeChecker
6.25.1 with every Python package it installs pinned by SHA-256, and clang 20.1.8 from the LLVM
release archive, checked against a pinned SHA-256; compiledb; the REUSE tool; and the
`mpy_analysis` package from the micropython submodule (`src/micropython/tools/mpy_analysis`), so
the tools are at the same commit as the code they analyse. Every
analysis target fails with the install command when a tool is missing, and cppcheck is checked
against its pinned version.

Output lands in each configuration's build directory under `sast/` (`compile_commands.json`,
`cppcheck/*.sarif`, `codechecker/results.sarif`, `codechecker/reports/`, `report/*.sarif`,
`fanalyzer/`), so removing a build directory removes its analysis output too. `make clean` removes
the current `BOARD`'s build directory and the unix and mpy-cross builds; the other board's needs
`make clean BOARD=USB2CAN_SEEED_ARCH_MIX`.

#### What is analysed

Every run analyses every translation unit a configuration compiles, whoever owns it: this
repository's code, the MicroPython submodule and its libraries, and the generated code in the build
directory. Assembly is the exception (Known limitations). Python is not analysed yet: the frozen
modules each configuration embeds, third-party ones included, are in scope, and the tool for them
is not decided.

#### Ownership

Ownership follows Zephyr's convention and is read from the path; nothing computes it:

| Class | What it is | Findings |
|---|---|---|
| First-party | Tracked in this repository, including third-party code copied into it | Gate |
| External | In a git submodule (`src/micropython`, `src/libs/*`) | Uploaded and visible; fixed and gated in the repository that owns the code |
| Generated | Inside the configuration's build directory | Uploaded and visible; the fix is in the generator or its input |

It decides where a finding is fixed. It gates through the platform, not through anything here: merge protection acts only on lines a pull request changes, and submodule and build directory code is never in this repository's diff.

To add a path:
- A new first-party directory or file needs nothing: tracked and not in a submodule is
  first-party.
- A new submodule needs nothing: it is external. Its findings gate in its own repository.
- Third-party code copied into this repository is first-party: it is analysed and gated like the
  rest. Record its copyright, licence and origin in `REUSE.toml`, and add the licence text under
  `LICENSES/` if it is not MIT; `make check-reuse` fails on a licence without its text.
- A new configuration gets its variables in the Makefile's static analysis section, including its
  build directory, and a matrix entry in `.github/workflows/sast.yml` and `.gitlab-ci.yml`.

Moving a first-party path into a submodule takes a reason in the commit, since it takes that code
out of this repository's gate.

#### Runs

Every run, pull request or not, analyses every unit of the configuration with cppcheck at
`warning,style,performance,portability` and with CodeChecker's Clang Static Analyzer. Off pull
requests, the ARM configurations also run GCC `-fanalyzer`. `make misra-<cfg>` runs cppcheck's
MISRA C:2012 addon over every unit into `sast/report/`, report-only; CI runs it off pull requests
in a job of its own, because the addon makes it several times slower than the analysis that gates,
and keeps its results as that job's artefact, kept 90 days: MISRA over every unit runs to tens of
thousands of results, and code scanning rejects a run of more than 25,000 and displays 5,000. Its
cppcheck coverage is asserted against `analysis/coverage-gaps.json` the same way as cppcheck's own
run, so an aborted addon fails it; the addon's own incompleteness is not (Known limitations). CI
runs on pull requests, pushes to `main` and `sast`, weekly and on demand.

#### CodeChecker and GitLab Advanced SAST

CodeChecker driving the Clang Static Analyzer is the engine of GitLab Advanced SAST's C/C++ job
(`gitlab-advanced-sast-cpp`, analyzer image `clangsa`). `make codechecker-<cfg>` runs the releases
that job's image carries, CodeChecker 6.25.1 and clang 20.1.8, with its default checker selection,
CodeChecker's `sensitive` profile over the `clangsa` analyzer alone, over the same compilation
database, without GitLab's own image, which is licensed under the GitLab EE licence, and on an
instance too old to have Advanced SAST. CodeChecker asks each entry's compiler (`arm-none-eabi-gcc`
or the host `gcc`) for its target and implicit include directories, so clang parses the ARM units
for `arm-none-eabi`. For the host configurations it reports at least what that job would (it keeps the build's `-DNDEBUG`, which that job drops). For the ARM
configurations it does not: GitLab's `clangsa` image has no `arm-none-eabi-gcc`, and without it
CodeChecker fails every unit (measured on the bootloader: `unknown target CPU cortex-m33`). Where clang cannot compile a unit that GCC
compiles, CodeChecker records the unit as failed with clang's error, and the unit is not analysed:
`make check-codechecker-<cfg>` fails unless every unit of the run's database is in CodeChecker's
record of the run (`codechecker/reports/metadata.json`) as analysed, or as failed on an error that
the `codechecker` list of `analysis/coverage-gaps.json` accepts for that unit and configuration.

Its SARIF is CodeChecker's own export, which names files by absolute path; GitHub's upload
relativises them against the directory the analysis ran in. Its results carry no security
severity, so on GitHub they are ordinary alerts under `codechecker/<cfg>` and do not gate.

#### What gates

Nothing in this repository decides whether a finding blocks a merge. CI uploads each
configuration's SARIF to GitHub code scanning under the categories `cppcheck/<cfg>`,
`codechecker/<cfg>` and `gcc-analyzer/<cfg>`, and a repository ruleset fails a pull request on a
new cppcheck alert whose security severity is High or higher: cppcheck's CWE-tagged `warning`
(8.5) and `error` (9.9)
findings. Alerts already on the base branch do not block, and dismissing one records a reason.
Merge protection only acts on alerts whose lines are all in the pull request's diff, which is what
keeps submodule and generated findings out of the gate: neither is ever in this repository's diff.
MISRA, CodeChecker and `-fanalyzer` results carry no security severity and never gate. MISRA results are in the
`misra` CI job's artefact (`sast/report/`), kept 90 days, not in code scanning, so none of them,
first-party included, has a triage trail there.

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
empty or does not explain every object the build produced, or cppcheck or CodeChecker could not
analyse a unit outside `analysis/coverage-gaps.json`. A separate job fails when `make check-reuse`
does.

GitLab runs the same targets (`.gitlab-ci.yml`) and keeps the SARIF and databases as artefacts,
but its Vulnerability Report and merge request approval policies read GitLab's own report schema,
not SARIF, so on GitLab the findings neither appear there nor block a merge request.

#### Known limitations

- Pull requests do not run `-fanalyzer`, so they upload no `gcc-analyzer/<cfg>` category. How code
  scanning reports a category the base branch has and a pull request lacks has not been observed
  yet; the ruleset requires only the Cppcheck tool. Check it on the first real pull request.
- A pull request from a fork gets no `security-events: write` token, so its results cannot be
  uploaded and the merge protection check waits on them.
- The upload job runs only when every analysis job passed. One failing configuration uploads
  nothing for any configuration, because a partial upload would mark the unanalysed units'
  alerts fixed.
- The ruleset above covers the default branch only; pull requests into other branches, `sast`
  included, are analysed and uploaded but not gated.
- Assembly sources are compiled but not analysed: cppcheck reads C, and GCC `-fanalyzer` only
  preprocesses and assembles them. mimxrt's two `.S` units are in its database, so they reach
  cppcheck, which skips them without a result, and CodeChecker, which skips them without a record;
  `make check-codechecker-<cfg>` lists them as not analysed.
- The Clang Static Analyzer analyses only what clang compiles. Clang rejects a statement other than
  inline assembly in a naked function, which GCC accepts, so MicroPython's `ports/stm32/mboot/main.c`
  (bootloader) and `ports/stm32/powerctrl.c` (firmware) are not analysed by it; both are accepted
  gaps in `analysis/coverage-gaps.json`, and cppcheck and `-fanalyzer` still analyse them.
- CodeChecker drops `-DNDEBUG` by design, which every configuration except mpy-cross carries; the
  Makefile passes it back through `--saargs`, so assertion-guarded paths are analysed as built.
  GitLab's job does not, and reports fewer results. clang's own predefines (`__GNUC__` 4, so
  MicroPython takes its pre-GCC-5 overflow and fallthrough code) and type model (enum size,
  `int32_t`) still differ from the image's gcc.
- CodeChecker 6.25.1's SARIF names files by absolute path. GitHub's upload relativises them against
  the analysis job's checkout path; a local run's SARIF keeps the local absolute paths.
- cppcheck's MISRA addon implements MISRA C:2012 partially; its results carry rule numbers only.
- Where the MISRA addon could not check part of a unit it reports `misra-config` ("misra checking
  is incomplete"), which is in the report but fails nothing. On 2026-09-30: firmware 14, mimxrt 34,
  unix-standard 73, mpy-cross 2, bootloader 0.
- `REUSE.toml` gives every file the repository's licence unless an annotation says otherwise, so
  copied code whose files carry no SPDX identifier is only declared if someone adds its
  annotation.

#### Policy files

`analysis/` holds this project's own analysis policy: `cppcheck-suppressions.txt` for findings
judged suppressible, with a reason per entry, and `coverage-gaps.json` for the coverage gaps
accepted, cppcheck's under `accepted` and CodeChecker's under `codechecker`. `REUSE.toml` and
`LICENSES/` declare the licence and origin of every file, and of copied code in particular.

### Testing
```bash
make tests               # unit tests on the unix port
make test=test_version tests
make integration-tests   # Robot Framework
make checks              # pre-commit: ruff, mypy, yamllint
```

## Build Environment

Builds run inside `micropython/build-micropython-arm`, the official MicroPython ARM toolchain
container and the image mpbuild selects for the stm32 port, pinned by digest in the Makefile's
`IMAGE` and in the CI configurations. The Makefile sets `RUN_IN_DOCKER=0` when `/.dockerenv`
exists or `CI` is set, so the same targets work inside a container and in GitHub Actions. Override
the image with `make IMAGE=... system`.

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
