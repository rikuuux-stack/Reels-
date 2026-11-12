#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reel.py — transforme un script JSON (japonais) + ta voix en vidéo verticale 9:16
(Reels / TikTok / Shorts) : fonds stock Pexels ou motion typographique,
sous-titres synchronisés, titre, source.

Dépendances : Python 3.9+ et ffmpeg (avec libass). Rien d'autre.

  python3 reel.py texte  script.json                 # texte à lire + durée estimée
  python3 reel.py render script.json                 # aperçu muet (sans voix)
  python3 reel.py render script.json --voice voix.wav
  python3 reel.py render script.json --voice prises/ # 01.wav, 02.wav… (1 par segment)
  options : --bgm musique.mp3  -o sortie.mp4  --keep

Clé Pexels (gratuite) : variable PEXELS_API_KEY ou fichier .env à côté du script.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

W, H, FPS = 1080, 1920, 30
HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
UA = "reelmaker/1.0 (script personnel)"

DEFAULT_STYLE = {
    "font": "Hiragino Sans" if sys.platform == "darwin" else "Noto Sans CJK JP",
    "accent": "#FFE500",          # couleur des *mots* accentués et des mots-clés
    "text": "#FFFFFF",
    "caption_size": 84,
    "caption_y": 1240,            # zone sûre : au-dessus de l'UI TikTok/Reels
    "keyword_size": 200,
    "title_size": 58,
    "title_y": 300,
    "gradient": ["#0B0B12", "#3A0CA3", "#F72585"],   # fond des segments "typo"
    "darken": 0.35,               # assombrissement des clips stock (0-1)
    "chars_per_sec": 7.5,         # débit estimé pour l'aperçu sans voix
}


# ───────────────────────── utilitaires ─────────────────────────
def die(msg):
    sys.exit(f"✗ {msg}")


def run(cmd, cwd=None):
    p = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        die("ffmpeg a échoué :\n" + " ".join(str(c) for c in cmd) + "\n" + p.stderr[-1500:])
    return p


def duration(path):
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path])
    return float(p.stdout.strip())


def q(t):
    """Cale un temps sur la grille d'images."""
    return round(t * FPS) / FPS


def load_env():
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


# ───────────────────────── texte japonais ─────────────────────────
PAUSE = {"、": 2, ",": 2, "。": 4, "！": 4, "？": 4, "!": 4, "?": 4, "…": 3}


def clean(text):
    return text.replace("*", "")


def weight(text):
    """Durée de lecture relative : 1 par caractère, plus les pauses."""
    w = 0.0
    for ch in clean(text):
        if ch.isspace():
            continue
        w += PAUSE.get(ch, 1)
    return max(w, 1.0)


def split_captions(text, max_len=14):
    """Découpe une phrase en sous-titres courts (ponctuation, puis particules)."""
    # caractères + drapeau d'accent, pour pouvoir couper au milieu d'un *accent*
    chars, on = [], False
    for ch in text:
        if ch == "*":
            on = not on
        elif not ch.isspace():
            chars.append((ch, on))

    def render(seq):
        out, cur = "", False
        for ch, f in seq:
            if f != cur:
                out += "*"
                cur = f
            out += ch
        return out + ("*" if cur else "")

    def cut(seq, res):
        if len(seq) <= max_len:
            res.append(seq)
            return
        mid = len(seq) // 2
        hira = lambda c: "ぁ" <= c <= "ゟ"
        # coupe après une particule, ou au passage hiragana → kanji/katakana
        cands = [i for i in range(2, len(seq) - 1)
                 if hira(seq[i - 1][0]) and not hira(seq[i][0])]
        best = min(cands, key=lambda i: abs(i - mid)) if cands else mid
        if abs(best - mid) > max(2, len(seq) // 3):
            best = mid
        cut(seq[:best], res)
        cut(seq[best:], res)

    pieces, cur = [], []
    for c in chars:
        cur.append(c)
        if c[0] in "。！？!?、":
            pieces.append(cur)
            cur = []
    if cur:
        pieces.append(cur)
    # regroupe les tout petits morceaux avec le suivant si ça tient
    merged = []
    for p in pieces:
        if merged and len(merged[-1]) + len(p) <= max_len and merged[-1][-1][0] == "、":
            merged[-1] = merged[-1] + p
        else:
            merged.append(p)
    res = []
    for p in merged:
        cut(p, res)
    out = []
    for seq in res:
        while seq and seq[-1][0] in "、。":
            seq = seq[:-1]
        if seq:
            out.append(render(seq))
    return out


# ───────────────────────── synchronisation voix ─────────────────────────
def detect_silences(path):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-af",
                        "silencedetect=noise=-32dB:d=0.22", "-f", "null", "-"],
                       capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", p.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", p.stderr)]
    return list(zip(starts, ends + [None] * (len(starts) - len(ends))))


def align_single(path, segs):
    """Un seul fichier voix : répartit les segments au prorata du texte,
    puis accroche chaque frontière au silence le plus proche."""
    total = duration(path)
    sil = detect_silences(path)
    s0, s1 = 0.0, total
    inner = []
    for a, b in sil:
        b = total if b is None else b
        if a <= 0.05:
            s0 = b
        elif b >= total - 0.05:
            s1 = a
        else:
            inner.append((a, b))
    ws = [weight(s["text"]) for s in segs]
    tot = sum(ws)
    bounds, acc = [0.0], 0.0
    used = set()
    for i, w in enumerate(ws[:-1]):
        acc += w
        ideal = s0 + (s1 - s0) * acc / tot
        seg_len = (s1 - s0) * min(ws[i], ws[i + 1]) / tot
        tol = min(1.6, 0.45 * seg_len)
        best = None
        for k, (a, b) in enumerate(inner):
            m = (a + b) / 2
            if k not in used and abs(m - ideal) <= tol and m > bounds[-1] + 0.4:
                # préfère les silences longs (fin de phrase) proches de l'idéal
                score = abs(m - ideal) - 0.6 * (b - a)
                if best is None or score < best[0]:
                    best = (score, k, m)
        if best:
            used.add(best[1])
            bounds.append(best[2])
        else:
            bounds.append(max(ideal, bounds[-1] + 0.4))
    bounds.append(total)
    snapped = len(used)
    return bounds, s0, s1, snapped


def build_voice_from_dir(d, n, work):
    files = sorted(p for p in Path(d).iterdir()
                   if p.suffix.lower() in (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"))
    if len(files) != n:
        die(f"{d} contient {len(files)} fichiers audio pour {n} segments "
            f"(attendu : 01, 02, … un par segment).")
    gap = 0.12
    durs = []
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for f in files:
        cmd += ["-i", f]
        durs.append(duration(f) + gap)
    filt = "".join(f"[{i}:a]aresample=48000,aformat=channel_layouts=mono,apad=pad_dur={gap}[a{i}];"
                   for i in range(n))
    filt += "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[out]"
    out = work / "voice.wav"
    run(cmd + ["-filter_complex", filt, "-map", "[out]", out])
    bounds = [0.0]
    for d_ in durs:
        bounds.append(bounds[-1] + d_)
    bounds[-1] = duration(out)
    return out, bounds


# ───────────────────────── Pexels ─────────────────────────
def pexels_fetch(query, need, used, credits):
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        return None
    CACHE.mkdir(exist_ok=True)
    url = ("https://api.pexels.com/videos/search?" +
           urllib.parse.urlencode({"query": query, "orientation": "portrait", "per_page": 12}))
    try:
        req = urllib.request.Request(url, headers={"Authorization": key, "User-Agent": UA})
        data = json.load(urllib.request.urlopen(req, timeout=30))
    except Exception as e:
        print(f"  ! Pexels « {query} » : {e}")
        return None
    vids = [v for v in data.get("videos", []) if v["id"] not in used]
    if not vids:
        print(f"  ! Pexels : aucun résultat pour « {query} »")
        return None
    vids.sort(key=lambda v: (v.get("duration", 0) < need, abs(v.get("duration", 0) - need - 2)))
    v = vids[0]
    files = [f for f in v["video_files"] if f.get("height") and f.get("width")
             and f["height"] > f["width"]] or v["video_files"]
    files.sort(key=lambda f: (f.get("height", 0) < 1280, abs(f.get("height", 0) - 1920)))
    f = files[0]
    dest = CACHE / f"pexels_{v['id']}_{f.get('height', 0)}.mp4"
    if not dest.exists():
        try:
            req = urllib.request.Request(f["link"], headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as o:
                shutil.copyfileobj(r, o)
        except Exception as e:
            print(f"  ! téléchargement Pexels : {e}")
            dest.unlink(missing_ok=True)
            return None
    used.add(v["id"])
    credits.append(f"Vidéo : {v.get('user', {}).get('name', '?')} / Pexels — {v.get('url', '')}")
    return dest


# ───────────────────────── fonds ─────────────────────────
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p",
       "-r", str(FPS), "-an"]
FIT = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS}"


def bg_typo(out, dur, st, seed):
    cols = st["gradient"]
    c = ":".join(f"c{i}={x.replace('#', '0x')}" for i, x in enumerate(cols))
    src = (f"gradients=s={W}x{H}:r={FPS}:d={dur:.3f}:nb_colors={len(cols)}:{c}"
           f":speed=0.012:seed={seed}")
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", src,
         "-vf", "format=yuv420p", "-t", f"{dur:.3f}", *ENC, out])


def bg_media(out, src, dur, st):
    dark = f",drawbox=c=black@{st['darken']}:t=fill" if st["darken"] > 0 else ""
    if Path(src).suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
        n = int(dur * FPS) + 1
        vf = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
              f"zoompan=z='1+0.08*on/{n}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":d={n}:s={W}x{H}:fps={FPS},setsar=1{dark}")
        run(["ffmpeg", "-y", "-v", "error", "-i", src, "-vf", vf, "-t", f"{dur:.3f}", *ENC, out])
    else:
        run(["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", src,
             "-vf", FIT + dark, "-t", f"{dur:.3f}", *ENC, out])


# ───────────────────────── sous-titres ASS ─────────────────────────
def ass_color(hexa):
    h = hexa.lstrip("#")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def ass_time(t):
    t = max(t, 0)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def markup(text, st):
    """*mot* → mot en couleur d'accent."""
    acc, base = ass_color(st["accent"]), ass_color(st["text"])
    parts = text.split("*")
    out = ""
    for i, p in enumerate(parts):
        out += (f"{{\\c{acc}&}}{p}{{\\c{base}&}}" if i % 2 else p)
    return out.replace("\n", "\\N")


def build_ass(script, segs, bounds, speech, st, path):
    font = st["font"]
    white, acc = ass_color(st["text"]), ass_color(st["accent"])
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{st['caption_size']},{white},{white},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,2,5,60,60,0,1
Style: Key,{font},{st['keyword_size']},{acc},{acc},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,9,4,5,40,40,0,1
Style: Title,{font},{st['title_size']},{white},{white},&HE6000000,&HE6000000,-1,0,0,0,100,100,0,0,3,18,0,5,60,60,0,1
Style: TitleTxt,{font},{st['title_size']},{white},{white},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,60,60,0,1
Style: Src,{font},30,&H00DDDDDD,&H00DDDDDD,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,3,0,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    total = bounds[-1]

    def add(a, b, style, text, layer=1):
        ev.append(f"Dialogue: {layer},{ass_time(a)},{ass_time(b)},{style},,0,0,0,,{text}")

    if script.get("title"):
        # boîte d'un seul tenant (texte invisible), puis le texte coloré par-dessus :
        # sinon chaque morceau *accentué* a sa propre boîte et les bords se chevauchent
        tpos = f"{{\\pos({W // 2},{st['title_y']})\\fad(200,0)"
        add(0, total, "Title", tpos + "\\1a&HFF&}" + clean(script["title"]), 2)
        add(0, total, "TitleTxt", tpos + "}" + markup(script["title"], st), 3)
    if script.get("source"):
        add(0, total, "Src", f"{{\\pos({W // 2},{st['title_y'] + st['title_size'] + 40})}}"
            f"出典：{script['source']}", 2)

    pop = "\\fad(50,30)\\fscx82\\fscy82\\t(0,110,\\fscx100\\fscy100)"
    for i, s in enumerate(segs):
        a = max(bounds[i], speech[0]) if i == 0 else bounds[i]
        b = min(bounds[i + 1], speech[1]) if i == len(segs) - 1 else bounds[i + 1]
        caps = s.get("captions") or split_captions(s["text"])
        ws = [weight(c) for c in caps]
        t = a
        for c, w in zip(caps, ws):
            d = (b - a) * w / sum(ws)
            add(t, t + d, "Cap", f"{{\\pos({W // 2},{st['caption_y']}){pop}}}" + markup(c, st))
            t += d
        if s.get("keyword"):
            kw = s["keyword"].replace("\n", "\\N")
            add(bounds[i] + 0.05, bounds[i + 1], "Key",
                f"{{\\pos({W // 2},{H * 0.42:.0f})\\fad(80,80)\\fscx60\\fscy60"
                f"\\t(0,180,\\fscx100\\fscy100)}}{kw}", 0)
    Path(path).write_text(head + "\n".join(ev) + "\n", encoding="utf-8")


# ───────────────────────── commandes ─────────────────────────
def load_script(path):
    try:
        script = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        die(f"script illisible : {e}")
    segs = script.get("segments") or die("le script n'a pas de « segments »")
    for i, s in enumerate(segs, 1):
        if not s.get("text"):
            die(f"segment {i} : « text » manquant")
        s.setdefault("visual", "typo")
    st = {**DEFAULT_STYLE, **script.get("style", {})}
    return script, segs, st


def estimate(segs, st):
    return [weight(s["text"]) / st["chars_per_sec"] + 0.25 for s in segs]


def cmd_texte(args):
    script, segs, st = load_script(args.script)
    est = estimate(segs, st)
    print(f"\n{clean(script.get('title', ''))}\n" + "─" * 30)
    for i, (s, d) in enumerate(zip(segs, est), 1):
        print(f"{i:02d}  {clean(s['text'])}   (~{d:.1f}s)")
    print("─" * 30 + f"\nDurée estimée : ~{sum(est):.0f} s  ({len(segs)} segments)")
    print("Enregistre soit un seul fichier (courte pause entre chaque segment),\n"
          "soit un fichier par segment : 01.wav, 02.wav… dans un dossier.")


def cmd_render(args):
    load_env()
    script, segs, st = load_script(args.script)
    n = len(segs)
    out = Path(args.output or Path(args.script).with_suffix(".mp4")).resolve()
    work = Path(tempfile.mkdtemp(prefix="reel_"))
    credits = []
    if not script.get("source"):
        print("  ! pas de « source » dans le script : cite toujours ta source.")

    # 1) voix + timing
    if args.voice and Path(args.voice).is_dir():
        voice, bounds = build_voice_from_dir(args.voice, n, work)
        speech = (0.0, bounds[-1])
        print(f"• voix : {n} prises, {bounds[-1]:.1f}s (synchro exacte)")
    elif args.voice:
        voice = Path(args.voice).resolve()
        if not voice.exists():
            die(f"fichier voix introuvable : {voice}")
        bounds, s0, s1, snapped = align_single(voice, segs)
        speech = (s0, s1)
        print(f"• voix : {bounds[-1]:.1f}s — {snapped}/{n - 1} coupes calées sur un silence")
        if snapped < n - 1:
            print("  ! synchro approximative sur certains segments : marque une pause nette "
                  "entre les segments, ou enregistre un fichier par segment.")
    else:
        voice = None
        bounds = [0.0]
        for d in estimate(segs, st):
            bounds.append(bounds[-1] + d)
        speech = (0.0, bounds[-1])
        print(f"• aperçu sans voix : {bounds[-1]:.1f}s estimées")
    bounds = [q(b) for b in bounds]
    total = bounds[-1]
    if total > 90:
        print(f"  ! {total:.0f}s : long pour un Reel/TikTok (vise 30-60s).")

    # 2) fonds
    used = set()
    listing = []
    for i, s in enumerate(segs):
        dur = bounds[i + 1] - bounds[i]
        clip = work / f"bg_{i:02d}.mp4"
        src = None
        kind = s["visual"]
        if kind == "stock":
            src = pexels_fetch(s.get("query") or "city", dur, used, credits)
            if src is None and not os.environ.get("PEXELS_API_KEY") and i == 0:
                print("  ! PEXELS_API_KEY absente : segments stock remplacés par le fond typo.")
        elif kind == "file":
            src = (Path(args.script).parent / s["path"]).resolve()
            if not src.exists():
                die(f"segment {i + 1} : fichier introuvable {src}")
        if src:
            bg_media(clip, src, dur, st)
        else:
            bg_typo(clip, dur, st, seed=i + 1)
        listing.append(f"file '{clip.name}'")
        print(f"• segment {i + 1}/{n} : {kind if src or kind == 'typo' else 'typo (repli)'} {dur:.1f}s")
    (work / "list.txt").write_text("\n".join(listing), encoding="utf-8")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "list.txt",
         "-c", "copy", "bg.mp4"], cwd=work)

    # 3) sous-titres
    build_ass(script, segs, bounds, speech, st, work / "subs.ass")

    # 4) assemblage final
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", "bg.mp4"]
    if voice:
        cmd += ["-i", voice]
    else:
        cmd += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    fc = "[0:v]ass=subs.ass[v];"
    if args.bgm:
        cmd += ["-stream_loop", "-1", "-i", Path(args.bgm).resolve()]
        voice_f = "loudnorm=I=-14:TP=-1.5:LRA=11" if voice else "anull"
        fc += (f"[1:a]{voice_f},aresample=48000,asplit[vo][sc];"
               f"[2:a]aresample=48000,volume={args.bgm_volume}[m];"
               "[m][sc]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[md];"
               "[vo][md]amix=inputs=2:duration=first:normalize=0[a]")
    else:
        fc += "[1:a]" + ("loudnorm=I=-14:TP=-1.5:LRA=11," if voice else "") + "aresample=48000[a]"
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
    run(cmd, cwd=work)

    # 5) crédits / description
    lines = [clean(script.get("title", "")), ""]
    if script.get("source"):
        lines.append(f"出典：{script['source']} {script.get('source_url', '')}".strip())
    lines += credits
    cred = out.with_suffix(".credits.txt")
    cred.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if args.keep:
        print(f"• fichiers intermédiaires : {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    print(f"✓ {out}  ({total:.1f}s, {W}x{H})\n✓ {cred}")


def main():
    ap = argparse.ArgumentParser(description="Script JSON → vidéo verticale 9:16")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("texte", help="affiche le texte à enregistrer")
    t.add_argument("script")
    t.set_defaults(fn=cmd_texte)
    r = sub.add_parser("render", help="génère la vidéo")
    r.add_argument("script")
    r.add_argument("--voice", help="fichier audio, ou dossier 01.wav 02.wav…")
    r.add_argument("--bgm", help="musique de fond (optionnel)")
    r.add_argument("--bgm-volume", type=float, default=0.18)
    r.add_argument("-o", "--output")
    r.add_argument("--keep", action="store_true", help="garde les fichiers intermédiaires")
    r.set_defaults(fn=cmd_render)
    args = ap.parse_args()
    if not shutil.which("ffmpeg"):
        die("ffmpeg introuvable (macOS : brew install ffmpeg)")
    args.fn(args)


if __name__ == "__main__":
    main()
