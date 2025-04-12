import logging
import os
import time
import zipfile
from datetime import datetime, timedelta
from typing import List, Tuple

import numpy as np
import polars as pl
import schedule
import serial

from pydaq.utils.utils import load_config, setup_logging


class Aurora3000:
    def __init__(self, config: dict):
        """
        Initialize the Aurora 3000 instrument class with parameters from a configuration file.

        :param config_file: Path to the configuration file.
        """
        try:
            # configure logging
            _logger = f"{os.path.basename(config['logging']['file'])}".split('.')[0]
            self.logger = logging.getLogger(f"{_logger}.{__name__}")
            self.logger.info("Initialize Aurora 3000 nephelometer")
            
            # configure serial port
            self.port = config['Aurora3000']['serial_port']
            self.baudrate = int(config['Aurora3000']['serial_baudrate'])
            self.timeout = float(config['Aurora3000']['serial_timeout'])
            
            # configure data collection
            self.sampling_interval = int(config['Aurora3000']['sampling_interval'])
            self.reporting_interval = int(config['Aurora3000']['reporting_interval'])
            if not (self.reporting_interval==10 or (self.reporting_interval % 60==0 and self.reporting_interval<=1440)):
                raise ValueError("'reporting_interval' must be 10 or a multiple of 60 and less or equal to 1440 minutes.")

            # configure data storage, staging and remote transfer
            root = os.path.expanduser(config['root'])
            self.data_path = os.path.join(root, config['data'], config['Aurora3000']['data_path'])
            self.staging_path = os.path.join(root, config['staging'], config['Aurora3000']['staging_path'])
            self.remote_path = config['Aurora3000']['remote_path']
           
            # configure file header
            self.header = 'dtm,ssp1,ssp2,ssp3,sbsp1,sbsp2,sbsp3,sample_temp,enclosure_temp,RH,pressure,major_state,DIO_state\n'

            # store readings and timestamp
            # initialize data response and datetime stamp           
            self._instant_readings = []
            self._dio_states = []
            self._last_timestamp = None
            self._data = str()
            self._dtm = None
            self.data_file = str()

        except serial.SerialException as err:
            self.logger.error(f"Serial communication error: {err}")
            pass
        except Exception as err:
            self.logger.error(f"General error: {err}")
            pass


    def setup_schedules(self):
        try:
            # configure data acquisition
            # collect readings every 5 seconds
            schedule.every(5).seconds.do(self.accumulate_instant_readings)
            # compute average every sampling_interval minute(s)
            schedule.every(self.sampling_interval).minutes.at(':00').do(self.accumulate_averages)            
            
            # configure saving and staging schedules
            if self.reporting_interval==10:
                self._file_timestamp_format = '%Y%m%d%H%M'
                minutes = [f"{self.reporting_interval*n:02}" for n in range(6) if self.reporting_interval*n < 6]
                for minute in minutes:
                    schedule.every(1).hour.at(f"{minute}:01").do(self._save_and_stage_data)
            elif self.reporting_interval==60:
                self._file_timestamp_format = '%Y%m%d%H'
                schedule.every(1).hour.at('00:02').do(self._save_and_stage_data)
            elif self.reporting_interval==1440:
                self._file_timestamp_format = '%Y%m%d'
                schedule.every(1).day.at('00:00:02').do(self._save_and_stage_data)

            # configure archive
            # self.archive_path = os.path.join(root, config['Aurora3000']['archive'])
            # os.makedirs(self.archive_path, exist_ok=True)


        except Exception as err:
            self.logger.error(err)


    def serial_comm(self, cmd: str, sep: str=',') -> str:
        try:
            data = bytes()
            with serial.Serial(self.port, self.baudrate, 8, 'N', 1, self.timeout) as ser:
                ser.write(f"{cmd}\r".encode())
                time.sleep(0.2)
                while ser.in_waiting > 0:
                    data += ser.read(1024)
                    time.sleep(0.1)
                data = data.decode("utf-8")
                data = data.replace('\r\n\n', '\r\n').replace(", ", ",").replace(",", sep)
            return data
        except Exception as err:
            self.logger.error(err)


    def read_new_data(self, sep: str=',') -> str:
        try:
           return self.serial_comm('***D')
        except Exception as err:
            self.logger.error(err)


    def get_instrument_id(self, sep: str=',') -> str:
        try:
           return self.serial_comm('ID0')
        except Exception as err:
            self.logger.error(err)


    def get_current_data(self, sep: str=',') -> str:
        try:
           return self.serial_comm('VI099')
        except Exception as err:
            self.logger.error(err)


    def get_status_word(self, sep: str=',') -> str:
        try:
           return self.serial_comm('VI088')
        except Exception as err:
            self.logger.error(err)


    def parse_current_data(self, reading: str) -> Tuple[datetime, np.ndarray]:
        """Parses a comma-separated reading string into a datetime object and a numpy array of values."""
        try:
            parts = reading.split(',')
            timestamp = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
            values = list(map(float, parts[1:-1]))
            values.append(int(parts[-1], 16))    # Convert last element from hex to decimal
            return timestamp, values
        except Exception as err:
            self.logger.error(err)


    def _round_to_full_minute(self, timestamp: datetime) -> datetime:
        """Rounds a datetime object to the nearest full minute."""
        try:
            if timestamp.second >= 30:
                timestamp += timedelta(minutes=1)
            return timestamp.replace(second=0, microsecond=0)
        except Exception as err:
            self.logger.error(err)


    def accumulate_instant_readings(self) -> None:
        """Collects a single reading and appends it to the self._instant_readings list."""
        try:
            reading_str = self.get_current_data()  # Assuming get_readings returns a string
            timestamp, values = self.parse_current_data(reading_str)
            self._last_timestamp = timestamp
            self._instant_readings.append(values)
            self.logger.debug(reading_str)
        except Exception as err:
            self.logger.error(err)


    def accumulate_averages(self) -> None:
        """
        Computes the average of the collected self._instant_readings and appends the result to self._data.
        The timestamp for the average is the last timestamp of the instant readings, rounded to a full minute.
        """
        try:
            if self._instant_readings:
                # Stack self._instant_readings and compute mean across columns
                readings_array = np.stack(self._instant_readings)
                averages = np.mean(readings_array, axis=0)
                
                # Round the last timestamp to the nearest full minute
                dtm = self._round_to_full_minute(self._last_timestamp)
                
                # Clear the self._instant_readings for the next 1-minute collection
                self._instant_readings = []
                
                # Return the rounded timestamp followed by the averaged values
                current_averages = ",".join(f"{avg:.3f}" for avg in averages)
                # self._data = f"{self._data}{dtm.strftime('%Y-%m-%d %H:%M:%S')},{current_averages}\n"
                self._data = f"{self._data}{dtm.isoformat(timespec='seconds')},{current_averages}\n"
                self.logger.info(f"Aurora3000, {current_averages[:60]}[...]")
            return

        except Exception as err:
            self.logger.error(err)


    def _save_data(self) -> None:
        try:
            data_file = str()
            if self._data:
                dtm = datetime.now()

                # configure folders needed
                yyyy = dtm.strftime('%Y')
                mm = dtm.strftime('%m')
                path = os.path.join(self.data_path, yyyy, mm)
                if self.reporting_interval < 1440:
                    path = os.path.join(path, dtm.strftime('%d'))
                os.makedirs(path, exist_ok=True)

                # create appropriate file path
                timestamp = dtm.strftime(self._file_timestamp_format)               
                data_file = os.path.join(path, f"aurora3000-{timestamp}.csv")

                # configure file mode, open file and write to it
                if os.path.exists(self.data_file):
                    with open(file=data_file, mode='a') as fh:
                        fh.write(self._data)
                else:
                    with open(file=data_file, mode='w') as fh:
                        fh.write(self.header)
                        fh.write(self._data)
                self.logger.info(f"file saved: {data_file}")
            
                # reset self._data
                self._data = str()

            self.data_file = data_file
            return

        except Exception as err:
            self.logger.error(err)


    def _stage_file(self):
        """ Create zip file from self.data_file and stage archive.
        """
        try:
            if self.data_file:
                os.makedirs(self.staging_path, exist_ok=True)

                archive = os.path.join(self.staging_path, os.path.basename(self.data_file).replace('.csv', '.zip'))
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    zf.write(self.data_file, os.path.basename(self.data_file))
                    self.logger.info(f"file staged: {archive}")

        except Exception as err:
            self.logger.error(err)


    def _save_and_stage_data(self):
        self._save_data()
        self._stage_file()


    def start(self):
        """
        Start the data collection process.
        """
        self.setup_schedules()

        while True:
            schedule.run_pending()
            time.sleep(1)


if __name__ == "__main__":
    neph = Aurora3000(config_file='nrbdaq.yml')
    neph.start()
