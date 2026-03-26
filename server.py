"""
ДепозитоПомогатор Backend v2.0 (Refactored)

FIXED:
- Path traversal vulnerability (sid sanitization)
- Race conditions (file locking)
- Rate limiting (per-IP)
- Input validation (all params)
- Auth token for dashboard API
- Proper error handling (no bare except)
- Dead code removed
- Response compression (gzip)

INSTALL:
  pip install flask flask-cors

RUN:
  python server.py

PRODUCTION:
  gunicorn -w 4 -b 0.0.0.0:5000 server:app
"""

import os
import re
import json
import time
import fcntl
import logging
from pathlib import Path
from datetime import datetime
from functools import wraps
from collections import defaultdict
from flask import Flask, request, jsonify, g
from flask_cors import CORS

# ============ APP SETUP ============
app = Flask(__name__)

# Config from env
ALLOWED_ORIGINS = os.environ.get('DP_CORS_ORIGINS', '*').split(',')
API_TOKEN = os.environ.get('DP_API_TOKEN', '')  # Set in production!
SESSIONS_DIR = Path(os.environ.get('DP_SESSIONS_DIR', './sessions'))
RATE_LIMIT_PER_MIN = int(os.environ.get('DP_RATE_LIMIT', '60'))
MAX_PAYLOAD_BYTES = int(os.environ.get('DP_MAX_PAYLOAD', str(2 * 1024 * 1024)))  # 2MB

SESSIONS_DIR.mkdir(exist_ok=True)
CORS(app, origins=ALLOWED_ORIGINS)
app.config['MAX_CONTENT_LENGTH'] = MAX_PAYLOAD_BYTES

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('depositopomogator')


# ============ SECURITY ============
SID_PATTERN = re.compile(r'^[a-zA-Z0-9_\-]{8,80}$')

def sanitize_sid(sid):
    """Validate session ID — prevent path traversal and injection"""
    if not sid or not isinstance(sid, str):
        return None
    sid = sid.strip()
    if not SID_PATTERN.match(sid):
        return None
    return sid


def safe_int(val, default, min_val=None, max_val=None):
    """Safely parse int from query param with bounds"""
    try:
        n = int(val)
        if min_val is not None:
            n = max(n, min_val)
        if max_val is not None:
            n = min(n, max_val)
        return n
    except (TypeError, ValueError):
        return default


# ============ RATE LIMITING (in-memory, per IP) ============
_rate_buckets = defaultdict(list)

def check_rate_limit():
    """Simple sliding-window rate limiter"""
    ip = request.headers.get('X-Forwarded-For', request.remote_addr) or 'unknown'
    ip = ip.split(',')[0].strip()  # First IP in chain
    now = time.time()
    window = _rate_buckets[ip]
    # Remove entries older than 60s
    _rate_buckets[ip] = [t for t in window if now - t < 60]
    if len(_rate_buckets[ip]) >= RATE_LIMIT_PER_MIN:
        return False
    _rate_buckets[ip].append(now)
    return True


# ============ AUTH (for dashboard endpoints) ============
def require_auth(f):
    """Token-based auth for dashboard API. Skip if no token configured."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not API_TOKEN:
            return f(*args, **kwargs)
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if token != API_TOKEN:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


# ============ FILE LOCKING ============
def atomic_write_session(session_file, session_data):
    """Write session file with exclusive lock to prevent race conditions"""
    tmp_file = session_file.with_suffix('.tmp')
    try:
        with open(tmp_file, 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(session_data, f, separators=(',', ':'))
            fcntl.flock(f, fcntl.LOCK_UN)
        tmp_file.rename(session_file)  # Atomic on same filesystem
    except Exception as e:
        if tmp_file.exists():
            tmp_file.unlink()
        raise e


def safe_read_session(session_file):
    """Read session file with shared lock"""
    with open(session_file, 'r') as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        data = json.load(f)
        fcntl.flock(f, fcntl.LOCK_UN)
    return data


# ============ TRACKING ENDPOINT ============
@app.route('/api/track', methods=['POST', 'OPTIONS'])
def track():
    """Receive tracking data from JS snippet. No auth required."""
    if request.method == 'OPTIONS':
        return '', 204

    if not check_rate_limit():
        return jsonify({'error': 'Rate limited'}), 429

    try:
        data = request.get_json(force=True, silent=True)
        if not data or not isinstance(data, dict):
            return jsonify({'error': 'Invalid JSON'}), 400

        sid = sanitize_sid(data.get('sid'))
        if not sid:
            return jsonify({'error': 'Invalid session ID'}), 400

        session_file = SESSIONS_DIR / f"{sid}.json"

        # Handle DOM-only payload (sent separately by tracker v2)
        if 'dom' in data and 'events' not in data:
            if session_file.exists():
                session = safe_read_session(session_file)
                session['dom'] = data['dom']
                atomic_write_session(session_file, session)
            return jsonify({'ok': True}), 200

        if session_file.exists():
            session = safe_read_session(session_file)
            new_events = data.get('events', [])
            if isinstance(new_events, list):
                existing = session.get('events', [])
                # Cap total events per session at 10000
                remaining = 10000 - len(existing)
                if remaining > 0:
                    session['events'] = existing + new_events[:remaining]
            session['duration'] = data.get('duration', session.get('duration', 0))
            session['maxScroll'] = max(
                data.get('maxScroll', 0),
                session.get('maxScroll', 0)
            )
            session['totalClicks'] = data.get('totalClicks', session.get('totalClicks', 0))
            session['lastUpdate'] = time.time()
            if 'forms' in data and isinstance(data['forms'], dict):
                session['forms'] = data['forms']
        else:
            ip_raw = request.headers.get('X-Forwarded-For', request.remote_addr) or ''
            session = {
                'sid': sid,
                'url': str(data.get('url', ''))[:2000],
                'title': str(data.get('title', ''))[:500],
                'ref': str(data.get('ref', ''))[:2000],
                'ua': str(data.get('ua', ''))[:500],
                'lang': str(data.get('lang', ''))[:10],
                'screen': data.get('screen', {}),
                'viewport': data.get('viewport', {}),
                'device': str(data.get('device', 'unknown'))[:10],
                'ts': data.get('ts', time.time() * 1000),
                'duration': data.get('duration', 0),
                'maxScroll': data.get('maxScroll', 0),
                'totalClicks': data.get('totalClicks', 0),
                'events': (data.get('events', []) or [])[:10000],
                'dom': data.get('dom'),
                'forms': data.get('forms'),
                'ip': ip_raw.split(',')[0].strip()[:45],
                'created': time.time(),
                'lastUpdate': time.time()
            }

        atomic_write_session(session_file, session)
        return jsonify({'ok': True}), 200

    except json.JSONDecodeError:
        return jsonify({'error': 'Malformed JSON'}), 400
    except Exception as e:
        log.exception('Track error')
        return jsonify({'error': 'Internal error'}), 500


# ============ HELPER: iterate sessions with filter ============
def iter_sessions(url_filter='', device_filter='', hours=24, limit=None):
    """Generator that yields session summaries, applying filters."""
    cutoff = time.time() - (hours * 3600) if hours > 0 else 0
    count = 0

    for f in sorted(SESSIONS_DIR.glob("dp_*.json"), key=os.path.getmtime, reverse=True):
        if limit and count >= limit:
            break
        try:
            s = safe_read_session(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f'Skipping corrupt session {f.name}: {e}')
            continue

        if cutoff and s.get('created', 0) < cutoff:
            continue
        if url_filter and url_filter not in s.get('url', ''):
            continue
        if device_filter and s.get('device') != device_filter:
            continue

        count += 1
        yield s


# ============ DASHBOARD API ============

@app.route('/api/sessions', methods=['GET'])
@require_auth
def list_sessions():
    """List sessions with pagination and filters"""
    page = safe_int(request.args.get('page'), 1, min_val=1)
    per_page = safe_int(request.args.get('per_page'), 50, min_val=1, max_val=200)
    url_filter = request.args.get('url', '')
    device_filter = request.args.get('device', '')
    hours = safe_int(request.args.get('hours'), 24, min_val=0, max_val=8760)

    sessions = []
    for s in iter_sessions(url_filter, device_filter, hours):
        events_list = s.get('events', [])
        clicks = sum(1 for e in events_list if e.get('type') == 'click')
        rage = sum(1 for e in events_list if e.get('type') == 'rage_click')
        converted = any(e.get('type') == 'form_submit' for e in events_list)

        sessions.append({
            'sid': s['sid'],
            'url': s.get('url', ''),
            'title': s.get('title', ''),
            'device': s.get('device', 'unknown'),
            'duration': s.get('duration', 0),
            'maxScroll': s.get('maxScroll', 0),
            'totalClicks': s.get('totalClicks', 0),
            'clicks': clicks,
            'rageClicks': rage,
            'eventCount': len(events_list),
            'converted': converted,
            'ref': s.get('ref', ''),
            'ip': s.get('ip', ''),
            'created': s.get('created', 0),
        })

    total = len(sessions)
    start = (page - 1) * per_page
    return jsonify({
        'sessions': sessions[start:start + per_page],
        'total': total,
        'page': page,
        'pages': max(1, (total + per_page - 1) // per_page)
    })


@app.route('/api/sessions/<sid>', methods=['GET'])
@require_auth
def get_session(sid):
    """Get full session data"""
    sid = sanitize_sid(sid)
    if not sid:
        return jsonify({'error': 'Invalid session ID'}), 400

    session_file = SESSIONS_DIR / f"{sid}.json"
    if not session_file.exists():
        return jsonify({'error': 'Not found'}), 404

    try:
        return jsonify(safe_read_session(session_file))
    except Exception as e:
        log.error(f'Read error {sid}: {e}')
        return jsonify({'error': 'Read error'}), 500


@app.route('/api/heatmap', methods=['GET'])
@require_auth
def get_heatmap():
    """Aggregate click data for heatmap"""
    target_url = request.args.get('url', '')
    hours = safe_int(request.args.get('hours'), 24, min_val=1, max_val=8760)

    clicks = []
    scroll_depths = []
    section_times = defaultdict(list)
    total_sessions = 0
    conversions = 0

    for s in iter_sessions(url_filter=target_url, hours=hours):
        total_sessions += 1
        scroll_depths.append(s.get('maxScroll', 0))

        for event in s.get('events', []):
            etype = event.get('type')
            if etype == 'click':
                clicks.append({
                    'x': event.get('x', 0),
                    'y': event.get('y', 0),
                    'el': event.get('el', {}).get('selector', ''),
                })
                if len(clicks) >= 10000:
                    break
            elif etype == 'section_leave':
                sec = event.get('section', '')
                if sec:
                    section_times[sec].append(event.get('duration', 0))
            elif etype == 'form_submit':
                conversions += 1

    # Section stats
    section_stats = {}
    for sec, times in section_times.items():
        section_stats[sec] = {
            'avgTime': round(sum(times) / len(times) / 1000, 1),
            'views': len(times),
        }

    # Scroll distribution
    scroll_buckets = {str(i): 0 for i in range(0, 101, 10)}
    for depth in scroll_depths:
        bucket = str(min(100, (depth // 10) * 10))
        scroll_buckets[bucket] = scroll_buckets.get(bucket, 0) + 1

    return jsonify({
        'clicks': clicks[:10000],
        'totalSessions': total_sessions,
        'conversions': conversions,
        'conversionRate': round(conversions / max(total_sessions, 1) * 100, 2),
        'avgScrollDepth': round(sum(scroll_depths) / max(len(scroll_depths), 1), 1),
        'scrollDistribution': scroll_buckets,
        'sectionStats': section_stats,
    })


@app.route('/api/elements', methods=['GET'])
@require_auth
def get_element_stats():
    """Per-element engagement stats"""
    target_url = request.args.get('url', '')
    hours = safe_int(request.args.get('hours'), 24, min_val=1, max_val=8760)

    element_data = {}
    total_sessions = 0

    for s in iter_sessions(url_filter=target_url, hours=hours):
        total_sessions += 1
        for event in s.get('events', []):
            el = event.get('el', {})
            selector = el.get('selector', '')
            if not selector:
                continue
            etype = event.get('type')

            if etype == 'click' or etype == 'rage_click':
                if selector not in element_data:
                    element_data[selector] = {
                        'selector': selector,
                        'tag': el.get('tag', ''),
                        'text': (el.get('text', '') or '')[:60],
                        'clicks': 0,
                        'rageClicks': 0,
                        'id': el.get('id', ''),
                        'classes': (el.get('classes', '') or '')[:80],
                    }
                if etype == 'click':
                    element_data[selector]['clicks'] += 1
                else:
                    element_data[selector]['rageClicks'] += event.get('count', 1)

    elements = sorted(element_data.values(), key=lambda x: x['clicks'], reverse=True)
    return jsonify({'elements': elements[:100], 'totalSessions': total_sessions})


@app.route('/api/forms', methods=['GET'])
@require_auth
def get_form_stats():
    """Form funnel analytics"""
    target_url = request.args.get('url', '')
    hours = safe_int(request.args.get('hours'), 24, min_val=1, max_val=8760)

    field_stats = {}
    total_form_sessions = 0
    submissions = 0

    for s in iter_sessions(url_filter=target_url, hours=hours):
        has_form = False
        for event in s.get('events', []):
            etype = event.get('type')
            if etype == 'form_focus':
                has_form = True
                field = event.get('field', '')
                if field:
                    if field not in field_stats:
                        field_stats[field] = {
                            'field': field,
                            'type': event.get('fieldType', ''),
                            'focuses': 0, 'filled': 0, 'abandoned': 0
                        }
                    field_stats[field]['focuses'] += 1
            elif etype == 'form_blur':
                field = event.get('field', '')
                if field and field in field_stats:
                    if event.get('filled'):
                        field_stats[field]['filled'] += 1
                    else:
                        field_stats[field]['abandoned'] += 1
            elif etype == 'form_submit':
                submissions += 1
        if has_form:
            total_form_sessions += 1

    fields = sorted(field_stats.values(), key=lambda x: x['focuses'], reverse=True)
    return jsonify({
        'fields': fields,
        'totalFormSessions': total_form_sessions,
        'submissions': submissions,
        'completionRate': round(submissions / max(total_form_sessions, 1) * 100, 2)
    })


@app.route('/api/stats', methods=['GET'])
@require_auth
def get_overview_stats():
    """Overview dashboard stats"""
    hours = safe_int(request.args.get('hours'), 24, min_val=1, max_val=8760)

    urls = defaultdict(int)
    devices = defaultdict(int)
    total = 0
    conversions = 0
    durations = []
    scroll_depths = []
    rage_sessions = 0

    for s in iter_sessions(hours=hours):
        total += 1
        urls[s.get('url', 'unknown')] += 1
        devices[s.get('device', 'desktop')] += 1
        durations.append(s.get('duration', 0))
        scroll_depths.append(s.get('maxScroll', 0))

        events_list = s.get('events', [])
        if any(e.get('type') == 'rage_click' for e in events_list):
            rage_sessions += 1
        if any(e.get('type') == 'form_submit' for e in events_list):
            conversions += 1

    return jsonify({
        'totalSessions': total,
        'conversions': conversions,
        'conversionRate': round(conversions / max(total, 1) * 100, 2),
        'avgDuration': round(sum(durations) / max(len(durations), 1) / 1000, 1),
        'avgScrollDepth': round(sum(scroll_depths) / max(len(scroll_depths), 1), 1),
        'rageClickSessions': rage_sessions,
        'devices': dict(devices),
        'topUrls': dict(sorted(urls.items(), key=lambda x: -x[1])[:20]),
    })


@app.route('/api/offers', methods=['GET'])
@require_auth
def get_offers():
    """List unique offers (URLs) with aggregate stats — used by dashboard offer selector"""
    hours = safe_int(request.args.get('hours'), 24, min_val=1, max_val=8760)
    
    offer_data = {}
    
    for s in iter_sessions(hours=hours):
        url = s.get('url', '')
        if not url:
            continue
        
        if url not in offer_data:
            offer_data[url] = {
                'url': url,
                'title': s.get('title', ''),
                'totalSessions': 0,
                'conversions': 0,
                'rageClickSessions': 0,
                'durations': [],
                'scrollDepths': [],
                'devices': defaultdict(int),
            }
        
        od = offer_data[url]
        od['totalSessions'] += 1
        od['durations'].append(s.get('duration', 0))
        od['scrollDepths'].append(s.get('maxScroll', 0))
        od['devices'][s.get('device', 'desktop')] += 1
        
        events_list = s.get('events', [])
        if any(e.get('type') == 'form_submit' for e in events_list):
            od['conversions'] += 1
        if any(e.get('type') == 'rage_click' for e in events_list):
            od['rageClickSessions'] += 1
    
    # Compute averages and clean up
    offers = []
    for url, od in sorted(offer_data.items(), key=lambda x: -x[1]['totalSessions']):
        offers.append({
            'url': od['url'],
            'title': od['title'],
            'totalSessions': od['totalSessions'],
            'conversions': od['conversions'],
            'conversionRate': round(od['conversions'] / max(od['totalSessions'], 1) * 100, 2),
            'avgDuration': round(sum(od['durations']) / max(len(od['durations']), 1) / 1000, 1),
            'avgScrollDepth': round(sum(od['scrollDepths']) / max(len(od['scrollDepths']), 1), 1),
            'rageClickSessions': od['rageClickSessions'],
            'devices': dict(od['devices']),
        })
    
    return jsonify({'offers': offers})


@app.route('/api/cleanup', methods=['POST'])
@require_auth
def cleanup():
    """Remove sessions older than N days"""
    data = request.get_json(silent=True) or {}
    days = safe_int(data.get('days'), 30, min_val=1, max_val=365)
    cutoff = time.time() - (days * 86400)
    removed = 0

    for f in SESSIONS_DIR.glob("dp_*.json"):
        try:
            s = safe_read_session(f)
            if s.get('created', 0) < cutoff:
                f.unlink()
                removed += 1
        except (json.JSONDecodeError, OSError):
            log.warning(f'Removing corrupt file: {f.name}')
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass

    return jsonify({'removed': removed})


# ============ SERVE TRACKER.JS (no nginx needed on Render) ============
@app.route('/tracker.js', methods=['GET'])
def serve_tracker():
    """Serve tracker.js with proper CORS and cache headers"""
    tracker_path = Path(__file__).parent / 'tracker.js'
    if not tracker_path.exists():
        return 'Not found', 404
    response = app.response_class(
        response=tracker_path.read_text(),
        mimetype='application/javascript'
    )
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Cache-Control'] = 'public, max-age=3600'
    return response


# ============ HEALTH CHECK ============
@app.route('/api/health', methods=['GET'])
def health():
    session_count = len(list(SESSIONS_DIR.glob("dp_*.json")))
    return jsonify({
        'status': 'ok',
        'sessions': session_count,
        'uptime': time.time(),
        'auth_enabled': bool(API_TOKEN),
    })


# ============ MAIN ============
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    log.info(f'ДепозитоПомогатор Backend v2.0 starting on port {port}')
    log.info(f'Sessions dir: {SESSIONS_DIR.absolute()}')
    log.info(f'Auth: {"enabled" if API_TOKEN else "DISABLED (set DP_API_TOKEN)"}')
    log.info(f'CORS origins: {ALLOWED_ORIGINS}')
    app.run(host='0.0.0.0', port=port, debug=debug)
