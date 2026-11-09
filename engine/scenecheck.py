# -*- coding: utf-8 -*-
import os, math, re, json
_D = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'families.json'), encoding='utf-8'))
FAMILIES = _D['families']
NOTE_NAMES = tuple(_D['note_names'])
BTC_RE = re.compile(r'(bc1[a-z0-9]{20,}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})')
TOR_RE = re.compile(r'[a-z2-7]{16,56}\.onion', re.I)
EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+')

def _entropy(b):
    if not b:
        return 0.0
    n = len(b)
    e = 0.0
    for i in range(256):
        c = b.count(i)
        if c:
            p = c / n
            e -= p * math.log2(p)
    return e

def scan_dir(directory, sample_entropy=60, max_files=200000, on_progress=None):
    ext_count = {}
    high_entropy = 0
    sampled = 0
    notes_found = []
    note_hits = []
    n = 0
    for dirpath, _, files in os.walk(directory):
        for fn in files:
            n += 1
            if n > max_files:
                break
            p = os.path.join(dirpath, fn)
            low = fn.lower()
            _, ext = os.path.splitext(low)
            if ext:
                ext_count[ext] = ext_count.get(ext, 0) + 1
            if any(k in low for k in NOTE_NAMES):
                notes_found.append(fn)
                try:
                    with open(p, 'rb') as f:
                        head = f.read(65536).decode('utf-8', 'replace')
                except OSError:
                    head = ''
                hl = head.lower()
                feats = []
                if BTC_RE.search(head):
                    feats.append('bitcoin-addr')
                if TOR_RE.search(head):
                    feats.append('onion-link')
                if EMAIL_RE.search(head):
                    feats.append('contact-email')
                for fam in FAMILIES:
                    if any(k in hl for k in fam['keywords']):
                        feats.append('kw:' + fam['name'])
                if feats:
                    note_hits.append({'file': fn, 'features': feats})
            elif sampled < sample_entropy:
                try:
                    if os.path.getsize(p) > 4096:
                        with open(p, 'rb') as f:
                            b = f.read(65536)
                        if _entropy(b) > 7.7:
                            high_entropy += 1
                        sampled += 1
                except OSError:
                    pass
            if on_progress and n % 2000 == 0:
                on_progress({'event': 'progress', 'files': n})
    return {'files': n, 'ext_count': ext_count, 'notes': notes_found, 'note_hits': note_hits, 'high_entropy': high_entropy, 'sampled': sampled}


def analyze(res):
    evidence = []
    fam_votes = {}
    if res['notes'] or res['note_hits']:
        shown = res['notes'][:5] or [h['file'] for h in res['note_hits'][:5]]
        evidence.append('ransom-note-found: %s' % ', '.join(shown))
    for h in res['note_hits']:
        for f in h['features']:
            if f.startswith('kw:'):
                fam_votes[f[3:]] = fam_votes.get(f[3:], 0) + 3
            else:
                fam_votes['__generic'] = fam_votes.get('__generic', 0) + 1
    common = {'.jpg', '.jpeg', '.png', '.gif', '.mp4', '.mp3', '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.zip', '.rar', '.7z', '.exe', '.dll', '.sys', '.py', '.js', '.html', '.css', '.ttf', '.woff', '.pyc', '.bin', '.dat', '.log', '.db', '.vhd', '.vhdx', '.vmdk', '.raw', '.img', '.iso', '.apk', '.lnk', '.url', '.chk'}
    suspicious_exts = []
    for ext, cnt in sorted(res['ext_count'].items(), key=lambda x: -x[1]):
        if ext not in common and cnt >= 20:
            suspicious_exts.append((ext, cnt))
            for fam in FAMILIES:
                if ext in fam['exts']:
                    fam_votes[fam['name']] = fam_votes.get(fam['name'], 0) + 5
    if suspicious_exts:
        evidence.append('mass-weird-ext: ' + ', '.join('%s x%d' % x for x in suspicious_exts[:5]))
    for ext, cnt in suspicious_exts:
        if 4 <= len(ext) <= 6 and cnt >= 50:
            fam_votes['__randomext'] = fam_votes.get('__randomext', 0) + 2
    if res['sampled'] >= 10 and res['high_entropy'] / res['sampled'] > 0.6:
        evidence.append('%d%% sampled files high-entropy' % (100 * res['high_entropy'] // max(res['sampled'],1)))
        fam_votes['__generic'] = fam_votes.get('__generic', 0) + 2
    votes = {k: v for k, v in fam_votes.items() if not k.startswith('__')}
    family = max(votes, key=votes.get) if votes else None
    fam_info = next((f for f in FAMILIES if f['name'] == family), None)
    score = sum(fam_votes.values())
    suspicion = 'high' if score >= 5 else ('medium' if score >= 2 else 'low')
    advice = []
    if suspicion != 'low':
        advice.append('disconnect network, stop writing to disk')
        advice.append('keep notes & samples; check nomoreransom.org for free decryptors')
        if fam_info and ('free' in fam_info['decryptor'].lower() or 'leaked' in fam_info['decryptor'].lower()):
            advice.append(fam_info['decryptor'])
        advice.append('image the disk first with this tool to preserve evidence')
    return {'suspicion': suspicion, 'family': family, 'family_decryptor': (fam_info['decryptor'] if fam_info else None), 'evidence': evidence or ['no ransom trace'], 'advice': advice, 'stats': {'files': res['files'], 'sampled': res['sampled'], 'high_entropy': res['high_entropy'], 'suspicious_exts': suspicious_exts[:10], 'notes': res['notes'][:10]}}
