import json
import os
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

root = os.path.dirname(__file__)
db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
db.close()
env = {**os.environ, 'CHESS_DB_PATH': db.name, 'PORT': '8765'}
proc = subprocess.Popen([sys.executable, '-m', 'gunicorn', '-w', '1', '--threads', '4', '--bind', '127.0.0.1:8765', 'server:app'], cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try:
            urlopen('http://127.0.0.1:8765/', timeout=1)
            break
        except Exception:
            time.sleep(.1)
    def call(path, data=None):
        body = None if data is None else json.dumps(data).encode()
        req = Request('http://127.0.0.1:8765' + path, data=body, headers={'Content-Type': 'application/json'} if body else {})
        try:
            with urlopen(req, timeout=3) as r:
                return r.status, json.loads(r.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())
    assert call('/')[0] == 200
    assert call('/missing')[0] == 404
    waiting = call('/v1/play-online', {'userId': 'smoke-a', 'timeControl': '10+0'})[1]
    assert waiting['status'] == 'searching'
    matched = call('/v1/play-online', {'userId': 'smoke-b', 'timeControl': '10+0'})[1]
    game_id = matched['gameId']
    assert matched['status'] == 'matched'
    assert call('/v1/game/' + game_id)[1]['status'] == 'active'
    assert call('/v1/game/' + game_id + '/move', {'move': 'e2e4'})[1]['fen'].startswith('rnbqkbnr')
    proc.terminate(); proc.wait(timeout=5)
    proc = subprocess.Popen([sys.executable, '-m', 'gunicorn', '-w', '1', '--threads', '4', '--bind', '127.0.0.1:8765', 'server:app'], cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        try:
            urlopen('http://127.0.0.1:8765/', timeout=1)
            break
        except Exception:
            time.sleep(.1)
    recovered = call('/v1/game/' + game_id)[1]
    assert recovered['fen'] == call('/v1/game/' + game_id)[1]['fen']
    print('smoke_ok', game_id, recovered['status'])
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill()
    os.unlink(db.name)
    for suffix in ('-wal', '-shm'):
        if os.path.exists(db.name + suffix): os.unlink(db.name + suffix)
