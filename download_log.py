import subprocess, urllib.request
res = subprocess.check_output(['gh', 'api', 'repos/muzape28-blip/ZMUX/actions/jobs/92175909553/logs', '--include', '-i'])
headers, body = res.decode().split('\r\n\r\n', 1)
location = next((line.split(':', 1)[1].strip() for line in headers.split('\r\n') if line.lower().startswith('location:')), None)
if location: urllib.request.urlretrieve(location, "job_log.txt")
