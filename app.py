import os
import time
import random
import threading
from flask import Flask, send_from_directory, request
from flask_socketio import SocketIO, emit, join_room
from poker import PokerGame, bot_decide

BASE = os.path.dirname(__file__)
app = Flask(__name__, static_folder=os.path.join(BASE, 'static'))
socketio = SocketIO(app, cors_allowed_origins='*')

rooms = {}      # room_id -> PokerGame
sid_info = {}   # sid -> (room_id, player_name)
turn_timers = {}  # room_id -> turn generation counter
turn_generation = {}  # room_id -> int (increments each new turn)
TURN_TIMEOUT = 25  # seconds
NEXT_HAND_DELAY = 15  # seconds after showdown before auto-dealing
next_hand_gen = {}  # room_id -> int (generation counter for auto-next)


def start_turn_timer(room_id):
    """Start a background task that will auto-fold after TURN_TIMEOUT."""
    game = rooms.get(room_id)
    if not game or game.state != 'playing':
        return
    gen = turn_generation.get(room_id, 0) + 1
    turn_generation[room_id] = gen
    game.turn_deadline = time.time() + TURN_TIMEOUT
    game.turn_gen = gen  # track which generation this deadline belongs to

    def _auto_fold():
        socketio.sleep(TURN_TIMEOUT + 0.5)  # small buffer
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


def broadcast(room_id):
    game = rooms.get(room_id)
    if not game:
        return
    for p in game.players:
        if game.is_bot(p):
            continue  # don't emit to fake bot SIDs
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
        socketio.sleep(1.5 + random.random() * 2)  # 1.5-3.5s delay
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
    room_id = data.get('room', '').upper().strip()[:6]
    name = data.get('name', '').strip()[:16]
    if not room_id or not name:
        emit('err', {'msg': 'Enter a valid name and room code'})
        return

    if room_id not in rooms:
        rooms[room_id] = PokerGame(room_id)
    game = rooms[room_id]

    if game.reconnect(name, request.sid):
        sid_info[request.sid] = (room_id, name)
        join_room(room_id)
        emit('joined', {'room': room_id, 'name': name, 'host': game.players[0]['name'] == name})
        broadcast(room_id)
        return

    if game.state != 'waiting':
        # Try to re-add a player who was removed (busted + disconnected)
        if game.rejoin_player(request.sid, name):
            sid_info[request.sid] = (room_id, name)
            join_room(room_id)
            emit('joined', {'room': room_id, 'name': name, 'host': False})
            broadcast(room_id)
            return
        emit('err', {'msg': 'Game already in progress — reconnect with same name'})
        return

    ok, msg = game.add_player(request.sid, name)
    if not ok:
        emit('err', {'msg': msg})
        return

    sid_info[request.sid] = (room_id, name)
    join_room(room_id)
    emit('joined', {'room': room_id, 'name': name, 'host': len(game.players) == 1})
    broadcast(room_id)


@socketio.on('add_bot')
def on_add_bot():
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
    info = sid_info.get(request.sid)
    if not info:
        return
    room_id, _ = info
    game = rooms.get(room_id)
    if not game:
        return
    ok, msg = game.action(request.sid, data.get('type'), data.get('amount', 0))
    if not ok:
        emit('err', {'msg': msg})
        return
    broadcast_and_timer(room_id)


@socketio.on('next_hand')
def on_next():
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
    info = sid_info.get(request.sid)
    if not info:
        return
    room_id, _ = info
    game = rooms.get(room_id)
    if not game:
        return
    amount = 5000
    if data and isinstance(data, dict):
        amt = data.get('amount', 5000)
        if isinstance(amt, (int, float)) and 1000 <= amt <= 5000:
            amount = int(amt)
    ok, msg = game.rebuy(request.sid, amount)
    if not ok:
        emit('err', {'msg': msg})
        return
    broadcast(room_id)


@socketio.on('disconnect')
def on_disconnect():
    info = sid_info.pop(request.sid, None)
    if not info:
        return
    room_id, name = info
    game = rooms.get(room_id)
    if not game:
        return

    # Check if disconnected player is the host (first player)
    is_host = len(game.players) > 0 and game.players[0]['sid'] == request.sid

    if is_host:
        # Host left — kick everyone and destroy room
        cancel_turn_timer(room_id)
        for p in game.players:
            if p['sid'] != request.sid:
                socketio.emit('host_disconnected', {}, to=p['sid'])
                sid_info.pop(p['sid'], None)
        rooms.pop(room_id, None)
        return

    if game.state == 'waiting':
        game.remove_player(request.sid)
        broadcast(room_id)
    elif game.state == 'playing':
        # auto-fold disconnected player
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
