"""Camera-only temporary networking, restored by an independent owner watchdog."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


class CameraNetworkSession:
    def __init__(self, host: str, camera: str):
        self.host, self.camera = host, camera
        self.process = None
        self.folder = None
        self.log = None
        self.state = {}

    def _read(self):
        try:
            self.state = json.loads((self.folder / 'state.json').read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            pass
        return self.state

    def open(self):
        if self.process is not None:
            raise RuntimeError('Camera network session is already open')
        self.folder = Path(tempfile.mkdtemp(prefix='krishnan-aravis-network-'))
        self.log = (self.folder / 'network.log').open('w', encoding='utf-8')
        # Windows venv executables redirect to the base interpreter. Firewall
        # application identity must use the mapped process image, not that stub.
        from .aravis_native import loaded_modules
        program = loaded_modules()[0]
        powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        command = [str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
                   str(Path(__file__).with_suffix('.ps1')), '-OwnerPid', str(os.getpid()),
                   '-StateDirectory', str(self.folder), '-Program', program,
                   '-HostAddress', self.host, '-CameraAddress', self.camera]
        try:
            self.process = subprocess.Popen(command, stdout=self.log, stderr=subprocess.STDOUT,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                state = self._read()
                if state.get('Error') or state.get('Closed') or self.process.poll() is not None:
                    raise RuntimeError(f"Camera network setup failed: {state.get('Error', 'helper exited')}; {self.folder}")
                if state.get('Ready'):
                    print(f'Aravis camera network ready; session diagnostics: {self.folder}', flush=True)
                    return
                time.sleep(.1)
            raise TimeoutError(f'Camera network setup timed out; {self.folder}')
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process is None:
            return
        (self.folder / 'stop').touch()
        try:
            self.process.wait(timeout=40)
            state = self._read()
            if not state.get('Closed') or state.get('CleanupErrors'):
                raise RuntimeError(f'Camera network restoration is unconfirmed: {state}; {self.folder}')
            confirmation={k:state.get(k) for k in ['Closed','RuleRemoved','BindingRestored','BindingDifferences','CleanupErrors']}
            print(f'Aravis camera network restored: {confirmation}; session diagnostics: {self.folder}', flush=True)
        finally:
            if self.process.poll() is not None:
                self.process = None
                self.log.close()
