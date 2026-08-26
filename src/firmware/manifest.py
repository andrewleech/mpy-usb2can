# Freeze the application
freeze("application", opt=0)

# Freeze the tests - this is only done when not on the device
# for example when running tests on the unix port.
if not options.platform_baremetal:
    freeze("test", opt=0)
