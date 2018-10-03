#!/bin/sh
token=$(cat migration-oauth-key.txt)

for name in "$@"; do
  curl -i -H "Authorization: token $token" \
    -d "{ \"name\": \"$name\" }" \
    https://api.github.com/orgs/llvm-git-prototype/repos
done
