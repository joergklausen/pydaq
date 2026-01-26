"""
Logging utilities for instrument data acquisition and processing.

This module provides file and console logging, as well as optional MQTT-based logging.
It uses a custom TimedRotatingFileHandler to add a date suffix to the log filename.

Typical usage:
    from utils.logging import setup_logging
    logger = setup_logging(config)
"""

import logging
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None


class CustomTimedRotatingFileHandler(TimedRotatingFileHandler):
    """
    Custom file handler that appends a date suffix to rotated log files.
    """
    def rotation_filename(self, default_name: str) -> str:
        base = self.baseFilename.rstrip(".log")
        date_suffix = datetime.now().strftime("%Y%m%d")
        return f"{base}-{date_suffix}.log"


class MQTTHandler(logging.Handler):
    """
    MQTT-based logging handler that publishes log records to an MQTT broker.

    Args:
        broker (str): MQTT broker hostname or IP address.
        port (int): Port to connect to on the MQTT broker.
        topic (str): MQTT topic to publish logs to.
    """
    def __init__(self, broker: str='localhost', port: int=1883, topic: str='logs'):
        if mqtt is None:
            raise ImportError("paho-mqtt is not installed.")
        self.client = mqtt.Client()
        self.client.connect(broker, port, 60)
        self.topic = topic

    def emit(self, record):
        log_entry = self.format(record)
        self.client.publish(self.topic, log_entry)


def setup_logging(config: dict, backup_count: int=50, use_mqtt: bool=False) -> logging.Logger:
    """
    Set up file, console, and optional MQTT logging using a configuration dictionary.

    Args:
        config (dict): Parsed YAML configuration containing logging setup.
        backup_count (int): Number of log backups to keep.
        use_mqtt (bool): Whether to enable MQTT logging (requires paho-mqtt).

    Returns:
        logging.Logger: A configured logger instance.

    Raises:
        RuntimeError: If MQTT logging is requested but dependencies are missing.
        Exception: If any other error occurs during logger setup.
    """
    try:
        root = Path(config["paths"]["root"]).expanduser()
        log_dir = root / config["paths"]["logging"]
        log_file = config["logging"]["file_name"]
        log_path = log_dir / log_file

        log_dir.mkdir(parents=True, exist_ok=True)
        logger_name = log_file.split('.')[0]
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.DEBUG)

        formatter = logging.Formatter(
            fmt='%(asctime)s, %(levelname)s, %(name)s, %(message)s',
            datefmt="%Y-%m-%dT%H:%M:%S"
        )

        file_handler = CustomTimedRotatingFileHandler(
            filename=log_path,
            when="midnight",
            backupCount=backup_count,
            encoding="utf-8"
        )
        file_handler.setLevel(logging.WARNING)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        if use_mqtt:
            if mqtt is None:
                raise RuntimeError("MQTT logging requested but paho-mqtt not installed.")
            mqtt_config = config.get("mqtt", {})
            mqtt_handler = MQTTHandler(
                broker=mqtt_config.get("broker", "localhost"),
                port=mqtt_config.get("port", 1883),
                topic=mqtt_config.get("topic", "logs")
            )
            mqtt_handler.setLevel(logging.INFO)
            mqtt_handler.setFormatter(formatter)
            logger.addHandler(mqtt_handler)

        logger.info("== PYDAQ started =============")
        return logger

    except Exception as err:
        print(f"Logging setup failed: {err}")
        raise
