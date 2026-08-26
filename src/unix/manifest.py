# # Include all code for the main application and libraries.
include("../firmware/manifest.py", platform_baremetal=False)
include("../libs/manifest.py", platform_baremetal=False)
freeze("simulation")
