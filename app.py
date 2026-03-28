import os
import re
import time
import random
import secrets
import threading
from collections import defaultdict
from flask import Flask, send_from_directory, request
from flask_socketio import SocketIO, emit, join_room
from poker import PokerGame, bot_decide
import stats

BASE = os.path.dirname(__file__)
app = Flask(__name__, static_folder=os.path.join(BASE, 'static'))
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))

ALLOWED_ORIGINS = os.environ.get(
    'CORS_ORIGINS',
    'https://holopoker.onrender.com,http://localhost:5002,http://127.0.0.1:5002'
).split(',')
socketio = SocketIO(app, cors_allowed_origins=ALLOWED_ORIGINS)
stats.init_db()

# ── Server state ─────────────────────────────
rooms = {}            # room_id -> PokerGame
sid_info = {}         # sid -> (room_id, player_name)
player_tokens = {}    # (room_id, name) -> token (for secure reconnect)
turn_timers = {}      # room_id -> turn generation counter
turn_generation = {}  # room_id -> int (increments each new turn)
next_hand_gen = {}    # room_id -> int (generation counter for auto-next)

TURN_TIMEOUT = 25     # seconds
NEXT_HAND_DELAY = 15  # seconds after showdown before auto-dealing
MAX_ROOMS = 50        # prevent memory exhaustion

# ── Input validation ─────────────────────────
VALID_NAME = re.compile(r'^[A-Za-z0-9 _\-]{1,16}$')
VALID_ROOM = re.compile(r'^[A-Z0-9]{1,6}$')

# ── Rate limiting ────────────────────────────
_rate = defaultdict(list)  # sid -> [timestamps]
_RATE_LIMIT = 10           # max events per second
_RATE_WINDOW = 1.0         # seconds


def _check_rate(sid):
    """Return True if request is within rate limit, False to reject."""
    now = time.time()
    ts = _rate[sid]
    ts[:] = [t for t in ts if now - t < _RATE_WINDOW]
    if len(ts) >= _RATE_LIMIT:
        return False
    ts.append(now)
    return True


def _cleanup_rate(sid):
    _rate.pop(sid, None)


# ── Security headers ─────────────────────────
@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "connect-src 'self' wss: ws:; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'"
    )
    return response


# ── Turn timers ──────────────────────────────
def start_turn_timer(room_id):
    """Start a background task that will auto-fold after TURN_TIMEOUT."""
    game = rooms.get(room_id)
    if not game or game.state != 'playing':
        return
    gen = turn_generation.get(room_id, 0) + 1
    turn_generation[room_id] = gen
    game.turn_deadline = time.time() + TURN_TIMEOUT
    game.turn_gen = gen

    def _auto_fold():
        socketio.sleep(TURN_TIMEOUT + 0.5)
        if turn_generation.get(room_id) != gen:
            return
        g = rooms.get(room_id)
        if not g or g.state != 'playing':
            return
        cp = g.players[g.current_player_idx]
        g.action(cp['sid'], 'fold')
        g.last_action = f'{cp["name"]} AUTO-FOLDED'
        broadcast_and_timer(room_id)

    socketio.start_background_task(_auto_fold)


def cancel_turn_timer(room_id):
    turn_generation[room_id] = turn_generation.get(room_id, 0) + 1


def start_next_hand_timer(room_id):
    """Auto-deal next hand after NEXT_HAND_DELAY seconds."""
    game = rooms.get(room_id)
    if not game:
        return
    gen = next_hand_gen.get(room_id, 0) + 1
    next_hand_gen[room_id] = gen
    game.next_hand_deadline = time.time() + NEXT_HAND_DELAY

    def _auto_next():
        socketio.sleep(NEXT_HAND_DELAY)
        if next_hand_gen.get(room_id) != gen:
            return
        g = rooms.get(room_id)
        if not g or g.state != 'showdown':
            return
        g.next_hand()
        broadcast_and_timer(room_id)

    socketio.start_background_task(_auto_next)


# ── Broadcast helpers ────────────────────────
def broadcast(room_id):
    game = rooms.get(room_id)
    if not game:
        return
    for p in game.players:
        if game.is_bot(p):
            continue
        state = game.to_dict(viewer_sid=p['sid'])
        if game.state == 'playing' and hasattr(game, 'turn_deadline'):
            state['turn_deadline'] = game.turn_deadline
            state['turn_gen'] = getattr(game, 'turn_gen', 0)
        if game.state == 'showdown' and hasattr(game, 'next_hand_deadline'):
            state['next_hand_deadline'] = game.next_hand_deadline
        socketio.emit('state', state, to=p['sid'])


def broadcast_and_timer(room_id):
    game = rooms.get(room_id)
    if not game:
        return
    if game.state == 'playing':
        start_turn_timer(room_id)
        schedule_bot_play(room_id)
    elif game.state == 'showdown':
        start_next_hand_timer(room_id)
        if game.hand_result:
            stats.record_hand(game.hand_result['participants'],
                              game.hand_result['winners'],
                              game.hand_result['pot'])
            for p in game.players:
                if not game.is_bot(p):
                    p['holo_credits'] = stats.get_credits(p['name'])
            game.hand_result = None
    broadcast(room_id)


def schedule_bot_play(room_id):
    """If current player is a bot, auto-play after a short delay."""
    game = rooms.get(room_id)
    if not game:
        return
    bot = game.current_player_is_bot()
    if not bot:
        return
    gen = turn_generation.get(room_id, 0)

    def _bot_act():
        socketio.sleep(1.5 + random.random() * 2)
        if turn_generation.get(room_id) != gen:
            return
        g = rooms.get(room_id)
        if not g or g.state != 'playing':
            return
        b = g.current_player_is_bot()
        if not b:
            return
        action, amount = bot_decide(b, g)
        g.action(b['sid'], action, amount)
        broadcast_and_timer(room_id)

    socketio.start_background_task(_bot_act)


def _load_player_cosmetics(game, sid, name):
    """Load cosmetics and credits from DB onto in-memory player dict."""
    p = game.get_player(sid)
    if p and not game.is_bot(p):
        p['cosmetics'] = stats.get_cosmetics(name)
        p['holo_credits'] = stats.get_credits(name)


def _destroy_room(room_id):
    """Clean up a room and its associated tokens."""
    game = rooms.pop(room_id, None)
    if game:
        for p in game.players:
            player_tokens.pop((room_id, p['name']), None)
    cancel_turn_timer(room_id)
    next_hand_gen.pop(room_id, None)
    turn_generation.pop(room_id, None)


# ── HTTP routes ──────────────────────────────
@app.route('/health')
def health():
    return 'ok', 200


@app.route('/')
def index():
    return send_from_directory(os.path.join(BASE, 'static'), 'index.html')


@app.route('/ads.txt')
def ads_txt():
    return send_from_directory(os.path.join(BASE, 'static'), 'ads.txt')


@app.route('/socket.io.min.js')
def sio_js():
    return send_from_directory(os.path.join(BASE, 'static'), 'socket.io.min.js')


# ── Socket events ────────────────────────────
@socketio.on('get_rooms')
def on_get_rooms():
    room_list = []
    for rid, game in rooms.items():
        room_list.append({
            'id': rid,
            'players': len(game.players),
            'max': 7,
            'state': game.state,
        })
    emit('rooms', room_list)


@socketio.on('sync_time')
def on_sync_time():
    emit('server_time', {'t': time.time()})


@socketio.on('join')
def on_join(data):
    if not _check_rate(request.sid):
        emit('err', {'msg': 'Too many requests — slow down'})
        return

    room_id = str(data.get('room', '')).upper().strip()[:6]
    name = str(data.get('name', '')).strip()[:16]
    token = str(data.get('token', '')) if data.get('token') else None

    if not room_id or not name:
        emit('err', {'msg': 'Enter a valid name and room code'})
        return

    # Strict input validation
    if not VALID_ROOM.match(room_id):
        emit('err', {'msg': 'Room code: letters and numbers only'})
        return
    if not VALID_NAME.match(name):
        emit('err', {'msg': 'Name: letters, numbers, spaces, hyphens only'})
        return

    # Room creation limit
    if room_id not in rooms:
        if len(rooms) >= MAX_ROOMS:
            emit('err', {'msg': 'Server full — try again later'})
            return
        rooms[room_id] = PokerGame(room_id)
    game = rooms[room_id]

    # ── Reconnect path ──
    if game.reconnect(name, request.sid):
        # Verify reconnect token
        expected = player_tokens.get((room_id, name))
        if expected and token != expected:
            # Undo the reconnect
            game.reconnect(name, '__invalid__')
            emit('err', {'msg': 'Invalid reconnect token'})
            return
        sid_info[request.sid] = (room_id, name)
        join_room(room_id)
        _load_player_cosmetics(game, request.sid, name)
        emit('joined', {
            'room': room_id, 'name': name,
            'host': game.players[0]['name'] == name,
            'token': expected or '',
        })
        broadcast(room_id)
        return

    # ── Rejoin path (busted + disconnected) ──
    if game.state != 'waiting':
        if game.rejoin_player(request.sid, name):
            new_token = secrets.token_hex(16)
            player_tokens[(room_id, name)] = new_token
            sid_info[request.sid] = (room_id, name)
            join_room(room_id)
            _load_player_cosmetics(game, request.sid, name)
            emit('joined', {
                'room': room_id, 'name': name, 'host': False,
                'token': new_token,
            })
            broadcast(room_id)
            return
        emit('err', {'msg': 'Game already in progress — reconnect with same name'})
        return

    # ── New player path ──
    ok, msg = game.add_player(request.sid, name)
    if not ok:
        emit('err', {'msg': msg})
        return

    new_token = secrets.token_hex(16)
    player_tokens[(room_id, name)] = new_token
    _load_player_cosmetics(game, request.sid, name)
    sid_info[request.sid] = (room_id, name)
    join_room(room_id)
    emit('joined', {
        'room': room_id, 'name': name,
        'host': len(game.players) == 1,
        'token': new_token,
    })
    broadcast(room_id)


@socketio.on('add_bot')
def on_add_bot():
    if not _check_rate(request.sid):
        return
    info = sid_info.get(request.sid)
    if not info:
        return
    room_id, _ = info
    game = rooms.get(room_id)
    if not game:
        return
    if game.state != 'waiting':
        emit('err', {'msg': 'Can only add bots in waiting room'})
        return
    if len(game.players) == 0 or game.players[0]['sid'] != request.sid:
        emit('err', {'msg': 'Only host can add bots'})
        return
    ok, msg = game.add_bot()
    if not ok:
        emit('err', {'msg': msg})
        return
    broadcast(room_id)


@socketio.on('start')
def on_start():
    if not _check_rate(request.sid):
        return
    info = sid_info.get(request.sid)
    if not info:
        return
    room_id, _ = info
    game = rooms.get(room_id)
    if not game:
        return
    ok, msg = game.start_game(request.sid)
    if not ok:
        emit('err', {'msg': msg})
        return
    broadcast_and_timer(room_id)


@socketio.on('action')
def on_action(data):
    if not _check_rate(request.sid):
        return
    info = sid_info.get(request.sid)
    if not info:
        return
    room_id, _ = info
    game = rooms.get(room_id)
    if not game:
        return
    atype = str(data.get('type', ''))[:10]
    amount = data.get('amount', 0)
    if not isinstance(amount, (int, float)):
        amount = 0
    amount = max(0, min(int(amount), 1_000_000))  # sane bounds
    ok, msg = game.action(request.sid, atype, amount)
    if not ok:
        emit('err', {'msg': msg})
        return
    broadcast_and_timer(room_id)


@socketio.on('next_hand')
def on_next():
    if not _check_rate(request.sid):
        return
    info = sid_info.get(request.sid)
    if not info:
        return
    room_id, _ = info
    game = rooms.get(room_id)
    if not game:
        return
    game.next_hand()
    broadcast_and_timer(room_id)


@socketio.on('rebuy')
def on_rebuy(data=None):
    if not _check_rate(request.sid):
        return
    info = sid_info.get(request.sid)
    if not info:
        return
    room_id, name = info
    game = rooms.get(room_id)
    if not game:
        return
    amount = 5000
    if data and isinstance(data, dict):
        amt = data.get('amount', 5000)
        if isinstance(amt, (int, float)) and 1000 <= amt <= 5000:
            amount = int(amt)
    # If player was removed (busted + next_hand already fired), re-add them
    if not game.get_player(request.sid):
        if not game.rejoin_player(request.sid, name):
            emit('err', {'msg': 'Cannot rejoin room'})
            return

    ok, msg = game.rebuy(request.sid, amount)
    if not ok:
        emit('err', {'msg': msg})
        return
    broadcast(room_id)


# ── Leaderboard & Stats ─────────────────────
@socketio.on('get_leaderboard')
def on_get_leaderboard():
    emit('leaderboard', stats.get_leaderboard())


@socketio.on('get_my_stats')
def on_get_my_stats():
    info = sid_info.get(request.sid)
    if not info:
        return
    _, name = info
    emit('my_stats', stats.get_stats(name))


# ── Cosmetic Shop ────────────────────────────
@socketio.on('get_shop')
def on_get_shop():
    info = sid_info.get(request.sid)
    if not info:
        return
    _, name = info
    emit('shop_data', stats.get_shop_data(name))


@socketio.on('buy_item')
def on_buy_item(data):
    if not _check_rate(request.sid):
        return
    info = sid_info.get(request.sid)
    if not info:
        return
    room_id, name = info
    item_id = str(data.get('item_id', ''))[:32]
    ok, msg = stats.buy_item(name, item_id)
    if not ok:
        emit('err', {'msg': msg})
        return
    game = rooms.get(room_id)
    if game:
        p = game.get_player(request.sid)
        if p:
            p['holo_credits'] = stats.get_credits(name)
    emit('shop_data', stats.get_shop_data(name))


@socketio.on('equip_item')
def on_equip_item(data):
    if not _check_rate(request.sid):
        return
    info = sid_info.get(request.sid)
    if not info:
        return
    room_id, name = info
    item_id = str(data.get('item_id', ''))[:32]
    ok, msg = stats.equip_item(name, item_id)
    if not ok:
        emit('err', {'msg': msg})
        return
    game = rooms.get(room_id)
    if game:
        p = game.get_player(request.sid)
        if p:
            p['cosmetics'] = stats.get_cosmetics(name)
        broadcast(room_id)
    emit('shop_data', stats.get_shop_data(name))


# ── Disconnect ───────────────────────────────
@socketio.on('disconnect')
def on_disconnect():
    _cleanup_rate(request.sid)
    info = sid_info.pop(request.sid, None)
    if not info:
        return
    room_id, name = info
    game = rooms.get(room_id)
    if not game:
        return

    is_host = len(game.players) > 0 and game.players[0]['sid'] == request.sid

    if is_host:
        # Host left — kick everyone and destroy room
        for p in game.players:
            if p['sid'] != request.sid:
                socketio.emit('host_disconnected', {}, to=p['sid'])
                sid_info.pop(p['sid'], None)
        _destroy_room(room_id)
        return

    if game.state == 'waiting':
        game.remove_player(request.sid)
        player_tokens.pop((room_id, name), None)
        broadcast(room_id)
    elif game.state == 'playing':
        p = game.get_player(request.sid)
        if p and not p['folded']:
            if game.can_act(request.sid):
                game.action(request.sid, 'fold')
                game.last_action = f'{name} FOLDS (DISCONNECTED)'
            else:
                p['folded'] = True
                game.action_required.discard(
                    next(i for i, pl in enumerate(game.players) if pl['sid'] == request.sid)
                )
                game._advance()
                game.last_action = f'{name} FOLDS (DISCONNECTED)'
            broadcast_and_timer(room_id)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5002))
    print(f'◈ HOLOPOKER — http://localhost:{port}')
    socketio.run(app, port=port, use_reloader=False, allow_unsafe_werkzeug=True)
