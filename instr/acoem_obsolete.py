"""
instr/acoem.py

Define a class NE300 facilitating communication with a Acoem NE-300 nephelometer.

@author: joerg.klausen@meteoswiss.ch
"""

import logging
import os
import socket
import struct
import time
import warnings
import zipfile
from datetime import datetime, timedelta, timezone
import threading

import colorama
import schedule


def _default_run_threaded(job, *args, **kwargs):
    thread = threading.Thread(target=job, args=args, kwargs=kwargs, daemon=True)
    thread.start()
    return thread

class NE300:
    """
    Instrument of type Acoem NE-300 or Ecotech Aurora 3000 nephelometer with methods, attributes for interaction.
    """
    def __init__(self, name: str, config: dict, verbosity: int=0) -> None:
        """
        Initialize instrument class.

        :param name: name of instrument
        :param config: dictionary of attributes defining instrument, serial port and more
            - config[name]['type']
            - config[name]['serial_number']
            - config[name]['serial_id']
            - config[name]['socket']['host']
            - config[name]['socket']['port']
            - config[name]['socket']['timeout']
            - config[name]['socket']['sleep']
            - config[name]['get_data_interval']
            - config[name]['reporting_interval']
            - config[name]['zero_check_duration_minutes']
            - config[name]['span_check_duration_minutes']
            - config['staging']['path'])
            - config[name]['staging_zip']
            - config[name]['protocol']
            - config[name]['data_log']['parameters']
            - config[name]['data_log']['wavelengths']
            - config[name]['data_log']['angles']
            - config[name]['data_log']['interval']
        """
        colorama.init(autoreset=True)

        try:
            self.name = name
            self.type = config[name]['type']
            self.serial_number = config[name]['serial_number']

            # configure logging
            _logger = f"{os.path.basename(config['logging']['file'])}".split('.')[0]
            self.logger = logging.getLogger(f"{_logger}.{__name__}")
            self.logger.info(f"[{self.name}] Initializing {self.type} (S/N: {self.serial_number})", extra={"to_logfile": True})

            self.verbosity = config[name]['verbosity']

            # read instrument control properties for later use
            self.serial_id = config[name]['serial_id']

            # configure tcp/ip
            self.sockaddr = (config[name]['socket']['host'],
                             config[name]['socket']['port'])
            self.socktout = config[name]['socket']['timeout']
            # self.__socksleep = config[name]['socket']['sleep']
            
            # flag to maintain state to prevent concurrent tcpip communication in a threaded setup
            self._tcpip_line_is_busy = False

            # configure comms protocol
            if config[name]['protocol'] in ["acoem", "aurora"]:
                self._protocol = config[name]['protocol']
            else:
               raise ValueError("Communication protocol not recognized.")

            # data log
            # self.data_log_parameters = config[name]['data_log']['parameters']
            # self.data_log_wavelengths = config[name]['data_log']['wavelengths']
            # self.data_log_angles = config[name]['data_log']['angles']
            # self.data_log_interval = config[name]['data_log']['interval']

            # sampling, aggregation, reporting/storage
            self.sampling_interval = config[name]['sampling_interval']
            self.reporting_interval = config[name]['reporting_interval']

            # zero and span check interval and durations
            self.zero_span_check_interval_minutes = config[name]['zero_span_check_interval_minutes']
            self.zero_check_duration_minutes = config[name]['zero_check_duration_minutes']
            self.span_check_duration_minutes = config[name]['span_check_duration_minutes']
            if self.zero_span_check_interval_minutes % 60 != 0:
                raise ValueError(
                    f"zero_span_check_interval_minutes={self.zero_span_check_interval_minutes} must be a multiple of 60 minutes."
                )
            total_duration = self.zero_check_duration_minutes + self.span_check_duration_minutes
            if total_duration >= 60:
                raise ValueError(
                    f"zero_check_duration_minutes + span_check_duration_minutes = {total_duration} must be < 60 minutes."
                )
            self._zero_span_check_hours = self.zero_span_check_interval_minutes // 60
            self._span_offset_min = self.zero_check_duration_minutes                                # mm offset from :00
            self._ambient_offset_min = self.zero_check_duration_minutes + self.span_check_duration_minutes  # mm offset from :00
            self.logger.info(
                "zero/span checks every %d hours, zero @ :00, span @ :%02d, return to ambient @ :%02d",
                self._zero_span_check_hours, self._span_offset_min, self._ambient_offset_min,
            )

            # configure saving, staging and remote path
            root = os.path.expanduser(config['root'])
            self.data_path = os.path.join(root, config['data'], config[name]['data_path'])
            os.makedirs(self.data_path, exist_ok=True)
            self.staging_path = os.path.join(root, config['staging'], config[name]['staging_path'])
            os.makedirs(self.staging_path, exist_ok=True)
            self.staging_zip = config[name]['staging_zip']

            self.remote_path = config[name]['remote_path']

            # retrieve instrument id and other instrument info
            id = self.get_id(verbosity=verbosity)
            if id=={}:
                self.logger.error(colorama.Fore.RED + f"[{self.name}] Could not communicate with instrument. Protocol set to '{self._protocol}'. Please verify instrument settings." + colorama.Fore.GREEN)
            else:
                self.logger.info(f"[{self.name}] id reported: '{id}'.", extra={"to_logfile": True})

                # get version
                version = self.get_version()
                self.logger.info(f"[{self.name}] firmware version reported: {version}.", extra={"to_logfile": True})            

                # put instrument in ambient mode
                state = self.do_ambient(verbosity=verbosity)
                if state==0:
                    self.logger.info(f"[{self.name}] current operation reported: 'ambient'.", extra={"to_logfile": True})
                else:
                    self.logger.warning(colorama.Fore.YELLOW + f"[{self.name}] Could not verify measurement mode as 'ambient'." + colorama.Fore.GREEN)

                # get dtm from instrument, then set date and time
                dtm_found, dtm_set = self.get_set_datetime(dtm=datetime.now(timezone.utc))            
                self.logger.info(f"[{self.name}] dtm found: {dtm_found} > dtm set: {dtm_set}.", extra={"to_logfile": True})            

                # get logging config
                self._datalog_config = self.get_data_log_config()[1:]
                self.logger.info(f"[{self.name}] logging config reported: {self._datalog_config}.", extra={"to_logfile": True})

                # set datalog interval
                datalog_interval = self.set_datalog_interval(verbosity=verbosity)
                self.logger.info(f"[{self.name}] Datalog interval set to {datalog_interval} seconds.")

            # datetime to keep track of retrievals from datalog
            self._start_datalog = datetime.now(timezone.utc).replace(second=0, microsecond=0)

            # initialize data response
            self._data = str()

            # initialize other stuff
            self._file_timestamp_format = str()
            self.data_file = str()

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)

    def setup_schedules(self, run_threaded=_default_run_threaded):
        try:
            # configure data acquisition schedule
            schedule.every(int(self.sampling_interval)).minutes.at(':02').do(run_threaded, self._accumulate_new_data)

            # configure zero and span check schedules
            self._setup_zero_span_check_schedules()

            # configure saving and staging schedules
            if self.reporting_interval==10:
                self._file_timestamp_format = '%Y%m%d%H%M'
                for minute in range(6):
                    schedule.every(1).hour.at(f"{minute}0:05").do(run_threaded, self._save_and_stage_data)
            elif self.reporting_interval==60:
                self._file_timestamp_format = '%Y%m%d%H'
                # schedule.every().hour.at('00:01').do(self._save_and_stage_data)
                schedule.every(1).hour.at('00:05').do(run_threaded, self._save_and_stage_data)
            elif self.reporting_interval==1440:
                self._file_timestamp_format = '%Y%m%d'
                # schedule.every().day.at('00:00:01').do(self._save_and_stage_data)
                schedule.every(1).day.at('00:00:05').do(run_threaded, self._save_and_stage_data)
            else:
                raise ValueError(f"A reporting interval of {self.reporting_interval} is not supported.")
            
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)

    def _setup_zero_span_check_schedules(self, run_threaded=_default_run_threaded) -> None:
        """
        Zero/span/ambient checks aligned to the top of the hour:

        - Zero:     every N hours at :00
        - Span:     every N hours at :<zero_duration>
        - Ambient:  every N hours at :<zero_duration + span_duration>

        Assumes __init__ validated that:
        - zero_span_check_interval_minutes is a multiple of 60
        - zero_check_duration_minutes + span_check_duration_minutes < 60
        """
        try:
            # Zero at :00 every N hours
            schedule.every(self._zero_span_check_hours).hours.at(":00").do(run_threaded, self.do_zero)
            # Span offset within the hour
            schedule.every(self._zero_span_check_hours).hours.at(f":{self._span_offset_min:02d}").do(run_threaded, self.do_span)
            # Ambient offset within the hour
            schedule.every(self._zero_span_check_hours).hours.at(f":{self._ambient_offset_min:02d}").do(run_threaded, self.do_ambient)

            self.logger.info(
                "Scheduled zero/span/ambient: every %d hour(s) at :00/:%02d/:%02d",
                self._zero_span_check_hours, self._span_offset_min, self._ambient_offset_min
            )
        except Exception as err:  # pragma: no cover
            self.logger.error("setup_zero_span_check_schedules failed: " + f"{err}" + colorama.Fore.GREEN)


    def _acoem_checksum(self, x: bytes) -> bytes:
        """
        Compute the checksum of all bytes except checksum and EOT by XORing bytes from left 
        to right. (Reference: Aurora NE Series User Manual v1.2 Appendix A.1)

        Args:
            x (bytes): Bytes 1..(N-2)

        Returns:
            bytes: Checksum (1 Byte)
        """
        try:
            cs = bytes([0])
            for i in range(len(x)):
                cs = bytes([a^b for a,b in zip(cs, bytes([x[i]]))])
            return cs

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            return b''


    def _acoem_timestamp_to_datetime(self, timestamp: int) -> datetime:
        try:
            dtm = timestamp
            SS = dtm % 64
            dtm = dtm // 64
            MM = dtm % 64
            dtm = dtm // 64
            HH = dtm % 32
            dtm = dtm // 32
            dd = dtm % 32
            dtm = dtm // 32
            mm = dtm % 16
            yyyy = dtm // 16 + 2000

            return datetime(yyyy, mm, dd, HH, MM, SS)

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            return datetime(1111, 1, 1)


    def _acoem_datetime_to_timestamp(self, dtm: datetime=datetime.now(timezone.utc)) -> bytes:
        try:
            SS = bin(dtm.time().second)[2:].zfill(6)
            MM = bin(dtm.time().minute)[2:].zfill(6)
            HH = bin(dtm.time().hour)[2:].zfill(5)
            dd = bin(dtm.date().day)[2:].zfill(5)
            mm = bin(dtm.date().month)[2:].zfill(4)
            yyyy = bin(dtm.date().year - 2000).zfill(6)

            return (int(yyyy + mm + dd + HH + MM + SS, base=2)).to_bytes(4, byteorder='big')

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            return b''


    def _acoem_construct_parameter_id(self, base_id: int, wavelength: int, angle: int) -> int:
        """
        Construct ACOEM measurement parameter id. Cf. ACOEM manual for details.
        Parameter ID = Base ID * 1,000,000 + Wavelength * 1,000 + Angle

        Args:
            base_id (int): cf. ACOEM manual Table 45 - Aurora Base IDs for Constructed Parameters
            wavelength (int): One of <450|525|635>. Defaults to None.
            angle (int): 0 for Fullscatter, 90 for Backscatter

        Returns:
            int: _description_
        """
        return base_id * 1000000 + wavelength * 1000 + angle
    

    def _acoem_construct_message(self, command: int, parameter_id: int=0, payload: bytes=b'') -> bytes:
        """
        Construct ACOEM packet to be sent to instrument. See the ACOEM manual for explanations.
        
        Byte  |1  |2  |3  |4  |5..6     |7..10    |11       |12
              |STX|SID|CMD|ETX|msg_len  |msg_data |checksum |EOT
        STX = chr(2)
        SID = serial_id
        CMD = command
        ETX = chr(3)
        msg_len = message length
        msg_data = message data
        EOT = chr(4)

        Args:
            command (int): cf. ACOEM manual Table 19 - List of Commands
            parameter (int, optional): cf. ACOEM manual Table 46 - Aurora Parameters. Defaults to 0 (Not a valid parameter).
            payload (int, optional): _description_. Defaults to None.

        Returns:
            bytes: _description_
        """
        msg_data = bytes()
        if parameter_id>0:
            msg_data = (parameter_id).to_bytes(4, byteorder='big')
        if len(payload)>0:
            msg_data += payload
        msg_len = len(msg_data)
        msg = bytes([2, self.serial_id, command, 3]) + (msg_len).to_bytes(2, byteorder='big') + msg_data
        return msg + self._acoem_checksum(msg) + bytes([4])
    

    def _acoem_bytes2int(self, response: bytes, verbosity: int=0) -> 'list[int]':
        """Convert byte response obtained from instrument into integers. 
    

        Args:
            response (bytes): Raw response obtained from instrument
            verbosity (int, optional): _description_. Defaults to 0.

        Returns:
            list[int]: integers corresponding to the bytes returned. NB: The resulting integers may represent IEEE encoded floats, i.e., this conversion is only meaningful for certian responses.
        """
        response_length = int(int.from_bytes(response[4:6], byteorder='big') / 4)
        if verbosity>1:
            self.logger.debug(f"response length : {response_length}")
        
        items = []
        for i in range(6, (response_length + 1) * 4 + 2, 4):
            item = int.from_bytes(response[i:(i+4)], byteorder='big')

            if verbosity>1:
                self.logger.debug(f"response item{(i-2)/4:3.0f}: {item}")
            items.append(item)

        return items


    def _acoem_response2values(self, parameters: 'list[int]', response: bytes, verbosity: int=0) -> dict:
        """Convert byte response obtained from instrument into integers, floats or datetime, depending on parameter.     

        Args:
            parameters (list[int]): Parameters requested from instrument
            response (bytes): Raw response obtained from instrument
            verbosity (int, optional): _description_. Defaults to 0.

        Returns:
            dict: dictionary with parameters and corresponding values, decoded. Parameter 1 is decoded to datetime, the others to either int or float.
        """
        data = dict()
        response_length = int(int.from_bytes(response[4:6], byteorder='big') / 4)
        if verbosity>1:
            self.logger.debug(colorama.Fore.WHITE + f"response length : {response_length}" + colorama.Fore.GREEN)

        items_bytes = [response[i:(i+4)] for i in range(6, (response_length + 1) * 4 + 2, 4)]
        if verbosity>1:
            self.logger.debug(f"items: {len(items_bytes)}\nitems (bytes): {items_bytes}")

        if len(parameters)==len(items_bytes):
            data_bytes = dict(zip(parameters, items_bytes))
        else:
            raise ValueError("Number of parameters does not match number of items retrieved from response.")    

        # decode values
        for parameter, item in data_bytes.items():
            if parameter in [1, 2201]:
                data[parameter] = self._acoem_timestamp_to_datetime(int.from_bytes(item, byteorder='big'))
            elif ((parameter>1000 and parameter<5000) \
                  or (parameter>12000000 and parameter<13000000) \
                  or (parameter>14000000 and parameter<15000000) \
                  or (parameter>14000000 and parameter<15000000) \
                  or (parameter>16000000 and parameter<17000000) \
                  or (parameter>27000000 and parameter<2027000000)):
                data[parameter] = struct.unpack('>i', item)[0]
            else:
                data[parameter] = struct.unpack('>f', item)[0]

        if verbosity==1:
            self.logger.debug(f"response items:\n{data}")
        if verbosity>1:
            self.logger.debug(f"response items (bytes):\n{data_bytes}")
            self.logger.debug(f"response items:\n{data}")

        return data


    def _acoem_decode_logged_data(self, response: bytes, digits: int=5, verbosity: int=0) -> 'list[dict]':
        """Decode the binary response received from the instrument after sending command 7.

        Args:
            response (bytes): See A.3.8 in the manual for more information
            digits (int, optional): (maximum) number of digits for floats
            verbosity (int, optional): _description_. Defaults to 0.

        Returns:
            list[dict]: List of dictionaries, where the keys are the parameter ids, and the values are the measured values.
        """
        # data = dict()
        all = []
        if response[2] == 7:
            # command 7 (byte 3)
            message_length = int(int.from_bytes(response[4:6], byteorder='big') / 4)
            response_body = response[6:-2]
            fields_per_record = int.from_bytes(response_body[12:16], byteorder='big')
            items_per_record = fields_per_record + 4
            number_of_records = message_length // items_per_record
            if verbosity>1:
                self.logger.debug(f"message length (items): {message_length}")
                self.logger.debug(f"response body length  : {len(response_body)}")
                self.logger.debug(f"response body (bytes) : {response_body}")
                self.logger.debug(f"number of records     : {number_of_records}")

            # parse bytearray into records and records into dict of header record(s) and data records
            records = [response_body[(i*items_per_record*4):((i+1)*(items_per_record*4)-1)] for i in range(number_of_records)]
            keys = []
            values = []
            for i in range(number_of_records):
                if records[i][0]==1:
                    # header record
                    number_of_fields = int.from_bytes(records[i][12:16], byteorder='big')
                    keys = [int.from_bytes(records[i][(16 +j*4):(16 + (j+1)*4)], byteorder='big') for j in range(number_of_fields)]
                elif records[i][0]==0:
                    # data record
                    number_of_fields = int.from_bytes(records[i][12:16], byteorder='big')
                    values = [records[i][(16 + j*4):(16 + (j+1)*4)] for j in range(number_of_fields)]

                    data = dict(zip(keys, values))
                    for k, v in data.items():
                        data[k] = round(struct.unpack('>f', v)[0], digits) if (k>1000 and len(v)>0) else v
                    data['logging_interval'] = int.from_bytes(records[i][8:12], byteorder='big') # type: ignore
                    data['dtm'] = self._acoem_timestamp_to_datetime(int.from_bytes(records[i][4:8], byteorder='big')).strftime('%Y-%m-%d %H:%M:%S') # type: ignore
                    if verbosity==1:
                        self.logger.debug(data)
                    if verbosity>1:
                        self.logger.debug(f"record  {i:2.0f}: {records[i]}")
                        self.logger.debug(f"type    : {records[i][0]}")
                        self.logger.debug(f"inst op : {records[i][0]}")
                        self.logger.debug(f"{i}: keys: {keys}")
                        self.logger.debug(f"{i}: values: {values}")
                        self.logger.debug(data)
                    all.append(data)
            return all
        elif response[2]==0:
            # response error
            return [{'communication_error': self._acoem_decode_error(error_code=response[7])}]
        else:
            return [dict()]


    def _acoem_decode_error(self, error_code: int) -> str:
        """A.3.1 Return description of error code

        Args:
            error_code (bytes): A.3.1. Table 21 error codes

        Returns:
            str: description of error code
        """
        error_map = {0: 'checksum failed',
                     1: 'invalid command byte',
                     2: 'invalid parameter',
                     3: 'invalid message length',
                     4: 'reserved',
                     5: 'reserved',
                     6: 'reserved',
                     7: 'reserved',
                     8: 'media not connected',
                     9: 'media busy',}
        return error_map[error_code]
    

    def _aurora_timestamp_to_date_time(self, fmt: str, dte: str, tme: str) -> datetime:
        """Convert a aurora timestamp to datetime

        Args:
            fmt (str): date reporting format as string: D/M/Y, M/D/Y or Y-M-D (where D=Day, M=Month, Y=Year)
            dte (str): instrument date
            tme (str): instrument time as %H:%M:%S

        Returns:
            datetime.datetime: instrument date and time
        """
        try:
            fmt = fmt.replace(' ', '').replace('\r\n', '')
            dte = dte.replace('\r\n', '')
            tme = tme.replace('\r\n', '')
            if fmt=="D/M/Y":
                fmt = "%d/%m/%Y"
            elif fmt=="M/D/Y":
                fmt = "%m/%d/%Y"
            elif fmt=="Y-M-D":
                fmt = "%Y-%m-%d"
            else:
                raise ValueError("'fmt' not recognized.")
            return datetime.strptime(f"{dte} {tme}", f"{fmt} %H:%M:%S")

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            return datetime(1111, 1, 1, 1, 1, 1)
        

    def _acoem_logged_data_to_string(self, data: 'list[dict]', sep: str=',') -> str:
        """Convert data retrieved using the get_logged_data to a string format ready for saving.

        Args:
            data (list[dict]): data retrieved from get_logged_data
            sep (str, optional): Separator to use for export. Defaults to ','.

        Returns:
            str: string consisting of rows of <sep>-separated items
        """
        result = []
        for d in data:
            dtm_value = d.pop('dtm')
            values = [str(dtm_value)] + [str(value) for key, value in d.items()]
            result.append(sep.join(values))

        return '\n'.join(result)


    def _tcpip_comm_wait_for_line(self) -> None:                        
        t0 = time.perf_counter()
        while self._tcpip_line_is_busy:
            time.sleep(0.1)
            if time.perf_counter() > (t0 + 3 * self.socktout):
                self.logger.warning(colorama.Fore.YELLOW + "'_tcpip_comm_wait_for_line' timed out!" + colorama.Fore.GREEN)
                break
        return


    def _tcpip_comm(self, message: bytes, expect_response: bool=True, verbosity: int=0) -> bytes:
        """
        Send and receive data using ACOEM protocol
        
        Args:
            message (bytes): message data as required by the ACOEM protocol.
            expect_repsonse (bool, optional): If True, read response after sending message. Defaults to True.
            verbosity (int, optional): level of printed output, one of 0 (none), 1 (condensed), 2 (full). Defaults to 0.

        Raises:
            ValueError: if protocol is unknown.

        Returns:
            bytes: bytes returned.
        """
        try:
            # prevent other callers from proceeding if they require to send and retrieve
            self._tcpip_line_is_busy = True

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM, ) as s:
                # connect to the server
                s.settimeout(self.socktout)
                s.connect(self.sockaddr)

                # clear buffer (at least the first 1024 bytes, should be sufficient)
                # tmp = s.recv(128)
                start = time.perf_counter()

                # send message
                if verbosity>0:
                    self.logger.debug(f"message sent: {message}")
                s.sendall(message)

                # receive response
                rcvd = b''
                if expect_response:
                    if self._protocol=='acoem':
                        while not b'\x04' in rcvd:
                        # while not rcvd.endswith(b'\x04'):
                            data = s.recv(1024)
                            if not data:
                                break
                            rcvd += data
                    elif self._protocol=='aurora':
                        while not (rcvd.endswith(b'\r\n') or rcvd.endswith(b'\r\n\n')):
                            data = s.recv(1024)
                            rcvd += data
                    else:
                        raise ValueError('Protocol not recognized.')
                    rcvd = rcvd.strip()
                    # remove pre-ambel
                    rcvd = rcvd.replace(b'\xff\xfb\x01\xff\xfe\x01\xff\xfb\x03', b'')

                    end = time.perf_counter()    
                    if verbosity>1:
                        self.logger.debug(f"response (bytes): {rcvd}")
                        self.logger.debug(f"time elapsed (s): {end - start:0.4f}")

                # inform other callers tha line is free
                self._tcpip_line_is_busy = False
                return rcvd

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            # inform other callers that line is free
            self._tcpip_line_is_busy = False
            return b''

    
    def get_instr_type(self, verbosity: int=0) -> list:
        """A.3.2 Requests details on the type of analyser being communicated with.

        Args:
            verbosity (int, optional): Verbosity for debugging purposes. Defaults to 0.

        Returns:
            list: 4 integers, namely, Model, Variant, Sub-Type, and Range.
        """
        try:
            if self._protocol=='acoem':
                message = self._acoem_construct_message(1)
                self._tcpip_comm_wait_for_line()       
                response = self._tcpip_comm(message, verbosity=verbosity)
                return self._acoem_bytes2int(response=response, verbosity=verbosity)
            else:
                self.logger.warning(colorama.Fore.YELLOW + "Not implemented." + colorama.Fore.GREEN)
                return []
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            return []


    def get_version(self, verbosity: int=0) -> list:
        """A.3.3 Requests the current firmware version running on the analyser.

        Args:
            verbosity (int, optional): Verbosity for debugging purposes. Defaults to 0.

        Returns:
            list: 2 integers, namely, Build, and Branch.
        """
        try:
            if self._protocol=='acoem':
                message = self._acoem_construct_message(2)
                self._tcpip_comm_wait_for_line()
                response = self._tcpip_comm(message, verbosity=verbosity)        
                return self._acoem_bytes2int(response=response, verbosity=verbosity)
            else:
                warnings.warn("Not implemented.")
                return []
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            return []


    def reset(self, verbosity: int=0) -> None:
        """
        A.3.4 Forces the analyser to do a full restart.
        The payload must contain the letters REALLY exactly, as 6 4-byte words.

        Args:
            verbosity (int, optional): Verbosity for debugging purposes. Defaults to 0.

        Returns:
            None.
        """
        try:
            if self._protocol=='acoem':
                payload = bytes([82, 69, 65, 76, 76, 89])
                message = self._acoem_construct_message(command=1, payload=payload)
            elif self._protocol=='aurora':
                message = f"**B\r".encode()
            else:
                raise ValueError('Protocol not recognized.')
            self._tcpip_comm(message, expect_response=False, verbosity=verbosity)
            return
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
    

    def get_values(self, parameters: 'list[int]', verbosity: int=0) -> dict:
        """
        Requests the value of one or more instrument parameters.
        If the ACOEM protocol is used, cf. A.3.5 Get Values, with A.4 List of Aurora parameters.
        If the aurora protocol s used, cf. B.7 Command VI, with Table 47 VI voltage input numbers.

        Args:
            parameters (list[int]): list of parameters to query
            verbosity (int, optional): _description_. Defaults to 0.

        Returns:
            dict: requested indexes are the keys, values are the responses from the instrument, decoded.
        """
        try:
            if self._protocol=='acoem':
                msg_data = b''
                for p in parameters:
                    msg_data += (p).to_bytes(4, byteorder='big')
                msg_len = len(msg_data)
                msg = bytes([2, self.serial_id, 4, 3]) + (msg_len).to_bytes(2, byteorder='big') + msg_data
                msg += self._acoem_checksum(msg) + bytes([4])
                self._tcpip_comm_wait_for_line()                
                response = self._tcpip_comm(message=msg, verbosity=verbosity)
                data = self._acoem_response2values(parameters=parameters, response=response, verbosity=verbosity)
                return data
            elif self._protocol=='aurora':
                items = []
                for p in parameters:
                    if p in range(100):
                        items.append(self._tcpip_comm(message=f"VI{self.serial_id}{p:02.0f}\r".encode(), verbosity=verbosity).decode())
                    else:
                        items.append('')
                data = dict(zip(parameters, items))
                return data
            else:
                self.logger.warning(colorama.Fore.YELLOW + "Not implemented." + colorama.Fore.GREEN)
                return dict()
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)

            return dict()


    def set_value(self, parameter_id: int, value: int, verify: bool=True, verbosity: int=0) -> int:
        """A.3.6 Sets the value of an instrument parameter.

        Args:
            parameter_id (int): Parameter to set.
            value (int): Value to be set.
            verify (bool, optional): Should the new value be queried and echoed after setting? Defaults to True.
            verbosity (int, optional): _description_. Defaults to 0.
        """
        try:
            response = int()
            if self._protocol=='acoem':
                # payload = bytes([0,0,0,value])
                payload = (value).to_bytes(4, byteorder='big')
                message = self._acoem_construct_message(command=5, parameter_id=parameter_id, payload=payload)
                self._tcpip_comm_wait_for_line()                
                self._tcpip_comm(message=message, expect_response=False, verbosity=verbosity)
                if verify:
                    time.sleep(0.1)
                    i = 0
                    while response!=value:
                        response = self.get_values(parameters=[parameter_id], verbosity=verbosity)[parameter_id]
                        # print(f"{time.perf_counter()} {response}")
                        time.sleep(0.1)
                        i = i + 1
                        if i > 100:
                            break
                    return response
                else:
                    return response
            else:
                warnings.warn("Not implemented.")
                return int()
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)

            return int()


    def get_data_log_config(self, verbosity: int=0) -> list:
        """
        A.3.7 Return the list of parameter IDs currently being logged. 
        It is sent with zero message data length.
        The first 4 byte word of the response data is the number of fields being logged (0..500).
        Each following 4 byte word is the ID of the parameter.

        Args:
            verbosity (int, optional): Verbosity. Defaults to 0.

        Returns:
            list: Parameter IDs (integers) currently being logged
        """
        try:
            if self._protocol=='acoem':
                message = self._acoem_construct_message(6)
                response = self._tcpip_comm(message, verbosity=verbosity)
                return self._acoem_bytes2int(response=response, verbosity=verbosity)
            else:
                self.logger.warning(colorama.Fore.YELLOW + "Not implemented." + colorama.Fore.GREEN)
                return list()
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)

            return list()


    # def set_data_log_config(self, verify: bool=False, verbosity: int=0) -> 'list[int]':
    #     """Pass datalog config to instrument. Verify configuration

    #     Args:
    #         verify (bool, optional): Should the datalog configuration be queried and returned after setting it? Defaults to False.
    #         verbosity (int, optional): _description_. Defaults to 0.

    #     Returns:
    #         list[int]: List of parameters logged.
    #     """
    #     try:
    #         data_log_parameter_indexes = range(2004, 2036)
    #         data_log_parameters = iter(self.data_log_parameters)
    #         for index in data_log_parameter_indexes:
    #             self.set_value(index, next(data_log_parameters), verify=False, verbosity=2)
    #         data_log_wavelength_indexes = range(2037, 2069)            
    #         data_log_wavelengths = iter(self.data_log_wavelengths)
    #         for index in data_log_wavelength_indexes:
    #             self.set_value(index, next(data_log_wavelengths), verify=False)
    #         data_log_angle_indexes = range(2070, 2102)
    #         data_log_angles = iter(self.data_log_angles)
    #         for index in data_log_angle_indexes:
    #             self.set_value(index, next(data_log_angles), verify=False)
    #         if verify:
    #             return self.get_data_log_config(verbosity=verbosity)
    #         else:
    #             return list()
    #     except Exception as err:
    #                    self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)

    #         return list()
    
    def set_datalog_interval(self, verbosity: int=0) -> int:
        try:
            datalog_interval = self.set_value(parameter_id=2002, value=self.sampling_interval)
            return datalog_interval

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)

            return int()
        
    def get_logged_data(self, start: datetime, end: datetime, verbosity: int=0) -> 'list[dict]':
        """
        A.3.8 Requests all logged data over a specific date range.

        Args:
            start (datetime.datetime): Beginning of period.
            end (datetime.datetime): End of period.
            verbosity (int, optional): _description_. Defaults to 0.

        Raises:
            ValueError: _description_
            Warning: _description_

        Returns:
            list[dict]: _description_
        """
        try:
            if self._protocol=='acoem':
                if start:
                    payload = self._acoem_datetime_to_timestamp(start)
                    if end:
                        payload += self._acoem_datetime_to_timestamp(end)
                else:
                    raise ValueError("start and/or end date not valid.")
                message = self._acoem_construct_message(command=7, payload=payload)
                response = self._tcpip_comm(message, verbosity=verbosity)
                data =  self._acoem_decode_logged_data(response=response, verbosity=verbosity)
                return data
                # # get next record
                # message = self._acoem_construct_message(command=7, payload=bytes([4]))
                # while response:=self._tcpip_comm(message, verbosity=verbosity):
                #     data.append(self._acoem_decode_logged_data(response=response, verbosity=verbosity))
            else:
                self.logger.warning(colorama.Fore.YELLOW + "Not implemented. For the aurora protocol, try 'get_all_data' or 'accumulate_new_data'." + colorama.Fore.GREEN)
                return list(dict())
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)

            return []

    def get_current_operation(self, verbosity: int=0) -> int:
        """
        Retrieves the operating state of the instrument. 
        If the ACOEM protocol is used, this requests Aurora parameter 4035 (cf.A.4 List of Aurora parameters).
        If the aurora protocol is used, this requests Voltage input number 71 (cf. Appendix B.7 Command: VI).

        Args:
            verbosity (int, optional): _description_. Defaults to 0.

        Returns:
            int: 0 (Normal monitoring), 1 (Zero calibration/check), 2 (Span calibration/check), 9 (Error)
        """
        try:
            if self._protocol=='acoem':
                parameter_id = 4035
                # message = self._acoem_construct_message(command=4, parameter_id=parameter_id)
                return self.get_values(parameters=[parameter_id], verbosity=verbosity)[parameter_id]
                # return self._acoem_bytes2int(response=response, verbosity=verbosity)[0]
            elif self._protocol=='aurora':
                response = self._tcpip_comm(message=f"VI{self.serial_id}71\r".encode(), verbosity=verbosity).decode()
                mapping = {'000': 0, '016': 2, '032': 1}
                return mapping[response]
                # warnings.warn("Not implemented.")
            else:
                raise ValueError("Protocol not recognized.")
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)

            return 9

    def set_current_operation(self, state: int=0, verify: bool=True, verbosity: int=0) -> int:
        """Sets the instrument operating state by actuating the internal valve.

        Args:
            state (int, optional): 0: ambient, 1: zero, 2: span. Defaults to 0.
            verify (bool, optional): Should the value be queried and echoed after setting? Defaults to True.
            verbosity (int, optional): _description_. Defaults to 0.
        """
        try:
            if self._protocol=='acoem':
                return self.set_value(parameter_id=4035, value=state, verify=verify, verbosity=verbosity)
            elif self._protocol=='aurora':
                # if self.get_instr_type()[0]==158:
                warnings.warn("Not implemented for NE-300.")
                return int()
                # else:
                #     map_state = {
                #         0: "0000", 
                #         1: "0011", 
                #         2: "0001", 
                #         }
                #     message = f"DO{self.serial_number}{map_state[state]}\r".encode()
                #     response = self._tcpip_comm(message=message, verbosity=verbosity).decode()
                #     inverse_map_state = {0: 0, 1: 2, 11: 1}
                #     # while response!=[state]:
                #     #     response = self.get_values(parameters=[71], verbosity=verbosity)    # Could be 68 (major state) rather than 71 (DO span/zero measure mode)
                #     #     # [k for k, v in inverse_map_state.items()]
                #     #     time.sleep(1)
                #     #     warnings.warn(f"Incomplete implementation. Please expand and test. Call returned {response}.")
                #     warnings.warn(f"Incomplete implementation. Please expand and test. Call returned '{response}'.")
                #     return state
            else:
                raise ValueError("Protocol not recognized.")
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)

            return 9

    def get_id(self, verbosity: int=0) -> 'dict[str, str]':
        """Get instrument type, s/w, firmware versions

        Parameters:
            verbosity (int, optional): level of printed output, one of 0 (none), 1 (condensed), 2 (full). Defaults to 0.

        Returns:
            dict: response depends on protocol
        """
        try:
            if self._protocol=="acoem":
                instr_type = self.get_instr_type(verbosity=verbosity)
                version = self.get_version(verbosity=verbosity)
                map_instr_type = {
                        'Model': {158: 'ACOEM Aurora',
                                  },
                        'Variant': {300: 'NE-300',
                                    },
                        }
                # resp = dict(zip(['Model', 'Variant', 'Sub-Type', 'Range', 'Build', 'Branch'], instr_type + version))
                id = f"{map_instr_type['Model'][instr_type[0]]} {map_instr_type['Variant'][instr_type[1]]} Sub-Type: {instr_type[2]} Range: {instr_type[3]} "
                id += f"Build: {version[0]} Branch: {version[1]}"
                resp = {'id': id}

            elif self._protocol=="aurora":
                resp = {'id': self._tcpip_comm(message=f"ID{self.serial_id}\r".encode(), verbosity=verbosity).decode()}
            else:
                raise ValueError(f"[{self.name}] Communication protocol unknown")

            self.logger.info(f"[{self.name}] get_id: {resp}")
            return resp

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)

            return dict()

    def get_datetime(self, verbosity: int=0) -> datetime:
        """Get date and time of instrument

        Parameters:
            verbosity (int, optional): level of printed output, one of 0 (none), 1 (condensed), 2 (full). Defaults to 0.

        Returns:
            datetime.datetime: Date and time of instrument
        """
        response = datetime(1,1,1)
        try:
            if self._protocol=="acoem":
                msg = self._acoem_construct_message(4, 1)
                response_bytes = self._tcpip_comm(message=msg, verbosity=verbosity)
                response_int = self._acoem_bytes2int(response=response_bytes, verbosity=verbosity)[0]
                response = self._acoem_timestamp_to_datetime(response_int)
            elif self._protocol=='aurora':
                fmt = self._tcpip_comm(message=f"VI{self.serial_id}64\r".encode(), verbosity=verbosity).decode()
                dte = self._tcpip_comm(message=f"VI{self.serial_id}80\r".encode(), verbosity=verbosity).decode()
                tme = self._tcpip_comm(message=f"VI{self.serial_id}81\r".encode(), verbosity=verbosity).decode()
                response = self._aurora_timestamp_to_date_time(fmt, dte, tme)
            else:
                raise ValueError("Protocol not recognized.")
            self.logger.info(f"get_datetime: {response}")
            return response
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)

            return response

    def get_set_datetime(self, dtm: datetime=datetime.now(timezone.utc), verbosity: int=0) -> 'tuple[dict, dict]':
        """Get and then set date and time of instrument

        Parameters:
            dtm (datetime.datetime, optional): Date and time to be set. Defaults to time.gmtime().
            verbosity (int, optional): level of printed output, one of 0 (none), 1 (condensed), 2 (full). Defaults to 0.

        Returns:
            None
        """
        try:
            if self._protocol=="acoem":
                # get dtm from instrument
                dtm_found = self.get_values(parameters=[1])[1].strftime('%Y-%m-%d %H:%M:%S')
                
                # set dtm of instrument
                payload = self._acoem_datetime_to_timestamp(dtm=dtm)
                msg = self._acoem_construct_message(command=5, parameter_id=1, payload=payload)
                self._tcpip_comm(message=msg, expect_response=False, verbosity=verbosity)
                
                # get new dtm from instrument
                dtm_set = self.get_values(parameters=[1])[1].strftime('%Y-%m-%d %H:%M:%S')
                return (dtm_found, dtm_set)
            else:
                warnings.warn("Not implemented.")
                return (dict(), dict())
                # resp = self.tcpip_comm1(f"**{self.serial_id}S{dtm.strftime('%H%M%S%d%m%y')}")
                # msg = f"DateTime of instrument {self.name} set to {dtm} ... {resp}"
                # print(f"{dtm} {msg}")
                # self.logger.info(msg)

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)

            return (dict(), dict())

    # def _do_zero_span_check(self, verbosity: int=0) -> None:
    #     """
    #     Launch a zero check, followed by a span check. Finally, return to Ambient mode.
    #     NB: Not to be used in operations, the wait loop is blocking

    #     Parameters:
    #         verbosity (int, optional): ...

    #     Returns:
    #         None
    #     """
    #     dtm = now = datetime.now(timezone.utc)

    #     # change operating state to ZERO
    #     msg = f"Switching to ZERO CHECK mode ..."
    #     self.logger.info(colorama.Fore.BLUE + f"[{self.name}] {msg}")
    #     resp = self.do_zero(verbosity=verbosity)
    #     if resp==1:
    #         self.logger.info(f"Instrument switched to ZERO CHECK")
    #     else:
    #         self.logger.warning(colorama.Fore.YELLOW + f"Instrument mode should be '1' (ZERO CHECK) but was returned as '{resp}'." + colorama.Fore.GREEN)
    #     while now < dtm + timedelta(minutes=self.zero_check_duration_minutes):
    #         now = datetime.now(timezone.utc)
    #         time.sleep(1)
        
    #     # change operating state to SPAN
    #     dtm = now = datetime.now(timezone.utc)
    #     msg = f"Switching to SPAN CHECK mode ..."
    #     self.logger.info(colorama.Fore.BLUE + f"[{self.name}] {msg}")
    #     resp = self.do_span(verbosity=verbosity)
        
    #     # open CO2 cylinder valve by setting digital out to HIGH
    #     # resp2 = self.set_value(7005, 1)
    #     # msg = f"CO2 cylinder valve 
    #     if resp==2:
    #         self.logger.info(f"Instrument switched to SPAN CHECK")
    #     else:
    #         self.logger.warning(colorama.Fore.YELLOW + f"Instrument mode should be '2' (SPAN CHECK) but was returned as '{resp}'." + colorama.Fore.GREEN)
    #     while now < dtm + timedelta(minutes=self.span_check_duration_minutes):
    #         now = datetime.now(timezone.utc)
    #         time.sleep(1)
        
    #     # change operating state to AMBIENT
    #     msg = f"Switching to AMBIENT mode."
    #     self.logger.info(colorama.Fore.BLUE + f"[{self.name}] {msg}")
    #     resp = self.do_ambient(verbosity=verbosity)
    #     if resp==0:
    #         self.logger.info(f"Instrument switched to AMBIENT mode")
    #     else:
    #         self.logger.warning(f"Instrument mode should be '0' (AMBIENT) but was returned as '{resp}'.")
    #     return

    def do_span(self, verify: bool=True, verbosity: int=0) -> int:
        """
        Override digital IO control and DOSPAN. Wrapper for set_current_operation.

        Parameters:
            verbosity (int, optional): ...

        Returns:
            int: 2: span
        """
        self._tcpip_comm_wait_for_line()
        return self.set_current_operation(state=2, verify=verify, verbosity=verbosity)

    def do_zero(self, verify: bool=True, verbosity: int=0) -> int:
        """
        Override digital IO control and DOZERO. Wrapper for set_current_operation.

        Parameters:
            verbosity (int, optional): ...
        
        Returns:
            int: 1: zero
        """
        self._tcpip_comm_wait_for_line()
        return self.set_current_operation(state=1, verify=verify, verbosity=verbosity)
    
    def do_ambient(self, verify: bool=True, verbosity: int=0) -> int:
        """
        Override digital IO control and return to ambient measurement. Wrapper for set_current_operation.

        Parameters:
            verbosity (int, optional): ...

        Returns:
            int: 0: ambient
        """
        self._tcpip_comm_wait_for_line()
        return self.set_current_operation(state=0, verify=verify, verbosity=verbosity)

    # def get_status_word(self, verbosity: int=0) -> int:
    #     """
    #     Read the System status of the Aurora 3000 microprocessor board. The status word 
    #     is the status of the nephelometer in hexadecimal converted to decimal.

    #     Parameters:
    
    #     Returns:
    #         int: {<STATUS WORD>}
    #     """
    #     return int(self._tcpip_comm(f"VI{self.serial_id}88\r".encode()).decode())


    # def get_all_data(self, verbosity: int=0) -> str:
    #     """
    #     Rewind the pointer of the data logger to the first entry, then retrieve all data (cf. B.4 ***R, B.3 ***D). 
    #     This only works with the aurora protocol (and doesn't work very well with the NE-300).

    #     Parameters:
    #         verbosity (int, optional): level of printed output, one of 0 (none), 1 (condensed), 2 (full). Defaults to 0.

    #     Returns:
    #         str: response
    #     """
    #     try:
    #         if self._protocol=="acoem":
    #             warnings.warn("Not implemented. Use 'get_logged_data' with specified period instead.")
    #         elif self._protocol=='aurora':
    #             self._tcpip_comm_wait_for_line()
    #             response = self._tcpip_comm(message=f"***R\r".encode(), verbosity=verbosity).decode()
    #             response = self.accumulate_new_data(verbosity=verbosity)
    #             # response = self._tcpip_comm(message=f"***D\r".encode(), verbosity=verbosity).decode()
    #             return response
    #         else:
    #             raise ValueError("Protocol not implemented.")
    #         return str()
    #     except Exception as err:
    #                    self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)

    #         return str()


    def get_current_data(self, add_params: list=[], strict: bool=False, sep: str=' ', verbosity: int=0) -> dict:
        """
        Retrieve latest near-real-time reading on one line.
        With the aurora protocol, this uses the command 99 (cf. B.7 VI: 99), returning parameters [80,81,30,2,31,3,32,17,18,16,19,00,90].
        These are mapped to the corresponding Acoem parameters (cf. A.4 List of Aurora parametes) [1,1635000,1525000,1450000,1635090,1525090,1450090,5001,5004,5003,5002,4036,4035].
        Optionally, several more parameters can be retrieved, depending on the protocol.

        Parameters:
            add_params (list, optional): read more values and append to dictionary. Defaults to [].
            strict (bool, optional): If True, the dictionary returned is {99: response}, where response is the <sep>-separated response of the VI<serial_id>99 aurora command. Defaults to False.
            sep (str, optional): Separator applied if strict=True. Defaults to ' '.
            verbosity (int, optional): level of printed output, one of 0 (none), 1 (condensed), 2 (full). Defaults to 0.

        Returns:
            dict: Dictionary of parameters and values obtained.
        """
        parameters = [1,1635000,1525000,1450000,1635090,1525090,1450090,5001,5004,5003,5002,4036,4035]
        if add_params:
            parameters += add_params
        try:
            if self._protocol=='acoem':
                # warnings.warn("Not implemented.")
                data = self.get_values(parameters=parameters, verbosity=verbosity)
                if strict:
                    if 1 in parameters:
                        data[1] = data[1].strftime(format=f"%d/%m/%Y{sep}%H:%M:%S")
                    response = sep.join([str(data[k]) for k, v in data.items()])
                    data = {99: response}
            elif self._protocol=='aurora':
                response = self._tcpip_comm(f"VI{self.serial_id}99\r".encode(), verbosity=verbosity).decode()
                response = response.replace(", ", ",")
                if strict:
                    response = response.replace(',', sep)
                    data = {99: response}
                else:
                    response = response.split(',')
                    response = [response[0]] + [float(v) for v in response[1:]]
                    data = dict(zip(parameters, response))
            else:
                raise ValueError("Protocol not recognized.")
            return data
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)
            return dict()


    def _accumulate_new_data(self, sep: str=",", verbosity: int=0) -> None:
        """
        For the acoem format: Retrieve all readings from (now - get_data_interval) until now.
        For the aurora format: Retrieve all readings from current cursor.
        
        Args:
            sep (str, optional): Separator to use for output and file, respectively. Defaults to ",".
            save (bool, optional): Should data be saved to file? Defaults to True.
            verbosity (int, optional): _description_. Defaults to 0.

        Raises:
            Warning: _description_
            ValueError: _description_

        Returns:
            nothing (but updates self._data)
        """
        try:
            if self._protocol=='acoem':
                if self.sampling_interval is None:
                    raise ValueError("'get_data_interval' cannot be None.")
                tmp = []

                # define period to retrieve and update state variable
                start = self._start_datalog
                end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
                self._start_datalog = end + timedelta(seconds=self.sampling_interval)

                # retrieve data
                self.logger.info(f"[{self.name}] .accumulate_new_data from {start} to {end}")
                self._tcpip_comm_wait_for_line()            
                data = self.get_logged_data(start=start, end=end, verbosity=verbosity)

                # prepare result
                for d in data:
                    values = [str(d.pop('dtm'))] + [str(value) for key, value in d.items()]
                    tmp.append(sep.join(values))
                data = '\n'.join(tmp) + '\n'

            elif self._protocol=='aurora':
                data = self._tcpip_comm(f"***D\r".encode()).decode()
                data = data.replace('\r\n\n', '\r\n').replace(", ", ",").replace(",", sep)
            else:
                raise ValueError("Protocol not recognized.")
            
            if verbosity>0:
                self.logger.info(data)

            # if save:
            #     if self.reporting_interval is None:
            #         raise ValueError("'reporting_interval' cannot be None.")
                
            #     # generate the datafile name
            #     self.__datafile = os.path.join(self.datadir, time.strftime("%Y"), time.strftime("%m"), time.strftime("%d"),
            #                                 "".join([self.name, "-",
            #                                         datetimebin.dtbin(self.reporting_interval), ".dat"]))

            #     os.makedirs(os.path.dirname(self.__datafile), exist_ok=True)
            #     with open(self.__datafile, "at", encoding='utf8') as fh:
            #         fh.write(data)
            #         fh.close()

            #     if self.staging_path:
            #         self.stage_data_file()
            self._data += data

            return 
        
        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)


    def _save_data(self) -> str | None:
        try:
            self.logger.debug(f"[{self.name}] _save_data")

            if not self._data or not self._data.strip():
                self.logger.info(f"[{self.name}] no data to save (skipping file creation).")
                self.data_file = str()
                return None

            now = datetime.now()
            timestamp = now.strftime(self._file_timestamp_format)
            yyyy = now.strftime('%Y')
            mm = now.strftime('%m')
            dd = now.strftime('%d')
          
            # create appropriate file name and write mode
            self.data_file = os.path.join(self.data_path, yyyy, mm, dd, f"{self.name}-{timestamp}.dat")
            os.makedirs(os.path.dirname(self.data_file), exist_ok=True)
            if os.path.exists(self.data_file):
                header = str()
            else:
                header = f"{','.join(str(n) for n in self.get_data_log_config())}\n"

            with open(file=self.data_file, mode='a') as fh:
                if header:
                    fh.write(header)
                fh.write(self._data)
                self.logger.info(f"[{self.name}] file saved: {self.data_file}")

                # reset self._data
                self._data = str()
            return

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)


    def _stage_file(self):
        """ Stage file, optionally as .zip archive.
        """
        try:
            self.logger.debug(f"[{self.name}]: ._stage_file")

            if not self.data_file or not os.path.isfile(self.data_file):
                self.logger.info(f"[{self.name}] no source file to stage (skipping).")
                return

            archive = os.path.join(self.staging_path, os.path.basename(self.data_file).replace('.dat', '.zip'))
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(self.data_file, os.path.basename(self.data_file))
                self.logger.info(f"file staged: {archive}")            
                # reset
                self.data_file = str()

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)


    def _save_and_stage_data(self):
        try:
            self.logger.debug(f"[{self.name}] ._save_and_stage_data")
        
            self._save_data()
            if self.data_file:
                self._stage_file()

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"{err}" + colorama.Fore.GREEN)


    def print_ssp_bssp(self) -> None:
        """Retrieve current readings and print."""
        try:
            data = self.get_values(parameters=[2635000, 2635090, 2525000, 2525090, 2450000, 2450090])
            data = f"ssp|bssp (Mm-1) r: {data[2635000]:0.4f}|{data[2635090]:0.4f} g: {data[2525000]:0.4f}|{data[2525090]:0.4f} b: {data[2450000]:0.4f}|{data[2450090]:0.4f}"
            self.logger.debug(colorama.Fore.GREEN + f"[{self.name}] {data}")

        except Exception as err:
            self.logger.error(colorama.Fore.RED + f"[{self.name}] print_ssp_bssp: {err}" + colorama.Fore.GREEN)


# %%
if __name__ == "__main__":
    pass