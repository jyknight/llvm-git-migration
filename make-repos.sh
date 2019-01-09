#!/bin/sh
token=$(cat migration-oauth-key.txt)

for name in "$@"; do
  curl -i -H "Authorization: token $token" \
    -d "{\
 \"name\": \"$name\",\
 \"has_issues\": false,\
 \"has_wiki\": false,\
 \"has_projects\": false }" \
    https://api.github.com/orgs/llvm/repos
done
