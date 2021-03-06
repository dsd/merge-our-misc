#!/bin/sh

if v=$(git describe --tags --dirty --long --match '201?.*' --always); then
  echo "$v" | tr - +
  exit 0
fi

if v=$(dpkg-parsechangelog | grep '^Version: '); then
  echo "${v#Version: }+~unknown-changes"
  exit 0
fi

echo "0~unknown-version"
exit 0
