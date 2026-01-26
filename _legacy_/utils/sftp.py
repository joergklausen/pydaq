#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SFTP file transfer utilities (Paramiko-based), with minimal boilerplate.

- Centralized SSH/SFTP connection handling
- Robust remote-directory creation for absolute/relative POSIX paths
- Optional deletion of local files after successful upload
- Simple scheduling helpers

Author: joerg.klausen@meteoswiss.ch
"""
from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath
from typing import Optional, List
import contextlib

import paramiko
import schedule


class SFTPClient:
    """
    SFTP-based file handling.

    Methods:
      - is_alive()
      - list_local_files()
      - remote_item_exists()
      - list_remote_items()
      - setup_remote_folders()
      - put_file()
      - remove_remote_item()
      - transfer_files()
      - setup_transfer_schedules()
    """

    def __init__(self, config: dict, logger: logging.Logger):
        """
        Initialize from a dict-like config.

        Supported keys (common aliases accepted):
          host (str)                    : remote host
          usr | user (str)              : username
          key_path | key (str, optional): path to private key (RSA/Ed25519)
          passphrase (str, optional)    : passphrase for the private key
          password  (str, optional)     : password-based auth (if no key)
          staging (str | Path)          : local base directory
          remote | remote_path (str)    : remote base directory (POSIX)
          accept_unknown_host_keys (bool, default: True)
          timeouts: dict with keys {connect, auth, banner} in seconds (optional)
        """
        self.logger = logger

        # Basic connection config (allow common aliases to reduce friction)
        self.host = config["host"]
        self.username = config.get("usr") or config.get("user") or config["usr"]

        key_path = config.get("key_path") or config.get("key")
        self.passphrase = config.get("passphrase")
        self.password = config.get("password")

        self.local_path = Path(config["staging"]).expanduser()
        self.remote_base = PurePosixPath(
            config.get("remote") or config.get("remote_path") or "."
        )

        # Host key policy
        self.accept_unknown_host_keys = config.get("accept_unknown_host_keys", True)

        # Optional timeouts
        timeouts = config.get("timeouts", {}) or {}
        self.connect_timeout = timeouts.get("connect", 20)
        self.auth_timeout = timeouts.get("auth", 20)
        self.banner_timeout = timeouts.get("banner", 30)

        # Prepare key if provided (support RSA and Ed25519 common cases)
        self.pkey = None
        if key_path:
            key_path = str(Path(key_path).expanduser())
            # Try RSA first, then Ed25519
            try:
                self.pkey = paramiko.RSAKey.from_private_key_file(
                    key_path, password=self.passphrase
                )
            except Exception:
                try:
                    self.pkey = paramiko.Ed25519Key.from_private_key_file(
                        key_path, password=self.passphrase
                    )
                except Exception as err:
                    self.logger.error(f"Failed to load private key '{key_path}' failed: {err}")
                    raise

        # Bookkeeping
        self.transferred_local: List[str] = []
        self.transferred_remote: List[str] = []

        self.logger.debug(
            "SFTPClient initialized: host=%s user=%s local=%s remote=%s",
            self.host,
            self.username,
            str(self.local_path),
            str(self.remote_base),
        )

    # -------- connection handling

    @contextlib.contextmanager
    def _sftp_client(self):
        """Context manager that yields a live SFTP client."""
        ssh = paramiko.SSHClient()
        if self.accept_unknown_host_keys:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            ssh.load_system_host_keys()
            ssh.set_missing_host_key_policy(paramiko.RejectPolicy())

        ssh.connect(
            hostname=self.host,
            username=self.username,
            pkey=self.pkey,
            password=self.password,
            timeout=self.connect_timeout,
            auth_timeout=self.auth_timeout,
            banner_timeout=self.banner_timeout,
            look_for_keys=False,  # explicit to avoid surprises
        )
        try:
            sftp = ssh.open_sftp()
            try:
                yield sftp
            finally:
                sftp.close()
        finally:
            ssh.close()

    # -------- utilities

    def is_alive(self) -> bool:
        """Return True if the SFTP server is reachable and we can open an SFTP session."""
        try:
            with self._sftp_client():
                pass
            self.logger.info("SFTP server is alive", extra={"to_logfile": True})
            return True
        except Exception as err:
            self.logger.error(f"is_alive failed: {err}")
            return False

    def list_local_files(self, local_path: Optional[Path] = None) -> List[Path]:
        """
        Recursively list files under the given local path (defaults to staging).
        """
        base = Path(local_path or self.local_path).resolve()
        if base.is_file():
            return [base]
        if not base.exists():
            self.logger.warning("Local path does not exist: %s", base)
            return []
        return [p for p in base.rglob("*") if p.is_file()]

    def remote_item_exists(self, remote_path: str | PurePosixPath) -> bool:
        """Check whether a remote item exists."""
        path = PurePosixPath(remote_path)
        try:
            with self._sftp_client() as sftp:
                try:
                    sftp.stat(str(path))
                    return True
                except FileNotFoundError:
                    return False
        except Exception as err:
            self.logger.error(f"remote_item_exists failed: {err}")
            return False

    def list_remote_items(
        self, remote_path: Optional[str | PurePosixPath] = None
        ) -> List[str]:
        """List names (not full paths) in the given remote directory."""
        path = PurePosixPath(remote_path or ".")
        try:
            with self._sftp_client() as sftp:
                return sftp.listdir(str(path))
        except Exception as err:
            self.logger.error(f"list_remote_items('{path}') failed: {err}")
            return []

    # -------- directory helpers

    def _ensure_remote_dir(self, sftp: paramiko.SFTPClient, path: PurePosixPath) -> None:
        """
        Create remote directory (and parents) if needed. Works with absolute or relative paths.
        """
        # Normalize parts, skipping empty and root markers
        parts = [p for p in path.parts if p not in ("", "/")]
        # Start from '/' if absolute, else from '.'
        cursor = PurePosixPath("/") if path.is_absolute() else PurePosixPath(".")
        try:
            sftp.chdir(str(cursor))
        except Exception:
            # Some servers chroot; '.' usually works
            cursor = PurePosixPath(".")
            sftp.chdir(str(cursor))

        for part in parts:
            cursor = cursor / part
            try:
                sftp.chdir(str(cursor))
            except IOError:
                # Create and enter
                sftp.mkdir(str(cursor), mode=0o755)
                sftp.chdir(str(cursor))

    def setup_remote_folders(
        self,
        local_path: Optional[str | Path] = None,
        remote_path: Optional[str | PurePosixPath] = None,
        ) -> None:
        """Replicate the local folder structure to the remote base."""
        local_base = Path(local_path or self.local_path).resolve()
        remote_base = PurePosixPath(remote_path or self.remote_base)

        try:
            with self._sftp_client() as sftp:
                for dirpath, dirnames, filenames in os.walk(local_base):
                    # Skip completely empty directories
                    if not dirnames and not filenames:
                        continue
                    rel = Path(dirpath).relative_to(local_base).as_posix()
                    remote_dir = remote_base / rel
                    self._ensure_remote_dir(sftp, PurePosixPath(remote_dir))
        except Exception as err:
            self.logger.error(f"setup_remote_folders failed failed: {err}")

    # -------- file operations

    def put_file(
        self,
        local_file: Path,
        remote_dir: str | PurePosixPath,
        remove_on_success: bool = False,
        ) -> Optional[PurePosixPath]:
        """
        Upload a single file to a remote directory (creating it if needed).
        """
        local_file = Path(local_file)
        remote_dir = PurePosixPath(remote_dir)
        remote_file = remote_dir / local_file.name

        try:
            with self._sftp_client() as sftp:
                self._ensure_remote_dir(sftp, remote_dir)
                attr = sftp.put(
                    localpath=local_file.as_posix(),
                    remotepath=remote_file.as_posix(),
                    confirm=True,
                )

                # Record
                self.transferred_local.append(local_file.as_posix())
                self.transferred_remote.append(remote_file.as_posix())

                if remove_on_success:
                    if attr.st_size == local_file.stat().st_size:
                        local_file.unlink(missing_ok=False)
                    else:
                        self.logger.warning(
                            "Size mismatch after put: %s vs %s; not deleting %s",
                            attr.st_size,
                            local_file.stat().st_size,
                            local_file,
                        )
                return remote_file
        except Exception as err:
            self.logger.error(f"put_file failed for {local_file} -> {remote_file} failed: {err}")
            return None

    def remove_remote_item(
        self, remote_path: str | PurePosixPath, recursive: bool = True
        ) -> None:
        """
        Remove a file or (empty) directory. If recursive=True, prune empty parents.
        """
        path = PurePosixPath(remote_path)
        try:
            with self._sftp_client() as sftp:
                # Try directory first
                try:
                    if sftp.listdir(path.as_posix()):
                        self.logger.warning(
                            "Cannot remove non-empty directory: %s", path
                        )
                        return
                    sftp.rmdir(path.as_posix())
                    self.logger.info("Removed directory: %s", path)
                except IOError:
                    # Not a directory; try as file
                    sftp.remove(path.as_posix())
                    self.logger.info("Removed file: %s", path)

                # Optionally prune empty parents
                if recursive:
                    parent = path.parent
                    while str(parent) not in ("", "/"):
                        try:
                            if not sftp.listdir(parent.as_posix()):
                                sftp.rmdir(parent.as_posix())
                                self.logger.info("Pruned empty parent: %s", parent)
                                parent = parent.parent
                            else:
                                break
                        except Exception:
                            break
        except FileNotFoundError:
            self.logger.warning("Remote item does not exist: %s", path)
        except Exception as err:
            self.logger.error(f"remove_remote_item failed: {err}")

    def transfer_files(
        self,
        local_path: Optional[Path] = None,
        remote_path: Optional[PurePosixPath] = None,
        remove_on_success: bool = True,
        ) -> None:
        """
        Transfer all files from local_path (recursively) to remote_path, mirroring
        subfolders. Deletes local files on success when remove_on_success=True.
        """
        self.transferred_local.clear()
        self.transferred_remote.clear()

        local_base = Path(local_path or self.local_path).resolve()
        if local_base.is_file():
            # Normalize: treat a single file as its parent folder for folder mirroring
            local_base = local_base.parent
        if not local_base.exists():
            raise FileNotFoundError(f"Local path '{local_base}' does not exist.")

        remote_base = PurePosixPath(remote_path or self.remote_base)

        try:
            with self._sftp_client() as sftp:
                created_dirs: set[PurePosixPath] = set()

                for root, _, files in os.walk(local_base):
                    if not files:
                        continue

                    rel = Path(root).relative_to(local_base).as_posix()
                    remote_dir = remote_base / rel
                    rdir = PurePosixPath(remote_dir)
                    if rdir not in created_dirs:
                        self._ensure_remote_dir(sftp, rdir)
                        created_dirs.add(rdir)

                    for name in files:
                        lfile = Path(root) / name
                        rfile = rdir / name
                        try:
                            attr = sftp.put(
                                localpath=lfile.as_posix(),
                                remotepath=rfile.as_posix(),
                                confirm=True,
                            )
                            self.logger.debug(
                                "Transferred %s -> %s", lfile.as_posix(), rfile.as_posix()
                            )
                            self.transferred_local.append(lfile.as_posix())
                            self.transferred_remote.append(rfile.as_posix())

                            if remove_on_success:
                                if attr.st_size == lfile.stat().st_size:
                                    lfile.unlink()
                                else:
                                    self.logger.warning(
                                        "Size mismatch: %s != %s; not deleting %s",
                                        attr.st_size,
                                        lfile.stat().st_size,
                                        lfile,
                                    )
                        except Exception as file_err:
                            self.logger.error(
                                "Failed to transfer %s -> %s: %s",
                                lfile,
                                rfile,
                                file_err,
                            )
        except Exception as err:
            self.logger.error(f"transfer_files failed failed: {err}")

    # -------- scheduling

    # def setup_transfer_schedules(
    #     self,
    #     remove_on_success: bool = True,
    #     interval: int = 60,
    #     local_path: Optional[str] = None,
    #     remote_path: Optional[str] = None,
    #     ) -> None:
    #     """
    #     Schedule periodic transfers.

    #     interval:
    #       - 10              -> every 10 minutes (at :00, :10, :20, :30, :40, :50 + 10s)
    #       - multiple of 60  -> every N hours (at HH:00:10)
    #       - 1440            -> daily at 00:00:10
    #     """
    #     try:
    #         if interval == 10:
    #             for m in range(0, 60, 10):
    #                 schedule.every().hour.at(f":{m:02}:10").do(
    #                     self.transfer_files,
    #                     remove_on_success=remove_on_success,
    #                     local_path=Path(local_path) if local_path else None,
    #                     remote_path=PurePosixPath(remote_path) if remote_path else None,
    #                 )
    #         elif interval % 60 == 0 and interval <= 1440:
    #             step = interval // 60
    #             for hh in range(0, 24, step):
    #                 schedule.every().day.at(f"{hh:02}:00:10").do(
    #                     self.transfer_files,
    #                     remove_on_success=remove_on_success,
    #                     local_path=Path(local_path) if local_path else None,
    #                     remote_path=PurePosixPath(remote_path) if remote_path else None,
    #                 )
    #         elif interval == 1440:
    #             schedule.every().day.at("00:00:10").do(
    #                 self.transfer_files,
    #                 remove_on_success=remove_on_success,
    #                 local_path=Path(local_path) if local_path else None,
    #                 remote_path=PurePosixPath(remote_path) if remote_path else None,
    #             )
    #         else:
    #             raise ValueError(
    #                 "'interval' must be 10, a multiple of 60, or 1440 minutes."
    #             )
    #     except Exception as err:
    #         self.logger.error(f"setup_transfer_schedules failed: {err}")


if __name__ == "__main__":
    pass

# #!/usr/bin/env python
# # -*- coding: utf-8 -*-
# """
# Manage file transfer. Currently, sftp transfer to MeteoSwiss is supported.

# @author: joerg.klausen@meteoswiss.ch
# """
# import logging
# import os
# import re
# from pathlib import Path, PurePosixPath
# from typing import Union, Optional, List
# import paramiko
# import schedule


# class SFTPClient:
#     """
#     SFTP based file handling, optionally using SOCKS5 proxy.

#     Available methods include
#     - is_alive():
#     - list_local_files():
#     - remote_item_exists():
#     - list_remote_items():
#     - setup_remote_folders():
#     - put_file():
#     - remove_remote_item():
#     - transfer_files(): transfer files,  optionally removing files from source
#     """

#     def __init__(self, config: dict, logger: logging.Logger):
#         # """
#         # Initialize the SFTPClient class with parameters from a configuration file.

#         # :param config: dictionary.
#         #             config['host']:
#         #             config['usr']:
#         #             config['key']:
#         #             config['staging']: relative path to local staging area
#         #             config['remote_path']: (absolute?) root of remote destination
#         # """
#         try:
#             # configure logging
#             self.logger = logger
#             self.logger.info("Initializing", extra={"to_logfile": True})

#             # sftp connection settings
#             self.host = config['host']
#             self.usr = config['usr']
#             self.key = paramiko.RSAKey.from_private_key_file(\
#                 str(Path(config['key_path']).expanduser()))

#             # configure client proxy if needed
#             proxy_url = config.get('proxy_url', None)
#             if proxy_url:
#                 import sockslib
#                 with sockslib.SocksSocket() as sock:
#                     sock.set_proxy((proxy_url,
#                                     config['proxy']['port']), sockslib.Socks.SOCKS5)

#             # configure local source
#             self.local_path = Path(config['staging']).expanduser()

#             # configure remote destination
#             self.remote_path = PurePosixPath(config.get('remote', ''))

#         except Exception as err:
#             self.logger.error(err)


#     def is_alive(self) -> bool:
#         """Test ssh connection to sftp server.

#         Returns:
#             bool: [description]
#         """
#         try:
#             with paramiko.SSHClient() as ssh:
#                 ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
#                 ssh.connect(hostname=self.host, username=self.usr, pkey=self.key)

#                 with ssh.open_sftp() as sftp:
#                     sftp.close()
#             self.logger.info("SFTP server is alive", extra={"to_logfile": True})
#             return True
#         except Exception as err:
#             self.logger.error(err)
#             return False


#     def list_local_files(self, local_path: Path=Path()) -> list:
#         """Establish list of local files.

#         Args:
#             localpath (Path, optional): Absolute path to directory containing folders and files. Defaults to str().

#         Returns:
#             list: absolute paths of local files
#         """
#         files = list()

#         if local_path is Path():
#             local_path = Path(self.local_path)

#         try:
#             files = []
#             for root, dirs, filenames in os.walk(local_path):
#                 for file in filenames:
#                     files.append(Path(root) / file)
#             return files

#         except Exception as err:
#             self.logger.error(err)
#             return list()


#     def remote_item_exists(self, remote_path: Union[str, PurePosixPath]) -> bool:
#         """Check on remote server if an item exists.

#         Args:
#             remote_path (PurePosixPath): Full path to remote item

#         Returns:
#             Boolean: True if item exists, False otherwise.
#         """
#         try:
#             path = PurePosixPath(remote_path)
#             with paramiko.SSHClient() as ssh:
#                 ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
#                 ssh.connect(hostname=self.host, username=self.usr, pkey=self.key)
#                 with ssh.open_sftp() as sftp:
#                     try:
#                         sftp.stat(str(path))
#                         return True
#                     except FileNotFoundError:
#                         return False
#         except Exception as err:
#             self.logger.error(err)
#             return False


#     def list_remote_items(self, remote_path: Optional[Union[str, PurePosixPath]] = None) -> List[str]:
#         """
#         List items in a remote SFTP directory.

#         Args:
#             remote_path (str | PurePosixPath | None): Remote directory path. Defaults to user's SFTP root.

#         Returns:
#             List[str]: List of item names in the specified remote directory.
#         """
#         path = PurePosixPath(remote_path or ".")

#         try:
#             with paramiko.SSHClient() as ssh:
#                 ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
#                 ssh.connect(hostname=self.host, username=self.usr, pkey=self.key)

#                 with ssh.open_sftp() as sftp:
#                     return sftp.listdir(str(path))

#         except Exception as err:
#             self.logger.error(f"Error listing remote items in '{path}' failed: {err}")
#             return []


#     def setup_remote_folders(self,
#                              local_path: Optional[Union[str, Path]] = None,
#                              remote_path: Optional[Union[str, PurePosixPath]] = None
#                              ) -> None:
#         """
#         Replicate the local directory structure under `local_path` to the remote SFTP server under `remote_path`.

#         Args:
#             local_path (str | None): Base local path to scan. Defaults to `self.local_path`.
#             remote_path (str | None): Base remote path to create folders. Defaults to `self.remote_path`.
#         """
#         try:
#             local_base = Path(local_path or self.local_path).resolve()
#             remote_base = PurePosixPath(remote_path or self.remote_path)

#             self.logger.info(f"Setting up remote folders from '{local_base}' to '{remote_base}'")

#             with paramiko.SSHClient() as ssh:
#                 ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
#                 ssh.connect(hostname=self.host, username=self.usr, pkey=self.key)

#                 with ssh.open_sftp() as sftp:
#                     for root, dirs, files in os.walk(local_base):
#                         if not dirs and not files:
#                             continue  # Skip empty directories

#                         rel_path = Path(root).relative_to(local_base)
#                         remote_dir = remote_base / PurePosixPath(rel_path.as_posix())

#                         self.logger.debug(f"Ensuring remote directory: {remote_dir}")
#                         try:
#                             sftp.stat(str(remote_dir))  # Check if directory exists
#                         except FileNotFoundError:
#                             try:
#                                 sftp.mkdir(str(remote_dir), mode=0o755)
#                                 self.logger.debug(f"Created remote directory: {remote_dir}")
#                             except Exception as mkdir_err:
#                                 self.logger.error(f"Could not create '{remote_dir}': {mkdir_err}")
#                         except Exception as stat_err:
#                             self.logger.error(f"Error checking existence of '{remote_dir}': {stat_err}")

#         except Exception as err:
#             self.logger.error(f"setup_remote_folders failed failed: {err}")


#     def remove_remote_item(self, remote_path: Union[str, PurePosixPath], recursive: bool = True) -> None:
#         """
#         Remove a file or (empty) directory from a remote host using SFTP and SSH.
#         If `recursive=True`, empty parent directories will also be pruned.

#         Args:
#             remote_path (Union[str, PurePosixPath]): Remote path to file or directory.
#             recursive (bool): If True, recursively prune empty parent directories.
#         """
#         try:
#             remote_path = PurePosixPath(remote_path)
#             if not self.remote_item_exists(remote_path):
#                 raise ValueError("remove_remote_item: remote_path does not exist.")

#             with paramiko.SSHClient() as ssh:
#                 ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
#                 ssh.connect(hostname=self.host, username=self.usr, pkey=self.key)
#                 with ssh.open_sftp() as sftp:
#                     try:
#                         # Check if it's a directory
#                         if sftp.listdir(remote_path.as_posix()):
#                             self.logger.warning(
#                                 f"Cannot remove non-empty directory: {remote_path}. "
#                                 f"Provide full path to file to remove it, or empty the directory first."
#                             )
#                             return
#                         sftp.rmdir(remote_path.as_posix())
#                         self.logger.info(f"Removed directory: {remote_path}")
#                     except IOError:
#                         # Not a directory → try removing as a file
#                         try:
#                             sftp.remove(remote_path.as_posix())
#                             self.logger.info(f"Removed file: {remote_path}")
#                         except Exception as err:
#                             self.logger.error(f"Failed to remove file: {remote_path} failed: {err}")
#                             return

#                     # Optionally prune empty parent directories
#                     if recursive:
#                         parent = remote_path.parent
#                         while str(parent) not in ('', '/'):
#                             try:
#                                 if not sftp.listdir(parent.as_posix()):
#                                     sftp.rmdir(parent.as_posix())
#                                     self.logger.info(f"Pruned empty parent directory: {parent}")
#                                     parent = parent.parent
#                                 else:
#                                     break
#                             except Exception as err:
#                                 self.logger.warning(f"Could not check or prune parent {parent} failed: {err}")
#                                 break
#         except Exception as err:
#             self.logger.error(f"remove_remote_item failed: {err}")


#     def setup_remote_path(self, 
#                           remote_path: Union[str, PurePosixPath]
#                           ) -> None:
#         """Create (and navigate to the leaf of) a remote path.

#         Args:
#             remote_path (str, PurePosixPath): Remote path to create. NB: The last bit of the path is always interpreted as a directory

#         """
#         try:
#             remote_path = PurePosixPath(remote_path)
#             with paramiko.SSHClient() as ssh:
#                 ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
#                 ssh.connect(hostname=self.host, username=self.usr, pkey=self.key)
#                 with ssh.open_sftp() as sftp:
#                     # create remote path if it doesn't exist and enter it
#                     try:
#                         sftp.chdir(remote_path.as_posix())
#                     except IOError:
#                         parts = remote_path.parts
#                         current_path = '.'
#                         for part in parts:
#                             if part:
#                                 current_path = f"{current_path}/{part}"
#                             try:
#                                 sftp.chdir(current_path)
#                             except IOError:
#                                 sftp.mkdir(part)
#                                 sftp.chdir(part)
#                                 self.logger.debug(f"setup_remote_path: created {part}")
#                     self.cwd = sftp.getcwd()
#                     self.logger.debug(f"setup_remote_path: switched to {self.cwd}")
#                     if self.cwd is None:
#                         self.cwd = PurePosixPath()
#             return
#         except Exception as err:
#             self.logger.error(f"setup_remote_path failed: {err}")


#     def transfer_files(self,
#                        local_path: Optional[Path] = None,
#                        remote_path: Optional[PurePosixPath] = None,
#                        remove_on_success: bool = True,
#                        ) -> None:
#         """
#         Transfer all files from local_path and its subfolders to remote_path.

#         Args:
#             remove_on_success (bool): If True, delete local files after successful transfer.
#             local_path (str | None): Full path to local directory. Defaults to self.local_path.
#             remote_path (str | None): Base path on remote host. Defaults to self.remote_path.
#                                     The last element must be a directory.
#         """
#         try:
#             self.transfered_local = list()
#             self.transfered_remote = list()

#             local_base = Path(local_path or self.local_path).resolve()
#             if local_base.is_file():
#                 # If local_base is a file, convert it to a directory containing that file
#                 local_base = local_base.parent
#             elif not local_base.is_dir():
#                 raise ValueError(f"Local path '{local_base}' is not a valid directory or file.")
#             if not local_base.exists():
#                 raise FileNotFoundError(f"Local path '{local_base}' does not exist.")
#             remote_base = PurePosixPath(remote_path or self.remote_path)

#             self.logger.info(f"Starting file transfer: {local_base} -> {remote_base}")

#             with paramiko.SSHClient() as ssh:
#                 ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
#                 ssh.connect(hostname=self.host, username=self.usr, pkey=self.key)

#                 with ssh.open_sftp() as sftp:
#                     for root, _, files in os.walk(local_base):
#                         if not files:
#                             continue

#                         rel_path = Path(root).relative_to(local_base).as_posix()
#                         remote_dir = remote_base / rel_path

#                         # Ensure remote subdirectory exists
#                         self.setup_remote_path(remote_dir)

#                         for file in files:
#                             local_file = Path(root) / file
#                             remote_file = remote_dir / file

#                             try:
#                                 attr = sftp.put(
#                                     localpath=local_file.as_posix(),
#                                     remotepath=remote_file.as_posix(),
#                                     confirm=True
#                                 )
#                                 self.logger.debug(f"transfered {local_file.as_posix()} -> {remote_file.as_posix()}")
#                                 self.transfered_local.append(local_file.as_posix())
#                                 self.transfered_remote.append(remote_file.as_posix())

#                                 if remove_on_success:
#                                     local_size = local_file.stat().st_size
#                                     remote_size = attr.st_size
#                                     if remote_size == local_size:
#                                         local_file.unlink()
#                                         self.logger.debug(f"Removed local file: {local_file}")
#                                     else:
#                                         self.logger.warning(
#                                             f"Size mismatch: {local_file} ({local_size}) != {remote_file} ({remote_size}). File not removed."
#                                         )
#                             except Exception as file_err:
#                                 self.logger.error(f"Failed to transfer {local_file} -> {remote_file}: {file_err}")
#             return

#         except Exception as err:
#             self.logger.error(f"transfer_files failed failed: {err}")


#     def setup_transfer_schedules(self,
#                                  remove_on_success: bool = True,
#                                  interval: int = 60,
#                                  local_path: Optional[str] = None,
#                                  remote_path: Optional[str] = None,
#                                  ) -> None:
#         try:
#             if interval==10:
#                 minutes = [f"{interval*n:02}" for n in range(6) if interval*n < 6]
#                 for minute in minutes:
#                     schedule.every(1).hour.at(f"{minute}:10").do(self.transfer_files, remove_on_success, local_path, remote_path)
#             elif (interval % 60) == 0:
#                 hrs = [f"{n:02}:00:10" for n in range(0, 24, interval // 60)]
#                 for hr in hrs:
#                     schedule.every(1).day.at(hr).do(self.transfer_files, remove_on_success, local_path, remote_path)
#             elif interval==1440:
#                 schedule.every(1).day.at('00:00:10').do(self.transfer_files, remove_on_success, local_path, remote_path)
#             else:
#                 raise ValueError("'interval' must be 10 minutes or a multiple of 60 minutes and a maximum of 1440 minutes.")

#         except Exception as err:
#             self.logger.error(err)


# if __name__ == "__main__":
#     pass
