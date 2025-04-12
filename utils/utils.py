import configparser
import logging
import os
import time

import paho.mqtt.client as mqtt
import yaml


class MQTTHandler(logging.Handler):
    def __init__(self, broker: str='localhost', port: int=1883, topic: str='logs'):
        self.client = mqtt.Client()
        self.client.connect(broker, port, 60)
        self.topic = topic

    def emit(self, record):
        log_entry = self.format(record)
        self.client.publish(self.topic, log_entry)


def load_config(config_file: str) -> configparser.ConfigParser:
    """
    Load configuration from config file.

    :param config_file: Path to the configuration file.
    :return: ConfigParser object with the loaded configuration.
    """
    extension = os.path.basename(config_file).split(".")[1].lower()
    if extension == "ini":
        config = configparser.ConfigParser()
        config.read(config_file)
    elif extension == 'yaml' or extension == 'yml':
        with open(config_file, 'r') as fh:
            config = yaml.safe_load(fh)
    else:
        print("Extension of config file not recognized!)")
    return config


# def setup_logging(file: str) -> logging:
#     """Setup the main logging device

#     Args:
#         file (str): full path to log file

#     Returns:
#         logging: a logger object
#     """
#     file_path = os.path.dirname(file)
#     os.makedirs(file_path, exist_ok=True)

#     main_logger = os.path.basename(file).split('.')[0]
#     logger = logging.getLogger(main_logger)
#     logger.setLevel(logging.DEBUG)

#     # create file handler which logs warning and above messages
#     fh = logging.FileHandler(file)
#     fh.setLevel(logging.WARNING)

#     # create console handler which logs even debugging information
#     ch = logging.StreamHandler()
#     ch.setLevel(logging.DEBUG)

#     # create formatter and add it to the handlers
#     formatter = logging.Formatter('%(asctime)s, %(levelname)s, %(name)s, %(message)s', datefmt="%Y-%m-%dT%H:%M:%S")
#     fh.setFormatter(formatter)
#     ch.setFormatter(formatter)

#     # add the handlers to the logger
#     logger.addHandler(fh)
#     logger.addHandler(ch)

#     # mqtt_handler = MQTTHandler()
#     # logger.addHandler(mqtt_handler)
#     logger.info("== PYDAQ started =============")
#     return logger


def seconds_to_next_n_minutes(n: int):
    # Get the current time in seconds since the epoch
    now = time.time()

    # Calculate minutes and seconds of the current time
    minutes = int(now // 60) % 60
    seconds = int(now % 60)

    # Calculate remaining time to the next n-minute mark
    minutes_to_next_n_minutes = n - (minutes % n)
    remaining_seconds = (minutes_to_next_n_minutes * 60) - seconds
    return remaining_seconds
