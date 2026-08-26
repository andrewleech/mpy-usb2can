import logging

try:
    from mptools.mpr import PyboardExtended
except ImportError:
    # TODO When micropython includes mpr import it here if there is none available.
    pass

try:
    from pyboardunixprocess import PyboardUnixProcess
except ImportError:
    # Assume we are running on Windows which doesn't have termios which pty needs,
    # and this module uses pty.
    pass


logger = logging.getLogger(__name__)
logging.basicConfig()


class MicropythonLibrary:
    ROBOT_LIBRARY_SCOPE = "GLOBAL"

    def __init__(self, port, use_unix_port):
        self._last_command_result = None
        self._micropython_version = None
        self.port = port
        self.use_unix_port = use_unix_port
        self.pyboard = None
        self._connect_to_device()

    def _connect_to_device(self):
        cls = PyboardUnixProcess if self.use_unix_port else PyboardExtended
        # Connect to the board.
        pyboard = cls(self.port)
        # This must be set here, there may be a concurrent task
        # that requires it.
        self.pyboard = pyboard

        pyboard.enter_raw_repl()

        # Reset the micropython kernel if running on device.
        if not self.use_unix_port:
            pyboard.exec_("import machine")
            pyboard.exec_("machine.soft_reset()")

        # Query the micropython version.
        pyboard.exec_("import sys")
        version = pyboard.exec_("print(sys.implementation.version)")

        pyboard.mount_local("./tests/drivers")

        # Device is active
        self._micropython_version = version.decode(encoding="ascii").strip()
        logging.info("Micropython version: %s" % self._micropython_version)

    def is_connected(self):
        return self.pyboard is not None

    def force_connection_close(self):
        self._disconnect_from_device()

    def _disconnect_from_device(self):
        if self.pyboard is not None:
            # Try to exit the raw REPL
            try:
                self.pyboard.exit_raw_repl()
            except Exception:
                pass

            # Close off the serial connection
            try:
                self.pyboard.close()
            except Exception:
                pass
        self.pyboard = None
        self._micropython_version = None

    def __del__(self):
        if self.pyboard is not None:
            # Try to exit the raw REPL
            try:
                self.pyboard.exit_raw_repl()
            except Exception:
                pass

            # Close off the serial connection
            try:
                self.pyboard.close()
            except Exception:
                pass

    def execute_command(self, cmd):
        logging.debug("MicropythonLibrary.execute_command: command: %s" % cmd)
        # This command may take a while so increase the timeout.
        result = self.exec_ascii(cmd, timeout=30)

        # Filter out console logs.
        lines = result.split("\n")
        log_levels = "DEBUG: INFO: WARN: WARNING: ERROR: EXCEPTION:".split()
        result_filtered = "\n".join(
            line for line in lines if not any(ll in line for ll in log_levels)
        )

        # Check for paste mode.
        paste_mode = "paste mode; Ctrl-C to cancel, Ctrl-D to finish"
        if paste_mode in result_filtered:
            # "paste_mode" is the first line.
            # Second line is our command.
            # Third line onwards is the output we actually want.
            lines = result_filtered.split("\n")
            result_filtered = "\n".join(lines[2:])

        self._last_command_result = result_filtered
        return result_filtered

    def last_command_result(self):
        return self._last_command_result

    def exec(self, cmd):
        return self.pyboard.exec_(cmd)

    def exec_ascii(self, cmd, timeout=10):
        # Call exec_raw so we can specify a timeout.
        ret, ret_err = self.pyboard.exec_raw(cmd, timeout=timeout)
        if ret_err:
            raise Exception("pyboard.exec_raw", ret, ret_err)
        response = ret.decode(encoding="ascii").strip()
        if "Traceback" in response:
            raise Exception(response)
        return response

    def reset_micropython(self):
        """
        Used at the start of a new test
        """
        self._disconnect_from_device()
        self._connect_to_device()
        if not self.use_unix_port:
            self.execute_command("import os")
            self.execute_command("import sys")
            self.execute_command("os.chdir('/')")
            self.execute_command("sys.path.append('/remote')")

    def micropython_version(self):
        """
        Returns the micropython version running on the target board
        """
        return self._micropython_version

    def get_firmware_version(self):
        self.execute_command("import version as v")

        cmd = (
            'print("Firmware version {}, bootloader {}".format(v.build_tag, v.bootloader_version))'
        )

        return self.execute_command(cmd)

    def collect_file_contents(self, filename):
        if self.use_unix_port:
            contents = self.pyboard.fs_cat(filename)
            # For some reason sometimes contents come back as (PASS, actual_contents) ??
            if contents[0] == "PASS":
                contents = contents[1]
        else:
            # Can't use real pyboard fs_cat because it does something funky
            # with stdout.
            self.execute_command("f = open('%s')" % filename)
            # "execute_command" filters out log lines, so we use exec_ascii.
            contents = self.exec_ascii("for line in f: print(line, end='')\n")
            self.execute_command("f.close()")
            contents.replace("\r\n", "\n")

        for line in contents.split("\n"):
            # Report it in the Robot log.html at the same level.
            for loglevel in ("INFO", "WARN", "ERROR", "EXCEPTION"):
                if loglevel in line:
                    # Call logging.info, .warn etc methods.
                    log_method = getattr(logging, loglevel.lower())
                    log_method(line)
                    break
            else:
                logging.debug(line)
        return contents

    def delete_file(self, filename):
        logging.info("MicropythonLibrary.delete_file: %s", filename)
        self.pyboard.fs_rm(filename)

    def put_file(self, filename, dest):
        logging.info("MicropythonLibrary.put_file: %s %s", filename, dest)
        self.pyboard.fs_put(filename, dest)

    def list_files(self, src):
        """Home-grown pyboard.fs_ls"""
        cmd = "import uos; print([f[0] for f in uos.ilistdir('%s')])" % src
        lst = self.execute_command(cmd)
        return eval(lst)
