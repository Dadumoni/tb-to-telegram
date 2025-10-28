#!/bin/bash

# Start Aria2 in background
aria2c --enable-rpc --rpc-listen-all=true --rpc-allow-origin-all \
       --rpc-secret=mysecret \
       --dir=/tmp/aria2_downloads \
       --max-connection-per-server=16 \
       --min-split-size=1M \
       --split=16 \
       --daemon=true

# Wait for Aria2 to start
sleep 2

# Start the bot
python bot.py
