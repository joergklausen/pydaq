import os
from pydaq.instr.thermo import Thermo49i
from pydaq.utils.utils import load_config, setup_logging


config = load_config(config_file='nrbdaq.yaml')
tei49i = Thermo49i(config=config)

# setup logging
logfile = os.path.join(os.path.expanduser(config['root']), config['logging']['file'])
logger = setup_logging(file=logfile)

tei49i.get_all_lrec()
