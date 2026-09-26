echo "== whoami/pwd"; id; pwd
echo "== pid ns (ps)"; ps aux 2>&1 | head -8
echo "== ns links"; readlink /proc/self/ns/net /proc/self/ns/pid /proc/self/ns/mnt
echo "== host home listing"; ls /home 2>&1 | head; ls /home/dev 2>&1 | head -3
echo "== read project .env"; cat /home/dev/wavebreak-agent/agent/spike/.env 2>&1 | head -2
echo "== read /etc/passwd"; head -2 /etc/passwd 2>&1
echo "== docker socket"; ls -l /var/run/docker.sock 2>&1; ls /run 2>&1 | head -3
echo "== write outside root"; touch /tmp/outside_probe 2>&1; touch /home/dev/outside_probe 2>&1
echo "== write inside cwd"; touch inside_probe && echo ok
echo "== net interfaces"; cat /proc/net/dev | tail -n +3 | cut -d: -f1
echo "== curl TrueForge localhost:8790"; curl -s -m 5 -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8790/api/v1/capabilities 2>&1
echo "== curl spike MCP localhost:8791"; curl -s -m 5 -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8791/mcp 2>&1
echo "== curl Prometheus localhost:9090"; curl -s -m 5 -o /dev/null -w "%{http_code}\n" http://127.0.0.1:9090/-/ready 2>&1
echo "== curl example.com (not allowlisted)"; curl -s -m 8 -o /dev/null -w "%{http_code}\n" https://example.com 2>&1
echo "== curl pypi.org (allowlisted)"; curl -s -m 8 -o /dev/null -w "%{http_code}\n" https://pypi.org/simple/ 2>&1
echo "== env"; env | grep -iE "key|token|secret|gemini" | sed 's/=.*/=<set>/' | head
