# -*- coding: utf-8 -*-
"""
Define a class G2401 facilitating communication with a Picarro G2401 instrument.

@author: joerg.klausen@meteoswiss.ch
"""

import logging
import os
import shutil
import socket
import time
import zipfile

import colorama
from utils.filesync import rsync

from instr.instrument import Instrument
from utils.logging import setup_logging


class G2401:
    """
    Instrument of type Picarro G2401.

    Instrument of type Picarro G2410 with methods, attributes for interaction.
    """

    def __init__(self, name: str, config: dict) -> None:
        """
        Constructor

        Parameters
        ----------
        name : str
            name of instrument as defined in config file
        config : dict
            dictionary of attributes defining the instrument and port
        """
        colorama.init(autoreset=True)

        try:
            # read instrument control properties for later use
            self._name = name
            self._serial_number = config[name]['serial_number']

            # configure logging
            _logger = f"{os.path.basename(config['logging']['file'])}".split('.')[0]
            self.logger = logging.getLogger(f"{_logger}.{__name__}")
            self.logger.info(f"[{self._name}] Initializing Picarro G2401 (S/N: {self._serial_number})")

            # configure tcp/ip
            self._sockaddr = (config[name]['socket']['host'],
                             config[name]['socket']['port'])
            self._socktout = config[name]['socket']['timeout']
            self._socksleep = config[name]['socket']['sleep']

            # reporting/storage
            self._buckets = config[name]['buckets']

            # netshare of user data files
            dbs = r"\\"
            self._netshare = os.path.join(f"{dbs}{config[name]['socket']['host']}", config[name]['netshare'])

            # days up to present for which files should be synched to data directory
            self._days_to_sync = config[name]['days_to_sync']

            # setup data directory
            root = os.path.expanduser(config['root'])
            self.data_path = os.path.join(root, config['data'], config[name]['data_path'])
            os.makedirs(self.data_path, exist_ok=True)

            # staging area for files to be transfered
            self.staging_path = os.path.join(root, config['staging'], config[name]['staging_path'])
            os.makedirs(self.staging_path, exist_ok=True)
            self.staging_zip = config[name]['staging_zip']

            # sampling, aggregation, reporting/storage
            self.reporting_interval = config[name]['reporting_interval']
            if not (self.reporting_interval==10 or (self.reporting_interval % 60)==0) and self.reporting_interval<=1440:
                raise ValueError('reporting_interval must be 10 or a multiple of 60 and less or equal to 1440 minutes.')

            # configure remote transfer
            self.remote_path = config[name]['remote_path']

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}")


    def tcpip_comm(self, cmd: str, tidy=True) -> str:
        """
        Send a command and retrieve the response. Assumes an open connection.

        :param cmd: command sent to instrument
        :param tidy: remove cmd echo, \n and *\r\x00 from result string, terminate with \n
        :return: response of instrument, decoded
        """
        rcvd = b''
        try:
            # open socket connection as a client
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM, ) as s:
                # connect to the server
                s.settimeout(self._socktout)
                s.connect(self._sockaddr)

                # send data
                s.sendall((cmd + chr(13) + chr(10)).encode())
                time.sleep(float(self._socksleep))

                # receive response
                while True:
                    try:
                        data = s.recv(1024)
                        rcvd = rcvd + data
                    except:
                        break

            # decode response, tidy
            rcvd = rcvd.decode()
            if tidy:
                if "\n" in rcvd:
                    rcvd = rcvd.split("\n")[0]

            return rcvd

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}")
            return str()


    def print_co2_ch4_co(self) -> None:
        try:
            conc = self.tcpip_comm("_Meas_GetConc").split(';')[0:3]
            self.logger.info(colorama.Fore.GREEN + f"[{self._name}] CO2 {conc[0]} ppm  CH4 {conc[1]} ppm  CO {conc[2]} ppm")

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}")


    def store_and_stage_files(self):
        """Copy files from source (netshare folder) to target (datadir) and stage them in the staging area for transfer.

        Raises:
            ValueError: raised if buckets is not correctly specified. Based on this, the subfolder structure is assumed.
        """
        sep = os.path.sep
        try:            
            if os.path.exists(str(self._netshare)):
                # copy 'new' files from source to target
                files_received = rsync(source=str(self._netshare), 
                                        target=str(self.data_path), 
                                        buckets=str(self._buckets), 
                                        days=int(self._days_to_sync))
                
                # stage data for transfer
                for file in files_received:
                    if self.staging_zip:
                        # create zip file
                        archive = os.path.join(self.staging_path, os.path.basename(file).replace('.dat', '.zip'))
                        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as fh:
                            fh.write(file, os.path.basename(file))
                    else:
                        shutil.copyfile(os.path.join(self.data_path, file), os.path.join(self.staging_path, os.path.basename(file)))

                    self.logger.info(f"[{self._name}] .store_and_stage_files (file={os.path.basename(file)})")

            else:
                self.logger.error(colorama.Fore.RED + f"[{self._name}]: {self._netshare} is not accessible!")
            return

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}")


    def get_meas_getconc(self) -> str:
        """
        Retrieve instantaneous data from instrument

        :return:
        """
        try:
            return self.tcpip_comm('_Meas_GetConc')

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}")
            return str()


    def get_co2_ch4_co(self) -> list:
        """
        Get instantaneous cleaned response to '_Meas_GetConc' from instrument.

        :return: list: concentration values from instrument
        """
        try:
            return self.tcpip_comm("_Meas_GetConc").split(';')[0:3]

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}")
            return list()
