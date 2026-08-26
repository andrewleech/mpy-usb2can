import os

# Include MicroPython asyncio
include("$(MPY_DIR)/extmod/asyncio/manifest.py")

# Include libraries
include("libs/manifest.py", platform_baremetal=options.platform_baremetal)

# Include application code (unless EXCLUDE_APP=1)
if os.getenv("EXCLUDE_APP", "0") != "1":
    include("firmware/manifest.py", platform_baremetal=options.platform_baremetal)
