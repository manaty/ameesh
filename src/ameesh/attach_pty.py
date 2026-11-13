# SPDX-License-Identifier: AGPL-3.0-only
"""PTY de l'hôte pour une session d'attach distante (#7120, avatar D2).

Le harnais interactif tourne sur l'hôte de l'agent, dans un pseudo-terminal ;
le poste ne reçoit que des octets. Ce module ouvre le PTY, lance la commande
comme session leader (elle possède alors son terminal de contrôle), transmet
les octets, la taille de fenêtre et les signaux, et **nettoie le groupe**
à la sortie ou à la coupure : jamais de harnais orphelin, jamais de second
porteur (§5.4 de la conception).

Bornes : les écritures non bloquantes sont mises en attente jusqu'à
`MAX_PENDING` octets ; au-delà, `PtyOverflow` fait fermer la session (un gel
silencieux serait pire qu'une coupure, §5.3). `close()` termine le groupe
par SIGTERM puis SIGKILL, et attend le reaping.
"""
from __future__ import annotations

import errno
import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios
import time

#: écritures en attente vers le PTY : au-delà, la session échoue proprement
MAX_PENDING = 1024 * 1024
#: signaux qu'un poste peut demander (jamais SIGKILL ni SIGSTOP)
ALLOWED_SIGNALS = frozenset({signal.SIGHUP, signal.SIGINT, signal.SIGQUIT,
                             signal.SIGTERM, signal.SIGCONT, signal.SIGTSTP})
DEFAULT_CLOSE_TIMEOUT = 3.0


class PtyError(RuntimeError):
    """Le PTY n'a pas pu être ouvert ou la commande n'a pas démarré."""


class PtyOverflow(PtyError):
    """Trop d'octets en attente : la session doit être fermée, pas gelée."""


def _winsize(rows: int, cols: int, xpix: int = 0, ypix: int = 0) -> bytes:
    if not 0 < rows <= 65535 or not 0 < cols <= 65535:
        raise ValueError("terminal size out of bounds")
    return struct.pack("HHHH", rows, cols, min(xpix, 65535), min(ypix, 65535))


class PtyProcess:
    """Une commande interactive dans un PTY, du côté de l'hôte.

    `argv` est une liste (jamais une chaîne passée à un shell) ; `env` et
    `cwd` sont ceux de l'hôte. `read`/`write` ne bloquent pas : le pont
    s'appuie sur `select`. Un enfant mort rend `eof` et `returncode`.
    """

    def __init__(self, argv, *, env=None, cwd=None, rows: int = 24, cols: int = 80,
                 close_timeout: float = DEFAULT_CLOSE_TIMEOUT):
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise PtyError("an argv of non-empty strings is required")
        self.argv = list(argv)
        self.rows, self.cols = int(rows), int(cols)
        self.close_timeout = float(close_timeout)
        self.eof = False
        self._status: int | None = None
        self._closed = False
        self._pending = bytearray()
        size = _winsize(self.rows, self.cols)
        try:
            pid, master = pty.fork()
        except OSError as exc:
            raise PtyError("cannot open a pseudo-terminal (%s)" % exc.strerror) from exc
        if pid == 0:  # enfant : uniquement des appels système avant l'exec
            try:
                if cwd:
                    os.chdir(cwd)
                fcntl.ioctl(0, termios.TIOCSWINSZ, size)
                os.execvpe(self.argv[0], self.argv,
                           dict(os.environ if env is None else env))
            except BaseException:
                pass
            os._exit(127)
        self.pid = pid
        self.master = master
        os.set_blocking(master, False)
        self.start_ts = time.monotonic()

    # -- état --------------------------------------------------------------

    def poll(self) -> int | None:
        """Code de sortie si l'enfant est terminé, None sinon (reap inclus)."""
        if self._status is not None:
            return self._status
        try:
            done, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self._status = 0
            return 0
        if done == 0:
            return None
        self._status = _exit_code(status)
        self.eof = True
        return self._status

    @property
    def returncode(self) -> int | None:
        return self._status

    @property
    def pending(self) -> int:
        return len(self._pending)

    def writable(self) -> bool:
        return bool(self._pending)

    # -- E/S ---------------------------------------------------------------

    def read(self, size: int = 65536) -> bytes:
        """Lit ce qui est disponible ; b"" sans donnée ou sur EOF."""
        if self._closed or self.eof:
            return b""
        if size <= 0:
            return b""
        try:
            data = os.read(self.master, size)
        except BlockingIOError:
            return b""
        except InterruptedError:
            return b""
        except OSError as exc:
            if exc.errno == errno.EIO:  # le slave n'a plus d'écrivain
                self.eof = True
                return b""
            raise PtyError("read from the pseudo-terminal failed (%s)"
                           % (exc.strerror or exc.errno)) from exc
        if not data:
            self.eof = True
        return data

    def write(self, data: bytes) -> int:
        """Écrit vers le PTY ; met en attente ce que le noyau refuse.

        Rend le nombre d'octets acceptés (tous, ou une partie). Au-delà de
        `MAX_PENDING`, lève `PtyOverflow` : la session se ferme.
        """
        if self._closed:
            return 0
        data = bytes(data)
        if not data:
            return 0
        if self._pending:
            if len(self._pending) + len(data) > MAX_PENDING:
                raise PtyOverflow("pending input exceeds %d bytes" % MAX_PENDING)
            self._pending.extend(data)
            return len(data)
        try:
            written = os.write(self.master, data)
        except BlockingIOError:
            written = 0
        except InterruptedError:
            written = 0
        except OSError as exc:
            if exc.errno == errno.EIO:
                self.eof = True
                return 0
            raise PtyError("write to the pseudo-terminal failed (%s)"
                           % (exc.strerror or exc.errno)) from exc
        if written < len(data):
            rest = data[written:]
            if len(rest) > MAX_PENDING:
                raise PtyOverflow("pending input exceeds %d bytes" % MAX_PENDING)
            self._pending.extend(rest)
        return len(data)

    def flush(self) -> int:
        """Écrit le tampon en attente ; rend le nombre d'octets restants."""
        if self._closed or not self._pending:
            return 0
        try:
            written = os.write(self.master, bytes(self._pending))
        except BlockingIOError:
            return len(self._pending)
        except InterruptedError:
            return len(self._pending)
        except OSError as exc:
            if exc.errno == errno.EIO:
                self._pending.clear()
                self.eof = True
                return 0
            raise PtyError("write to the pseudo-terminal failed (%s)"
                           % (exc.strerror or exc.errno)) from exc
        del self._pending[:written]
        return len(self._pending)

    def resize(self, rows: int, cols: int, xpix: int = 0, ypix: int = 0) -> None:
        if self._closed:
            return
        try:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, _winsize(rows, cols, xpix, ypix))
        except OSError:
            return  # terminal déjà fermé : la session va s'arrêter
        self.rows, self.cols = int(rows), int(cols)

    def send_signal(self, signum: int) -> bool:
        """Envoie un signal au groupe de la session ; refusé hors liste."""
        if signum not in ALLOWED_SIGNALS:
            raise PtyError("signal %d is not allowed" % signum)
        if self._closed:
            return False
        try:
            os.killpg(self.pid, signum)
        except ProcessLookupError:
            return False
        except OSError as exc:
            raise PtyError("signal %d failed (%s)" % (signum, exc.strerror)) from exc
        return True

    # -- fermeture ---------------------------------------------------------

    def close(self) -> None:
        """Arrête le groupe (SIGTERM puis SIGKILL) et libère le PTY.

        Le signal vise le **groupe**, pas seulement le leader : un shell déjà
        sorti peut laisser un descendant, et un descendant qui ignore TERM
        doit recevoir KILL même si le leader est mort entre-temps. On ne
        s'appuie donc jamais sur le seul statut du leader pour décider de
        l'escalade.
        """
        if self._closed:
            return
        self._closed = True
        self.poll()  # reap the leader if it already exited
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        deadline = time.monotonic() + max(0.0, self.close_timeout)
        while time.monotonic() < deadline:
            if self.poll() is not None:
                break
            time.sleep(0.02)
        # toujours KILL : le groupe peut encore porter un descendant vivant
        # alors que le leader est déjà sorti (et reaped)
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        deadline = time.monotonic() + max(0.0, min(self.close_timeout, 0.5))
        while time.monotonic() < deadline:
            if self.poll() is not None:
                break
            time.sleep(0.02)
        self.poll()
        try:
            os.close(self.master)
        except OSError:
            pass
        self.master = -1
        self.eof = True


def _exit_code(status: int) -> int:
    try:
        return os.waitstatus_to_exitcode(status)
    except ValueError:
        return -1


def run(argv, *, env=None, cwd=None, input_bytes: bytes = b"", timeout: float = 5.0,
        rows: int = 24, cols: int = 80) -> tuple[int | None, bytes]:
    """Lance `argv`, lui écrit `input_bytes`, rend (code, sortie). Tests/bancs.

    La sortie est lue jusqu'à EOF ou `timeout` ; le groupe est toujours
    arrêté à la fin. Aucun terminal n'est requis : le PTY est le terminal.
    """
    import select
    proc = PtyProcess(argv, env=env, cwd=cwd, rows=rows, cols=cols)
    output = bytearray()
    try:
        if input_bytes:
            proc.write(input_bytes)
        deadline = time.monotonic() + max(0.1, timeout)
        while time.monotonic() < deadline:
            timeout_select = min(0.1, max(0.0, deadline - time.monotonic()))
            ready, writable, _ = select.select(
                [proc.master], [proc.master] if proc.writable() else [], [], timeout_select)
            if writable:
                proc.flush()
            if ready:
                chunk = proc.read(65536)
                if chunk:
                    output.extend(chunk)
                elif proc.eof:
                    break
            if proc.poll() is not None:
                # vider ce que l'enfant a écrit avant de mourir
                for _ in range(100):
                    chunk = proc.read(65536)
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(chunk) < 65536:
                        break
                break
        code = proc.poll()
        if code is None:
            # EOF lu : le fils vient de sortir, mais le reap peut suivre d'un
            # instant ; on ne rend jamais un code None pour une sortie lue
            deadline = time.monotonic() + 1.0
            while code is None and time.monotonic() < deadline:
                time.sleep(0.01)
                code = proc.poll()
        return code, bytes(output)
    finally:
        proc.close()
