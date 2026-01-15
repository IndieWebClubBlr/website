#!/bin/bash

# Get the file path from the first argument
FILE_PATH=$1

# Check the file extension
if [[ "$FILE_PATH" == *.opml || "$FILE_PATH" == *.py || "$FILE_PATH" == *.html || "$FILE_PATH" == *.md ]]; then
    make -s build CACHE=true
else
    make -s assets
fi
