from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from http.cookies import SimpleCookie
import os
import cgi
import time
import json
import sqlite3
import subprocess
import traceback
from urllib.parse import unquote, urlsplit, parse_qs
import datetime
import glob

import metis_db as db
import homepage_api as hp

VAULT_DIR = '/home/ubuntu/vp'
NEET_PG_DIR = '/home/ubuntu/vp/NEET_PG'
DROPZONE = '/home/ubuntu/vp/NEET_PG/Dropzone'
DASHBOARD_FILE = '/home/ubuntu/vp/NEET_PG/athena_dashboard.html'
METRICS_FILE = '/home/ubuntu/vp/NEET_PG/study_metrics.json'
SESSION_COOKIE = 'metis_session'
VERIFIED_PANTHEON_SESSIONS = {}

# NEET_PG/ is the effective webroot (dashboard, PWA manifest, service worker
# all live there). VAULT_DIR is a fallback for the METIS report link, which
# uses a '../06-Agent_Outputs/...' relative path that browsers resolve up to
# vault root since the page itself is served at a bare '/metis_dashboard.html'.
STATIC_ROOTS = [NEET_PG_DIR, VAULT_DIR]

CONTENT_TYPES = {
    '.html': 'text/html',
    '.json': 'application/json',
    '.js': 'application/javascript',
    '.css': 'text/css',
    '.md': 'text/markdown',
    '.png': 'image/png',
    '.svg': 'image/svg+xml',
}

VALID_QUESTION_TYPES = {
    'Clinical_Vignette', 'Except_Negative', 'Multi_Statement_Evaluation', 'Simple_Direct_Recall'
}


def companion_path_for(screenshot_file):
    """The companion for foo_1234.png is foo_1234.meta.json (same stem)."""
    stem = os.path.splitext(screenshot_file)[0]
    return os.path.join(DROPZONE, stem + '.meta.json')


def list_companions():
    out = []
    for path in sorted(glob.glob(os.path.join(DROPZONE, '*.meta.json'))):
        try:
            with open(path, 'r') as f:
                comp = json.load(f)
            if comp.get('companion_type') == 'qbank_completion':
                out.append(comp)
        except Exception:
            continue
    return out


class SimpleUploadHandler(BaseHTTPRequestHandler):
    # ---------------------------------------------------------- helpers ----

    def send_json(self, obj, status=200, set_cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Credentials', 'true')
        if set_cookie:
            self.send_header('Set-Cookie', set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def get_session_token(self):
        header = self.headers.get('Cookie')
        if not header:
            return None
        cookie = SimpleCookie()
        cookie.load(header)
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def verify_vikunja_session(self, token):
        import urllib.request
        global VERIFIED_PANTHEON_SESSIONS
        now = time.time()
        if token in VERIFIED_PANTHEON_SESSIONS:
            user, exp = VERIFIED_PANTHEON_SESSIONS[token]
            if exp > now:
                return user
        try:
            req = urllib.request.Request(
                'http://localhost:3456/api/v1/user',
                headers={'Authorization': f'Bearer {token}'}
            )
            with urllib.request.urlopen(req, timeout=2) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode())
                    if data.get('username') == 'vpk':
                        user = {'id': 1, 'username': 'venky'}
                        VERIFIED_PANTHEON_SESSIONS[token] = (user, now + 300)  # cache for 5 minutes
                        return user
        except Exception as e:
            print(f"Error verifying pantheon_session against Vikunja: {e}")
        return None

    def get_current_user(self):
        user = db.get_user_from_session(self.get_session_token())
        if user:
            return user
        # Check pantheon_session cookie fallback
        header = self.headers.get('Cookie')
        if header:
            cookie = SimpleCookie()
            cookie.load(header)
            p_morsel = cookie.get('pantheon_session')
            if p_morsel:
                return self.verify_vikunja_session(p_morsel.value)
        return None

    def session_cookie_header(self, token):
        return f'{SESSION_COOKIE}={token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax'

    CLEAR_COOKIE = f'{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax'

    # -------------------------------------------------------------- HEAD ---

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Credentials', 'true')
        self.end_headers()

    # --------------------------------------------------------------- GET ---

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if path == '/metrics':
            self.handle_athena_metrics()
            return

        if path.startswith('/api/metis/'):
            try:
                self.handle_metis_get(path, query)
            except Exception as e:
                traceback.print_exc()
                self.send_json({'error': str(e)}, status=500)
            return

        if path == '/api/dropzone/companions':
            try:
                self.send_json({'companions': list_companions()})
            except Exception as e:
                traceback.print_exc()
                self.send_json({'error': str(e)}, status=500)
            return

        if path == '/api/homepage/today':
            try:
                self.send_json(hp.get_today_data())
            except Exception as e:
                traceback.print_exc()
                self.send_json({'error': str(e)}, status=500)
            return

        if path == '/api/homepage/status':
            try:
                self.send_json(hp.get_service_status())
            except Exception as e:
                traceback.print_exc()
                self.send_json({'error': str(e)}, status=500)
            return

        if path == '/api/homepage/weather':
            try:
                self.send_json(hp.get_weather())
            except Exception as e:
                traceback.print_exc()
                self.send_json({'error': str(e)}, status=500)
            return

        if path == '/api/homepage/finance':
            try:
                self.send_json(hp.get_finance_snapshot())
            except Exception as e:
                traceback.print_exc()
                self.send_json({'error': str(e)}, status=500)
            return

        if path == '/api/homepage/wellness':
            try:
                self.send_json(hp.get_wellness_snapshot())
            except Exception as e:
                traceback.print_exc()
                self.send_json({'error': str(e)}, status=500)
            return

        self.serve_static(path)

    def handle_athena_metrics(self):
        try:
            subprocess.run(['python3', '/home/ubuntu/vp/NEET_PG/athena_metrics.py'], capture_output=True)
            with open(METRICS_FILE, 'r') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(content.encode())
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def serve_static(self, path):
        # Static file router: serve any file that actually exists by its real
        # name (dashboard, PWA manifest, service worker, JSON data), so links
        # between the Athena/METIS pages resolve instead of always falling
        # back to the Athena dashboard.
        rel_path = path.lstrip('/')

        if not rel_path:
            file_path = DASHBOARD_FILE
        else:
            file_path = None
            for root in STATIC_ROOTS:
                candidate = os.path.normpath(os.path.join(root, rel_path))
                if candidate.startswith(root) and os.path.isfile(candidate):
                    file_path = candidate
                    break

        if file_path:
            ext = os.path.splitext(file_path)[1]
            with open(file_path, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', CONTENT_TYPES.get(ext, 'application/octet-stream'))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not found.')

    def handle_metis_get(self, path, query):
        if path == '/api/metis/me':
            user = self.get_current_user()
            self.send_json({'user': user})
            return

        user = self.get_current_user()
        if not user:
            self.send_json({'error': 'Not authenticated'}, status=401)
            return

        if path == '/api/metis/decks':
            self.send_json({'decks': db.list_decks_with_counts(user['id'])})
            return

        if path == '/api/metis/cards':
            deck_id = int(query['deck_id'][0]) if 'deck_id' in query else None
            search = query.get('search', [None])[0]
            tag = query.get('tag', [None])[0]
            self.send_json({'cards': db.list_cards(user['id'], deck_id=deck_id, search=search, tag=tag)})
            return

        if path == '/api/metis/queue':
            deck_id = int(query['deck_id'][0]) if 'deck_id' in query else None
            limit = int(query.get('limit', [20])[0])
            due_only = query.get('due_only', ['0'])[0] == '1'
            self.send_json(db.get_due_queue(user['id'], deck_id=deck_id, limit=limit, due_only=due_only))
            return

        if path == '/api/metis/preview':
            card_id = query.get('card_id', [None])[0]
            if not card_id:
                self.send_json({'error': 'card_id required'}, status=400)
                return
            self.send_json({'intervals': db.preview_intervals(user['id'], card_id)})
            return

        if path == '/api/metis/stats':
            self.send_json(db.get_stats(user['id']))
            return

        if path == '/api/metis/settings':
            self.send_json({'settings': db.get_user_settings(user['id'])})
            return

        if path == '/api/metis/flags':
            self.send_json({'flags': db.list_flags(status='open')})
            return

        self.send_json({'error': 'Not found'}, status=404)

    # -------------------------------------------------------------- POST ---

    def do_POST(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)

        if path == '/session':
            self.handle_athena_session()
            return

        if path.startswith('/api/metis/'):
            try:
                self.handle_metis_post(path)
            except sqlite3.IntegrityError:
                self.send_json({'error': 'Username already taken'}, status=409)
            except ValueError as e:
                self.send_json({'error': str(e)}, status=400)
            except Exception as e:
                traceback.print_exc()
                self.send_json({'error': str(e)}, status=500)
            return

        if path == '/api/homepage/task/toggle':
            try:
                body = self.read_json_body()
                result = hp.toggle_task(body.get('date'), body.get('line_no'),
                                         body.get('text', ''), bool(body.get('done')))
                self.send_json({'ok': True, 'task': result})
            except hp.TaskConflict as e:
                self.send_json({'error': str(e)}, status=409)
            except Exception as e:
                traceback.print_exc()
                self.send_json({'error': str(e)}, status=500)
            return

        if path == '/api/homepage/vikunja/toggle':
            try:
                body = self.read_json_body()
                result = hp.toggle_vikunja_task(body.get('id'), bool(body.get('done')))
                self.send_json({'ok': True, 'task': result})
            except hp.TaskConflict as e:
                self.send_json({'error': str(e)}, status=409)
            except Exception as e:
                traceback.print_exc()
                self.send_json({'error': str(e)}, status=500)
            return

        if path == '/api/homepage/quickadd':
            try:
                body = self.read_json_body()
                result = hp.quick_add_task(body.get('text', ''))
                self.send_json({'ok': True, **result})
            except hp.QuickAddError as e:
                self.send_json({'error': str(e)}, status=400)
            except Exception as e:
                traceback.print_exc()
                self.send_json({'error': str(e)}, status=500)
            return

        self.handle_dropzone_upload()

    def handle_athena_session(self):
        try:
            session = self.read_json_body()

            with open(METRICS_FILE, 'r') as f:
                metrics = json.load(f)

            today = time.strftime('%Y-%m-%d')
            log = metrics['daily_logs'].setdefault(today, {
                'date': today,
                'questions_completed': 0,
                'subject_breakdown': {},
                'topic_breakdown': {},
                'topic_mastery': {},
                'sessions': [],
                'active_hours': 0.0,
                'speed_q_per_hour': 0.0,
                'endurance_score': 0
            })
            log.setdefault('sessions', []).append(session)

            with open(METRICS_FILE, 'w') as f:
                json.dump(metrics, f, indent=2)

            subprocess.run(['python3', '/home/ubuntu/vp/NEET_PG/athena_metrics.py'], capture_output=True)
            self.send_json({'ok': True})
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def handle_metis_post(self, path):
        body = self.read_json_body()

        if path == '/api/metis/register':
            username = (body.get('username') or '').strip()
            user_id = db.create_user(username, body.get('password') or '')
            token = db.create_session(user_id)
            self.send_json({'user': {'id': user_id, 'username': username}},
                            set_cookie=self.session_cookie_header(token))
            return

        if path == '/api/metis/login':
            username = (body.get('username') or '').strip()
            user_id = db.verify_user(username, body.get('password') or '')
            if not user_id:
                self.send_json({'error': 'Invalid username or password'}, status=401)
                return
            token = db.create_session(user_id)
            self.send_json({'user': {'id': user_id, 'username': username}},
                            set_cookie=self.session_cookie_header(token))
            return

        if path == '/api/metis/logout':
            token = self.get_session_token()
            if token:
                db.delete_session(token)
            self.send_json({'ok': True}, set_cookie=self.CLEAR_COOKIE)
            return

        # Everything past this point requires an authenticated user.
        user = self.get_current_user()
        if not user:
            self.send_json({'error': 'Not authenticated'}, status=401)
            return

        if path == '/api/metis/review':
            result = db.apply_review(user['id'], body['card_id'], body['rating'], body.get('duration_ms'))
            self.send_json(result)
            return

        if path == '/api/metis/undo':
            ok = db.undo_last_review(user['id'])
            self.send_json({'ok': ok})
            return

        if path == '/api/metis/optimize':
            result = db.optimize_user_parameters(user['id'])
            self.send_json({'ok': True, 'review_count': result['review_count']})
            return

        if path == '/api/metis/optimize/reset':
            db.reset_fsrs_parameters(user['id'])
            self.send_json({'ok': True})
            return

        if path == '/api/metis/cards':
            card_id = db.create_card(body['subject'], body['topic'], body['front'], body['back'], body.get('tags', []))
            self.send_json({'id': card_id})
            return

        if path.startswith('/api/metis/cards/') and path.endswith('/archive'):
            card_id = path[len('/api/metis/cards/'):-len('/archive')].rstrip('/')
            db.archive_card(user['id'], card_id)
            self.send_json({'ok': True})
            return

        if path.startswith('/api/metis/flags/') and path.endswith('/dismiss'):
            flag_id = int(path[len('/api/metis/flags/'):-len('/dismiss')].rstrip('/'))
            ok = db.set_flag_status(flag_id, 'dismissed')
            self.send_json({'ok': ok}, status=200 if ok else 404)
            return

        if path.startswith('/api/metis/flags/') and path.endswith('/resolve'):
            flag_id = int(path[len('/api/metis/flags/'):-len('/resolve')].rstrip('/'))
            ok = db.set_flag_status(flag_id, 'resolved')
            self.send_json({'ok': ok}, status=200 if ok else 404)
            return

        self.send_json({'error': 'Not found'}, status=404)

    def handle_dropzone_upload(self):
        try:
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={'REQUEST_METHOD': 'POST',
                         'CONTENT_TYPE': self.headers['Content-Type'],
                         }
            )
            if 'file' in form:
                fileitem = form['file']
                if fileitem.filename:
                    fn = os.path.basename(fileitem.filename)
                    name, ext = os.path.splitext(fn)
                    ts = int(time.time())
                    safe_fn = f"{name}_{ts}{ext}"
                    save_path = os.path.join(DROPZONE, safe_fn)
                    with open(save_path, 'wb') as f:
                        f.write(fileitem.file.read())

                    now = datetime.datetime.now().astimezone().isoformat()
                    companion = {
                        'companion_type': 'qbank_completion',
                        'screenshot_file': safe_fn,
                        'uploaded_at': now,
                        'completed_at': now,
                        'question_type': None,
                        'correct': None,
                        'subject': None,
                        'topic': None,
                        'notes': None,
                    }
                    companion_path = os.path.join(DROPZONE, f"{name}_{ts}.meta.json")
                    with open(companion_path, 'w') as f:
                        json.dump(companion, f, indent=2)

                    subprocess.run(['python3', '/home/ubuntu/vp/NEET_PG/athena_metrics.py'], capture_output=True)

                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'Screenshot uploaded successfully to Dropzone!\n')
                    return
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'Bad request: No file found in form data\n')
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f'Internal Server Error: {str(e)}\n'.encode())

    # --------------------------------------------------------- PUT/DELETE --

    def do_PUT(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        try:
            if path.startswith('/api/dropzone/companion/'):
                screenshot_file = path.rsplit('/', 1)[-1]
                cpath = companion_path_for(screenshot_file)
                if not os.path.isfile(cpath):
                    self.send_json({'error': 'Companion not found'}, status=404)
                    return
                with open(cpath, 'r') as f:
                    comp = json.load(f)
                body = self.read_json_body()
                if 'question_type' in body:
                    if body['question_type'] is not None and body['question_type'] not in VALID_QUESTION_TYPES:
                        self.send_json({'error': f'question_type must be one of {sorted(VALID_QUESTION_TYPES)}'}, status=400)
                        return
                    comp['question_type'] = body['question_type']
                for field in ('correct', 'subject', 'topic', 'notes'):
                    if field in body:
                        comp[field] = body[field]
                with open(cpath, 'w') as f:
                    json.dump(comp, f, indent=2)
                self.send_json({'ok': True, 'companion': comp})
                return

            if path.startswith('/api/metis/cards/'):
                user = self.get_current_user()
                if not user:
                    self.send_json({'error': 'Not authenticated'}, status=401)
                    return
                card_id = path.rsplit('/', 1)[-1]
                body = self.read_json_body()
                ok = db.update_card(card_id, front=body.get('front'), back=body.get('back'), tags=body.get('tags'))
                self.send_json({'ok': ok}, status=200 if ok else 404)
                return

            if path == '/api/metis/settings':
                user = self.get_current_user()
                if not user:
                    self.send_json({'error': 'Not authenticated'}, status=401)
                    return
                body = self.read_json_body()
                settings = db.update_user_settings(user['id'], body)
                self.send_json({'settings': settings})
                return

            self.send_json({'error': 'Not found'}, status=404)
        except ValueError as e:
            self.send_json({'error': str(e)}, status=400)
        except Exception as e:
            traceback.print_exc()
            self.send_json({'error': str(e)}, status=500)

    def do_DELETE(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        try:
            if path.startswith('/api/metis/cards/'):
                user = self.get_current_user()
                if not user:
                    self.send_json({'error': 'Not authenticated'}, status=401)
                    return
                card_id = path.rsplit('/', 1)[-1]
                db.delete_card(card_id)
                self.send_json({'ok': True})
                return
            self.send_json({'error': 'Not found'}, status=404)
        except Exception as e:
            traceback.print_exc()
            self.send_json({'error': str(e)}, status=500)


if __name__ == '__main__':
    os.makedirs(DROPZONE, exist_ok=True)
    db.init_db()
    server_address = ('0.0.0.0', 8085)
    httpd = ThreadingHTTPServer(server_address, SimpleUploadHandler)
    print("Starting ATHENA Dropzone & Dashboard Server on port 8085...")
    httpd.serve_forever()
