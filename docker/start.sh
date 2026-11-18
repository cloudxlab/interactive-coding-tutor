#!/bin/sh
# Entry point of the per-user JupyterLab container. The root filesystem is read-only, so everything Jupyter
# writes (config, runtime, cache) goes to $HOME on the tmpfs; notebooks live in the /work volume.
set -e
: "${HOME:=/tmp/home}"
mkdir -p "$HOME/config" "$HOME/runtime" "$HOME/data" "$HOME/ipython" "$HOME/cache"
cp /opt/tutor/jupyter_server_config.py "$HOME/config/jupyter_server_config.py"
export JUPYTER_CONFIG_DIR="$HOME/config" JUPYTER_RUNTIME_DIR="$HOME/runtime" JUPYTER_DATA_DIR="$HOME/data" \
       IPYTHONDIR="$HOME/ipython" XDG_CACHE_HOME="$HOME/cache"
exec jupyter lab   # the token comes from the JUPYTER_TOKEN environment variable
