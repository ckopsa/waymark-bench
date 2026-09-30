#!/bin/sh
# The rig image's entrypoint. It keeps the Clojure deps cache on the data
# volume, so a restart does not fetch the deps again:
#   $HOME/.m2      -> /data/m2
#   $HOME/.gitlibs -> /data/gitlibs
# It makes the directories when they are missing, and a second run changes
# nothing. Then it runs the command it was given.
set -eu

data="${BENCH_CACHE_ROOT:-/data}"
home="${HOME:-/root}"
mkdir -p "$home"
for name in m2 gitlibs; do
  mkdir -p "$data/$name"
  link="$home/.$name"
  # A real directory in the link's place is a cache off the volume: drop it.
  if [ ! -L "$link" ]; then
    rm -rf "$link"
  fi
  ln -sfn "$data/$name" "$link"
done

exec "$@"
