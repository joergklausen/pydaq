#!/bin/bash

# Set necessary environment variables
export PATH="/usr/local/bin:/usr/bin:/bin"
export PYTHONPATH="/usr/lib/python311.zip:/usr/lib/python3.11:/usr/lib/python3.11/lib-dynload:/usr/local/lib/python3.11/dist-packages:/usr/lib/python3/dist-packages:/usr/lib/python3.11/dist-packages"

# Ensure the named pipe exists or create it
#PIPE="/tmp/nrbdaq"
#if [ ! -p "$PIPE" ]; then
#    mkfifo "$PIPE"
#fi

# Check if the script is already running
if pgrep -f -a "pydaq.py" > /dev/null
then
    echo "$(date +\%FT\%T), INFO, pydaq.sh, pydaq.py already running."
    # exit 1
else
    # load venv
    source /home/gaw/.venv/bin/activate
    echo "$(date +\%FT\%T), INFO, pydaq.sh, .venv activated"

    # change cwd
    cd /home/gaw/git/pydaq/

    # Start the script
    /home/gaw/.venv/bin/python3 -u /home/gaw/git/pydaq/pydaq.py
#    /home/gaw/.venv/bin/python3 -u /home/gaw/git/nrbdaq/nrbdaq.py > "$PIPE"
    echo "$(date +\%FT\%T), INFO, pydaq.sh, == PYDAQ (re)started ====" >> /home/gaw/Documents/pydaq/pydaq.log 2>&1
fi

# NB: make sure, these are executable
# chmod +x /home/gaw/git/pydaq/pydaq.sh
# chmod +x .venv/bin/activate
