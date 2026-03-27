import sqlite3
import json
import os
import threading

DB_PATH = os.path.join(os.path.dirname(__file__), 'holopoker.db')
_lock = threading.Lock()
_conn = None

SHOP_ITEMS = {
    # Card backs
    'back_neon_red':    {'type': 'card_back', 'name': 'NEON RED',    'price': 200, 'css': '#ff3344'},
    'back_neon_purple': {'type': 'card_back', 'name': 'NEON PURPLE', 'price': 200, 'css': '#cc55ff'},
    'back_neon_gold':   {'type': 'card_back', 'name': 'NEON GOLD',   'price': 300, 'css': '#ffdd00'},
    'back_neon_white':  {'type': 'card_back', 'name': 'NEON WHITE',  'price': 400, 'css': '#ffffff'},
    'back_matrix':      {'type': 'card_back', 'name': 'MATRIX',      'price': 500, 'css': '#00ff00'},
    'back_fire':        {'type': 'card_back', 'name': 'FIRE',        'price': 500, 'css': '#ff6600'},
    # Name colors
    'name_red':         {'type': 'name_color', 'name': 'RED GLOW',    'price': 150, 'css': '#ff3344'},
    'name_purple':      {'type': 'name_color', 'name': 'PURPLE GLOW', 'price': 150, 'css': '#cc55ff'},
    'name_gold':        {'type': 'name_color', 'name': 'GOLD GLOW',   'price': 250, 'css': '#ffdd00'},
    'name_white':       {'type': 'name_color', 'name': 'WHITE GLOW',  'price': 300, 'css': '#ffffff'},
    'name_rainbow':     {'type': 'name_color', 'name': 'RAINBOW',     'price': 500, 'css': 'rainbow'},
}


def _get_conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def init_db():
    with _lock:
        conn = _get_conn()
        conn.execute('''CREATE TABLE IF NOT EXISTS player_stats (
            name TEXT PRIMARY KEY,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            hands_played INTEGER DEFAULT 0,
            biggest_pot INTEGER DEFAULT 0,
            total_winnings INTEGER DEFAULT 0,
            credits INTEGER DEFAULT 100,
            equipped_card_back TEXT DEFAULT 'default',
            equipped_name_color TEXT DEFAULT 'default',
            owned_items TEXT DEFAULT '[]'
        )''')
        conn.commit()


def _ensure_player(conn, name):
    conn.execute('INSERT OR IGNORE INTO player_stats (name) VALUES (?)', (name,))


def record_hand(participants, winners, pot):
    """Record stats for a completed hand."""
    with _lock:
        conn = _get_conn()
        for name in participants:
            _ensure_player(conn, name)
            won = name in winners
            share = pot // len(winners) if won and len(winners) > 0 else 0
            conn.execute('''UPDATE player_stats SET
                hands_played = hands_played + 1,
                wins = wins + ?,
                losses = losses + ?,
                biggest_pot = MAX(biggest_pot, ?),
                total_winnings = total_winnings + ?,
                credits = credits + ?
                WHERE name = ?''',
                (1 if won else 0,
                 0 if won else 1,
                 share if won else 0,
                 share,
                 50 if won else 10,
                 name))
        conn.commit()


def get_leaderboard(limit=10):
    with _lock:
        conn = _get_conn()
        rows = conn.execute('''SELECT name, wins, losses, hands_played,
            biggest_pot, total_winnings, credits
            FROM player_stats WHERE hands_played > 0
            ORDER BY wins DESC, total_winnings DESC
            LIMIT ?''', (limit,)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        hp = d['hands_played']
        d['win_rate'] = round(d['wins'] / hp * 100) if hp > 0 else 0
        result.append(d)
    return result


def get_stats(name):
    with _lock:
        conn = _get_conn()
        _ensure_player(conn, name)
        conn.commit()
        row = conn.execute('SELECT * FROM player_stats WHERE name = ?',
                           (name,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d['owned_items'] = json.loads(d['owned_items'])
    hp = d['hands_played']
    d['win_rate'] = round(d['wins'] / hp * 100) if hp > 0 else 0
    return d


def get_cosmetics(name):
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            'SELECT equipped_card_back, equipped_name_color FROM player_stats WHERE name = ?',
            (name,)).fetchone()
    if not row:
        return {'card_back': None, 'name_color': None}
    cb_id = row['equipped_card_back']
    nc_id = row['equipped_name_color']
    return {
        'card_back': SHOP_ITEMS[cb_id]['css'] if cb_id != 'default' and cb_id in SHOP_ITEMS else None,
        'name_color': SHOP_ITEMS[nc_id]['css'] if nc_id != 'default' and nc_id in SHOP_ITEMS else None,
    }


def get_credits(name):
    with _lock:
        conn = _get_conn()
        row = conn.execute('SELECT credits FROM player_stats WHERE name = ?',
                           (name,)).fetchone()
    return row['credits'] if row else 0


def buy_item(name, item_id):
    if item_id not in SHOP_ITEMS:
        return False, 'Item not found'
    item = SHOP_ITEMS[item_id]
    with _lock:
        conn = _get_conn()
        _ensure_player(conn, name)
        row = conn.execute('SELECT credits, owned_items FROM player_stats WHERE name = ?',
                           (name,)).fetchone()
        owned = json.loads(row['owned_items'])
        if item_id in owned:
            return False, 'Already owned'
        if row['credits'] < item['price']:
            return False, f'Need {item["price"]} credits (have {row["credits"]})'
        owned.append(item_id)
        conn.execute('UPDATE player_stats SET credits = credits - ?, owned_items = ? WHERE name = ?',
                     (item['price'], json.dumps(owned), name))
        conn.commit()
    return True, 'OK'


def equip_item(name, item_id):
    with _lock:
        conn = _get_conn()
        _ensure_player(conn, name)

        if item_id == 'default_card_back':
            conn.execute('UPDATE player_stats SET equipped_card_back = ? WHERE name = ?',
                         ('default', name))
            conn.commit()
            return True, 'OK'
        if item_id == 'default_name_color':
            conn.execute('UPDATE player_stats SET equipped_name_color = ? WHERE name = ?',
                         ('default', name))
            conn.commit()
            return True, 'OK'

        if item_id not in SHOP_ITEMS:
            return False, 'Item not found'

        row = conn.execute('SELECT owned_items FROM player_stats WHERE name = ?',
                           (name,)).fetchone()
        owned = json.loads(row['owned_items'])
        if item_id not in owned:
            return False, 'Item not owned'

        item = SHOP_ITEMS[item_id]
        if item['type'] == 'card_back':
            conn.execute('UPDATE player_stats SET equipped_card_back = ? WHERE name = ?',
                         (item_id, name))
        elif item['type'] == 'name_color':
            conn.execute('UPDATE player_stats SET equipped_name_color = ? WHERE name = ?',
                         (item_id, name))
        conn.commit()
    return True, 'OK'


def get_shop_data(name):
    with _lock:
        conn = _get_conn()
        _ensure_player(conn, name)
        conn.commit()
        row = conn.execute(
            'SELECT credits, owned_items, equipped_card_back, equipped_name_color '
            'FROM player_stats WHERE name = ?', (name,)).fetchone()
    owned = json.loads(row['owned_items'])
    items = []
    for item_id, item in SHOP_ITEMS.items():
        items.append({
            'id': item_id,
            'type': item['type'],
            'name': item['name'],
            'price': item['price'],
            'css': item['css'],
            'owned': item_id in owned,
            'equipped': item_id == row['equipped_card_back'] or item_id == row['equipped_name_color'],
        })
    return {
        'credits': row['credits'],
        'items': items,
        'equipped_card_back': row['equipped_card_back'],
        'equipped_name_color': row['equipped_name_color'],
    }
