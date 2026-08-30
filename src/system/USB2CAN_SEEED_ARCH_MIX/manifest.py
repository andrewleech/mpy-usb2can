# The port's own _boot.py mounts the internal flash at /flash and chdirs
# there. Unlike the stm32 board, whose application boot.py mounts its own
# LFS2 through pyb.Flash, nothing else on this port does that, so without
# this the board comes up with no filesystem at all.
freeze("$(PORT_DIR)/modules")

# Include code for the main application and libraries.
# This includes asyncio, libs, and (unless EXCLUDE_APP=1) firmware.
include("../../manifest.py", platform_baremetal=True)
