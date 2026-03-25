import random
from collections import Counter
from itertools import combinations

SUITS = ['♠', '♥', '♦', '♣']
RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
RANK_VAL = {r: i for i, r in enumerate(RANKS)}
HAND_NAMES = [
    'High Card', 'One Pair', 'Two Pair', 'Three of a Kind',
    'Straight', 'Flush', 'Full House', 'Four of a Kind', 'Straight Flush', 'Royal Flush'
]

class Card:
    def __init__(self, rank, suit):
        self.rank = rank
        self.suit = suit
    def to_dict(self):
        return {'rank': self.rank, 'suit': self.suit}

class Deck:
    def __init__(self):
        self.cards = [Card(r, s) for s in SUITS for r in RANKS]
        random.shuffle(self.cards)
    def deal(self, n=1):
        return [self.cards.pop() for _ in range(n)]

def _score_five(cards):
    vals = sorted([RANK_VAL[c.rank] for c in cards], reverse=True)
    suits = [c.suit for c in cards]
    flush = len(set(suits)) == 1
    straight = max(vals) - min(vals) == 4 and len(set(vals)) == 5
    if set(vals) == {12, 3, 2, 1, 0}:  # wheel A-2-3-4-5
        straight, vals = True, [3, 2, 1, 0, -1]
    counts = Counter(vals)
    freq = sorted(counts.values(), reverse=True)
    groups = sorted(counts.keys(), key=lambda v: (counts[v], v), reverse=True)
    if flush and straight:
        return (9 if vals[0] == 12 else 8, vals)
    if freq[0] == 4: return (7, groups)
    if freq[0] == 3 and freq[1] == 2: return (6, groups)
    if flush: return (5, vals)
    if straight: return (4, vals)
    if freq[0] == 3: return (3, groups)
    if freq[0] == 2 and freq[1] == 2: return (2, groups)
    if freq[0] == 2: return (1, groups)
    return (0, vals)

def evaluate_hand(cards):
    best = None
    for combo in combinations(cards, 5):
        s = _score_five(list(combo))
        if best is None or s > best:
            best = s
    return best

class PokerGame:
    def __init__(self, room_id):
        self.room_id = room_id
        self.players = []
        self.state = 'waiting'
        self.deck = None
        self.community = []
        self.pot = 0
        self.current_bet = 0
        self.dealer_idx = 0
        self.current_player_idx = 0
        self.stage = None
        self.SB, self.BB = 25, 50
        self.winner_info = None
        self.last_action = None
        self.action_required = set()
        self.hand_names = {}

    def add_player(self, sid, name):
        if len(self.players) >= 7:
            return False, 'Table full (max 7)'
        if any(p['name'] == name for p in self.players):
            return False, 'Name already taken'
        self.players.append({
            'sid': sid, 'name': name, 'chips': 5100,
            'hand': [], 'bet': 0, 'folded': False, 'all_in': False,
        })
        return True, 'OK'

    def remove_player(self, sid):
        self.players = [p for p in self.players if p['sid'] != sid]

    def reconnect(self, name, new_sid):
        for p in self.players:
            if p['name'] == name:
                p['sid'] = new_sid
                return True
        return False

    def get_player(self, sid):
        return next((p for p in self.players if p['sid'] == sid), None)

    def start_game(self, sid):
        if len(self.players) < 2:
            return False, 'Need at least 2 players'
        if self.players[0]['sid'] != sid:
            return False, 'Only host can start'
        self.state = 'playing'
        self.new_hand()
        return True, 'OK'

    def new_hand(self):
        self.deck = Deck()
        self.community = []
        self.pot = 0
        self.current_bet = self.BB
        self.winner_info = None
        self.last_action = None
        self.hand_names = {}
        for p in self.players:
            p['hand'] = []
            p['bet'] = 0
            p['folded'] = False
            p['all_in'] = False
        for p in self.players:
            p['hand'] = [c.to_dict() for c in self.deck.deal(2)]
        n = len(self.players)
        self.stage = 'preflop'
        if n == 2:
            sb_idx, bb_idx = self.dealer_idx, (self.dealer_idx + 1) % n
        else:
            sb_idx = (self.dealer_idx + 1) % n
            bb_idx = (self.dealer_idx + 2) % n
        self._post_blind(sb_idx, self.SB)
        self._post_blind(bb_idx, self.BB)
        self.current_player_idx = (bb_idx + 1) % n
        self.action_required = {i for i in range(n) if not self.players[i]['all_in']}

    def _post_blind(self, idx, amount):
        p = self.players[idx]
        amt = min(amount, p['chips'])
        p['chips'] -= amt
        p['bet'] += amt
        self.pot += amt
        if p['chips'] == 0:
            p['all_in'] = True

    def active(self):
        return [p for p in self.players if not p['folded']]

    def can_act(self, sid):
        if self.state != 'playing':
            return False
        cp = self.players[self.current_player_idx]
        return cp['sid'] == sid and not cp['folded'] and not cp['all_in']

    def action(self, sid, atype, amount=0):
        if not self.can_act(sid):
            return False, 'Not your turn'
        idx = self.current_player_idx
        p = self.players[idx]

        if atype == 'fold':
            p['folded'] = True
            self.action_required.discard(idx)
            self.last_action = f'{p["name"]} FOLDS'

        elif atype == 'check':
            if self.current_bet > p['bet']:
                return False, 'Cannot check — call or fold'
            self.action_required.discard(idx)
            self.last_action = f'{p["name"]} CHECKS'

        elif atype == 'call':
            amt = min(self.current_bet - p['bet'], p['chips'])
            p['chips'] -= amt
            p['bet'] += amt
            self.pot += amt
            self.action_required.discard(idx)
            if p['chips'] == 0:
                p['all_in'] = True
            self.last_action = f'{p["name"]} CALLS {amt}'

        elif atype in ('raise', 'allin'):
            if atype == 'allin':
                total = p['bet'] + p['chips']
            else:
                total = int(amount)
            add = total - p['bet']
            if add <= 0 or add > p['chips']:
                return False, 'Invalid raise'
            p['chips'] -= add
            p['bet'] = total
            self.pot += add
            if atype == 'allin' or p['chips'] == 0:
                p['all_in'] = True
            if total > self.current_bet:
                self.current_bet = total
                self.action_required = {
                    i for i, pl in enumerate(self.players)
                    if not pl['folded'] and not pl['all_in'] and i != idx
                }
            else:
                self.action_required.discard(idx)
            self.last_action = f'{p["name"]} {"ALL-IN" if p["all_in"] else "RAISES"} → {total}'
        else:
            return False, 'Unknown action'

        self._advance()
        return True, 'OK'

    def _advance(self):
        if len(self.active()) <= 1:
            self._end_hand(self.active())
            return
        if not self.action_required:
            self._next_stage()
            return
        n = len(self.players)
        nxt = (self.current_player_idx + 1) % n
        for _ in range(n):
            if nxt in self.action_required:
                break
            nxt = (nxt + 1) % n
        if nxt in self.action_required:
            self.current_player_idx = nxt
        else:
            self._next_stage()

    def _next_stage(self):
        for p in self.players:
            p['bet'] = 0
        self.current_bet = 0
        n = len(self.players)
        start = (self.dealer_idx + 1) % n
        self.current_player_idx = start
        for i in range(n):
            idx = (start + i) % n
            if not self.players[idx]['folded'] and not self.players[idx]['all_in']:
                self.current_player_idx = idx
                break
        self.action_required = {
            i for i, p in enumerate(self.players)
            if not p['folded'] and not p['all_in']
        }
        if self.stage == 'preflop':
            self.stage = 'flop'
            self.community = [c.to_dict() for c in self.deck.deal(3)]
        elif self.stage == 'flop':
            self.stage = 'turn'
            self.community.append(self.deck.deal(1)[0].to_dict())
        elif self.stage == 'turn':
            self.stage = 'river'
            self.community.append(self.deck.deal(1)[0].to_dict())
        elif self.stage == 'river':
            self._showdown()
            return
        if not self.action_required:
            self._next_stage()

    def _showdown(self):
        self.stage = 'showdown'
        acts = self.active()
        comm = [Card(c['rank'], c['suit']) for c in self.community]
        best, winners = None, []
        for p in acts:
            hole = [Card(c['rank'], c['suit']) for c in p['hand']]
            score = evaluate_hand(hole + comm)
            self.hand_names[p['name']] = HAND_NAMES[score[0]] if score else '?'
            if best is None or score > best:
                best, winners = score, [p]
            elif score == best:
                winners.append(p)
        self._end_hand(winners)

    def _end_hand(self, winners):
        self.state = 'showdown'
        if not winners:
            return
        split = self.pot // len(winners)
        for i, w in enumerate(winners):
            w['chips'] += split + (self.pot % len(winners) if i == 0 else 0)
        self.winner_info = {
            'names': [w['name'] for w in winners],
            'pot': self.pot,
            'hand': self.hand_names.get(winners[0]['name'], ''),
        }
        self.dealer_idx = (self.dealer_idx + 1) % len(self.players)

    def rebuy(self, sid, amount=5000):
        p = self.get_player(sid)
        if not p:
            return False, 'Player not found'
        if p['chips'] > 0:
            return False, 'You still have chips'
        p['chips'] = amount
        return True, 'OK'

    def next_hand(self):
        self.players = [p for p in self.players if p['chips'] > 0]
        if len(self.players) < 2:
            self.state = 'waiting'
            return False, 'Not enough players with chips'
        self.state = 'playing'
        self.new_hand()
        return True, 'OK'

    def to_dict(self, viewer_sid=None):
        showdown = self.state == 'showdown'
        players_data = []
        for i, p in enumerate(self.players):
            reveal = p['sid'] == viewer_sid or showdown
            hand_name = self.hand_names.get(p['name'], '') if showdown else ''
            players_data.append({
                'name': p['name'],
                'chips': p['chips'],
                'bet': p['bet'],
                'folded': p['folded'],
                'all_in': p['all_in'],
                'hand': p['hand'] if reveal else [None, None],
                'is_current': i == self.current_player_idx and self.state == 'playing',
                'is_dealer': i == self.dealer_idx,
                'is_you': p['sid'] == viewer_sid,
                'hand_name': hand_name,
            })
        vp = self.get_player(viewer_sid) if viewer_sid else None
        call_amt = max(0, self.current_bet - vp['bet']) if vp else 0
        can_check = vp and self.current_bet <= vp['bet'] if vp else False
        return {
            'state': self.state,
            'stage': self.stage,
            'players': players_data,
            'community': self.community,
            'pot': self.pot,
            'current_bet': self.current_bet,
            'winner_info': self.winner_info,
            'last_action': self.last_action,
            'your_turn': self.can_act(viewer_sid) if viewer_sid else False,
            'call_amount': call_amt,
            'can_check': can_check,
        }
