import logging
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

class CustomTimedRotatingFileHandler(TimedRotatingFileHandler):
    def rotation_filename(self, default_name: str) -> str:
        # Override this to change the file naming convention
        base = self.baseFilename.rstrip(".log")
        date_suffix = datetime.now().strftime("%Y%m%d")
        return f"{base}-{date_suffix}.log"


def setup_logging(config: dict, backup_count: int=50) -> logging.Logger:
    """
    Set up logging using pathlib for path operations.

    Args:
        config (dict): Configuration dictionary with keys:
            - paths['root']: base directory (e.g., "~/Documents/pydaq")
            - paths['logging']: subdirectory for logs (e.g., "logs")
            - logging['filename']: name of the log file (e.g., "pydaq.log")

    Returns:
        logging.Logger: Configured logger instance
    """
    try:
        root = Path(config["paths"]["root"]).expanduser()
        log_dir = root / config["paths"]["logging"]
        log_file = config["logging"]["file_name"]
        log_path = log_dir / log_file

        log_dir.mkdir(parents=True, exist_ok=True)

        # Derive logger name from filename of the calling script
        main_logger = log_file.split('.')[0]
        logger = logging.getLogger(main_logger)
        logger.setLevel(logging.DEBUG)

        # File handler with rotation
        file_handler = CustomTimedRotatingFileHandler(
            filename=log_path,
            when="midnight",
            backupCount=backup_count,
            encoding="utf-8"
        )
        file_handler.setLevel(logging.WARNING)

        # Console handler for DEBUG and above
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)

        # Formatter
        formatter = logging.Formatter(
            fmt='%(asctime)s, %(levelname)s, %(name)s, %(message)s',
            datefmt="%Y-%m-%dT%H:%M:%S"
        )
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        # Attach handlers
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

        logger.info("== PYDAQ started =============")

        return logger

    except Exception as err:
        print(f"Logging setup failed: {err}")
        raise
