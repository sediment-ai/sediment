# SPDX-License-Identifier: AGPL-3.0-or-later
"""Opt-in checks against exact built images, without contacting a model provider."""

from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
RESTRICTED_RUNTIME = (
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges:true",
    "--memory=2g",
    "--cpus=2",
    "--pids-limit=256",
)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=120
    )
    if check:
        assert result.returncode == 0, result.stdout[-16000:] + result.stderr[-16000:]
    return result


def image(kind: str) -> str:
    value = os.environ.get(f"SEDIMENT_TEST_{kind}_IMAGE")
    if not value:
        pytest.skip(f"Set SEDIMENT_TEST_{kind}_IMAGE to test the built artifact")
    return value


def test_api_runtime_has_supported_python_without_installers() -> None:
    result = docker(
        "run",
        "--rm",
        *RESTRICTED_RUNTIME,
        "--tmpfs=/tmp:rw,noexec,nosuid,size=256m",
        "--entrypoint",
        "python",
        image("API"),
        "-c",
        "import importlib.util,os,shutil,sys; "
        "assert sys.version_info[:3] >= (3,12,14); "
        "assert sys.version_info[:2] == (3,12); "
        "assert os.getuid() != 0; "
        "assert shutil.which('uv') is None; "
        "assert importlib.util.find_spec('pip') is None; "
        "assert importlib.util.find_spec('ensurepip') is None; "
        "assert shutil.which('git'); "
        "assert importlib.util.find_spec('psycopg_binary') is None; "
        "from psycopg import pq; assert pq.__impl__ == 'python'; "
        "assert pq.version() >= 150018; "
        "import sediment_core,sediment_api,sediment_cli; print(sys.version)",
    )
    assert "3.12." in result.stdout
    assert (
        docker(
            "run",
            "--rm",
            *RESTRICTED_RUNTIME,
            "--tmpfs=/tmp:rw,noexec,nosuid,size=256m",
            image("API"),
            "sediment",
            "db",
            "--help",
        ).returncode
        == 0
    )


def test_postgres_runtime_uses_native_privilege_tool() -> None:
    result = docker(
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        image("POSTGRES"),
        "-ec",
        "test ! -e /usr/local/bin/gosu; command -v setpriv; "
        "setpriv --reuid=postgres --regid=postgres --init-groups id -u",
    )
    assert result.stdout.strip().endswith("999")


@pytest.mark.parametrize("kind", ["API", "POSTGRES"])
def test_runtime_excludes_setid_executables_and_infocmp(kind: str) -> None:
    result = docker(
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--user=0",
        "--entrypoint",
        "sh",
        image(kind),
        "-ec",
        "find / -xdev -type f -perm /6000 -print; test ! -e /usr/bin/infocmp",
    )
    assert not result.stdout.strip(), result.stdout


@contextmanager
def postgres_container(image_name: str, volume: str, init_script: Path):
    name = f"sediment-image-test-{uuid4().hex}"
    docker(
        "run",
        "-d",
        "--name",
        name,
        "-e",
        "POSTGRES_PASSWORD=image-test-only",
        "-e",
        "POSTGRES_DB=image_test",
        "-v",
        f"{volume}:/var/lib/postgresql/data",
        "-v",
        f"{init_script}:/docker-entrypoint-initdb.d/probe.sql:ro",
        image_name,
    )
    try:
        for _ in range(60):
            ready = docker(
                "exec",
                name,
                "psql",
                "-h",
                "127.0.0.1",
                "-U",
                "postgres",
                "-d",
                "image_test",
                "-Atqc",
                "select value from image_probe",
                check=False,
            )
            if ready.returncode == 0 and ready.stdout.strip() == "retained":
                break
            time.sleep(0.5)
        else:
            pytest.fail(docker("logs", name).stdout)
        yield name
    finally:
        docker("rm", "-f", name, check=False)


def test_postgres_init_existing_volume_locale_and_shutdown(tmp_path: Path) -> None:
    image_name = image("POSTGRES")
    volume = f"sediment-image-test-{uuid4().hex}"
    init_script = tmp_path / "probe.sql"
    init_script.write_text(
        "CREATE TABLE image_probe(value text); "
        "INSERT INTO image_probe VALUES ('retained');\n"
    )
    docker("volume", "create", volume)
    try:
        for _ in range(2):
            with postgres_container(image_name, volume, init_script) as name:
                assert (
                    docker("exec", name, "stat", "-c", "%u", "/proc/1").stdout.strip()
                    == "999"
                )
                client = docker(
                    "run",
                    "--rm",
                    *RESTRICTED_RUNTIME,
                    "--tmpfs=/tmp:rw,noexec,nosuid,size=256m",
                    "--network",
                    f"container:{name}",
                    "--entrypoint",
                    "python",
                    image("API"),
                    "-c",
                    "import psycopg; from psycopg import pq; "
                    "assert pq.__impl__ == 'python'; "
                    "connection=psycopg.connect(host='127.0.0.1', dbname='image_test', "
                    "user='postgres', password='image-test-only'); "
                    "assert connection.execute('select value from image_probe')"
                    ".fetchone()[0] == 'retained'; connection.close(); print(pq.version())",
                )
                assert int(client.stdout.strip()) >= 150018
                locale = docker(
                    "exec",
                    name,
                    "psql",
                    "-U",
                    "postgres",
                    "-d",
                    "image_test",
                    "-Atqc",
                    "select datcollate from pg_database where datname=current_database()",
                ).stdout.strip()
                assert locale in {"en_US.utf8", "en_US.UTF-8"}
                docker("stop", "--time", "10", name)
                state = json.loads(docker("inspect", name).stdout)[0]["State"]
                assert state["ExitCode"] == 0
    finally:
        docker("volume", "rm", "-f", volume, check=False)


def test_gateway_nonroot_and_standalone_callback_delivery() -> None:
    result = docker(
        "run",
        "--rm",
        *RESTRICTED_RUNTIME,
        "--tmpfs=/tmp:rw,noexec,nosuid,size=512m",
        "--entrypoint",
        "python",
        "-e",
        "PYTHONPATH=/capture",
        "-e",
        "SEDIMENT_INGEST_URL=http://127.0.0.1:18000",
        "-e",
        "SEDIMENT_API_BEARER_TOKEN=image-test-only",
        "-v",
        f"{ROOT / 'litellm/sediment_callback.py'}:/capture/sediment_callback.py:ro",
        "-v",
        f"{ROOT / 'cli/sediment_cli/delivery.py'}:/capture/sediment_delivery.py:ro",
        image("GATEWAY"),
        "-c",
        r"""
import asyncio,ctypes.util,json,os,pathlib,shutil,threading
from http.server import BaseHTTPRequestHandler,HTTPServer
assert os.getuid() != 0
assert shutil.which('pgbouncer') is None
assert not pathlib.Path('/usr/local/bin/pgbouncer').exists()
assert ctypes.util.find_library('event') is None
received=[]
class Receiver(BaseHTTPRequestHandler):
    def do_POST(self):
        received.append((self.path,self.headers.get('Authorization'),json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"fact_id":"00000000-0000-0000-0000-000000000001","stored":true}')
    def log_message(self,*args): pass
server=HTTPServer(('127.0.0.1',18000),Receiver)
thread=threading.Thread(target=server.serve_forever,daemon=True)
thread.start()
from sediment_callback import handler
asyncio.run(handler.async_log_success_event({'standard_logging_object':{'id':'image-test-call','model':'image-test-model'}},{},None,None))
server.shutdown()
thread.join()
assert len(received)==1,received
assert received[0][0]=='/ingest/gateway'
assert received[0][1]=='Bearer image-test-only'
assert received[0][2]['payload']['id']=='image-test-call'
assert received[0][2]['capture']['id']
handler.close_delivery()
print('standalone callback delivered')
""",
    )
    assert "standalone callback delivered" in result.stdout


def test_gateway_supported_protocol_dependencies_interoperate() -> None:
    result = docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "python",
        image("GATEWAY"),
        "-c",
        r"""
from importlib.metadata import version
assert tuple(map(int,version('protobuf').split('.'))) >= (6,33,6)
assert tuple(map(int,version('grpcio').split('.'))) >= (1,83,1)
assert tuple(map(int,version('grpcio-status').split('.'))) >= (1,83,1)
from google.cloud.kms_v1.types import EncryptRequest
from google.rpc.status_pb2 import Status
from grpc_status import rpc_status
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
request=EncryptRequest(name='projects/test/locations/global/keyRings/test/cryptoKeys/test',plaintext=b'probe')
assert EncryptRequest.deserialize(EncryptRequest.serialize(request)) == request
assert rpc_status.from_call is not None
assert rpc_status.to_status(Status(code=0)).details == ''
assert ExportTraceServiceRequest.FromString(ExportTraceServiceRequest().SerializeToString()) == ExportTraceServiceRequest()
exporter=OTLPSpanExporter(endpoint='127.0.0.1:4317',insecure=True)
exporter.shutdown()
assert FastAPIInstrumentor().instrumentation_dependencies()
print('protocol consumers interoperate')
""",
    )
    assert "protocol consumers interoperate" in result.stdout


def test_gateway_tokenizers_and_hub2_download_and_count_tokens() -> None:
    result = docker(
        "run",
        "--rm",
        "--network=none",
        "--entrypoint",
        "python",
        image("GATEWAY"),
        "-c",
        r"""
import os
os.environ['LITELLM_LOCAL_MODEL_COST_MAP']='True'
os.environ['HF_HUB_DISABLE_XET']='1'
os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
import tempfile, threading, json, hashlib, base64, csv, io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from importlib.metadata import distribution, version
calls=[]
body_holder={}
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_HEAD(self): self.respond(False)
    def do_GET(self): self.respond(True)
    def respond(self,send_body):
        calls.append({'method':self.command,'path':self.path,'authorization':self.headers.get('Authorization')})
        body=body_holder['body']
        self.send_response(200)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('ETag','"'+hashlib.sha1(body).hexdigest()+'"')
        self.send_header('X-Repo-Commit','a'*40)
        self.end_headers()
        if send_body: self.wfile.write(body)
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
os.environ['HF_ENDPOINT']=f'http://127.0.0.1:{server.server_port}'
os.environ['HF_HUB_CACHE']=tempfile.mkdtemp(prefix='hub2-cache-')
thread=threading.Thread(target=server.serve_forever,daemon=True)
thread.start()
from tokenizers import Tokenizer, models, pre_tokenizers
import huggingface_hub, httpx, httpx2
source=Tokenizer(models.WordLevel({'[UNK]':0,'hello':1,'pilot':2},unk_token='[UNK]'))
source.pre_tokenizer=pre_tokenizers.Whitespace()
body_holder['body']=source.to_str().encode()
for name,token in [('anonymous',None),('authorized','local-review-token')]:
    tokenizer=Tokenizer.from_pretrained('sediment/'+name,revision='pilot-revision',token=token)
    assert tokenizer.encode('hello pilot').ids==[1,2]
    assert tokenizer.decode([1,2])=='hello pilot'
    selected=[call for call in calls if '/'+name+'/' in call['path']]
    assert {call['method'] for call in selected}=={'HEAD','GET'},selected
    assert all(call['path']==f'/sediment/{name}/resolve/pilot-revision/tokenizer.json' for call in selected),selected
    expected=None if token is None else 'Bearer '+token
    assert all(call['authorization']==expected for call in selected),selected
import litellm
from litellm.utils import _load_huggingface_tokenizer
local=_load_huggingface_tokenizer('anthropic')
assert local.decode(local.encode('hello pilot').ids)=='hello pilot'
ids=litellm.encode(model='claude-2',text='hello pilot')
assert ids and litellm.decode(model='claude-2',tokens=ids)=='hello pilot'
assert litellm.token_counter(model='claude-2',text='hello pilot')>0
p=distribution('tokenizers')._path/'METADATA'
row=next(row for row in csv.reader(io.StringIO((p.parent/'RECORD').read_text())) if row[0]==p.parent.name+'/METADATA')
expected_hash='sha256='+base64.urlsafe_b64encode(hashlib.sha256(p.read_bytes()).digest()).rstrip(b'=').decode()
assert row[1:]==[expected_hash,str(p.stat().st_size)]
print(json.dumps({'result':'PASS','versions':{n:version(n) for n in ('litellm','tokenizers','huggingface-hub','httpx','httpcore','httpx2','httpcore2','truststore')},'tokenizers_metadata_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'request_methods':[c['method'] for c in calls],'checks':['real Rust Tokenizer.from_pretrained','real Hub 2 hf_hub_download against loopback HTTP server','HEAD/GET revision routing','anonymous requests omit Authorization','string token propagated as Bearer','local Claude tokenizer encode/decode','LiteLLM Claude encode/decode/token_counter','Tokenizers METADATA RECORD integrity','HTTPX and HTTPX2 module coexistence']},indent=2))
server.shutdown()
""",
    )
    assert '"result": "PASS"' in result.stdout


def test_gateway_compression_uses_reviewed_released_zlib() -> None:
    result = docker(
        "run",
        "--rm",
        "--network=none",
        "--entrypoint",
        "python",
        image("GATEWAY"),
        "-c",
        "import pathlib,zlib; "
        "world=pathlib.Path('/etc/apk/world').read_text().splitlines(); "
        "assert 'zlib=1.3.2-r7' in world, world; "
        "assert zlib.ZLIB_RUNTIME_VERSION == '1.3.2'; "
        "payload=b'gateway compression probe'*128; "
        "assert zlib.decompress(zlib.compress(payload)) == payload",
    )
    assert result.returncode == 0


def test_gateway_tls_uses_reviewed_openssl_packages() -> None:
    catalog = json.loads((ROOT / "security/maintenance.json").read_text())
    reviewed = {
        name: catalog["packages"]["apk/" + name]["versions"]
        for name in ("openssl", "libcrypto3", "libssl3")
    }
    result = docker(
        "run",
        "--rm",
        "--network=none",
        "--entrypoint",
        "python",
        image("GATEWAY"),
        "-c",
        r"""
import json, pathlib, ssl, sys
installed = {}
for record in pathlib.Path('/lib/apk/db/installed').read_text().split('\n\n'):
    fields = dict(line.split(':', 1) for line in record.splitlines() if ':' in line)
    if 'P' in fields:
        installed[fields['P']] = fields['V']
world = pathlib.Path('/etc/apk/world').read_text().splitlines()
for name, versions in json.loads(sys.argv[1]).items():
    assert installed[name] in versions, (name, installed[name], versions)
    assert name + '=' + installed[name] in world, name
assert ssl.OPENSSL_VERSION.startswith('OpenSSL ' + installed['libssl3'].split('-r')[0] + ' ')
context = ssl.create_default_context()
assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
assert context.get_ca_certs()
""",
        json.dumps(reviewed),
    )
    assert result.returncode == 0


def test_gateway_tar_filters_keep_relocated_hardlinks_inside_destination() -> None:
    result = docker(
        "run",
        "--rm",
        "--network=none",
        "--entrypoint",
        "python",
        image("GATEWAY"),
        "-c",
        r"""
import io,pathlib,tarfile,tempfile
for extraction_filter in ('data', 'tar'):
    with tempfile.TemporaryDirectory() as directory:
        root=pathlib.Path(directory)
        outside=root/'escape'
        outside.write_bytes(b'private outside file')
        outside.chmod(0o600)
        before=outside.stat()
        destination=root/'extracted'
        destination.mkdir()
        archive=io.BytesIO()
        with tarfile.open(fileobj=archive,mode='w') as writer:
            regular=tarfile.TarInfo('a/escape')
            regular.size=len(b'decoy')
            writer.addfile(regular,io.BytesIO(b'decoy'))
            symlink=tarfile.TarInfo('a/b/s')
            symlink.type=tarfile.SYMTYPE
            symlink.linkname='../escape'
            writer.addfile(symlink)
            hardlink=tarfile.TarInfo('s')
            hardlink.type=tarfile.LNKTYPE
            hardlink.linkname='a/b/s'
            hardlink.mode=0o777
            writer.addfile(hardlink)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as reader:
            reader.extractall(destination,filter=extraction_filter)
        relocated=destination/'s'
        assert not relocated.is_symlink(), extraction_filter
        assert relocated.read_bytes()==b'decoy', extraction_filter
        after=outside.stat()
        assert (after.st_mode,after.st_mtime_ns)==(before.st_mode,before.st_mtime_ns)
        assert outside.read_bytes()==b'private outside file'
print('tar extraction filters preserve the destination boundary')
""",
    )
    assert "tar extraction filters preserve the destination boundary" in result.stdout


def test_gateway_pdf_reader_bounds_alphabetical_page_labels() -> None:
    result = docker(
        "run",
        "--rm",
        "--network=none",
        "--entrypoint",
        "python",
        image("GATEWAY"),
        "-c",
        """
from io import BytesIO
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject
writer = PdfWriter()
writer.add_blank_page(width=72, height=72)
writer._root_object[NameObject('/PageLabels')] = DictionaryObject({
    NameObject('/Nums'): ArrayObject([NumberObject(0), DictionaryObject({
        NameObject('/S'): NameObject('/A'), NameObject('/St'): NumberObject(15000)
    })])
})
encoded = BytesIO()
writer.write(encoded)
encoded.seek(0)
reader = PdfReader(encoded)
assert len(reader.pages) == 1
assert reader.page_labels == ['1'], 'Oversized alphabetical label must fall back'
""",
    )
    assert result.returncode == 0


def test_postgres_entrypoint_patch_fails_closed(tmp_path: Path) -> None:
    entrypoint = tmp_path / "entrypoint.sh"
    original = '#!/bin/bash\nexec gosu postgres "$BASH_SOURCE" "$@"\n'
    entrypoint.write_text(original)
    patcher = ROOT / "docker/postgres/patch-entrypoint.sh"
    subprocess.run(["sh", str(patcher), str(entrypoint)], check=True)
    assert (
        'exec setpriv --reuid=postgres --regid=postgres --init-groups "$BASH_SOURCE" "$@"'
        in entrypoint.read_text()
    )
    for changed in [original.replace("gosu", "other"), original + original]:
        entrypoint.write_text(changed)
        assert subprocess.run(["sh", str(patcher), str(entrypoint)]).returncode != 0
        assert entrypoint.read_text() == changed


@pytest.mark.parametrize("streaming", [False, True], ids=["completion", "stream"])
def test_gateway_starts_routes_completion_and_captures(streaming: bool) -> None:
    result = docker(
        "run",
        "--rm",
        *RESTRICTED_RUNTIME,
        "--tmpfs=/tmp:rw,noexec,nosuid,size=512m",
        "--entrypoint",
        "python",
        "-e",
        "PYTHONPATH=/capture",
        "-e",
        "SEDIMENT_INGEST_URL=http://127.0.0.1:18000",
        "-e",
        "SEDIMENT_API_BEARER_TOKEN=image-test-only",
        "-e",
        "LITELLM_MASTER_KEY=sk-image-test-only",
        "-e",
        "LITELLM_LOCAL_MODEL_COST_MAP=True",
        "-v",
        f"{ROOT / 'litellm/sediment_callback.py'}:/capture/sediment_callback.py:ro",
        "-v",
        f"{ROOT / 'cli/sediment_cli/delivery.py'}:/capture/sediment_delivery.py:ro",
        image("GATEWAY"),
        "-c",
        r"""
import importlib.util,json,os,subprocess,sys,tempfile,threading,time,urllib.error,urllib.request
from http.server import BaseHTTPRequestHandler,HTTPServer
streaming = sys.argv[1] == 'True'
assert os.getuid() != 0
assert importlib.util.find_spec('langfuse') is None
assert importlib.util.find_spec('prisma') is None
assert importlib.util.find_spec('backoff') is None
for package in ('psycopg','psycopg_binary','aws_sdk_bedrock_runtime','aws_sdk_signers',
                'smithy_aws_core','smithy_aws_event_stream','smithy_core',
                'smithy_http','smithy_json','awscrt','ijson'):
    assert importlib.util.find_spec(package) is None, package
assert not os.path.exists('/opt/prisma')
import shutil
assert shutil.which('node') is None
captured=[]
upstream=[]
class Receiver(BaseHTTPRequestHandler):
    def do_POST(self):
        payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path.startswith('/v1/messages'):
            upstream.append(payload)
            body={'id':'msg_image_test','type':'message','role':'assistant','model':'claude-sonnet-4-5-20250929','content':[{'type':'text','text':'image completion works'}],'stop_reason':'end_turn','stop_sequence':None,'usage':{'input_tokens':4,'output_tokens':3}}
            if payload.get('stream'):
                events=[
                    {'type':'message_start','message':{**body,'content':[],'stop_reason':None,'usage':{'input_tokens':4,'output_tokens':0}}},
                    {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
                    {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'image '}},
                    {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'completion works'}},
                    {'type':'content_block_stop','index':0},
                    {'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':3}},
                    {'type':'message_stop'},
                ]
                wire=''.join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.send_header('Content-Length',str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)
                return
        elif self.path=='/ingest/gateway':
            captured.append(payload)
            body={'fact_id':'00000000-0000-0000-0000-000000000001','stored':True}
        else:
            self.send_error(404)
            return
        wire=json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(wire)))
        self.end_headers()
        self.wfile.write(wire)
    def log_message(self,*args): pass
server=HTTPServer(('127.0.0.1',18000),Receiver)
thread=threading.Thread(target=server.serve_forever,daemon=True)
thread.start()
with tempfile.TemporaryDirectory() as directory:
    config=directory+'/config.yaml'
    with open(config,'w') as f:
        f.write("""
        + '"""'
        + """model_list:
  - model_name: image-test
    litellm_params:
      model: anthropic/claude-sonnet-4-5-20250929
      api_base: http://127.0.0.1:18000
      api_key: image-test-only
litellm_settings:
  callbacks: sediment_callback.handler
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
"""
        + '"""'
        + r""")
    with tempfile.TemporaryFile(mode='w+') as log:
        process=subprocess.Popen(['python','/usr/local/bin/sediment-gateway.py','--config',config,'--port','14000'],stdout=log,stderr=log)
        try:
            for _ in range(120):
                if process.poll() is not None: raise RuntimeError('gateway exited during startup')
                try:
                    with urllib.request.urlopen('http://127.0.0.1:14000/health/liveliness',timeout=1): break
                except Exception: time.sleep(0.5)
            else: raise RuntimeError('gateway did not become ready')
            for key in (None, 'invalid-key', 'sk-invalid-key'):
                headers = {} if key is None else {'Authorization': 'Bearer ' + key}
                denied = urllib.request.Request('http://127.0.0.1:14000/v1/models', headers=headers)
                try:
                    urllib.request.urlopen(denied, timeout=5)
                except urllib.error.HTTPError as exc:
                    assert exc.code == 401, (key, exc.code)
                else:
                    raise AssertionError('gateway accepted an invalid credential')
            assert not upstream
            request=urllib.request.Request('http://127.0.0.1:14000/v1/chat/completions',data=json.dumps({'model':'image-test','messages':[{'role':'user','content':'image test'}],'stream':streaming}).encode(),headers={'Authorization':'Bearer sk-image-test-only','Content-Type':'application/json','x-sediment-session':'00000000-0000-0000-0000-000000000002'})
            with urllib.request.urlopen(request,timeout=20) as response:
                if streaming:
                    parts=[]
                    finished=False
                    for line in response:
                        if not line.startswith(b'data: '): continue
                        data=line[len(b'data: '):].strip()
                        if data==b'[DONE]':
                            finished=True
                            break
                        event=json.loads(data)
                        for choice in event.get('choices',[]):
                            parts.append(choice.get('delta',{}).get('content') or '')
                    assert finished,parts
                    content=''.join(parts)
                else:
                    answer=json.load(response)
                    content=answer['choices'][0]['message']['content']
            assert content=='image completion works',content
            for _ in range(50):
                if captured: break
                time.sleep(0.1)
            assert len(upstream)==1,upstream
            assert bool(upstream[0].get('stream'))==streaming,upstream
            assert len(captured)==1,captured
            assert captured[0]['provider']=='litellm'
            assert captured[0]['payload']['id']
            assert captured[0]['capture']['id']
            assert captured[0]['payload']['response']['choices'][0]['message']['content']==content,captured
        except Exception:
            log.seek(0)
            print(log.read()[-16000:])
            raise
        finally:
            process.terminate()
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
server.shutdown()
thread.join()
print('gateway routed and captured completion')
""",
        str(streaming),
    )
    assert "gateway routed and captured completion" in result.stdout


@pytest.mark.parametrize("name", ["DATABASE_URL", "LITELLM_PGBOUNCER_ENABLED"])
def test_gateway_image_entrypoint_rejects_database_secrets(name: str) -> None:
    result = docker(
        "run",
        "--rm",
        "-e",
        f"{name}=secret",
        image("GATEWAY"),
        "--config",
        "/not-loaded.yaml",
        check=False,
    )
    assert result.returncode != 0
    assert "does not support database configuration" in result.stderr
    assert "secret" not in result.stderr


def test_capture_http_hook_runs_without_libpq() -> None:
    """Build --target dependencies as SEDIMENT_TEST_CAPTURE_IMAGE for this check."""
    result = docker(
        "run",
        "--rm",
        *RESTRICTED_RUNTIME,
        "--tmpfs=/tmp:rw,noexec,nosuid,size=256m",
        "--entrypoint",
        "/app/.venv/bin/python",
        "-e",
        "HOME=/tmp",
        image("CAPTURE"),
        "-c",
        r"""
import ctypes.util,importlib.util,json,os,pathlib,subprocess,tempfile,threading
from http.server import BaseHTTPRequestHandler,HTTPServer
assert ctypes.util.find_library('pq') is None
assert importlib.util.find_spec('psycopg_binary') is None
import sediment_core,sediment_cli.cli
try:
    import psycopg
except ImportError:
    pass
else:
    raise AssertionError('this capture-only check must have no loadable libpq')
received=[]
class Receiver(BaseHTTPRequestHandler):
    def do_POST(self):
        received.append((self.path,self.headers.get('Authorization'),json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{}')
    def log_message(self,*args): pass
server=HTTPServer(('127.0.0.1',18000),Receiver)
thread=threading.Thread(target=server.serve_forever,daemon=True)
thread.start()
try:
    with tempfile.TemporaryDirectory() as directory:
        target=pathlib.Path(directory)/'app.py'
        target.write_text('retained = True\n')
        transcript=pathlib.Path(directory)/'transcript.jsonl'
        entries=[{'type':'assistant','sessionId':'sess-image','timestamp':'2026-09-12T10:00:00Z','message':{'content':[{'type':'tool_use','id':'toolu-image','name':'Write','input':{'file_path':str(target),'content':'retained = True\n'}}]}},{'type':'user','sessionId':'sess-image','timestamp':'2026-09-12T10:00:01Z','message':{'content':[{'type':'tool_result','tool_use_id':'toolu-image','is_error':False}]}}]
        transcript.write_text('\n'.join(json.dumps(entry) for entry in entries)+'\n')
        environment={**os.environ,'SEDIMENT_OTLP_ENDPOINT':'http://127.0.0.1:18000','SEDIMENT_INGEST_TOKEN':'capture-image-only'}
        command=subprocess.run(['/app/.venv/bin/sediment','transcript','--agent','claude-code'],input=json.dumps({'session_id':'sess-image','transcript_path':str(transcript)}),env=environment,text=True,capture_output=True)
        assert command.returncode==0,command.stderr
        assert len(received)==1,(received,command.stderr)
        path,authorization,payload=received[0]
        assert path=='/v1/logs'
        assert authorization=='Bearer capture-image-only'
        record=payload['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
        attributes={attribute['key']:attribute['value']['stringValue'] for attribute in record['attributes']}
        assert attributes['session.id']=='sess-image'
        assert attributes['tool_use_id']=='toolu-image'
        assert attributes['applied_text']=='retained = True\n'
finally:
    server.shutdown()
    server.server_close()
    thread.join()
print('capture hook delivered without a database client library')
""",
    )
    assert "capture hook delivered without a database client library" in result.stdout
