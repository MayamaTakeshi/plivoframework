#!/bin/bash

set -o nounset
set -o errexit
set -o pipefail

cp -rf src /usr/local/plivo/src/plivo

/etc/init.d/plivo restart

ps -e | grep plivo
