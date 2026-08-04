import urllib.request, json, os

req = urllib.request.Request(
    'https://api.github.com/repos/muzape28-blip/ZMUX/actions/runs/30949505745',
    headers={'Accept': 'application/vnd.github.v3+json'}
)
with urllib.request.urlopen(req) as response:
    print(response.read().decode())
