#!/usr/bin/env bash
# Downloads the hawkBit MCP server jar from Maven Central into build/hawkbit-mcp/ (D19).
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
VERSION=${HAWKBIT_MCP_VERSION:-1.1.0}
dest="$REPO/build/hawkbit-mcp/hawkbit-mcp-server.jar"
url="https://repo1.maven.org/maven2/org/eclipse/hawkbit/hawkbit-mcp-server/$VERSION/hawkbit-mcp-server-$VERSION.jar"
mkdir -p "$(dirname "$dest")"
if [[ -s "$dest" ]]; then
  echo "present: $dest"
  exit 0
fi
curl -fL --retry 3 -o "$dest.tmp" "$url"
mv "$dest.tmp" "$dest"
echo "downloaded: $dest"
