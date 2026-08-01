/* Aetherlings — engine core.
   Screen geometry, RNG, the creature model and its stat maths, input (keyboard +
   on-screen buttons), the scene stack, the main loop, and 3-slot save/load. */
(function (AE) {
  'use strict';

  AE.W = 240;
  AE.H = 320;
  AE.VERSION = 1;

  /* ---------------- RNG ---------------- */
  AE.rand = function (n) { return Math.floor(Math.random() * n); };
  AE.randInt = function (a, b) { return a + Math.floor(Math.random() * (b - a + 1)); };
  AE.chance = function (pct) { return Math.random() * 100 < pct; };
  AE.pick = function (arr) { return arr[Math.floor(Math.random() * arr.length)]; };

  /* Small seeded generator, used where output must be stable (map decoration). */
  AE.seeded = function (seed) {
    var s = seed >>> 0 || 1;
    return function () {
      s ^= s << 13; s >>>= 0;
      s ^= s >> 17;
      s ^= s << 5; s >>>= 0;
      return s / 4294967296;
    };
  };

  /* ---------------- creature model ---------------- */
  var STAT_KEYS = ['hp', 'atk', 'def', 'spa', 'spd', 'spe'];
  AE.STAT_KEYS = STAT_KEYS;
  AE.STAT_NAME = { hp: 'HP', atk: 'Attack', def: 'Defence', spa: 'Sp. Atk', spd: 'Sp. Def', spe: 'Speed' };

  /* Gen-3 stat formulas. EVs are kept but earned in much smaller amounts. */
  AE.calcStat = function (mon, key) {
    var sp = AE.species(mon.sp);
    var base = sp.base[key], iv = mon.ivs[key] || 0, ev = mon.evs[key] || 0;
    var core = Math.floor((2 * base + iv + Math.floor(ev / 4)) * mon.lvl / 100);
    if (key === 'hp') return core + mon.lvl + 10;
    return Math.floor((core + 5) * AE.natureMod(mon.nature, key));
  };

  AE.stats = function (mon) {
    var out = {};
    STAT_KEYS.forEach(function (k) { out[k] = AE.calcStat(mon, k); });
    return out;
  };

  AE.maxHP = function (mon) { return AE.calcStat(mon, 'hp'); };
  AE.monName = function (mon) { return mon.nick || AE.species(mon.sp).name; };
  AE.isFainted = function (mon) { return mon.hp <= 0; };

  AE.makeMon = function (speciesId, level, opts) {
    opts = opts || {};
    var sp = AE.species(speciesId);
    var ivs = {}, evs = {};
    STAT_KEYS.forEach(function (k) {
      ivs[k] = opts.perfect ? 31 : AE.randInt(4, 28);
      evs[k] = 0;
    });
    var mon = {
      sp: speciesId,
      nick: opts.nick || null,
      lvl: level,
      exp: AE.expForLevel(sp.growth, level),
      ivs: ivs, evs: evs,
      nature: (opts.nature || AE.pick(AE.NATURES).name),
      moves: [],
      hp: 0,
      status: 'none',
      sleepTurns: 0,
      origLvl: level,
      met: opts.met || null
    };
    /* Learn the four most recent level-up moves available at this level. */
    var pool = AE.movesUpTo(speciesId, level);
    var chosen = opts.moves || pool.slice(-4);
    chosen.forEach(function (id) { AE.teachMove(mon, id, true); });
    if (!mon.moves.length) AE.teachMove(mon, sp.learn[0][1], true);
    mon.hp = AE.maxHP(mon);
    return mon;
  };

  AE.teachMove = function (mon, moveId, silent) {
    if (mon.moves.some(function (m) { return m.id === moveId; })) return false;
    if (mon.moves.length >= 4 && !silent) return false;
    var mv = AE.move(moveId);
    if (mon.moves.length >= 4) mon.moves.shift();
    mon.moves.push({ id: moveId, pp: mv.pp, maxpp: mv.pp });
    return true;
  };

  AE.healMon = function (mon) {
    mon.hp = AE.maxHP(mon);
    mon.status = 'none';
    mon.sleepTurns = 0;
    mon.moves.forEach(function (m) { m.pp = m.maxpp; });
  };

  AE.expToNext = function (mon) {
    var sp = AE.species(mon.sp);
    if (mon.lvl >= 100) return 0;
    return AE.expForLevel(sp.growth, mon.lvl + 1) - mon.exp;
  };

  AE.expProgress = function (mon) {
    var sp = AE.species(mon.sp);
    if (mon.lvl >= 100) return 1;
    var lo = AE.expForLevel(sp.growth, mon.lvl), hi = AE.expForLevel(sp.growth, mon.lvl + 1);
    return hi === lo ? 0 : (mon.exp - lo) / (hi - lo);
  };

  /* Adds EXP and returns a list of events for the battle log to narrate. */
  AE.giveExp = function (mon, amount) {
    var sp = AE.species(mon.sp), events = [];
    if (mon.lvl >= 100) return events;
    mon.exp += amount;
    events.push({ t: 'exp', amount: amount });
    while (mon.lvl < 100 && mon.exp >= AE.expForLevel(sp.growth, mon.lvl + 1)) {
      var beforeMax = AE.maxHP(mon);
      mon.lvl++;
      mon.hp += AE.maxHP(mon) - beforeMax; /* level-ups raise current HP too */
      events.push({ t: 'level', lvl: mon.lvl });
      sp.learn.forEach(function (e) {
        if (e[0] === mon.lvl) {
          if (mon.moves.length < 4) {
            AE.teachMove(mon, e[1], true);
            events.push({ t: 'move', move: e[1] });
          } else {
            events.push({ t: 'moveFull', move: e[1] });
          }
        }
      });
      if (sp.evo && sp.evo.lvl && mon.lvl >= sp.evo.lvl) {
        events.push({ t: 'evolve', to: sp.evo.to });
      }
    }
    return events;
  };

  AE.evolveMon = function (mon, toId) {
    var beforeMax = AE.maxHP(mon);
    mon.sp = toId;
    mon.hp += AE.maxHP(mon) - beforeMax;
    /* Pick up any stage-1 move the new form knows, if there's a free slot. */
    AE.species(toId).learn.forEach(function (e) {
      if (e[0] <= 1 && mon.moves.length < 4) AE.teachMove(mon, e[1], true);
    });
  };

  /* ---------------- game state ---------------- */
  AE.newGame = function (name) {
    return {
      version: AE.VERSION,
      name: name || 'Tamer',
      map: 'willowmere',
      x: 14, y: 12, dir: 's',
      money: 3000,
      party: [],
      box: [],
      bag: {},
      badges: [],
      flags: {},
      seen: {},
      caught: {},
      visited: { willowmere: true },
      skills: [],
      playtime: 0,
      started: Date.now()
    };
  };

  AE.addItem = function (g, id, n) {
    AE.item(id);
    g.bag[id] = (g.bag[id] || 0) + (n || 1);
  };
  AE.removeItem = function (g, id, n) {
    n = n || 1;
    if (!g.bag[id] || g.bag[id] < n) return false;
    g.bag[id] -= n;
    if (g.bag[id] <= 0) delete g.bag[id];
    return true;
  };
  AE.hasItem = function (g, id) { return (g.bag[id] || 0) > 0; };

  AE.addToParty = function (g, mon) {
    if (g.party.length < 6) { g.party.push(mon); return 'party'; }
    g.box.push(mon); return 'box';
  };

  AE.healParty = function (g) { g.party.forEach(AE.healMon); };

  AE.firstHealthy = function (g) {
    return g.party.findIndex(function (m) { return m.hp > 0; });
  };

  AE.partyAlive = function (g) {
    return g.party.some(function (m) { return m.hp > 0; });
  };

  AE.hasSkill = function (g, id) { return g.skills.indexOf(id) >= 0; };

  AE.FIELD_SKILLS = [
    { id: 'cleave',  name: 'Cleave',  badge: 1, tile: 'B', verb: 'cut through the brush' },
    { id: 'shatter', name: 'Shatter', badge: 2, tile: 'R', verb: 'shatter the rock' },
    { id: 'surge',   name: 'Surge',   badge: 4, tile: '~', verb: 'ride across the water' },
    { id: 'ascend',  name: 'Ascend',  badge: 6, tile: 'C', verb: 'climb the cliff' },
    { id: 'recall',  name: 'Recall',  badge: 3, tile: null, verb: 'return to a town you know' }
  ];

  /* ---------------- input ---------------- */
  var Input = {
    held: {}, pressed: {}, consumed: {},
    dirs: ['up', 'down', 'left', 'right'],
    isDown: function (b) { return !!this.held[b]; },
    /* One-shot: true on the frame a button goes down, then cleared. */
    tap: function (b) {
      if (this.pressed[b] && !this.consumed[b]) { this.consumed[b] = true; return true; }
      return false;
    },
    endFrame: function () { this.pressed = {}; this.consumed = {}; }
  };
  AE.Input = Input;

  var KEYMAP = {
    ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right',
    w: 'up', s: 'down', a: 'left', d: 'right',
    W: 'up', S: 'down', A: 'left', D: 'right',
    z: 'a', Z: 'a', Enter: 'a', ' ': 'a',
    x: 'b', X: 'b', Backspace: 'b',
    Escape: 'start', m: 'start', M: 'start'
  };

  function press(btn) {
    if (!btn) return;
    if (!Input.held[btn]) Input.pressed[btn] = true;
    Input.held[btn] = true;
  }
  function release(btn) {
    if (!btn) return;
    Input.held[btn] = false;
  }
  AE.pressButton = press;
  AE.releaseButton = release;

  AE.bindInput = function (root) {
    window.addEventListener('keydown', function (e) {
      var b = KEYMAP[e.key];
      if (b) { e.preventDefault(); press(b); }
    });
    window.addEventListener('keyup', function (e) {
      var b = KEYMAP[e.key];
      if (b) { e.preventDefault(); release(b); }
    });
    window.addEventListener('blur', function () { Input.held = {}; });

    /* On-screen controls. Pointer events cover touch, pen and mouse in one path. */
    Array.prototype.forEach.call(root.querySelectorAll('[data-btn]'), function (el) {
      var btn = el.getAttribute('data-btn');
      var down = function (e) { e.preventDefault(); press(btn); el.classList.add('on'); };
      var up = function (e) { e.preventDefault(); release(btn); el.classList.remove('on'); };
      el.addEventListener('pointerdown', down);
      el.addEventListener('pointerup', up);
      el.addEventListener('pointercancel', up);
      el.addEventListener('pointerleave', up);
      el.addEventListener('contextmenu', function (e) { e.preventDefault(); });
    });
  };

  /* ---------------- text ---------------- */
  AE.setFont = function (ctx, size, bold) {
    ctx.font = (bold ? 'bold ' : '') + (size || 10) + 'px "Trebuchet MS", "Segoe UI", sans-serif';
    ctx.textBaseline = 'top';
  };

  AE.text = function (ctx, str, x, y, opts) {
    opts = opts || {};
    AE.setFont(ctx, opts.size || 10, opts.bold);
    if (opts.shadow !== false) {
      ctx.fillStyle = opts.shadowColor || 'rgba(0,0,0,.55)';
      ctx.fillText(str, x + 1, y + 1);
    }
    ctx.fillStyle = opts.color || '#ffffff';
    ctx.fillText(str, x, y);
  };

  AE.textRight = function (ctx, str, rx, y, opts) {
    opts = opts || {};
    AE.setFont(ctx, opts.size || 10, opts.bold);
    var w = ctx.measureText(str).width;
    AE.text(ctx, str, rx - w, y, opts);
  };

  AE.wrap = function (ctx, str, maxW, size) {
    AE.setFont(ctx, size || 10);
    var words = String(str).split(' '), lines = [], cur = '';
    for (var i = 0; i < words.length; i++) {
      var test = cur ? cur + ' ' + words[i] : words[i];
      if (ctx.measureText(test).width > maxW && cur) { lines.push(cur); cur = words[i]; }
      else cur = test;
    }
    if (cur) lines.push(cur);
    return lines;
  };

  AE.panel = function (ctx, x, y, w, h, opts) {
    opts = opts || {};
    ctx.fillStyle = opts.fill || 'rgba(22,26,38,.94)';
    ctx.fillRect(x, y, w, h);
    ctx.strokeStyle = opts.border || '#6f7fa8';
    ctx.lineWidth = 1;
    ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
    ctx.strokeStyle = opts.inner || 'rgba(255,255,255,.10)';
    ctx.strokeRect(x + 2.5, y + 2.5, w - 5, h - 5);
  };

  /* ---------------- scene stack ---------------- */
  var scenes = [];
  AE.Scenes = scenes;
  AE.top = function () { return scenes[scenes.length - 1]; };

  AE.push = function (scene) {
    scenes.push(scene);
    if (scene.onEnter) scene.onEnter();
    return scene;
  };
  AE.pop = function () {
    var s = scenes.pop();
    if (s && s.onExit) s.onExit();
    var t = AE.top();
    if (t && t.onResume) t.onResume();
    return s;
  };
  AE.replaceAll = function (scene) {
    while (scenes.length) { var s = scenes.pop(); if (s.onExit) s.onExit(); }
    return AE.push(scene);
  };

  /* ---------------- main loop ---------------- */
  var last = 0, ctxRef = null, running = false;

  function frame(now) {
    if (!running) return;
    var dt = Math.min(50, now - last) || 16;
    last = now;

    if (AE.game) AE.game.playtime += dt;

    /* Update only the top scene; draw everything below it so menus overlay the map. */
    var t = AE.top();
    if (t && t.update) t.update(dt);

    ctxRef.save();
    ctxRef.imageSmoothingEnabled = false;
    ctxRef.fillStyle = '#0b0e15';
    ctxRef.fillRect(0, 0, AE.W, AE.H);
    for (var i = 0; i < scenes.length; i++) {
      if (scenes[i].draw) scenes[i].draw(ctxRef, now);
    }
    ctxRef.restore();

    Input.endFrame();
    requestAnimationFrame(frame);
  }

  AE.start = function (ctx) {
    ctxRef = ctx;
    running = true;
    last = performance.now();
    requestAnimationFrame(frame);
  };
  AE.stop = function () { running = false; };

  /* ---------------- save / load ---------------- */
  var MEM = {}; /* fallback when localStorage is unavailable */

  function store() {
    try {
      window.localStorage.setItem('__ae_probe', '1');
      window.localStorage.removeItem('__ae_probe');
      return window.localStorage;
    } catch (e) {
      return {
        getItem: function (k) { return Object.prototype.hasOwnProperty.call(MEM, k) ? MEM[k] : null; },
        setItem: function (k, v) { MEM[k] = v; },
        removeItem: function (k) { delete MEM[k]; }
      };
    }
  }

  var KEY = function (slot) { return 'aetherlings.save.' + slot; };

  AE.save = function (g, slot) {
    try {
      store().setItem(KEY(slot), JSON.stringify(g));
      return true;
    } catch (e) { return false; }
  };

  AE.load = function (slot) {
    try {
      var raw = store().getItem(KEY(slot));
      if (!raw) return null;
      var g = JSON.parse(raw);
      if (!g || g.version !== AE.VERSION) return null;
      return g;
    } catch (e) { return null; }
  };

  AE.deleteSave = function (slot) {
    try { store().removeItem(KEY(slot)); return true; } catch (e) { return false; }
  };

  AE.saveSummary = function (slot) {
    var g = AE.load(slot);
    if (!g) return null;
    return {
      name: g.name,
      badges: g.badges.length,
      party: g.party.length,
      top: g.party.reduce(function (a, m) { return Math.max(a, m.lvl); }, 0),
      time: AE.formatTime(g.playtime)
    };
  };

  AE.formatTime = function (ms) {
    var total = Math.floor((ms || 0) / 1000);
    var h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60);
    return h + ':' + (m < 10 ? '0' : '') + m;
  };

})(window.AE = window.AE || {});
