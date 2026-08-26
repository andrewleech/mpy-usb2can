# micropython-lib packages
require("functools")
require("logging")
require("os-path")
require("copy")
require("pathlib")
require("aiorepl")

# local libraries
add_library("libs", ".")  # register the src/lib folder as libs to add to the dirs require() searches in.

freeze("structured_config", "structured_config/__init__.py", opt=0)
freeze("structured_config", "structured_config/structured_config.py", opt=0)

freeze(".", ("typing.py"), opt=0)

# unittest library
if not options.platform_baremetal:
    include("micropython_unittest_junit/manifest.py")
