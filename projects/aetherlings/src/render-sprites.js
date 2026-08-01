/* Aetherlings — all drawing that isn't UI chrome.
   Nothing is loaded from disk: creatures, tiles and people are drawn procedurally.

   Creatures render once into a 32x32 offscreen canvas, then upscale with smoothing
   off, which is what gives them the chunky pixel look. Results are cached per
   species + facing, so a battle is two cache hits rather than 60 redraws a second. */
(function (AE) {
  'use strict';

  var TS = 16; /* world tile size in px */
  AE.TS = TS;

  /* ---------------- colour helpers ---------------- */
  function clamp255(v) { return v < 0 ? 0 : v > 255 ? 255 : v | 0; }

  function shade(hex, amt) {
    var h = hex.replace('#', '');
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    var r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
    r = clamp255(r + amt); g = clamp255(g + amt); b = clamp255(b + amt);
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  }
  AE.shade = shade;

  /* Deterministic 2D noise so ground detail is stable between frames. */
  function hash2(x, y) {
    var n = (x * 374761393 + y * 668265263) ^ 0x5bf03635;
    n = (n ^ (n >>> 13)) * 1274126177;
    return ((n ^ (n >>> 16)) >>> 0) / 4294967296;
  }
  AE.hash2 = hash2;

  /* ---------------- tiles ---------------- */
  /* Base colours per tile char. Maps can be flagged with a biome that swaps a few. */
  var TILE = {
    '.': { c: '#5ea24e', d: '#4d8b3f', solid: false },       /* grass            */
    ',': { c: '#42863a', d: '#356d2e', solid: false },       /* tall grass       */
    '=': { c: '#c2a878', d: '#ab9166', solid: false },       /* dirt path        */
    ':': { c: '#e0d097', d: '#cdbc83', solid: false },       /* sand             */
    'F': { c: '#5ea24e', d: '#4d8b3f', solid: false },       /* flowers          */
    '#': { c: '#2f6b33', d: '#1f4a24', solid: true },        /* tree             */
    'M': { c: '#7b6a55', d: '#5b4d3c', solid: true },        /* cliff wall       */
    '~': { c: '#3d7fc4', d: '#2f66a3', solid: true },        /* water            */
    'b': { c: '#a5794a', d: '#87613b', solid: false },       /* bridge           */
    '^': { c: '#5ea24e', d: '#3b6b31', solid: false },       /* ledge (hop S)    */
    'R': { c: '#8e8272', d: '#6d6357', solid: true },        /* breakable rock   */
    'B': { c: '#3f7d38', d: '#2d5b28', solid: true },        /* cuttable brush   */
    'C': { c: '#8a7a63', d: '#655741', solid: true },        /* climbable cliff  */
    'H': { c: '#b8695a', d: '#8e4f43', solid: true },        /* building         */
    'D': { c: '#6b4a33', d: '#4a3222', solid: false },       /* door             */
    'S': { c: '#a8845a', d: '#7d6242', solid: true },        /* sign             */
    '-': { c: '#d8c9a8', d: '#c2b393', solid: false },       /* interior floor   */
    '|': { c: '#8a6f55', d: '#6a5340', solid: true },        /* interior wall    */
    'T': { c: '#9c7448', d: '#795737', solid: true },        /* counter/table    */
    'c': { c: '#c46a6a', d: '#a85555', solid: false },       /* carpet           */
    'P': { c: '#7ac0d8', d: '#4f93ab', solid: true },        /* heal machine     */
    'x': { c: '#6b6357', d: '#574f45', solid: false },       /* cave floor       */
    'X': { c: '#3b352d', d: '#2a251f', solid: true },        /* cave wall        */
    'w': { c: '#e4eef2', d: '#cfdde3', solid: false },       /* snow             */
    'W': { c: '#cfe0e8', d: '#b6cbd6', solid: false },       /* deep snow (enc)  */
    'a': { c: '#5c4a44', d: '#483833', solid: false },       /* ash ground       */
    'A': { c: '#4a3a35', d: '#382b27', solid: false }        /* ash drifts (enc) */
  };
  AE.TILE = TILE;

  AE.isSolid = function (ch) { var t = TILE[ch]; return t ? t.solid : true; };
  /* Tall grass, deep snow and ash drifts. Cave floor counts too — underground,
     encounters happen on open ground rather than in vegetation. */
  AE.isEncounterTile = function (ch) {
    return ch === ',' || ch === 'W' || ch === 'A' || ch === 'x';
  };

  AE.drawTile = function (ctx, ch, px, py, time) {
    var t = TILE[ch] || TILE['.'];
    ctx.fillStyle = t.c;
    ctx.fillRect(px, py, TS, TS);

    var gx = px / TS, gy = py / TS, h;

    switch (ch) {
      case '.': case 'w': case 'x': case 'a': case ':':
        /* scattered speckle so open ground isn't a flat colour field */
        h = hash2(gx, gy);
        if (h > 0.55) {
          ctx.fillStyle = t.d;
          ctx.fillRect(px + ((h * 11) | 0) % 12 + 1, py + ((h * 29) | 0) % 12 + 1, 2, 2);
        }
        if (h > 0.85) { ctx.fillStyle = t.d; ctx.fillRect(px + 9, py + 4, 2, 2); }
        break;

      case ',': case 'W': case 'A':
        ctx.fillStyle = t.d;
        for (var i = 0; i < 4; i++) {
          var bx = px + 1 + i * 4, bh = 5 + (hash2(gx * 4 + i, gy) * 4 | 0);
          ctx.fillRect(bx, py + TS - bh, 2, bh);
        }
        ctx.fillStyle = shade(t.c, 22);
        ctx.fillRect(px + 3, py + 6, 2, 3);
        ctx.fillRect(px + 11, py + 8, 2, 3);
        break;

      case 'F':
        h = hash2(gx, gy);
        var fc = h > 0.66 ? '#f0d84a' : h > 0.33 ? '#e87ab0' : '#e8e0f0';
        ctx.fillStyle = fc;
        ctx.fillRect(px + 4, py + 5, 3, 3);
        ctx.fillRect(px + 10, py + 9, 3, 3);
        break;

      case '#':
        ctx.fillStyle = '#4a3423';
        ctx.fillRect(px + 6, py + 9, 4, 7);
        ctx.fillStyle = t.c;
        ctx.beginPath(); ctx.arc(px + 8, py + 7, 7.2, 0, 6.284); ctx.fill();
        ctx.fillStyle = shade(t.c, 26);
        ctx.beginPath(); ctx.arc(px + 6, py + 5, 3.4, 0, 6.284); ctx.fill();
        ctx.fillStyle = t.d;
        ctx.beginPath(); ctx.arc(px + 11, py + 10, 2.6, 0, 6.284); ctx.fill();
        break;

      case 'B':
        ctx.fillStyle = t.c;
        ctx.beginPath(); ctx.arc(px + 8, py + 9, 6.5, 0, 6.284); ctx.fill();
        ctx.fillStyle = t.d;
        ctx.fillRect(px + 3, py + 11, 10, 2);
        ctx.fillStyle = shade(t.c, 24);
        ctx.fillRect(px + 6, py + 4, 2, 4);
        break;

      case 'M': case 'C': case 'X':
        ctx.fillStyle = t.d;
        ctx.fillRect(px, py + 11, TS, 5);
        ctx.fillStyle = shade(t.c, 18);
        ctx.fillRect(px + 1, py + 1, 6, 4);
        ctx.fillRect(px + 9, py + 6, 5, 4);
        if (ch === 'C') { /* climbing notches so it reads as scalable */
          ctx.fillStyle = shade(t.c, -34);
          ctx.fillRect(px + 4, py + 3, 8, 2);
          ctx.fillRect(px + 4, py + 9, 8, 2);
        }
        break;

      case 'R':
        ctx.fillStyle = TILE['.'].c; ctx.fillRect(px, py, TS, TS);
        ctx.fillStyle = t.c;
        ctx.beginPath(); ctx.arc(px + 8, py + 9, 6, 0, 6.284); ctx.fill();
        ctx.fillStyle = shade(t.c, 26); ctx.fillRect(px + 5, py + 5, 4, 3);
        ctx.fillStyle = t.d; ctx.fillRect(px + 3, py + 12, 10, 2);
        break;

      case '~':
        var wob = Math.sin((time || 0) / 380 + gx * 0.9 + gy * 0.6);
        ctx.fillStyle = shade(t.c, wob > 0 ? 10 : -8);
        ctx.fillRect(px, py, TS, TS);
        ctx.fillStyle = shade(t.c, 34);
        ctx.fillRect(px + 2 + (wob > 0 ? 2 : 0), py + 5, 6, 1);
        ctx.fillRect(px + 8 - (wob > 0 ? 2 : 0), py + 11, 5, 1);
        break;

      case 'b':
        ctx.fillStyle = t.d;
        for (var k = 0; k < 4; k++) ctx.fillRect(px, py + k * 4 + 3, TS, 1);
        ctx.fillStyle = shade(t.c, 20);
        ctx.fillRect(px, py, 2, TS); ctx.fillRect(px + 14, py, 2, TS);
        break;

      case '^':
        ctx.fillStyle = TILE['.'].c; ctx.fillRect(px, py, TS, TS);
        ctx.fillStyle = '#8a6b3f'; ctx.fillRect(px, py + 8, TS, 8);
        ctx.fillStyle = '#6d5330'; ctx.fillRect(px, py + 13, TS, 3);
        ctx.fillStyle = shade('#8a6b3f', 26);
        ctx.fillRect(px + 2, py + 9, 3, 2); ctx.fillRect(px + 9, py + 10, 3, 2);
        break;

      case 'H':
        ctx.fillStyle = t.d; ctx.fillRect(px, py, TS, 3);
        ctx.fillStyle = shade(t.c, 18); ctx.fillRect(px + 2, py + 6, 5, 5);
        ctx.fillStyle = '#31435c';
        ctx.fillRect(px + 3, py + 7, 3, 3);
        break;

      case 'D':
        ctx.fillStyle = TILE['H'].c; ctx.fillRect(px, py, TS, TS);
        ctx.fillStyle = t.c; ctx.fillRect(px + 2, py + 2, 12, 14);
        ctx.fillStyle = t.d; ctx.fillRect(px + 2, py + 2, 12, 2);
        ctx.fillStyle = '#e8d060'; ctx.fillRect(px + 11, py + 9, 2, 2);
        break;

      case 'S':
        ctx.fillStyle = TILE['.'].c; ctx.fillRect(px, py, TS, TS);
        ctx.fillStyle = '#6b5335'; ctx.fillRect(px + 7, py + 9, 2, 6);
        ctx.fillStyle = t.c; ctx.fillRect(px + 2, py + 2, 12, 8);
        ctx.fillStyle = t.d; ctx.fillRect(px + 3, py + 4, 10, 1); ctx.fillRect(px + 3, py + 6, 7, 1);
        break;

      case '|':
        ctx.fillStyle = t.d; ctx.fillRect(px, py + 12, TS, 4);
        ctx.fillStyle = shade(t.c, 16); ctx.fillRect(px + 1, py + 2, 6, 3); ctx.fillRect(px + 9, py + 7, 5, 3);
        break;

      case 'T':
        ctx.fillStyle = TILE['-'].c; ctx.fillRect(px, py, TS, TS);
        ctx.fillStyle = t.c; ctx.fillRect(px, py + 2, TS, 12);
        ctx.fillStyle = t.d; ctx.fillRect(px, py + 12, TS, 2);
        ctx.fillStyle = shade(t.c, 20); ctx.fillRect(px, py + 3, TS, 1);
        break;

      case 'c':
        ctx.fillStyle = t.d; ctx.fillRect(px + 2, py + 2, 12, 12);
        ctx.fillStyle = t.c; ctx.fillRect(px + 4, py + 4, 8, 8);
        break;

      case 'P':
        ctx.fillStyle = TILE['-'].c; ctx.fillRect(px, py, TS, TS);
        ctx.fillStyle = '#5d6c7c'; ctx.fillRect(px + 2, py + 3, 12, 12);
        ctx.fillStyle = t.c; ctx.fillRect(px + 4, py + 5, 8, 6);
        ctx.fillStyle = '#f0f6ff'; ctx.fillRect(px + 5, py + 6, 3, 2);
        break;

      case '-':
        h = hash2(gx, gy);
        if (h > 0.8) { ctx.fillStyle = t.d; ctx.fillRect(px + 6, py + 7, 3, 1); }
        break;
    }
  };

  /* =========================================================================
     Creature sprites
     ========================================================================= */

  var creatureCache = {};

  function mkCanvas(w, h) {
    var c = document.createElement('canvas');
    c.width = w; c.height = h;
    return c;
  }

  function ell(ctx, cx, cy, rx, ry, fill, line) {
    ctx.beginPath();
    ctx.ellipse(cx, cy, rx, ry, 0, 0, 6.2832);
    ctx.fillStyle = fill; ctx.fill();
    if (line) { ctx.strokeStyle = line; ctx.lineWidth = 1; ctx.stroke(); }
  }

  function tri(ctx, pts, fill) {
    ctx.beginPath();
    ctx.moveTo(pts[0], pts[1]); ctx.lineTo(pts[2], pts[3]); ctx.lineTo(pts[4], pts[5]);
    ctx.closePath(); ctx.fillStyle = fill; ctx.fill();
  }

  /* Draws one creature into a 32x32 buffer. `back` omits the face for the
     player-side view, which is the cheap way to fake a rear-facing sprite. */
  function paintCreature(sp, back) {
    var cv = mkCanvas(32, 32), g = cv.getContext('2d');
    var s = sp.sprite, f = s.feat || [];
    var c1 = s.c1, c2 = s.c2, c3 = s.c3;
    var dark = shade(c1, -60), sc = s.scale || 1;
    var has = function (k) { return f.indexOf(k) >= 0; };

    g.save();
    /* Scale about the feet so bigger creatures sit on the same ground line. */
    g.translate(16, 30);
    g.scale(sc, sc);
    g.translate(-16, -30);

    /* --- behind-body layers --- */
    if (has('aura')) {
      g.globalAlpha = 0.28;
      ell(g, 16, 18, 14, 13, c3);
      g.globalAlpha = 1;
    }
    if (has('wings')) {
      var wc = has('leaf') ? c3 : shade(c2, -10);
      tri(g, [16, 12, 3, 4, 6, 20], wc);
      tri(g, [16, 12, 29, 4, 26, 20], wc);
      g.globalAlpha = 0.5;
      tri(g, [16, 13, 6, 7, 8, 17], shade(wc, -40));
      tri(g, [16, 13, 26, 7, 24, 17], shade(wc, -40));
      g.globalAlpha = 1;
    } else if (has('smallwings')) {
      tri(g, [16, 14, 7, 9, 10, 19], shade(c2, -10));
      tri(g, [16, 14, 25, 9, 22, 19], shade(c2, -10));
    }

    /* --- body --- */
    var headX = 16, headY = 12, headR = 5;

    switch (s.body) {
      case 'blob':
        ell(g, 16, 20, 10, 9, c1, dark);
        ell(g, 16, 23, 6, 5, c2);
        headX = 16; headY = 16; headR = 6;
        g.fillStyle = dark;
        g.fillRect(9, 28, 4, 2); g.fillRect(19, 28, 4, 2);
        break;

      case 'quad':
        g.fillStyle = shade(c1, -30);
        g.fillRect(9, 22, 3, 7); g.fillRect(14, 23, 3, 6);
        g.fillRect(19, 22, 3, 7); g.fillRect(23, 23, 3, 6);
        ell(g, 16, 19, 10, 6.5, c1, dark);
        ell(g, 16, 22, 7, 3.5, c2);
        headX = 24; headY = 13; headR = 5.2;
        break;

      case 'serp':
        ell(g, 15, 25, 10, 5, c1, dark);
        ell(g, 16, 19, 8, 5, c1, dark);
        ell(g, 17, 14, 6.5, 4.5, c1, dark);
        ell(g, 15, 25, 6, 2.6, c2);
        headX = 18; headY = 9; headR = 4.8;
        break;

      case 'avian':
        g.fillStyle = shade(c3, -10);
        g.fillRect(13, 26, 2, 4); g.fillRect(18, 26, 2, 4);
        ell(g, 16, 20, 7.5, 8, c1, dark);
        ell(g, 16, 22, 4.5, 5, c2);
        headX = 16; headY = 11; headR = 5.2;
        break;

      case 'bug':
        g.fillStyle = shade(c1, -35);
        g.fillRect(6, 21, 5, 2); g.fillRect(21, 21, 5, 2);
        g.fillRect(6, 25, 5, 2); g.fillRect(21, 25, 5, 2);
        ell(g, 16, 24, 8, 6, c1, dark);
        ell(g, 16, 17, 6.5, 5, c1, dark);
        ell(g, 16, 24, 5, 3.5, c2);
        headX = 16; headY = 12; headR = 4.6;
        break;

      case 'biped':
        g.fillStyle = shade(c1, -30);
        g.fillRect(11, 24, 4, 6); g.fillRect(17, 24, 4, 6);
        ell(g, 16, 19, 8, 7.5, c1, dark);
        ell(g, 16, 21, 4.5, 5, c2);
        g.fillStyle = shade(c1, -18);
        g.fillRect(6, 16, 4, 8); g.fillRect(22, 16, 4, 8);
        headX = 16; headY = 9; headR = 5.4;
        break;

      case 'fish':
        tri(g, [7, 20, 1, 12, 1, 28], c3);
        ell(g, 17, 20, 10, 7, c1, dark);
        ell(g, 18, 23, 6, 3.5, c2);
        headX = 22; headY = 17; headR = 4.8;
        break;
    }

    /* --- head --- */
    ell(g, headX, headY, headR, headR * 0.92, c1, dark);
    if (s.body === 'avian') tri(g, [headX + headR - 1, headY, headX + headR + 5, headY + 2, headX + headR - 1, headY + 4], c3);
    if (s.body === 'fish') tri(g, [headX + 2, headY - 2, headX + 7, headY + 1, headX + 2, headY + 4], c3);

    /* --- head-mounted features --- */
    if (has('mane')) {
      g.globalAlpha = 0.9;
      ell(g, headX - 1, headY + 1, headR + 3, headR + 2.4, shade(c3, 30));
      g.globalAlpha = 1;
      ell(g, headX, headY, headR, headR * 0.92, c1, dark);
    }
    if (has('horns')) {
      tri(g, [headX - 4, headY - 3, headX - 7, headY - 10, headX - 1, headY - 5], c3);
      tri(g, [headX + 4, headY - 3, headX + 7, headY - 10, headX + 1, headY - 5], c3);
    } else if (has('horn')) {
      tri(g, [headX - 2, headY - 4, headX, headY - 11, headX + 2, headY - 4], c3);
    }
    if (has('ears')) {
      tri(g, [headX - 5, headY - 2, headX - 6, headY - 9, headX - 1, headY - 4], c1);
      tri(g, [headX + 5, headY - 2, headX + 6, headY - 9, headX + 1, headY - 4], c1);
    }
    if (has('crest')) {
      tri(g, [headX - 3, headY - 4, headX, headY - 12, headX + 3, headY - 4], c3);
    }
    if (has('antenna')) {
      g.strokeStyle = c3; g.lineWidth = 1;
      g.beginPath(); g.moveTo(headX - 2, headY - 4); g.lineTo(headX - 6, headY - 11); g.stroke();
      g.beginPath(); g.moveTo(headX + 2, headY - 4); g.lineTo(headX + 6, headY - 11); g.stroke();
      g.fillStyle = c3;
      g.fillRect(headX - 7, headY - 12, 2, 2); g.fillRect(headX + 5, headY - 12, 2, 2);
    }
    if (has('tuft')) {
      ell(g, headX, headY - headR - 1, 3, 2.4, shade(c2, 10));
    }
    if (has('leaf')) {
      ell(g, headX + 3, headY - headR - 2, 4, 2.2, '#5fbf4a');
      g.strokeStyle = '#356b2c'; g.lineWidth = 1;
      g.beginPath(); g.moveTo(headX, headY - headR); g.lineTo(headX + 6, headY - headR - 3); g.stroke();
    }
    if (has('flame')) {
      tri(g, [headX - 3, headY - headR, headX, headY - headR - 8, headX + 3, headY - headR], '#f0a03c');
      tri(g, [headX - 1.6, headY - headR, headX, headY - headR - 5, headX + 1.6, headY - headR], '#f8e07a');
    }

    /* --- body-mounted features --- */
    if (has('plates')) {
      g.fillStyle = shade(c3, 20);
      g.fillRect(11, 15, 10, 2); g.fillRect(12, 19, 8, 2);
    }
    if (has('shell')) {
      ell(g, 16, 20, 9, 8, shade(c3, 40));
      ell(g, 16, 20, 6, 5, c1);
    }
    if (has('spikes')) {
      for (var i = 0; i < 3; i++) {
        var sx = 10 + i * 6;
        tri(g, [sx, 14, sx + 2.5, 8, sx + 5, 14], c3);
      }
    }
    if (has('fins')) {
      tri(g, [10, 18, 4, 13, 9, 23], c3);
      tri(g, [22, 18, 28, 13, 23, 23], c3);
    }
    if (has('tailfin')) {
      tri(g, [8, 24, 1, 20, 2, 30], c3);
    }
    if (has('tail')) {
      g.strokeStyle = c1; g.lineWidth = 2.4; g.lineCap = 'round';
      g.beginPath(); g.moveTo(8, 21); g.quadraticCurveTo(2, 20, 3, 13); g.stroke();
      g.lineCap = 'butt';
    }
    if (has('claws')) {
      g.fillStyle = c3;
      g.fillRect(9, 28, 2, 2); g.fillRect(21, 28, 2, 2);
    }

    /* --- face (front view only) --- */
    if (!back) {
      var ec = '#1b1420';
      if (has('tri-eyes')) {
        g.fillStyle = ec;
        g.fillRect(headX - 4, headY - 1, 2, 2);
        g.fillRect(headX + 2, headY - 1, 2, 2);
        g.fillRect(headX - 1, headY - 4, 2, 2);
      } else if (has('bigeyes')) {
        g.fillStyle = '#ffffff';
        g.fillRect(headX - 4, headY - 2, 3, 3);
        g.fillRect(headX + 1, headY - 2, 3, 3);
        g.fillStyle = ec;
        g.fillRect(headX - 3, headY - 1, 2, 2);
        g.fillRect(headX + 2, headY - 1, 2, 2);
      } else {
        g.fillStyle = ec;
        g.fillRect(headX - 3, headY - 1, 2, 2);
        g.fillRect(headX + 1, headY - 1, 2, 2);
      }
    }

    g.restore();
    return cv;
  }

  AE.creatureCanvas = function (speciesId, back) {
    var key = speciesId + (back ? '|b' : '|f');
    if (!creatureCache[key]) creatureCache[key] = paintCreature(AE.species(speciesId), !!back);
    return creatureCache[key];
  };

  /* Draw a creature centred on (x, y+size) so it stands on the given baseline. */
  AE.drawCreature = function (ctx, speciesId, x, y, size, back) {
    var cv = AE.creatureCanvas(speciesId, back);
    var prev = ctx.imageSmoothingEnabled;
    ctx.imageSmoothingEnabled = false;
    if (back) {
      ctx.save();
      ctx.translate(x + size, y);
      ctx.scale(-1, 1);
      ctx.drawImage(cv, 0, 0, size, size);
      ctx.restore();
    } else {
      ctx.drawImage(cv, x, y, size, size);
    }
    ctx.imageSmoothingEnabled = prev;
  };

  /* =========================================================================
     Overworld people (player, NPCs, trainers) — 16x16, 4 directions, 2 frames
     ========================================================================= */

  var personCache = {};

  function paintPerson(pal, dir, frame) {
    var cv = mkCanvas(16, 16), g = cv.getContext('2d');
    var skin = pal.skin || '#e8bd94';
    var hair = pal.hair || '#3a2a1e';
    var body = pal.body || '#d84a4a';
    var legs = pal.legs || '#33456b';
    var step = frame === 1 ? 1 : 0;

    /* legs */
    g.fillStyle = legs;
    g.fillRect(5 + (dir === 'w' ? -1 : 0), 12 - step, 2, 4);
    g.fillRect(9 + (dir === 'e' ? 1 : 0), 12 + step, 2, 4);
    /* torso */
    g.fillStyle = body;
    g.fillRect(4, 8, 8, 5);
    g.fillStyle = shade(body, -30);
    g.fillRect(4, 12, 8, 1);
    /* arms */
    g.fillStyle = skin;
    g.fillRect(3, 9 + step, 2, 3);
    g.fillRect(11, 9 - step, 2, 3);
    /* head */
    g.fillStyle = skin;
    g.fillRect(4, 3, 8, 6);
    /* hair by facing */
    g.fillStyle = hair;
    if (dir === 's') { g.fillRect(4, 2, 8, 3); g.fillRect(3, 3, 1, 3); g.fillRect(12, 3, 1, 3); }
    else if (dir === 'n') { g.fillRect(4, 2, 8, 7); }
    else { g.fillRect(4, 2, 8, 3); g.fillRect(dir === 'w' ? 4 : 11, 3, 1, 4); }
    /* eyes */
    if (dir !== 'n') {
      g.fillStyle = '#241a14';
      if (dir === 's') { g.fillRect(6, 6, 1, 2); g.fillRect(9, 6, 1, 2); }
      else if (dir === 'w') g.fillRect(5, 6, 1, 2);
      else g.fillRect(10, 6, 1, 2);
    }
    /* shadow */
    g.globalAlpha = 0.22; g.fillStyle = '#000';
    g.fillRect(4, 15, 8, 1);
    g.globalAlpha = 1;
    return cv;
  }

  AE.drawPerson = function (ctx, pal, x, y, dir, frame) {
    var key = (pal.key || 'p') + dir + frame;
    if (!personCache[key]) personCache[key] = paintPerson(pal, dir, frame);
    var prev = ctx.imageSmoothingEnabled;
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(personCache[key], x, y);
    ctx.imageSmoothingEnabled = prev;
  };

  /* A handful of named looks so NPCs aren't all the same person. */
  AE.PALETTES = {
    player:   { key: 'player', skin: '#e8bd94', hair: '#3a2a1e', body: '#d84a4a', legs: '#33456b' },
    rival:    { key: 'rival', skin: '#e0b088', hair: '#8a4a2a', body: '#4a6fd8', legs: '#2b3448' },
    prof:     { key: 'prof', skin: '#e8c4a0', hair: '#d8d8d8', body: '#eef2f6', legs: '#5a6472' },
    villager: { key: 'vil', skin: '#e8bd94', hair: '#4a3a2a', body: '#6fae5a', legs: '#5a4a38' },
    villager2:{ key: 'vil2', skin: '#c99a72', hair: '#241a14', body: '#c8a24a', legs: '#4a4a5a' },
    elder:    { key: 'eld', skin: '#dcb894', hair: '#c8c8c8', body: '#8a6fb0', legs: '#4a4258' },
    child:    { key: 'kid', skin: '#f0c9a0', hair: '#5a3a1e', body: '#f0a03c', legs: '#4a6a8a' },
    trainer:  { key: 'trn', skin: '#dcae86', hair: '#2a2a3a', body: '#4aa878', legs: '#38424f' },
    warden:   { key: 'wrd', skin: '#e0b48c', hair: '#2a1a30', body: '#8a5ac8', legs: '#3a2a4a' },
    ashen:    { key: 'ash', skin: '#c0a48c', hair: '#1a1a1a', body: '#3a3540', legs: '#22202a' },
    conclave: { key: 'con', skin: '#e0b48c', hair: '#c8a44a', body: '#2a3a5c', legs: '#1c2740' },
    nurse:    { key: 'nur', skin: '#e8bd94', hair: '#e07ab0', body: '#f0f4f8', legs: '#c8ccd4' },
    clerk:    { key: 'clk', skin: '#d8a880', hair: '#3a2a1e', body: '#5a86c8', legs: '#3a4458' },
    sailor:   { key: 'sai', skin: '#c99a72', hair: '#2a2a2a', body: '#3a6fa8', legs: '#2a3a4a' },
    miner:    { key: 'min', skin: '#c99a72', hair: '#4a3a2a', body: '#8a7a4a', legs: '#4a4238' }
  };

})(window.AE = window.AE || {});
