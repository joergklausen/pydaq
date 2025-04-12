import logging
import os
import shutil
import zipfile
from datetime import datetime

import colorama
import polars as pl
import schedule
import serial


class AE31:
    def __init__(self, config: dict):
        """Initialize the AE31 instrument class with parameters from a configuration file.

        Args:
            config (dict): general configuration
        """
        colorama.init(autoreset=True)

        try:
            # configure logging
            _logger = f"{os.path.basename(config['logging']['file'])}".split('.')[0]
            self.logger = logging.getLogger(f"{_logger}.{__name__}")
            self.logger.info("Initialize AE31")
            
            # configure serial port
            self._serial_port = config['AE31']['serial_port']
            self._serial_timeout = config['AE31']['serial_timeout']
            
            root = os.path.expanduser(config['root'])

            # configure data collection and saving
            self.sampling_interval = int(config['AE31']['sampling_interval'])
            self.reporting_interval = int(config['AE31']['reporting_interval'])
            if not (self.reporting_interval % 60)==0 and self.reporting_interval<=1440:
                raise ValueError('reporting_interval must be a multiple of 60 and less or equal to 1440 minutes.')
            header = "dtm,id,date,time,UV370,B470,G520,Y590,R660,IR880,IR950,flow"
            header = f"{header},UV370_1,UV370_2,UV370_3,UV370_4,,UV370_5,UV370_6"
            header = f"{header},B470_1,B470_2,B470_3,B470_4,,B470_5,B470_6"
            header = f"{header},G520_1,G520_2,G520_3,G520_4,,G520_5,G520_6"
            header = f"{header},Y590_1,Y590_2,Y590_3,Y590_4,,Y590_5,Y590_6"
            header = f"{header},R660_1,R660_2,R660_3,R660_4,,R660_5,R660_6"
            header = f"{header},IR880_1,IR880_2,IR880_3,IR880_4,,IR880_5,IR880_6"
            header = f"{header},IR950_1,IR950_2,IR950_3,IR950_4,,IR950_5,IR950_6\n"
            self.header = header

            self.data_path = os.path.join(root, config['data'], config['AE31']['data_path'])
            os.makedirs(self.data_path, exist_ok=True)
            # schedule.every(int(self.sampling_interval)).minutes.at(':00').do(self.accumulate_data)
            # schedule.every(int(self.sampling_interval)).minutes.at(':01').do(self._save_data)
                     
            # configure staging
            self.staging_path = os.path.join(root, config['staging'], config['AE31']['staging_path'])
            # os.makedirs(self.staging_path, exist_ok=True)
            # if self.reporting_interval==1440:
            #     schedule.every(1).day.at('00:00:05').do(self._save_and_stage_data)
            # elif self.reporting_interval==60:
            #     schedule.every(1).hour.at('00:05').do(self._save_and_stage_data)

            # configure archive
            # self.archive_path = os.path.join(root, config['AE31']['archive'])
            # os.makedirs(self.archive_path, exist_ok=True)

            # configure remote transfer
            self.remote_path = config['AE31']['remote_path']

            # initialize data response and datetime stamp           
            self._data = str()
            self.data_file = str()
            self._dtm = None

        except Exception as err:
            self.logger.error(err)
            pass

    
    def setup_schedules(self):
        try:
            # configure folders needed
            os.makedirs(self.data_path, exist_ok=True)
            os.makedirs(self.staging_path, exist_ok=True)
            # os.makedirs(self.archive_path, exist_ok=True)

            # configure data acquisition schedule
            schedule.every(self.sampling_interval).minutes.at(':00').do(self.accumulate_data)
            
            # configure saving and staging schedules
            if self.reporting_interval==10:
                self._file_timestamp_format = '%Y%m%d%H%M'
                minutes = [f"{self.reporting_interval*n:02}" for n in range(6) if self.reporting_interval*n < 6]
                for minute in minutes:
                    schedule.every(1).hour.at(f"{minute}:01").do(self._save_and_stage_data)
            elif self.reporting_interval==60:
                self._file_timestamp_format = '%Y%m%d%H'
                schedule.every(1).hour.at('00:01').do(self._save_and_stage_data)
            elif self.reporting_interval==1440:
                self._file_timestamp_format = '%Y%m%d'
                schedule.every(1).day.at('00:00:01').do(self._save_and_stage_data)

        except Exception as err:
            self.logger.error(err)


    def accumulate_data(self):
        """
        Read data waiting at serial port. Opens the port, assigns lines read to self._data.
        """
        try:
            with serial.Serial(self._serial_port, 9600, 8, 'N', 1, int(self._serial_timeout)) as ser:
                self._dtm = datetime.now().isoformat(timespec='seconds')
                _ = f"{self._dtm},{ser.readline().decode('ascii').strip()}\n"
                self._data = f"{self._data}{_}"
                self.logger.info(f"AE31, {_[:60]} [...]"),
            return

        except serial.SerialException as err:
            self.logger.error(f"SerialException: {err}")
            pass
        except Exception as err:
            self.logger.error(err)


    def _save_data(self):
        """
        Saves data to a .csv file at self.data_path. 
        Filenames have the form 'AE31-{timestamp}.csv', where timestamp depends on self.reporting_interval.
        """
        try:
            if self._data:
                timestamp = datetime.now().strftime(self._file_timestamp_format)               
                self.data_file = os.path.join(self.data_path, f"ae31-{timestamp}.csv")
                if os.path.exists(self.data_file):
                    mode = 'a'
                    header = ''
                else:
                    mode = 'w'
                    header = self.header
                    self.logger.info(f"AE31, Reading data and writing to {self.data_path}/ae31-{timestamp}.csv")
                
                # open file and write to it
                with open(file=self.data_file, mode=mode) as fh:
                    fh.write(f"{header}{self._data}")

                # reset self._data
                self._data = str()

        except Exception as err:
            self.logger.error(err)


    def _stage_file(self):
        """ Create zip file from self.data_file and stage archive.
        """
        try:
            if self.data_file:
                archive = os.path.join(self.staging_path, os.path.basename(self.data_file).replace('.csv', '.zip'))
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    zf.write(self.data_file, os.path.basename(self.data_file))
                    self.logger.info(f"file staged: {archive}")

        except Exception as err:
            self.logger.error(err)


    def _save_and_stage_data(self):
        self._save_data()
        self._stage_file()

    # def _stage_data(self):
    #     """
    #     Copy final data file to the staging area. 
    #     Establish the timestamp of the previous (now complete) file, then copy it to the staging area.
    #     """
    #     if self.reporting_interval==1440:
    #         timestamp = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
    #     else:
    #         timestamp = (datetime.now() - timedelta(hours=self.reporting_interval)).strftime('%Y%m%d%H')
    #     file = f"ae31-{timestamp}.csv"
    #     self.logger.debug(f"file to stage: {file}")
    #     try:
    #         if os.path.exists(os.path.join(self.data_path, file)):
    #             dst = shutil.copyfile(src=os.path.join(self.data_path, file), 
    #                             dst=os.path.join(self.staging_path, file))
    #             self.logger.info(f"file staged: {dst}")
    #     except Exception as err:
    #         self.logger.error(err)


    def csv_to_df(self, file: str) -> pl.DataFrame:
        """Read an AE31 .csv file and return a pl.DataFrame

            14.9.3  Data File Format - Seven wavelength Instruments 
            The AE-3 series seven wavelength Aethalometers measure optical absorbance at seven optical wavelengths 
            from 370 to 950 nm.  The data are reported on a single line written to disk as follows: 
            Expanded Data Format:  “date”, “time”, UV [370 nm] result, Blue [470 nm] result, Green [520 nm] result, 
            Yellow [590 nm] result, Red [660 nm] result, IR1 [880 nm, “standard BC”] result, IR2 [950 nm] result,  
            #air flow (LPM), bypass fraction#, and then the following columns of data repeated for the seven 
            measurement wavelengths: 
            sensing zero signal, sensing beam signal, reference zero signal, reference beam signal, optical attenuation, 
            air flow (LPM), bypass fraction.    
            The ‘air flow’ and ‘bypass fraction’ columns are repeated to allow for easy visual identification of the 
            separation between the seven sets of data columns. 
            A typical line in the data file might look like: 
            "24-jul-00","16:40", 610 , 604 , 605 , 612 , 617 , 611 , 641 , 
            3.131, -.9812 , -.9814 , 1.1881 , 1.8384 , 1 , 6.4 , 
            2.704 , -.9812 , -.9814 , 4.2483 , 2.7373 , 1 , 6.4 , 
            2.45  , -.9812 , -.9814 , 2.1716 , 1.9438 , 1 , 6.4 , 
            2.232 , -.9812 , -.9814 , 2.854 , 3.5259 , 1 , 6.4 , 
            1.957 , -.9812 , -.9814 , 3.3428 , 2.596 , 1 , 6.4 , 
            1.452  , -.9812 , -.9814 , 4.6719 , 3.3935 , 1 , 6.4 , 
            1.396 , -.9812 , -.9814 , 2.705 , 2.438 , 1 , 6.4  
        
        Args:
            file (str): full path to file

        Returns:
            pl.DataFrame: dataframe with header
        """
        cols = ["dtm","unknown","date","time","UV370","B470","G520","Y590","R660","IR880","IR950","flow",]# "bypass",]
        cols += ["?370", "sens_zero_370","sens_beam_370","ref_zero_370","ref_beam_370","att_370", ]#"flow_370", "bypass_370",] 
        cols += ["?470", "sens_zero_470","sens_beam_470","ref_zero_470","ref_beam_470","att_470", ]#"flow_470", "bypass_470",] 
        cols += ["?520", "sens_zero_520","sens_beam_520","ref_zero_520","ref_beam_520","att_520", ]#"flow_520", "bypass_520",] 
        cols += ["?590", "sens_zero_590","sens_beam_590","ref_zero_590","ref_beam_590","att_590", ]#"flow_590", "bypass_590",] 
        cols += ["?660", "sens_zero_660","sens_beam_660","ref_zero_660","ref_beam_660","att_660", ]#"flow_660", "bypass_660",] 
        cols += ["?880", "sens_zero_880","sens_beam_880","ref_zero_880","ref_beam_880","att_880", ]#"flow_880", "bypass_880",] 
        cols += ["?950", "sens_zero_950","sens_beam_950","ref_zero_950","ref_beam_950","att_950", ]#"flow_950", "bypass_950",] 

        df = pl.DataFrame()

        try:
            with open(file, "r") as fh:
                content = fh.read().replace(" ", "").encode()

            df = pl.read_csv(content, has_header=False)
            df = df.cast({pl.Int64: pl.Float32, pl.Float64: pl.Float32})
            df.columns = cols
            df = df.with_columns(pl.col("dtm").str.to_datetime(time_unit='us', time_zone='UTC'),
                                pl.col("date").str.to_date("%d-%b-%Y").dt.combine(pl.col("time").str.to_time("%H:%M")).alias("dtm_ae31"))

            return df
        except Exception as err:
            self.logger.error(err)
            

    def compile_data(self, remove_duplicates: bool=True, archive: bool=True) -> pl.DataFrame:
        """Compile data files and save as .parquet

        Returns:
            pl.DataFrame: compiled data set
        """
        df = pl.DataFrame()

        for root, dirs, files in os.walk(self.data_path):
            for file in files:
                if df.is_empty():
                    df = self.csv_to_df(os.path.join(root, file))
                else:
                    try:
                        _ = self.csv_to_df(os.path.join(root, file))
                        df = pl.concat([df, _], how="diagonal")
                    except Exception as err:
                        self.logger.error(f"{file} could not be appended. Error: {err}")
                        pass
        if remove_duplicates:
            df = df.unique()
        
        df.sort(by=['dtm_ae31'])

        if archive:
            df.write_parquet(os.path.join(self.archive_path, 'ae31_nrb.parquet'))

        return df


    def plot_data(self, filepath: str, save: bool=True):
        self.logger.warning("Not implemented.")


if __name__ == "__main__":
    pass