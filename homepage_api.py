import os
import re
import sys
import json
import sqlite3
import datetime
import subprocess
import tempfile
import urllib.request

import yaml

sys.path.insert(0, '/home/ubuntu/vp/.scripts')
sys.path.insert(0, '/home/ubuntu')

VAULT_DIR = '/home/ubuntu/vp'
DAILY_NOTES_DIR = os.path.join(VAULT_DIR, '00-Daily_Notes')
STUDY_METRICS_FILE = os.path.join(os.path.dirname(__file__), 'study_metrics.json')
NYX_STATE_FILE = '/home/ubuntu/vp/05-Development/pantheon-server/data/nyx_state.json'
BUDGET_MD = os.path.join(VAULT_DIR, '04-Finance', 'Budget.md')
PLUTUS_DB = os.path.join(VAULT_DIR, '04-Finance', 'plutus.db')
HYGIEIA_CONFIG = os.path.join(VAULT_DIR, '.agents', 'hygieia_config.json')
PROTEUS_CONFIG = os.path.join(VAULT_DIR, '.agents', 'proteus_config.json')
VK_CLI = os.path.join(VAULT_DIR, '.scripts', 'vk')

TASK_RE = re.compile(r'^(\s*-\s*\[)([ x!])(\]\s*)(.*)$')
TAG_RE = re.compile(r'#([\w-]+)')
FRONTMATTER_RE = re.compile(r'^---\n(.*?)\n---\n?', re.DOTALL)
HEADING_RE = re.compile(r'^##\s')

SYSTEMD_SERVICES = ['caddy', 'tailscaled', 'AdGuardHome', 'ssh', 'docker', 'code-server@ubuntu']
PM2_BIN = '/usr/bin/pm2'
DOCKER_BIN = '/usr/bin/docker'

TASKS_HEADING = '## 🛠️ Tasks'


class TaskConflict(Exception):
    pass


class QuickAddError(Exception):
    pass


def _note_filename(date):
    # Daily notes are named e.g. "Jul 31, 2026.md" — confirmed against the
    # actual vault, not derived from any config.
    return date.strftime('%b %d, %Y') + '.md'


def _note_path(date):
    return os.path.join(DAILY_NOTES_DIR, _note_filename(date))


def _parse_frontmatter_duties(fm_text):
    # Hand-rolled instead of pulling in a YAML dependency — we only ever need
    # the `duties:` list, and frontmatter across notes isn't schema-consistent.
    duties = []
    in_duties = False
    for line in fm_text.splitlines():
        if line.strip() == 'duties:':
            in_duties = True
            continue
        if in_duties:
            m = re.match(r'^-\s*(.+)$', line.strip())
            if m:
                duties.append(m.group(1).strip())
                continue
            in_duties = False
    return duties


def get_today_data():
    date = datetime.date.today()
    path = _note_path(date)
    result = {
        'date': date.isoformat(),
        'note_exists': os.path.isfile(path),
        'duties': [],
        'tasks': [],
    }
    result.update(get_exam_countdown())

    if not result['note_exists']:
        try:
            result['tasks'] = get_vikunja_tasks_today()
        except Exception:
            pass
        return result

    with open(path, 'r') as f:
        text = f.read()

    fm_match = FRONTMATTER_RE.match(text)
    if fm_match:
        result['duties'] = _parse_frontmatter_duties(fm_match.group(1))

    tasks = []
    for i, line in enumerate(text.splitlines()):
        m = TASK_RE.match(line)
        if not m:
            continue
        rest = m.group(4).strip()
        tasks.append({
            'source': 'obsidian',
            'line_no': i,
            'text': rest,
            'done': m.group(2) == 'x',
            'tags': TAG_RE.findall(rest),
        })

    try:
        tasks.extend(get_vikunja_tasks_today())
    except Exception:
        pass

    result['tasks'] = tasks
    return result


def toggle_task(date_str, line_no, expected_text, done):
    try:
        date = datetime.date.fromisoformat(date_str) if date_str else datetime.date.today()
    except ValueError:
        date = datetime.date.today()

    path = _note_path(date)
    if not os.path.isfile(path):
        raise TaskConflict('Daily note not found')

    with open(path, 'r') as f:
        lines = f.readlines()

    if line_no is None or line_no < 0 or line_no >= len(lines):
        raise TaskConflict('Task line no longer exists')

    line = lines[line_no]
    stripped = line[:-1] if line.endswith('\n') else line
    m = TASK_RE.match(stripped)
    if not m or m.group(4).strip() != expected_text:
        raise TaskConflict('Note changed since this task was loaded — refresh and try again')

    new_char = 'x' if done else ' '
    new_line = m.group(1) + new_char + m.group(3) + m.group(4)
    lines[line_no] = new_line + ('\n' if line.endswith('\n') else '')

    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w') as f:
            f.writelines(lines)
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise

    return {'line_no': line_no, 'text': expected_text, 'done': done}


from zoneinfo import ZoneInfo
IST = ZoneInfo('Asia/Kolkata')


# ------------------------------------------------------------------ Vikunja ---

_vikunja_helper = None


def _get_vikunja_helper():
    global _vikunja_helper
    if _vikunja_helper is None:
        from vikunja_helper import VikunjaHelper
        _vikunja_helper = VikunjaHelper()
    return _vikunja_helper


def get_vikunja_tasks_today():
    """Tasks due today or overdue, normalized to the same shape as Obsidian tasks."""
    helper = _get_vikunja_helper()
    if not helper.client:
        return []
    seen = set()
    out = []
    for t in helper.get_tasks_today() + helper.get_tasks_overdue():
        if t['id'] in seen:
            continue
        seen.add(t['id'])
        out.append({
            'source': 'vikunja',
            'id': t['id'],
            'text': t.get('title', 'Untitled'),
            'done': bool(t.get('done')),
            'tags': [],
        })
    return out


def toggle_vikunja_task(task_id, done):
    helper = _get_vikunja_helper()
    if not helper.client:
        raise TaskConflict('Vikunja not reachable')
    try:
        task = helper.client.update_task(int(task_id), {'done': bool(done)})
    except Exception as e:
        raise TaskConflict(str(e))
    return {'id': task.get('id', task_id), 'done': bool(task.get('done', done))}


def _ensure_daily_note(date):
    """Create the daily note with standard frontmatter/heading if it doesn't exist. Returns True if created."""
    path = _note_path(date)
    if os.path.isfile(path):
        return False
    content = (
        "---\n"
        f"date: '{date.isoformat()}'\n"
        f"id: {date.strftime('%d%b%Y')}\n"
        "---\n\n"
        f"# 📓 {date.strftime('%A, %B %d, %Y')}\n"
    )
    fd, tmp_path = tempfile.mkstemp(dir=DAILY_NOTES_DIR)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise
    return True


def _append_task_line(date, text):
    """Append `- [ ] text` under the Tasks heading (creating it if absent). Atomic write."""
    path = _note_path(date)
    with open(path, 'r') as f:
        lines = f.readlines()

    new_line = f"- [ ] {text}\n"

    heading_idx = None
    for i, l in enumerate(lines):
        if l.rstrip('\n') == TASKS_HEADING:
            heading_idx = i
            break

    if heading_idx is None:
        if lines and lines[-1].strip() != '':
            lines.append('\n')
        lines.append(TASKS_HEADING + '\n')
        lines.append(new_line)
    else:
        insert_at = len(lines)
        for j in range(heading_idx + 1, len(lines)):
            if HEADING_RE.match(lines[j]):
                insert_at = j
                break
        lines.insert(insert_at, new_line)

    fd, tmp_path = tempfile.mkstemp(dir=DAILY_NOTES_DIR)
    try:
        with os.fdopen(fd, 'w') as f:
            f.writelines(lines)
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise


def quick_add_task(text):
    """Parse+create via the `vk` CLI (Vikunja NLP engine, gemini-service-backed),
    then mirror the created task as a checkbox line on the target day's daily note."""
    text = (text or '').strip()
    if not text:
        raise QuickAddError('Empty task text')

    try:
        result = subprocess.run(
            [VK_CLI, '--json', text],
            capture_output=True, text=True, timeout=45,
        )
    except subprocess.TimeoutExpired:
        raise QuickAddError('Vikunja parser timed out')

    if result.returncode != 0:
        raise QuickAddError((result.stderr or '').strip()[-300:] or 'vk exited non-zero')

    try:
        parsed = json.loads(result.stdout.strip())
    except Exception:
        raise QuickAddError('Could not parse vk output')

    task = parsed[0] if isinstance(parsed, list) else parsed
    if not task or task.get('error'):
        raise QuickAddError((task or {}).get('error') or 'Task creation failed')

    target_date = datetime.date.today()
    due_date = task.get('due_date')
    if due_date and not str(due_date).startswith('0001'):
        try:
            dt = datetime.datetime.fromisoformat(str(due_date).replace('Z', '+00:00'))
            target_date = dt.astimezone(IST).date()
        except Exception:
            pass

    note_created = _ensure_daily_note(target_date)
    _append_task_line(target_date, task.get('title') or text)

    return {
        'vikunja_task': task,
        'daily_note_date': target_date.isoformat(),
        'daily_note_created': note_created,
    }


# ------------------------------------------------------------------ Finance ---

def get_finance_snapshot():
    try:
        budget = {}
        if os.path.isfile(BUDGET_MD):
            text = open(BUDGET_MD, 'r').read()
            m = FRONTMATTER_RE.match(text)
            if m:
                data = yaml.safe_load(m.group(1)) or {}
                budget = data.get('budget', {}) or {}

        refill_total = sum(
            v.get('amount', 0) for v in budget.values()
            if isinstance(v, dict) and v.get('type') == 'refill'
        )

        spent_by_category = {}
        if os.path.isfile(PLUTUS_DB):
            month_prefix = datetime.date.today().strftime('%Y-%m')
            conn = sqlite3.connect(PLUTUS_DB)
            try:
                cur = conn.execute(
                    "SELECT category, type, amount FROM transactions WHERE date LIKE ?",
                    (month_prefix + '%',)
                )
                for category, tx_type, amount in cur.fetchall():
                    tx_type = (tx_type or '').lower().strip()
                    category = (category or '').strip()
                    if tx_type in ('transfer', 'income') or not category:
                        continue
                    spent_by_category[category] = spent_by_category.get(category, 0) + (amount or 0)
            finally:
                conn.close()

        spent_total = sum(spent_by_category.values())

        top_categories = []
        for cat, spent in spent_by_category.items():
            cat_budget = budget.get(cat)
            limit = cat_budget.get('amount') if isinstance(cat_budget, dict) else None
            ratio = (spent / limit) if limit else None
            top_categories.append({'category': cat, 'spent': round(spent, 2), 'limit': limit, 'ratio': ratio})
        top_categories.sort(key=lambda c: (c['ratio'] is None, -(c['ratio'] or 0), -c['spent']))

        today = datetime.date.today()
        next_month = datetime.date(today.year + (1 if today.month == 12 else 0), (today.month % 12) + 1, 1)
        days_left = (next_month - today).days

        return {
            'budget_total': refill_total,
            'spent_total': round(spent_total, 2),
            'remaining': round(refill_total - spent_total, 2),
            'days_left_in_month': days_left,
            'top_categories': top_categories[:5],
        }
    except Exception as e:
        return {'error': str(e)}


# --------------------------------------------------------------- Wellness/Admin ---

def get_wellness_snapshot():
    items = []
    today = datetime.date.today()

    try:
        if os.path.isfile(HYGIEIA_CONFIG):
            cfg = json.load(open(HYGIEIA_CONFIG, 'r'))
            for key, label in (('beard_trim', 'Beard trim'), ('haircut', 'Haircut')):
                entry = cfg.get(key) or {}
                interval = entry.get('interval_days')
                last_done = entry.get('last_done')
                if not interval or not last_done:
                    continue
                try:
                    last = datetime.date.fromisoformat(last_done)
                except Exception:
                    continue
                due_in = interval - (today - last).days
                if due_in < 0:
                    items.append({'label': label, 'status': f"{label} overdue {abs(due_in)}d", 'urgency': 100 + abs(due_in)})
                elif due_in == 0:
                    items.append({'label': label, 'status': f"{label} due today", 'urgency': 90})
                else:
                    items.append({'label': label, 'status': f"{label} due in {due_in}d", 'urgency': max(0, 30 - due_in)})
    except Exception:
        pass

    try:
        if os.path.isfile(PROTEUS_CONFIG):
            cfg = json.load(open(PROTEUS_CONFIG, 'r'))

            rent = cfg.get('rent') or {}
            if rent:
                due_day = rent.get('due_date', 1)
                amount = rent.get('amount', 0)
                last_paid = rent.get('last_paid')
                next_due = datetime.date(today.year, today.month, due_day)
                if last_paid:
                    try:
                        lp = datetime.date.fromisoformat(last_paid)
                        if lp.year == today.year and lp.month == today.month:
                            next_due = datetime.date(today.year + (1 if today.month == 12 else 0), (today.month % 12) + 1, due_day)
                    except Exception:
                        pass
                days = (next_due - today).days
                lead = rent.get('alert_lead_days', 3)
                if days < 0:
                    items.append({'label': 'Rent', 'status': f"Rent overdue {abs(days)}d (₹{amount:,})", 'urgency': 200})
                elif days <= lead:
                    items.append({'label': 'Rent', 'status': f"Rent due in {days}d (₹{amount:,})", 'urgency': 80 - days})

            for doc in cfg.get('documents', []):
                exp = doc.get('expiry_date')
                if not exp:
                    continue
                try:
                    exp_date = datetime.date.fromisoformat(exp)
                except Exception:
                    continue
                days = (exp_date - today).days
                lead = doc.get('renewal_lead_days', 30)
                if days <= lead:
                    name = doc.get('name', 'Document')
                    status = f"{name} expires in {days}d" if days >= 0 else f"{name} expired {abs(days)}d ago"
                    items.append({'label': name, 'status': status, 'urgency': 150 - days})

            vehicle = cfg.get('vehicle') or {}
            for svc_key, svc in (vehicle.get('services') or {}).items():
                last_date = svc.get('last_date')
                interval_months = svc.get('interval_months')
                if not last_date or not interval_months:
                    continue
                try:
                    last = datetime.date.fromisoformat(last_date)
                except Exception:
                    continue
                month0 = last.month - 1 + interval_months
                next_due = datetime.date(last.year + month0 // 12, month0 % 12 + 1, min(last.day, 28))
                days = (next_due - today).days
                if days <= 14:
                    label = svc_key.replace('_', ' ').title()
                    status = f"{label} due in {days}d" if days >= 0 else f"{label} overdue {abs(days)}d"
                    items.append({'label': label, 'status': status, 'urgency': (100 if days < 0 else 40 - days)})
    except Exception:
        pass

    items.sort(key=lambda i: -i['urgency'])
    return {'items': items[:3], 'all_items': items}


def get_exam_countdown():
    try:
        with open(STUDY_METRICS_FILE, 'r') as f:
            metrics = json.load(f)
        exam_date_str = metrics.get('exam_date')
        if not exam_date_str:
            return {'exam_date': None, 'exam_days_left': None}
        exam_date = datetime.date.fromisoformat(exam_date_str)
        return {
            'exam_date': exam_date_str,
            'exam_days_left': (exam_date - datetime.date.today()).days,
        }
    except Exception:
        return {'exam_date': None, 'exam_days_left': None}


def _systemd_status(name):
    try:
        out = subprocess.run(['systemctl', 'is-active', name], capture_output=True, text=True, timeout=3)
        return out.stdout.strip() == 'active'
    except Exception:
        return None


def _pm2_list():
    try:
        out = subprocess.run([PM2_BIN, 'jlist'], capture_output=True, text=True, timeout=5)
        procs = json.loads(out.stdout)
        return sorted(
            [{'name': p['name'], 'status': p.get('pm2_env', {}).get('status'), 'up': p.get('pm2_env', {}).get('status') == 'online'} for p in procs],
            key=lambda p: p['name']
        )
    except Exception:
        return []


def _docker_list():
    try:
        out = subprocess.run(
            [DOCKER_BIN, 'ps', '-a', '--format', '{{.Names}}|{{.State}}'],
            capture_output=True, text=True, timeout=5
        )
        containers = []
        for line in out.stdout.strip().splitlines():
            if '|' not in line:
                continue
            name, state = line.split('|', 1)
            containers.append({'name': name, 'status': state, 'up': state == 'running'})
        return sorted(containers, key=lambda c: c['name'])
    except Exception:
        return []


def get_service_status():
    return {
        'systemd': {name: _systemd_status(name) for name in SYSTEMD_SERVICES},
        'docker': _docker_list(),
        'pm2': _pm2_list(),
    }


def get_weather():
    # Location comes from the Tasker push pipeline (server/api/nyx/location.post.ts
    # writes last_gps into this same state file on every phone location ping) —
    # not browser geolocation, which was unreliable in the kiosk context.
    try:
        with open(NYX_STATE_FILE, 'r') as f:
            state = json.load(f)
        gps = state.get('last_gps')
    except Exception:
        gps = None

    if not gps:
        return {'error': 'no_location', 'message': 'No GPS fix from the Tasker pipeline yet'}

    updated_at = datetime.datetime.fromisoformat(gps['updated_at'].replace('Z', '+00:00'))
    age_seconds = (datetime.datetime.now(datetime.timezone.utc) - updated_at).total_seconds()

    url = (
        'https://api.open-meteo.com/v1/forecast'
        f'?latitude={gps["lat"]}&longitude={gps["lon"]}'
        '&current=temperature_2m,weather_code'
        '&daily=temperature_2m_max,temperature_2m_min&timezone=auto'
    )
    try:
        with urllib.request.urlopen(url, timeout=6) as resp:
            forecast = json.loads(resp.read())
    except Exception as e:
        return {'error': 'fetch_failed', 'message': str(e)}

    return {
        'temperature_c': forecast['current']['temperature_2m'],
        'weather_code': forecast['current']['weather_code'],
        'high_c': forecast['daily']['temperature_2m_max'][0],
        'low_c': forecast['daily']['temperature_2m_min'][0],
        'location_age_seconds': int(age_seconds),
    }
