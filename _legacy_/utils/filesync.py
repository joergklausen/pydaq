# %%
import os
import datetime
import time
import shutil
from filecmp import dircmp
import zipfile
import colorama


# %%
def rsync(source: str, target: str, buckets: str = [None, "daily", "monthly"], days: int = 1, delay: int=3600) -> list:
    """Determine files under 'source' that are not present under 'target' and copy them over.

    Args:
        source (str): full path to top level directory of source
        target (str): full path to top level directory of target
        buckets (str, optional): Are files organized in sub-folders by yyyy, mm, dd (daily) or by yyyy, mm (monthly) or not at all (None)? Defaults to [None, "daily", "monthly"].
        days (int, optional): Number of days to look back. Defaults to 1.
        delay (int, optional): Period (seconds) during which the file must not have been modified. Determines which files are copied. Defaults to 3600.

    Raises:
        ValueError: raised if buckets are not correctly specified.

    Returns:
        list: list of files with full file copied to target.
    """
    try:
        sep = os.path.sep
        if buckets=="daily":
            fmt = f"%Y{sep}%m{sep}%d"
            if delay is None:
                delay = 3600
        elif buckets=="monthly":
            fmt = f"%Y{sep}%m"
            if delay is None:
                delay = 86400
        elif buckets is None:
            fmt = None
            if delay is None:
                delay = 3600
        else:
            raise ValueError(f"'buckets' must be <None|hourly|daily>.")

        files_copied = []
        now = time.time()

        if fmt:
            for day in range(0, days):
                dte = (datetime.datetime.now() - datetime.timedelta(days=day)).strftime(fmt)
                src = os.path.join(source, dte)
                if os.path.exists(src):
                    tgt = os.path.join(target, dte)
                    os.makedirs(tgt, exist_ok=True)
                    dcmp = dircmp(src, tgt)
                    for file in dcmp.left_only:
                        if os.path.isfile(os.path.join(src, file)):
                            # print(now)
                            # print(os.path.getmtime(os.path.join(src, file)))
                            # print(now - os.path.getmtime(os.path.join(src, file)))
                            if (now - os.path.getmtime(os.path.join(src, file))) > delay:
                                shutil.copy(os.path.join(src, file), os.path.join(tgt, file))
                                files_copied.append(os.path.join(tgt, file))
                else:
                    print(f"'{src}' does not exist.")
        else:
            if os.path.exists(source):
                os.makedirs(target, exist_ok=True)
                dcmp = dircmp(source, target)
                for file in dcmp.left_only:
                    if os.path.isfile(os.path.join(source, file)):
                        if (now - os.path.getmtime(os.path.join(source, file))) > delay:
                            shutil.copy(os.path.join(source, file), os.path.join(target, file))
                            files_copied.append(os.path.join(target, file))
            else:
                print(f"'{source}' does not exist.")

        return files_copied

    except Exception as err:
        print(err)

# %%


    def store_and_stage_new_files(self):
        try:
            # list data files available on netshare
            # retrieve a list of all files on netshare for sync_period, except the latest file (which is presumably still written too)
            # retrieve a list of all files on local disk for sync_period
            # copy and stage files available on netshare but not locally
            
            if self._data_storage_interval == 'hourly':
                ftime = "%Y/%m/%d"
            elif self._data_storage_interval == 'daily':
                ftime = "%Y/%m"
            else:
                raise ValueError(f"Configuration 'data_storage_interval' of {self._name} must be <hourly|daily>.")

            try:
                if os.path.exists(self._netshare):
                    for delta in (0, 1):
                        relative_path = (datetime.datetime.today() - datetime.timedelta(days=delta)).strftime(ftime)
                        netshare_path = os.path.join(self._netshare, relative_path)
                        # local_path = os.path.join(self._datadir, relative_path)
                        local_path = os.path.join(self._datadir, time.strftime("%Y"), time.strftime("%m"), time.strftime("%d"), relative_path)
                        os.makedirs(local_path, exist_ok=True)

                        # files on netshare except the most recent one
                        if delta==0:
                            netshare_files = os.listdir(netshare_path)[:-1]
                        else:
                            netshare_files = os.listdir(netshare_path)

                        # local files
                        local_files = os.listdir(local_path)

                        files_to_copy = set(netshare_files) - set(local_files)

                        for file in files_to_copy:
                            # store data file on local disk
                            shutil.copyfile(os.path.join(netshare_path, file), os.path.join(local_path, file))            

                            # stage data for transfer
                            stage = os.path.join(self._staging, self._name)
                            os.makedirs(stage, exist_ok=True)

                            if self._zip:
                                # create zip file
                                archive = os.path.join(stage, "".join([file[:-4], ".zip"]))
                                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as fh:
                                    fh.write(os.path.join(local_path, file), file)
                            else:
                                shutil.copyfile(os.path.join(local_path, file), os.path.join(stage, file))

                            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} .store_and_stage_new_files (name={self._name}, file={file})")
                else:
                    msg = f"{time.strftime('%Y-%m-%d %H:%M:%S')} (name={self._name}) Warning: {self._netshare} is not accessible!)"
                    if self._log:
                        self._logger.error(msg)
                    print(colorama.Fore.RED + msg)

            except:
                print(colorama.Fore.RED + f"{time.strftime('%Y-%m-%d %H:%M:%S')} (name={self._name}) Warning: {self._netshare} is not accessible!)")

                return
                
        except Exception as err:
            if self._log:
                self._logger.error(err)
            print(err)

    # Methods below not currently in use

    def store_and_stage_latest_file(self):
        try:
            # get data file from netshare
            if self._data_storage_interval == 'hourly':
                path = os.path.join(self._netshare, time.strftime("/%Y/%m/%d"))
            elif self._data_storage_interval == 'daily':
                path = os.path.join(self._netshare, time.strftime("/%Y/%m"))
            else:
                raise ValueError(f"Configuration 'data_storage_interval' of {self._name} must be <hourly|daily>.")
            file = max(os.listdir(path))

            # store data file on local disk
            shutil.copyfile(os.path.join(path, file), os.path.join(self._datadir, file))

            # stage data for transfer
            stage = os.path.join(self._staging, self._name)
            os.makedirs(stage, exist_ok=True)

            if self._zip:
                # create zip file
                archive = os.path.join(stage, "".join([file[:-4], ".zip"]))
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as fh:
                    fh.write(os.path.join(path, file), file)
            else:
                shutil.copyfile(os.path.join(path, file), os.path.join(stage, file))

            print("%s .store_and_stage_latest_file (name=%s)" % (time.strftime('%Y-%m-%d %H:%M:%S'), self._name))

        except Exception as err:
            if self._log:
                self._logger.error(err)
            print(err)


    def store_and_stage_files(self):
        """
        Fetch data files from local source and move to datadir. Zip files and place in staging area.

        :return: None
        """
        try:
            print("%s .store_and_stage_files (name=%s)" % (time.strftime('%Y-%m-%d %H:%M:%S'), self._name))

            # get data file from local source
            files = os.listdir(self._source)

            if files:
                # staging location for transfer
                stage = os.path.join(self._staging, self._name)
                os.makedirs(stage, exist_ok=True)

                # store and stage data files
                for file in files:
                    # stage file
                    if self._zip:
                        # create zip file
                        archive = os.path.join(stage, "".join([file[:-4], ".zip"]))
                        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as fh:
                            fh.write(os.path.join(self._source, file), file)
                    else:
                        shutil.copyfile(os.path.join(self._source, file), os.path.join(stage, file))

                    # move to data storage location
                    shutil.move(os.path.join(self._source, file), os.path.join(self._datadir, file))

        except Exception as err:
            if self._log:
                self._logger.error(err)
            print(err)
