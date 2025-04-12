#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Manage file transfer. Currently, sftp transfer to MeteoSwiss is supported.

@author: joerg.klausen@meteoswiss.ch
"""
import logging
import os
import re

import paramiko
import schedule


class SFTPClient:
    """
    SFTP based file handling, optionally using SOCKS5 proxy.

    Available methods include
    - is_alive():
    - list_local_files():
    - remote_item_exists():
    - list_remote_items():
    - setup_remote_folders():
    - put_file():
    - remove_remote_item():
    - transfer_files(): transfer files,  optionally removing files from source
    """

    def __init__(self, config: dict):
        """
        Initialize the SFTPClient class with parameters from a configuration file.

        :param config_file: Path to the configuration file.
                    config['sftp']['host']:
                    config['sftp']['usr']:
                    config['sftp']['key']:
                    config['sftp']['local_path']: relative path to local source (= staging)
                    config['sftp']['remote_path']: (absolute?) root of remote destination
        """
        try:
            # configure logging
            _logger = f"{os.path.basename(config['logging']['file'])}".split('.')[0]
            self.logger = logging.getLogger(f"{_logger}.{__name__}")
            self.schedule_logger = logging.getLogger(f"{_logger}.schedule")
            self.schedule_logger.setLevel(level=logging.DEBUG)
            self.logger.info("Initialize SFTPClient")

            # sftp connection settings
            self.host = config['sftp']['host']
            self.usr = config['sftp']['usr']
            self.key = paramiko.RSAKey.from_private_key_file(\
                os.path.expanduser(config['sftp']['key']))
            
            # configure client proxy if needed
            # if config['sftp']['proxy']['socks5']:
            #     import sockslib
            #     with sockslib.SocksSocket() as sock:
            #         sock.set_proxy((config['sftp']['proxy']['socks5'],
            #                         config['sftp']['proxy']['port']), sockslib.Socks.SOCKS5)

            # configure local source
            self.local_path = os.path.join(os.path.expanduser(config['root']), config['staging'])
            self.logger.debug(f"__init__: {self.local_path}")

            # configure remote destination
            self.remote_path = config['sftp']['remote_path']
            self.logger.debug(f"__init__: {self.remote_path}")

        except Exception as err:
            self.logger.error(err)


    def is_alive(self) -> bool:
        """Test ssh connection to sftp server.

        Returns:
            bool: [description]
        """
        try:
            with paramiko.SSHClient() as ssh:
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(hostname=self.host, username=self.usr, pkey=self.key)

                with ssh.open_sftp() as sftp:
                    sftp.close()
            return True
        except Exception as err:
            self.logger.error(err)
            return False


    def list_local_files(self, local_path: str=str()) -> list:
        """Establish list of local files.

        Args:
            localpath (str, optional): Absolute path to directory containing folders and files. Defaults to str().

        Returns:
            list: absolute paths of local files
        """
        files = list()

        if local_path is None:
            local_path = self.local_path

        try:
            files = []
            for root, dirs, filenames in os.walk(local_path):
                for file in filenames:
                    files.append(os.path.join(root, file))
            return files

        except Exception as err:
            self.logger.error(err)
            return list()


    def remote_item_exists(self, remote_path: str) -> bool:
        """Check on remote server if an item exists. Assume this indicates successful transfer.

        Args:
            remote_path (str): path to remote item

        Returns:
            Boolean: True if item exists, False otherwise.
        """
        try:
            remote_path = remote_path.replace('\\', '/').rstrip('/')
            with paramiko.SSHClient() as ssh:
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(hostname=self.host, username=self.usr, pkey=self.key)
                with ssh.open_sftp() as sftp:
                    try:
                        sftp.stat(remote_path)
                        return True
                    except FileNotFoundError:
                        return False
        except Exception as err:
            self.logger.error(err)
            return False
        

    def list_remote_items(self, remote_path: str='.') -> list:
        try:
            with paramiko.SSHClient() as ssh:
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(hostname=self.host, username=self.usr, pkey=self.key)
                with ssh.open_sftp() as sftp:
                    return sftp.listdir(remote_path)

        except Exception as err:
            self.logger.error(err)
            return list()


    def setup_remote_folders(self, local_path: str=str(), remote_path: str=str()) -> None:
        """
        Determine directory structure under local_path and replicate on remote host.

        :param str local_path:
        :param str remote_path:
        :return: Nothing
        """
        try:
            if local_path is None:
                local_path = self.local_path

            # sanitize local_path
            local_path = re.sub(r'(/?\.?\\){1,2}', '/', local_path)

            if remote_path is str():
                remote_path = self.remote_path

            # sanitize remote_path
            remote_path = re.sub(r'(\\){1,2}', '/', remote_path)

            self.logger.info(f"setup_remote_folders (local_path: {local_path}, remote_path: {remote_path})")

            with paramiko.SSHClient() as ssh:
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(hostname=self.host, username=self.usr, pkey=self.key)
                with ssh.open_sftp() as sftp:
                    # determine local directory structure, establish same structure on remote host
                    for root, dirs, files in os.walk(local_path):
                        root = re.sub(r'(/?\.?\\){1,2}', '/', root).replace(local_path, remote_path)
                        self.logger.debug(f"root: {root}")
                        try:
                            sftp.mkdir(root, mode=16877)
                        except OSError as err:
                            # [todo] check if remote items exists, adapt error message accordingly ...
                            self.logger.error(f"Could not create '{root}', error: {err}. Maybe path exists already?")
                            pass
                    sftp.close()

        except Exception as err:
            self.logger.error(err)


    def put_file(self, local_path: str, remote_path: str):
        """Send a file to a remote host using SFTP and SSH.

        Args:
            local_path (str): full path to local file
            remote_path (str): relative path to remote directory
        """
        try:
            if os.path.exists(local_path):
                # remove the file name from remote_path in case it was appended, then add the file name
                remote_path = os.path.join(os.path.dirname(remote_path), os.path.basename(local_path)).replace('\\', '/')
                with paramiko.SSHClient() as ssh:
                    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    ssh.connect(hostname=self.host, username=self.usr, pkey=self.key)
                    with ssh.open_sftp() as sftp:
                        attr = sftp.put(localpath=local_path,
                                        remotepath=remote_path,
                                        confirm=True)
                        sftp.close()
                    self.logger.info(f"put_file {local_path} > {remote_path}")
                return attr
            else:
                raise ValueError(f"local_path {local_path} does not exist.")
        except Exception as err:
            self.logger.error(err)


    def remove_remote_item(self, remote_path: str) -> None:
        """
        Remove a file or prune (the last part of remote_path, not iterative) an (empty) directory from a remote host using SFTP and SSH.

        Args:
            remote_path (str): relative path to remote item
        """
        try:
            remote_path = remote_path.replace('\\', '/')
            if self.remote_item_exists(remote_path):
                with paramiko.SSHClient() as ssh:
                    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    ssh.connect(hostname=self.host, username=self.usr, pkey=self.key)
                    with ssh.open_sftp() as sftp:
                        try:
                            if sftp.listdir(remote_path):                        
                                # neither an empty directory, nor a file: do nothing
                                self.logger.warning('Cannot remove non-empty directory. Provide full path to file to remove it, or empty the directory first.')
                                return
                            else:
                                # remote path is an empty directory
                                sftp.rmdir(remote_path)
                        except:
                            # remote_path is a file
                            try:
                                sftp.remove(remote_path)
                            except Exception as err:
                                self.logger.error(err)
                        self.logger.info(f"remove_remote_item {remote_path}")
                        sftp.close()

            else:
                raise ValueError("remove_remote_item: remote_path does not exist.")
        except Exception as err:
            self.logger.error(f"remove_remote_item: {err}")


    def setup_remote_path(self, remote_path: str) -> str:
        """Create (and navigate to the leaf of) a remote path.

        Args:
            remote_path (str): Remote path to create. NB: The last bit of the path is always interpreted as a directory

        Returns:
            str: full path of current remote directory
        """
        try:
            remote_path = remote_path.replace('\\', '/').replace('./', '')
            with paramiko.SSHClient() as ssh:
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(hostname=self.host, username=self.usr, pkey=self.key)
                with ssh.open_sftp() as sftp:
                    # create remote path if it doesn't exist and enter it        
                    try:
                        sftp.chdir(remote_path)
                    except IOError:
                        parts = remote_path.split("/")
                        current_path = '.'
                        for part in parts:
                            if part:
                                current_path = f"{current_path}/{part}"
                            try:
                                sftp.chdir(current_path)
                            except IOError:
                                sftp.mkdir(part)
                                sftp.chdir(part)
                                self.logger.debug(f"setup_remote_path: created {part}")
                    cwd = sftp.getcwd()
                    self.logger.debug(f"setup_remote_path: switched to {cwd}")
                    if cwd is None:
                        cwd = str()
            return cwd
        except Exception as err:
            self.logger.error(f"setup_remote_path: {err}")
            return str()


    def transfer_files(self, local_path: str=str(), remote_path: str=str(), remove_on_success: bool=True) -> None:
        """Transfer (move) all files from local_path and sub-folders to remote_path.

        Args:
            local_path (str, optional): full path to local directory location. Defaults to empty string.
            remote_path (str, optional): relative path to remote directory location. Defaults to empty string. 
                                         NB: last element in remote_path must be a directory, not a file!
            remove_on_success (bool, optional): Remove successfully transfered files from local_path?. Defaults to True.
        """
        try:
            self.transfered = []
            if not local_path:
                local_path = self.local_path

            if not remote_path:
                remote_path = self.remote_path

            # sanitize paths
            local_path = local_path.replace('\\', '/')
            remote_path = remote_path.replace('\\', '/')

            with paramiko.SSHClient() as ssh:
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(hostname=self.host, username=self.usr, pkey=self.key)
                with ssh.open_sftp() as sftp:
                    # walk local directory structure, put file to remote location
																
                    top = local_path
                    for root, dirs, files in os.walk(top=top):
                        for file in files:
                            local_file = os.path.join(root, file).replace('\\', '/').rstrip('/')
                            parts = root.replace('\\', '/').replace(local_path, '').strip('/')
                            remote_file = f"{remote_path}/{parts}/{file}"
                            
                            cwd = self.setup_remote_path(f"{remote_path}/{parts}")
												 
                            attr = sftp.put(localpath=local_file, remotepath=remote_file, confirm=True)
                            self.logger.debug(f"put {local_file} > {remote_file}")
                            self.transfered.append(file)

                            if remove_on_success:
																								  
                                local_size = os.stat(local_file).st_size
                                remote_size = attr.st_size
                                if remote_size == local_size:
                                    os.remove(local_file)
                                else:
                                    self.logger.warning(f"local file size: {local_size}, remote file: {remote_size} differ. Did not remove {local_file}.")
                return
                            
        except Exception as err:
            self.logger.error(f"transfer_files: {local_path} > {remote_path}: {err}")


    def setup_transfer_schedules(self, local_path: str, remote_path: str, remove_on_success: bool=True, interval: int=60):
        try:
            if interval==10:
                minutes = [f"{interval*n:02}" for n in range(6) if interval*n < 6]
                for minute in minutes:
                    schedule.every(1).hour.at(f"{minute}:10").do(self.transfer_files, local_path, remote_path, remove_on_success)
            elif (interval % 60) == 0:
                hrs = [f"{n:02}:00:10" for n in range(0, 24, interval // 60)]
                for hr in hrs:
                    schedule.every(1).day.at(hr).do(self.transfer_files, local_path, remote_path, remove_on_success)
            elif interval==1440:
                schedule.every(1).day.at('00:00:10').do(self.transfer_files, local_path, remote_path, remove_on_success)
            else:
                raise ValueError("'interval' must be 10 minutes or a multiple of 60 minutes and a maximum of 1440 minutes.")
            
        except Exception as err:
            self.schedule_logger.error(err)


if __name__ == "__main__":
    pass