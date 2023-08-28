import contextlib
import logging
import os
import platform
import re
import shutil
import stat
import tempfile
import threading
from pathlib import Path
from typing import IO, Any, BinaryIO, Generator, Optional

from wandb.sdk.lib.paths import StrPath

logger = logging.getLogger(__name__)

WRITE_PERMISSIONS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH | stat.S_IWRITE


# https://en.wikipedia.org/wiki/Filename#Comparison_of_filename_limitations
PROBLEMATIC_PATH_CHARS = "".join(chr(i) for i in range(0, 32)) + ':"*<>?|'


def mkdir_exists_ok(dir_name: StrPath) -> None:
    """Create `dir_name` and any parent directories if they don't exist.

    Raises:
        FileExistsError: if `dir_name` exists and is not a directory.
        PermissionError: if `dir_name` is not writable.
    """
    try:
        os.makedirs(dir_name, exist_ok=True)
    except FileExistsError as e:
        raise FileExistsError(f"{dir_name!s} exists and is not a directory") from e
    except PermissionError as e:
        raise PermissionError(f"{dir_name!s} is not writable") from e


def path_fallbacks(path: StrPath) -> Generator[str, None, None]:
    """Yield variations of `path` that may exist on the filesystem.

    Return a sequence of paths that should be checked in order for existence or
    create-ability. Essentially, keep replacing "suspect" characters until we run out.
    """
    path = str(path)
    root, tail = os.path.splitdrive(path)
    yield os.path.join(root, tail)
    for char in PROBLEMATIC_PATH_CHARS:
        if char in tail:
            tail = tail.replace(char, "-")
            yield os.path.join(root, tail)


def mkdir_allow_fallback(dir_name: StrPath) -> StrPath:
    """Create `dir_name`, removing invalid path characters if necessary.

    Returns:
        The path to the created directory, which may not be the original path.
    """
    for new_name in path_fallbacks(dir_name):
        try:
            os.makedirs(new_name, exist_ok=True)
            if Path(new_name) != Path(dir_name):
                logger.warning(f"Creating '{new_name}' instead of '{dir_name}'")
            return Path(new_name) if isinstance(dir_name, Path) else new_name
        except (ValueError, NotADirectoryError):
            pass
        except OSError as e:
            if e.errno != 22:
                raise

    raise OSError(f"Unable to create directory '{dir_name}'")


def files_in(path: StrPath) -> Generator[os.DirEntry, None, None]:
    """Yield a directory entry for each file under a given path (recursive)."""
    if not os.path.isdir(path):
        return
    for entry in os.scandir(path):
        if entry.is_dir():
            yield from files_in(entry.path)
        else:
            yield entry


class WriteSerializingFile:
    """Wrapper for a file object that serializes writes."""

    def __init__(self, f: BinaryIO) -> None:
        self.lock = threading.Lock()
        self.f = f

    def write(self, *args, **kargs) -> None:  # type: ignore
        self.lock.acquire()
        try:
            self.f.write(*args, **kargs)
            self.f.flush()
        finally:
            self.lock.release()

    def close(self) -> None:
        self.lock.acquire()  # wait for pending writes
        try:
            self.f.close()
        finally:
            self.lock.release()


class CRDedupedFile(WriteSerializingFile):
    def __init__(self, f: BinaryIO) -> None:
        super().__init__(f=f)
        self._buff = b""

    def write(self, data) -> None:  # type: ignore
        lines = re.split(b"\r\n|\n", data)
        ret = []  # type: ignore
        for line in lines:
            if line[:1] == b"\r":
                if ret:
                    ret.pop()
                elif self._buff:
                    self._buff = b""
            line = line.split(b"\r")[-1]
            if line:
                ret.append(line)
        if self._buff:
            ret.insert(0, self._buff)
        if ret:
            self._buff = ret.pop()
        super().write(b"\n".join(ret) + b"\n")

    def close(self) -> None:
        if self._buff:
            super().write(self._buff)
        super().close()


def copy_or_overwrite_changed(source_path: StrPath, target_path: StrPath) -> StrPath:
    """Copy source_path to target_path, unless it already exists with the same mtime.

    We liberally add write permissions to deal with the case of multiple users needing
    to share the same cache or run directory.

    Args:
        source_path: The path to the file to copy.
        target_path: The path to copy the file to.

    Returns:
        The path to the copied file (which may be different from target_path).
    """
    return_type = type(target_path)

    target_path = system_preferred_path(target_path, warn=True)

    need_copy = (
        not os.path.isfile(target_path)
        or os.stat(source_path).st_mtime != os.stat(target_path).st_mtime
    )

    permissions_plus_write = os.stat(source_path).st_mode | WRITE_PERMISSIONS
    if need_copy:
        dir_name, file_name = os.path.split(target_path)
        target_path = os.path.join(mkdir_allow_fallback(dir_name), file_name)
        try:
            # Use copy2 to preserve file metadata (including modified time).
            shutil.copy2(source_path, target_path)
        except PermissionError:
            # If the file is read-only try to make it writable.
            try:
                os.chmod(target_path, permissions_plus_write)
                shutil.copy2(source_path, target_path)
            except PermissionError as e:
                raise PermissionError("Unable to overwrite '{target_path!s}'") from e
        # Prevent future permissions issues by universal write permissions now.
        os.chmod(target_path, permissions_plus_write)

    return return_type(target_path)  # type: ignore  # 'os.PathLike' is abstract.


@contextlib.contextmanager
def safe_open(
    path: StrPath, mode: str = "r", *args: Any, **kwargs: Any
) -> Generator[IO, None, None]:
    """Open a file, ensuring any changes only apply atomically after close.

    This context manager ensures that even unsuccessful writes will not leave a "dirty"
    file or overwrite good data, and that all temp data is cleaned up.

    The semantics and behavior are intended to be nearly identical to the built-in
    open() function. Differences:
        - It creates any parent directories that don't exist, rather than raising.
        - In 'x' mode, it checks at the beginning AND end of the write and fails if the
            file exists either time.
    """
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    if "x" in mode and path.exists():
        raise FileExistsError(f"{path!s} already exists")

    if "r" in mode and "+" not in mode:
        # This is read-only, so we can just open the original file.
        # TODO (hugh): create a reflink and read from that.
        with path.open(mode, *args, **kwargs) as f:
            yield f
        return

    with tempfile.TemporaryDirectory(dir=path.parent) as tmp_dir:
        tmp_path = Path(tmp_dir) / path.name

        if ("r" in mode or "a" in mode) and path.exists():
            # We need to copy the original file in order to support reads and appends.
            # TODO (hugh): use reflinks to avoid the copy on platforms that support it.
            shutil.copy2(path, tmp_path)

        with tmp_path.open(mode, *args, **kwargs) as f:
            yield f
            f.flush()
            os.fsync(f.fileno())

        if "x" in mode:
            # Ensure that if another process has beaten us to writing the file we raise
            # rather than overwrite. os.link() atomically creates a hard link to the
            # target file and will raise FileExistsError if the target already exists.
            os.link(tmp_path, path)
            os.unlink(tmp_path)
        else:
            tmp_path.replace(path)


def safe_copy(source_path: StrPath, target_path: StrPath) -> StrPath:
    """Copy a file, ensuring any changes only apply atomically once finished."""
    # TODO (hugh): check that there is enough free space.
    output_path = Path(target_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output_path.parent) as tmp_dir:
        tmp_path = (Path(tmp_dir) / Path(source_path).name).with_suffix(".tmp")
        shutil.copy2(source_path, tmp_path)
        tmp_path.replace(output_path)
    return target_path


def check_exists(path: StrPath) -> Optional[StrPath]:
    """Look for variations of `path` and return the first found.

    This exists to support former behavior around system-dependent paths; we used to use
    ':' in Artifact paths unless we were on Windows, but this has issues when e.g. a
    Linux machine is accessing an NTFS filesystem; we might need to look for the
    alternate path. This checks all the possible directories we would consider creating.
    """
    for dest in path_fallbacks(path):
        if os.path.exists(dest):
            return Path(dest) if isinstance(path, Path) else dest
    return None


def system_preferred_path(path: StrPath, warn: bool = False) -> StrPath:
    """Replace ':' with '-' in paths on Windows.

    Args:
        path: The path to convert.
        warn: Whether to warn if ':' is replaced.
    """
    if platform.system() != "Windows":
        return path
    head, tail = os.path.splitdrive(path)
    if warn and ":" in tail:
        logger.warning(f"Replacing ':' in {tail} with '-'")
    new_path = head + tail.replace(":", "-")
    return Path(new_path) if isinstance(path, Path) else new_path
