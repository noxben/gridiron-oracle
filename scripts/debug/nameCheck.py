import re
import json

with open('src/utils/adp_data.js') as f:
    adp_raw = f.read()
adp_json = adp_raw.split('export const ADP_LIST = ', 1)[1]
adp_json = adp_json.split(';\n\n// Lookup by normalized name')[0]
adp_list = json.loads(adp_json)

with open('src/utils/nfl_data.js') as f:
    nfl_raw = f.read()
nfl_json = nfl_raw.split('export const NFL_PLAYERS = ', 1)[1]
nfl_json = nfl_json.split('\n];\n', 1)[0] + '\n]'
nfl_json = nfl_json.replace('NaN', 'null')
nfl_players = json.loads(nfl_json)

def last_from_initial(name):
    parts = name.split('.')
    last = parts[1] if len(parts) > 1 else parts[0]
    return re.sub(r'[^a-z]', '', last.lower())

def last_from_full(name):
    cleaned = re.sub(r"[.']", '', name.lower())
    parts = re.split(r'[\s-]+', cleaned)
    parts = [p for p in parts if p]
    suffixes = {'jr', 'sr', 'ii', 'iii', 'iv', 'v'}
    parts = [p for p in parts if p not in suffixes]
    return parts[-1] if parts else ''

TEAM_ALIASES = {'WSH': 'WAS', 'JAC': 'JAX', 'LA': 'LAR'}

def norm_team(t):
    t = (t or '').upper()
    return TEAM_ALIASES.get(t, t)

nfl_keys = set()
for p in nfl_players:
    key = (last_from_initial(p['name']), norm_team(p['team']), p['position'])
    nfl_keys.add(key)

misses = []
for a in adp_list[:60]:
    key = (last_from_full(a['name']), norm_team(a['team']), a['position'])
    if key not in nfl_keys:
        misses.append((a['name'], a['team'], a['position'], key))

print(f'{len(misses)} misses out of top 60 ADP')
for name, team, pos, key in misses:
    print(f'  {name!r:30} {team:4} {pos:4} -> tried key {key}')