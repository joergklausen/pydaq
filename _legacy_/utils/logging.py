import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

def setup_logging(file: str | Path, level_console:int=20, level_file:int=40) -> logging.Logger:
    """Setup the main logging device.
    Provides an attribute 'extra' to direct logging information to file. 
    Usage: .logger.info("message", extra={"to_logfile": True})

    Args:
        file (str): full path to log file
        level_console (int, optional): minimum level for logging to console. Defaults to 20 (INFO)
        level_file (int, optional): minimum level for logging to file. Defaults to 40 (ERROR)

    Returns:
        logging.Logger: a logger object
    """
    _file = Path(file).expanduser()

    _file.parent.mkdir(parents=True, exist_ok=True)

    main_logger = _file.stem
    logger = logging.getLogger(main_logger)
    logger.setLevel(logging.DEBUG)

    # create file handler which logs level_file and above messages
    fh = TimedRotatingFileHandler(filename=_file,
                                  when='W0',
                                  interval=1,
                                #   backupCount=8,
                                  )
    fh.setLevel(level_file)

    # file handler for selective INFO logging
    info_fh = TimedRotatingFileHandler(filename=_file,
                                  when='W0',
                                  interval=1,
                                #   backupCount=6,
                                  )
    info_fh.setLevel(logging.INFO)
    info_fh.addFilter(lambda record: getattr(record, 'to_logfile', False))

    # create console handler which logs level_console and above messages
    ch = logging.StreamHandler()
    ch.setLevel(level_console)

    # create formatter and add it to the handlers
    formatter = logging.Formatter('%(asctime)s, %(levelname)s, %(name)s, %(message)s', datefmt="%Y-%m-%dT%H:%M:%S")
    fh.setFormatter(formatter)
    info_fh.setFormatter(formatter)
    ch.setFormatter(formatter)

    # add the handlers to the logger
    logger.addHandler(fh)
    logger.addHandler(info_fh)
    logger.addHandler(ch)

    return logger