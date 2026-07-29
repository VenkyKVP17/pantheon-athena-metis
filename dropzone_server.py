from http.server import HTTPServer, BaseHTTPRequestHandler
import os
import cgi
import time
import json
import subprocess
from urllib.parse import unquote, urlsplit

VAULT_DIR = '/home/ubuntu/vp'
NEET_PG_DIR = '/home/ubuntu/vp/NEET_PG'
DROPZONE = '/home/ubuntu/vp/NEET_PG/Dropzone'
DASHBOARD_FILE = '/home/ubuntu/vp/NEET_PG/athena_dashboard.html'
METRICS_FILE = '/home/ubuntu/vp/NEET_PG/study_metrics.json'

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

class SimpleUploadHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()

    def do_GET(self):
        if self.path == '/metrics':
            try:
                subprocess.run(['python3', '/home/ubuntu/vp/NEET_PG/athena_metrics.py'], capture_output=True)
                with open(METRICS_FILE, 'r') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(content.encode())
                return
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())
                return

        # Static file router: serve any file that actually exists by its real
        # name (dashboard, PWA manifest, service worker, JSON data), so links
        # between the Athena/METIS pages resolve instead of always falling
        # back to the Athena dashboard.
        req_path = unquote(urlsplit(self.path).path)
        rel_path = req_path.lstrip('/')

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

    def do_POST(self):
        if self.path == '/session':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length)
                session = json.loads(body)

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

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())
            return

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
                    safe_fn = f"{name}_{int(time.time())}{ext}"
                    save_path = os.path.join(DROPZONE, safe_fn)
                    with open(save_path, 'wb') as f:
                        f.write(fileitem.file.read())
                    
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

if __name__ == '__main__':
    os.makedirs(DROPZONE, exist_ok=True)
    server_address = ('0.0.0.0', 8085)
    httpd = HTTPServer(server_address, SimpleUploadHandler)
    print("Starting ATHENA Dropzone & Dashboard Server on port 8085...")
    httpd.serve_forever()
