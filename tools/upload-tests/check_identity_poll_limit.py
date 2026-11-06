"""Verify actual polling HTTP limit with a separate synthetic proxy IP on localhost."""
import json,urllib.request,urllib.error
from pathlib import Path
folder=Path('D:/Yuzibei/source/local-data/task6-20261107')
trust=json.loads((folder/'trust.json').read_text())
assert trust['server_origin']=='http://127.0.0.1:5136'
codes=[]
for _ in range(61):
    req=urllib.request.Request(trust['server_origin']+'/api/director/device-auth/token',
        data=b'{"deviceCode":"nonexistent","proof":"invalid"}',
        headers={'Content-Type':'application/json','X-Real-IP':'127.0.0.199'})
    try:
        with urllib.request.urlopen(req) as response:codes.append(response.status)
    except urllib.error.HTTPError as error:
        codes.append(error.code);error.close()
assert codes==[400]*60+[429],codes
(folder/'poll-limit-check.json').write_text(json.dumps({'passed':1,'accepted_attempts':60,'next_status':429}))
print('PASS real poll route permits normal 5-second polling and throttles attempt 61')
