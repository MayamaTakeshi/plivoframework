#!/bin/bash

set -o nounset
set -o errexit
set -o pipefail

for i in $(git status -s |cut -d ' ' -f 3 ); do echo $i; cp $i /usr/local/plivo/src/plivo/$i;done

/etc/init.d/plivo restart

ps -e | grep plivo
