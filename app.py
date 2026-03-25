import os
from flask import Flask, send_from_directory, request
from flask_socketio import SocketIO, emit, join_room
from poker import PokerGame

BASE = os.path.dirname(__file__)
app = Flask(__name__, static_folder=os.path.join(BASE, 'static'))
socketio = SocketIO(app, cors_allowed_origins='*')

rooms = {}      # room_id -> PokerGame
sid_info = {}   # sid -> (room_id, player_name)


def broadcast(room_id):
    game = rooms.get(room_id)
    if not game:
        return
    for p in game.players:
        socketio.emit('state', game.to_dict(viewer_sid=p['sid']), to=p['sid'])


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
    broadcast(room_id)


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
    broadcast(room_id)


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
    broadcast(room_id)


@socketio.on('rebuy')
def on_rebuy():
    info = sid_info.get(request.sid)
    if not info:
        return
    room_id, _ = info
    game = rooms.get(room_id)
    if not game:
        return
    ok, msg = game.rebuy(request.sid)
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
    if game and game.state == 'waiting':
        game.remove_player(request.sid)
        broadcast(room_id)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5002))
    print(f'◈ HOLOPOKER — http://localhost:{port}')
    socketio.run(app, port=port, use_reloader=False, allow_unsafe_werkzeug=True)
