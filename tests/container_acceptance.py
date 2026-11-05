"""Opt-in disposable Docker packaging acceptance. Run as Docker-capable operator.

No published ports, network access, live configuration, or Plex linking.
Supply a NEW resource prefix and an unpacked, sanitized build context.
Retains stopped test containers/volumes and a private report for inspection.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import shutil
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("context", type=Path)
    parser.add_argument("prefix")
    parser.add_argument("--image-prefix", help="Reuse already-built acceptance images; no rebuild")
    parser.add_argument("--docker", default=shutil.which("docker") or "/usr/local/bin/docker")
    parser.add_argument("--protect-container", action="append", default=[], help="Read-only identity guard for an existing container")
    args = parser.parse_args()
    if not re.fullmatch(r"pb-acceptance-[a-z0-9-]+", args.prefix):
        parser.error("prefix must start pb-acceptance- and contain only lowercase/digits/hyphens")
    os.umask(0o077)
    docker = args.docker
    prefix = args.prefix
    image_prefix = args.image_prefix or prefix
    if not re.fullmatch(r"pb-acceptance-[a-z0-9-]+", image_prefix):
        parser.error("invalid acceptance image prefix")
    services = ("setup", "topology", "playback")
    volumes = ("private", "playback-config", "topology-config", "handoff", "playback-state", "topology-state")
    created = []
    report = {"checks": [], "prefix": prefix}

    def run(*command, **kwargs):
        try:
            return subprocess.check_output(command, stderr=subprocess.STDOUT, **kwargs).decode()
        except subprocess.CalledProcessError as exc:
            # Private evidence only: Docker errors may include configuration.
            (args.context / (prefix + "-command-error.log")).write_bytes(exc.output or b"")
            raise

    def dk(*command):
        return run(docker, *command)

    def check(name, condition):
        if not condition:
            raise RuntimeError("Acceptance failed: " + name)
        report["checks"].append(name)
        print("PASS " + name, flush=True)

    def execute(service, code):
        return run(docker, "exec", "-i", prefix + "-" + service, "python", "-", input=code.encode())

    def baseline():
        # This network-disabled packaging test cannot certify a remote Plex server.
        if not args.protect_container:
            return {}
        data = json.loads(dk("inspect", *args.protect_container))
        return {v["Name"]: [v["Id"], v["State"]["StartedAt"], v["RestartCount"]] for v in data}

    def healthy():
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            data = json.loads(dk("inspect", *(prefix + "-" + s for s in services)))
            if all(v["State"].get("Health", {}).get("Status") == "healthy" for v in data):
                return
            if any(not v["State"]["Running"] for v in data):
                raise RuntimeError("Test container exited; inspect its private Docker log")
            time.sleep(2)
        raise RuntimeError("Test healthcheck deadline exceeded")

    def fingerprint(service):
        return execute(service, "from pathlib import Path\nimport hashlib\np=Path('/state/identity.json') if " + repr(service) + " == 'playback' else Path('/handoff/ready.json')\nprint(hashlib.sha256(p.read_bytes()).hexdigest())")

    report_path = args.context / (prefix + "-report.json")
    check("new report destination", not report_path.exists())
    check("resource names unused", not dk("ps", "-a", "--filter", "name=" + prefix, "-q").strip()
          and not dk("volume", "ls", "--filter", "name=" + prefix, "-q").strip()
          and not dk("images", prefix + "-*", "-q").strip())
    report["production_before"] = baseline()
    report["production_scope"] = "Bridge containers only; Plex is not contacted by this isolated test"
    report["source_hashes"] = {str(p.relative_to(args.context)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted(args.context.rglob("*.py"))}
    (args.context / (prefix + "-preflight.json")).write_text(json.dumps(report, indent=2))
    try:
        for service in services:
            if not args.image_prefix:
                print("Building " + service, flush=True)
                with (args.context / (service + "-build.log")).open("x") as log:
                    subprocess.run([docker, "build", "--network=none", "--target", service,
                                    "-t", prefix + "-" + service, str(args.context)],
                                   stdout=log, stderr=subprocess.STDOUT, check=True, timeout=300)
        report["image_ids"] = {s: json.loads(dk("image", "inspect", image_prefix + "-" + s))[0]["Id"] for s in services}
        check("three Docker targets available", True)
        capabilities = json.loads(dk("info", "--format", "{{json .}}"))
        limits = ["--memory", "192m", "--cpu-shares", "128"]
        if capabilities.get("CPUCfsQuota"):
            limits += ["--cpus", "0.5"]
        if capabilities.get("PidsLimit"):
            limits += ["--pids-limit", "64"]
        for volume in volumes:
            dk("volume", "create", "--label", "plex-bridge.acceptance=" + prefix, prefix + "-" + volume)
        envs = {
            "setup": {"SETUP_PATH": "/private", "SETUP_HOST": "127.0.0.1", "SETUP_PORT": "8795",
                      "SETUP_ORIGIN": "http://127.0.0.1:8795", "EXPECTED_BRIDGE_ORIGIN": "http://192.0.2.10:8794",
                      "RUNTIME_EXPORTS": "/exports", "HANDOFF_PATH": "/handoff"},
            "topology": {"RUNTIME_PATH": "/config", "HANDOFF_PATH": "/handoff", "LISTEN_HOST": "127.0.0.1",
                         "LISTEN_PORT": "8793", "HISTORY_PATH": "/state/history.json", "POLICY_PATH": "/state/leadership.json",
                         "TRIGGER_STANDBY_ENABLED": "0"},
            "playback": {"RUNTIME_PATH": "/config", "HANDOFF_PATH": "/handoff", "LISTEN_HOST": "127.0.0.1",
                         "LISTEN_PORT": "8794", "STATE_PATH": "/state", "GROUP_API_BASE": "http://127.0.0.1:8793",
                         "PLEX_GROUP_ENDPOINT_MODE": "local"},
        }
        mounts = {"setup": ["private:/private", "playback-config:/exports/playback", "topology-config:/exports/topology", "handoff:/handoff"],
                  "topology": ["topology-config:/config:ro", "handoff:/handoff:ro", "topology-state:/state"],
                  "playback": ["playback-config:/config:ro", "handoff:/handoff:ro", "playback-state:/state"]}
        for service in services:
            command = ["run", "-d", "--name", prefix + "-" + service, "--label", "plex-bridge.acceptance=" + prefix,
                       "--network", "none", "--init", "--read-only", "--user", "10001:10001", "--cap-drop", "ALL",
                       "--security-opt", "no-new-privileges:true", "--tmpfs", "/tmp:size=8m,mode=1777", "--restart", "no",
                       "--log-opt", "max-size=2m", "--log-opt", "max-file=1"] + limits
            for key, value in envs[service].items():
                command += ["-e", key + "=" + value]
            for mount in mounts[service]:
                command += ["-v", prefix + "-" + mount]
            created.append(service)
            dk(*command, image_prefix + "-" + service)
        healthy()
        check("fresh setup and waiting runtimes healthy", True)
        for service in services:
            data = json.loads(dk("inspect", prefix + "-" + service))[0]
            check(service + " isolation", data["HostConfig"]["NetworkMode"] == "none"
                  and not data["HostConfig"].get("PortBindings") and data["HostConfig"]["ReadonlyRootfs"]
                  and data["Config"]["User"] == "10001:10001" and data["HostConfig"]["CapDrop"] == ["ALL"])
        execute("setup", '''
from http.client import HTTPConnection
from urllib.parse import urlencode
from pathlib import Path
import json, secrets, os
assert os.getuid() == 10001
cookie = ''
def request(path, fields=None, origin='http://127.0.0.1:8795'):
    global cookie
    conn = HTTPConnection('127.0.0.1',8795,timeout=10)
    headers = {'Cookie':cookie, 'Origin':origin,'Content-Type':'application/x-www-form-urlencoded'}
    conn.request('POST' if fields is not None else 'GET',path,urlencode(fields) if fields is not None else None,headers)
    response = conn.getresponse()
    if response.getheader('Set-Cookie'): cookie = response.getheader('Set-Cookie').split(';')[0]
    result = response.status,response.read().decode()
    location = response.getheader('Location')
    conn.close()
    if result[0] == 303:
        assert location == '/'
        return request('/')
    return result
code=json.loads(Path('/private/setup-code.json').read_text())['code']
assert request('/claim',{'code':code},'http://example.invalid')[0]==403
assert request('/claim',{'code':code})[0]==200
assert request('/administrator',{'username':'acceptance','password':secrets.token_urlsafe(32)})[0]==200
assert request('/players',{'advertised_origin':'http://192.0.2.10:8794','plex_server_origin':'http://192.0.2.20:32400','rooms':'Synthetic room, 192.0.2.30'})[0]==200
status,body=request('/finish',{})
assert status==200 and 'Local setup is complete' in body
assert '/onboarding' in body and code not in body
assert Path('/handoff/ready.json').exists()
''')
        check("HTTP setup, cross-origin rejection and atomic handoff", True)
        for service in ("topology", "playback"):
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                value = execute(service, "from pathlib import Path\nprint(Path('/tmp/bridge-waiting.json').exists())").strip()
                if value == "False":
                    break
                time.sleep(1)
            check(service + " left setup wait", value == "False")
            execute(service, '''
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError
import os,json,time
# Leaving the waiting process precedes binding the runtime HTTP listener.
deadline=time.monotonic()+30
while True:
    try:
        with urlopen('http://127.0.0.1:'+os.environ['LISTEN_PORT']+'/health',timeout=3) as response:
            assert response.status==200
        break
    except (URLError,TimeoutError):
        if time.monotonic()>=deadline: raise
        time.sleep(0.25)
assert not Path('/private/setup.json').exists()
assert not Path('/exports/playback/credentials.json').exists()
assert Path('/config/credentials.json').stat().st_mode & 0o777 == 0o600
''')
            data = json.loads(dk("inspect", prefix + "-" + service))[0]
            check(service + " credential mount isolation", {v["Name"] for v in data["Mounts"] if v["Type"] == "volume"}
                  == {prefix + "-" + service + "-config", prefix + "-handoff", prefix + "-" + service + "-state"}
                  and all(not v["RW"] for v in data["Mounts"] if v["Destination"] in ("/config", "/handoff")))
        before = {s: fingerprint(s) for s in services}
        execute("playback", '''
from http.client import HTTPConnection
from pathlib import Path
from plex_bridge.playback import App
import json,re,html
credentials=json.loads(Path('/config/credentials.json').read_text())
cookie='wiim_focus_session='+App(None,'unused','unused',credentials['WEB_SESSION_SECRET']).session_token()
def get(path, authenticated=False):
    conn=HTTPConnection('127.0.0.1',8794,timeout=10)
    conn.request('GET',path,headers={'Cookie':cookie} if authenticated else {})
    response=conn.getresponse()
    result=response.status,dict(response.getheaders()),response.read()
    conn.close()
    return result
assert get('/diagnostics')[0]==303
get('/private-probe-title?token=private-probe-token')
status,headers,page=get('/diagnostics',True)
assert status==200 and headers['Cache-Control']=='no-store'
assert "frame-ancestors 'none'" in headers['Content-Security-Policy']
incident=re.search(rb'/diagnostics/download/([0-9a-f]{32})',page).group(1).decode()
assert get('/diagnostics/download/'+incident)[0]==303
status,headers,download=get('/diagnostics/download/'+incident,True)
assert status==200 and incident in headers['Content-Disposition']
preview=html.unescape(re.search(r'<pre[^>]*>(.*?)</pre>',page.decode(),re.S).group(1))
assert preview.encode()==download
payload=json.loads(download)
assert payload['incident_id']==incident and payload['events']
assert payload['health']['devices']=='not_checked'
for secret in credentials.values():
    assert str(secret).encode() not in download
for private in (b'private-probe-title',b'private-probe-token',b'192.0.2.',b'Synthetic room'):
    assert private not in download
''')
        check("authenticated diagnostics preview matches download without private data", True)
        for service in services:
            logs = dk("logs", prefix + "-" + service)
            lines = [json.loads(line) for line in logs.splitlines() if line.strip()]
            check(service + " structured privacy-safe logs", bool(lines)
                  and all(isinstance(row, dict) and "event" in row and "component" in row for row in lines)
                  and not any(value in logs for value in ("private-probe-title", "private-probe-token", "192.0.2.", "Synthetic room", "Traceback")))
        dk("restart", *(prefix + "-" + s for s in services))
        healthy()
        check("restart preserves identity and handoff", before == {s: fingerprint(s) for s in services})
        # Restore all six quiesced volumes into fresh volumes, then recreate
        # the isolated containers against the restored set using identical config.
        dk("stop", *(prefix + "-" + s for s in services))
        for volume in volumes:
            original = prefix + "-" + volume
            restored = original + "-restored"
            dk("volume", "create", "--label", "plex-bridge.acceptance=" + prefix, restored)
            dk("run", "--rm", "--network", "none", "--user", "0", "--entrypoint", "sh",
               "-v", original + ":/from:ro", "-v", restored + ":/to",
               image_prefix + "-setup", "-c", "cd /from && tar cf - . | tar xpf - -C /to")
        for service in services:
            # Only this harness's uniquely named disposable containers are removed.
            dk("rm", prefix + "-" + service)
            command = ["run", "-d", "--name", prefix + "-" + service,
                       "--label", "plex-bridge.acceptance=" + prefix, "--network", "none",
                       "--init", "--read-only", "--user", "10001:10001", "--cap-drop", "ALL",
                       "--security-opt", "no-new-privileges:true", "--tmpfs", "/tmp:size=8m,mode=1777",
                       "--restart", "no"] + limits
            for key, value in envs[service].items():
                command += ["-e", key + "=" + value]
            for mount in mounts[service]:
                volume, destination = mount.split(":", 1)
                command += ["-v", prefix + "-" + volume + "-restored:" + destination]
            dk(*command, image_prefix + "-" + service)
        healthy()
        check("restore preserves identity and handoff", before == {s: fingerprint(s) for s in services})
        report["result"] = "passed"
    except BaseException as exc:
        report["result"] = "incomplete"
        report["error_type"] = type(exc).__name__
        raise
    finally:
        for service in reversed(created):
            # A failed docker run can leave a created-but-not-started container.
            subprocess.run([docker, "stop", "-t", "5", prefix + "-" + service],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        report["production_after"] = baseline()
        report["production_unchanged"] = report["production_before"] == report["production_after"]
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        print("Test containers stopped; disposable volumes retained. Report: " + str(report_path), flush=True)
    check("production container identities/start times unchanged", report["production_unchanged"])


if __name__ == "__main__":
    main()
