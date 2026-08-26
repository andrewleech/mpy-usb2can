import logging
import os
import pty
import select
import subprocess
import time

# These are large as filesystem access in Docker in Windows can be slow.
READ_TIMEOUT_S = 90
READ_UNTIL_TIMEOUT_S = 120


class PyboardUnixProcess:
    def __init__(self, _port):
        assert os.environ["MICROPYTHON_CMD"], "Environment variable MICROPYTHON_CMD must exist"
        self.micropython_cmd = [os.environ["MICROPYTHON_CMD"]]
        self._create_process()

    def _create_process(self):
        pty_us, pty_them = pty.openpty()
        assert os.environ["MICROPYPATH"], "Environment variable MICROPYPATH must exist"
        proc = subprocess.Popen(
            self.micropython_cmd,
            stdin=pty_them,
            stdout=pty_them,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        self.pty_us = pty_us
        self.pty_them = pty_them
        self.proc = proc
        self._read_until(b">>> ")  # get and discard initial banner

    def _destroy_process(self):
        self._write(b"\x04")  # exit
        self._read()  # drain
        try:
            self.proc.kill()
        except ProcessLookupError:
            pass
        os.close(self.pty_us)
        os.close(self.pty_them)

    def _read(self, required=False, timeout=READ_TIMEOUT_S):
        r = b""
        t0 = time.monotonic()
        while True:
            ready = select.select([self.pty_us], [], [], 1 if required and not r else 0.001)
            if ready[0] == [self.pty_us]:
                r += os.read(self.pty_us, 1024)
            else:
                if not required or r:
                    return r
                returncode = self.proc.poll()
                assert returncode is None, "processed terminated unexpectedly with code {}".format(
                    returncode
                )
            assert time.monotonic() - t0 < timeout, "timeout waiting for process to output data"

    def _read_until(self, data, timeout=READ_UNTIL_TIMEOUT_S):
        r = b""
        t0 = time.monotonic()
        while not r.endswith(data):
            try:
                r += self._read(True)
            except:
                logging.exception(
                    "exception in _read_until calling _read: waiting for %r, got %r" % (data, r)
                )
                raise
            assert (
                time.monotonic() - t0 < timeout
            ), "timeout waiting for data to end with {}, got {}".format(data, r)
        return r

    def _write(self, data):
        os.write(self.pty_us, data)

    def close(self):
        self._destroy_process()

    def enter_raw_repl(self):
        pass

    def exit_raw_repl(self):
        pass

    def mount_local(self, path):
        # TODO
        pass

    def exec_raw(self, cmd, timeout=10):
        return self.exec_(cmd), b""

    def exec_(self, cmd):
        if not isinstance(cmd, bytes):
            cmd = bytes(cmd, encoding="utf8")
        assert (
            b"\n" not in cmd
        ), "command must be a single-line command (to easily discard echo'd data)"

        # Write.
        self._write(b"\x05" + cmd)
        self._read_until(cmd)  # Discard echo'd back paste data from the command.
        self._write(b"\x04")  # Finish paste mode.

        # Read.
        result = self._read_until(b">>> ")
        msg = "Result incorrectly formatted: {}".format(result)
        # Handle both \r\n and \r\r\n (platform-specific behavior)
        if result.startswith(b"\r\r\n"):
            result = result[1:]  # Strip extra \r
        assert result.startswith(b"\r\n"), msg
        assert result.endswith(b">>> "), msg
        result = result[2:-4]
        if b"Traceback" in result:
            raise Exception(result)
        return result

    def fs_cat(self, src, chunk_size=256):
        with open(src) as f:
            contents = f.read()
        return contents

    def fs_rm(self, src):
        import os

        os.remove(src)

    def fs_put(self, src, dest, chunk_size=256):
        g = open(dest, "wb")
        with open(src, "rb") as f:
            while True:
                data = f.read(chunk_size)
                if not data:
                    break
                g.write(data)
        g.close()
