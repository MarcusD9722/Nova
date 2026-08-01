/* Aetherlings — the Verdane region.

   Maps are built from declarative paint ops rather than raw character grids: it
   keeps them short, and it means a typo produces a wrong-looking map rather than
   a ragged array. NPCs and dialogue live in data-story.js so this file stays
   purely geography. ?test=1 walks every warp and checks it lands somewhere real. */
(function (AE) {
  'use strict';

  var MAPS = {};
  AE.MAPS = MAPS;

  /* ---------------- builder ---------------- */
  function blank(w, h, ch) {
    var g = [];
    for (var y = 0; y < h; y++) {
      var row = [];
      for (var x = 0; x < w; x++) row.push(ch);
      g.push(row);
    }
    return g;
  }

  function inb(m, x, y) { return x >= 0 && y >= 0 && x < m.w && y < m.h; }

  var OPS = {
    rect: function (m, a) {
      for (var y = a[2]; y < a[2] + a[4]; y++)
        for (var x = a[1]; x < a[1] + a[3]; x++)
          if (inb(m, x, y)) m.grid[y][x] = a[5];
    },
    outline: function (m, a) {
      for (var y = a[2]; y < a[2] + a[4]; y++)
        for (var x = a[1]; x < a[1] + a[3]; x++)
          if (inb(m, x, y) && (x === a[1] || y === a[2] || x === a[1] + a[3] - 1 || y === a[2] + a[4] - 1))
            m.grid[y][x] = a[5];
    },
    hline: function (m, a) { OPS.rect(m, ['rect', a[1], a[2], a[3], 1, a[4]]); },
    vline: function (m, a) { OPS.rect(m, ['rect', a[1], a[2], 1, a[3], a[4]]); },
    put: function (m, a) { if (inb(m, a[1], a[2])) m.grid[a[2]][a[1]] = a[3]; },
    /* Scatter: deterministic from a seed so a map looks the same every load. */
    scatter: function (m, a) {
      var rnd = AE.seeded(a[6] || 1);
      for (var y = a[2]; y < a[2] + a[4]; y++)
        for (var x = a[1]; x < a[1] + a[3]; x++)
          if (inb(m, x, y) && rnd() < (a[7] || 0.25)) m.grid[y][x] = a[5];
    },
    /* Building: solid block with a door on the bottom edge. */
    building: function (m, a) {
      OPS.rect(m, ['rect', a[1], a[2], a[3], a[4], 'H']);
      OPS.put(m, ['put', a[5], a[2] + a[4] - 1, 'D']);
    },
    border: function (m, a) {
      var t = a[2] || 1;
      for (var y = 0; y < m.h; y++)
        for (var x = 0; x < m.w; x++)
          if (x < t || y < t || x >= m.w - t || y >= m.h - t) m.grid[y][x] = a[1];
    }
  };

  function makeMap(id, o) {
    var m = {
      id: id,
      name: o.name,
      w: o.w, h: o.h,
      grid: blank(o.w, o.h, o.base || '.'),
      warps: {},
      enc: o.enc || null,
      water: o.water || null,
      indoor: !!o.indoor,
      music: o.music || null,
      dark: !!o.dark
    };
    (o.paint || []).forEach(function (op) {
      var fn = OPS[op[0]];
      if (!fn) throw new Error('Unknown paint op "' + op[0] + '" in map ' + id);
      fn(m, op);
    });
    (o.warps || []).forEach(function (w) {
      m.warps[w[0] + ',' + w[1]] = { to: w[2], tx: w[3], ty: w[4], dir: w[5] || 's' };
      carveWarp(m, w[0], w[1]);
    });
    MAPS[id] = m;
    return m;
  }

  /* A warp tile must be standable, and reachable from inside the map. Border
     painting happily buries edge exits, so every warp carves its own way in
     rather than relying on each map's paint list getting it right. */
  function carveWarp(m, x, y) {
    if (!inb(m, x, y)) return;
    var floor = m.indoor ? '-' : (m.grid[y][x] === 'x' || m.base === 'x' ? 'x' : '=');
    if (m.grid[y][x] !== 'D' && AE.isSolid(m.grid[y][x])) m.grid[y][x] = floor;

    var dx = x < 2 ? 1 : x > m.w - 3 ? -1 : 0;
    var dy = y < 2 ? 1 : y > m.h - 3 ? -1 : 0;
    if (!dx && !dy) return;
    for (var i = 1; i <= 2; i++) {
      var nx = x + dx * i, ny = y + dy * i;
      if (!inb(m, nx, ny)) break;
      if (m.grid[ny][nx] !== 'D' && AE.isSolid(m.grid[ny][nx])) m.grid[ny][nx] = floor;
    }
  }
  AE.makeMap = makeMap;

  AE.map = function (id) {
    var m = MAPS[id];
    if (!m) throw new Error('Unknown map: ' + id);
    return m;
  };

  AE.tileAt = function (m, x, y) {
    if (x < 0 || y < 0 || x >= m.w || y >= m.h) return '#';
    return m.grid[y][x];
  };

  AE.warpAt = function (m, x, y) { return m.warps[x + ',' + y] || null; };

  /* Encounter table entry helper: species, min level, max level, weight. */
  function e(sp, min, max, w) { return { sp: sp, min: min, max: max, w: w || 10 }; }
  function table(rate, list) { return { rate: rate, table: list }; }

  /* =====================================================================
     Interior templates — every town has the same two buildings, so they are
     generated rather than hand-drawn twenty times.
     ===================================================================== */

  /* Hearth & Supply: healer counter on the left, shop counter on the right. */
  function hearth(id, townName, backTo, bx, by) {
    return makeMap(id, {
      name: townName + ' Hearth', w: 15, h: 11, base: '-', indoor: true,
      paint: [
        ['border', '|', 1],
        ['rect', 2, 2, 4, 2, 'T'],
        ['put', 2, 1, 'P'],
        ['rect', 9, 2, 4, 2, 'T'],
        ['rect', 6, 7, 3, 2, 'c']
      ],
      warps: [[7, 10, backTo, bx, by, 's'], [6, 10, backTo, bx, by, 's']]
    });
  }

  /* Sanctum: entrance at the bottom, two guard trainers, the Warden at the top. */
  function sanctum(id, name, backTo, bx, by, deco) {
    return makeMap(id, {
      name: name, w: 15, h: 17, base: '-', indoor: true,
      paint: [
        ['border', '|', 1],
        ['rect', 5, 1, 5, 3, 'c'],
        ['rect', 2, 6, 3, 3, deco || 'T'],
        ['rect', 10, 6, 3, 3, deco || 'T'],
        ['rect', 2, 11, 3, 2, deco || 'T'],
        ['rect', 10, 11, 3, 2, deco || 'T'],
        ['rect', 6, 14, 3, 1, 'c']
      ],
      warps: [[7, 16, backTo, bx, by, 's'], [6, 16, backTo, bx, by, 's']]
    });
  }

  /* =====================================================================
     1. Willowmere — home village
     ===================================================================== */
  makeMap('willowmere', {
    name: 'Willowmere', w: 30, h: 24,
    paint: [
      ['border', '#', 2],
      ['scatter', 2, 2, 26, 20, 'F', 7, 0.05],
      ['rect', 2, 20, 26, 2, '#'],
      /* main path: a T running north from the south gate */
      ['vline', 14, 3, 19, '='],
      ['hline', 5, 11, 20, '='],
      /* player's home + rival's home */
      ['building', 5, 5, 5, 4, 7],
      ['building', 18, 5, 5, 4, 20],
      /* professor's lab, larger */
      ['building', 9, 14, 7, 5, 12],
      /* hearth */
      ['building', 20, 13, 5, 4, 22],
      ['put', 4, 11, 'S'],
      ['put', 17, 11, 'S'],
      /* pond */
      ['rect', 3, 15, 4, 4, '~'],
      ['rect', 24, 4, 4, 3, ','],
      ['hline', 12, 3, 5, ',']
    ],
    warps: [
      [7, 8, 'home-willowmere', 6, 8, 's'],
      [20, 8, 'rival-home', 6, 8, 's'],
      [12, 18, 'lab-willowmere', 8, 12, 's'],
      [22, 16, 'hearth-willowmere', 7, 9, 's'],
      [14, 2, 'route1', 12, 32, 'n'],
      [14, 1, 'route1', 12, 32, 'n']
    ],
    enc: table(12, [e(10, 2, 3, 40), e(17, 2, 3, 35), e(12, 3, 4, 25)])
  });

  makeMap('home-willowmere', {
    name: 'Your House', w: 13, h: 10, base: '-', indoor: true,
    paint: [
      ['border', '|', 1],
      ['rect', 2, 2, 3, 2, 'T'],
      ['rect', 8, 2, 3, 3, 'c'],
      ['rect', 2, 6, 2, 2, 'T']
    ],
    warps: [[6, 9, 'willowmere', 7, 9, 's'], [7, 9, 'willowmere', 7, 9, 's']]
  });

  makeMap('rival-home', {
    name: 'Ferren\'s House', w: 13, h: 10, base: '-', indoor: true,
    paint: [
      ['border', '|', 1],
      ['rect', 8, 2, 3, 2, 'T'],
      ['rect', 2, 2, 3, 3, 'c']
    ],
    warps: [[6, 9, 'willowmere', 20, 9, 's'], [7, 9, 'willowmere', 20, 9, 's']]
  });

  makeMap('lab-willowmere', {
    name: 'Rowan\'s Laboratory', w: 17, h: 14, base: '-', indoor: true,
    paint: [
      ['border', '|', 1],
      ['rect', 2, 2, 5, 2, 'T'],
      ['rect', 10, 2, 5, 2, 'T'],
      ['rect', 2, 6, 3, 4, 'T'],
      ['rect', 12, 6, 3, 4, 'T'],
      ['rect', 7, 4, 3, 2, 'c']
    ],
    warps: [[8, 13, 'willowmere', 12, 19, 's'], [7, 13, 'willowmere', 12, 19, 's']]
  });

  hearth('hearth-willowmere', 'Willowmere', 'willowmere', 22, 17);

  /* =====================================================================
     Route 1 — Willowmere to Thornhollow (north)
     ===================================================================== */
  makeMap('route1', {
    name: 'Route 1', w: 26, h: 34,
    paint: [
      ['border', '#', 2],
      /* Scatter first: paths and grass patches painted after it always win,
         so a stray tree can never land on a route's road or an NPC. */
      ['scatter', 3, 3, 20, 28, '#', 11, 0.06],
      ['rect', 4, 6, 7, 5, ','],
      ['rect', 15, 12, 8, 6, ','],
      ['rect', 4, 20, 6, 6, ','],
      ['rect', 16, 25, 6, 5, ','],
      ['vline', 12, 2, 31, '='],
      ['hline', 8, 17, 8, '='],
      ['put', 10, 30, 'S'],
      ['put', 14, 9, 'S'],
      ['hline', 9, 22, 7, '^'],
      ['rect', 20, 4, 4, 4, '~']
    ],
    warps: [
      [12, 33, 'willowmere', 14, 3, 's'],
      [12, 32, 'willowmere', 14, 3, 's'],
      [12, 1, 'thornhollow', 14, 25, 'n'],
      [12, 0, 'thornhollow', 14, 25, 'n']
    ],
    enc: table(18, [e(10, 2, 4, 30), e(12, 2, 4, 30), e(17, 3, 5, 25), e(14, 3, 4, 15)])
  });

  /* =====================================================================
     2. Thornhollow — Verdant Sanctum
     ===================================================================== */
  makeMap('thornhollow', {
    name: 'Thornhollow', w: 30, h: 28,
    paint: [
      ['border', '#', 2],
      ['scatter', 3, 3, 24, 22, 'F', 21, 0.07],
      ['vline', 14, 3, 23, '='],
      ['hline', 4, 12, 22, '='],
      ['hline', 4, 20, 22, '='],
      ['building', 4, 8, 5, 4, 6],
      ['building', 20, 8, 6, 4, 22],
      ['building', 5, 16, 5, 4, 7],
      ['building', 19, 15, 7, 5, 22],
      ['put', 12, 12, 'S'],
      ['put', 16, 20, 'S'],
      ['rect', 3, 22, 5, 3, ','],
      ['rect', 23, 22, 4, 3, ','],
      ['rect', 10, 4, 5, 3, 'B']
    ],
    warps: [
      [6, 11, 'hearth-thornhollow', 7, 9, 's'],
      [22, 11, 'shop-thorn-house', 6, 8, 's'],
      [7, 19, 'thorn-house', 6, 8, 's'],
      [22, 19, 'sanctum-thornhollow', 7, 15, 's'],
      [14, 26, 'route1', 12, 1, 's'],
      [14, 27, 'route1', 12, 1, 's'],
      [29, 12, 'route2', 1, 9, 'e'],
      [28, 12, 'route2', 1, 9, 'e']
    ],
    enc: table(10, [e(17, 4, 6, 50), e(10, 4, 6, 50)])
  });

  hearth('hearth-thornhollow', 'Thornhollow', 'thornhollow', 6, 12);
  sanctum('sanctum-thornhollow', 'Verdant Sanctum', 'thornhollow', 22, 20, 'B');

  makeMap('thorn-house', {
    name: 'Thornhollow Home', w: 13, h: 10, base: '-', indoor: true,
    paint: [['border', '|', 1], ['rect', 2, 2, 3, 2, 'T'], ['rect', 8, 3, 3, 2, 'c']],
    warps: [[6, 9, 'thornhollow', 7, 20, 's'], [7, 9, 'thornhollow', 7, 20, 's']]
  });

  makeMap('shop-thorn-house', {
    name: 'Thornhollow Supply', w: 13, h: 10, base: '-', indoor: true,
    paint: [['border', '|', 1], ['rect', 3, 2, 7, 2, 'T']],
    warps: [[6, 9, 'thornhollow', 22, 12, 's'], [7, 9, 'thornhollow', 22, 12, 's']]
  });

  /* =====================================================================
     Route 2 — Thornhollow to Cinderfall (east), with the Emberdeep entrance
     ===================================================================== */
  makeMap('route2', {
    name: 'Route 2', w: 44, h: 20,
    paint: [
      ['border', '#', 2],
      ['scatter', 3, 3, 38, 14, '#', 31, 0.05],
      ['rect', 6, 3, 8, 5, ','],
      ['rect', 18, 12, 9, 5, ','],
      ['rect', 31, 4, 8, 5, ','],
      ['hline', 1, 9, 42, '='],
      ['vline', 25, 3, 7, '='],
      ['put', 8, 10, 'S'],
      ['put', 30, 8, 'S'],
      ['rect', 24, 2, 3, 2, 'M'],
      ['put', 25, 3, 'D'],
      ['rect', 12, 14, 5, 4, 'R']
    ],
    warps: [
      [0, 9, 'thornhollow', 28, 12, 'w'],
      [1, 9, 'thornhollow', 28, 12, 'w'],
      [43, 9, 'cinderfall', 2, 14, 'e'],
      [42, 9, 'cinderfall', 2, 14, 'e'],
      [25, 3, 'emberdeep', 10, 26, 'n']
    ],
    enc: table(20, [e(10, 6, 8, 20), e(12, 6, 9, 20), e(14, 6, 8, 20), e(17, 7, 9, 20), e(32, 7, 9, 20)])
  });

  makeMap('emberdeep', {
    name: 'Emberdeep Cavern', w: 22, h: 28, base: 'x', dark: true,
    paint: [
      ['border', 'X', 2],
      ['scatter', 2, 2, 18, 24, 'X', 41, 0.16],
      ['vline', 10, 2, 25, 'x'],
      ['hline', 4, 8, 14, 'x'],
      ['hline', 4, 16, 14, 'x'],
      ['rect', 3, 4, 5, 3, 'x'],
      ['rect', 14, 20, 5, 4, 'x'],
      ['rect', 4, 20, 4, 3, 'R'],
      ['rect', 15, 5, 4, 3, 'x'],
      ['put', 10, 26, 'x'], ['put', 10, 27, 'x']
    ],
    warps: [[10, 27, 'route2', 25, 4, 's'], [10, 26, 'route2', 25, 4, 's']],
    /* Caves roll on every floor tile, so the rate is lower than a grass route's. */
    enc: table(11, [e(20, 10, 13, 30), e(35, 10, 13, 25), e(32, 10, 12, 25), e(30, 11, 14, 20)])
  });

  /* =====================================================================
     3. Cinderfall — Ember Sanctum
     ===================================================================== */
  makeMap('cinderfall', {
    name: 'Cinderfall', w: 30, h: 26, base: 'a',
    paint: [
      ['border', 'M', 2],
      ['scatter', 3, 3, 24, 20, 'A', 51, 0.06],
      ['vline', 14, 3, 21, '='],
      ['hline', 3, 14, 24, '='],
      ['hline', 3, 7, 24, '='],
      ['building', 4, 9, 6, 4, 6],
      ['building', 20, 9, 6, 4, 22],
      ['building', 5, 17, 5, 4, 7],
      ['building', 18, 16, 8, 5, 21],
      ['put', 12, 14, 'S'],
      ['put', 17, 7, 'S'],
      ['rect', 3, 3, 4, 3, 'A'],
      ['rect', 24, 22, 4, 2, 'A']
    ],
    warps: [
      [6, 12, 'hearth-cinderfall', 7, 9, 's'],
      [22, 12, 'cinder-house', 6, 8, 's'],
      [7, 20, 'cinder-shop', 6, 8, 's'],
      [21, 20, 'sanctum-cinderfall', 7, 15, 's'],
      [1, 14, 'route2', 42, 9, 'w'],
      [2, 14, 'route2', 42, 9, 'w'],
      [14, 25, 'route3', 12, 1, 's'],
      [14, 24, 'route3', 12, 1, 's']
    ],
    enc: table(8, [e(30, 10, 12, 50), e(35, 10, 12, 50)])
  });

  hearth('hearth-cinderfall', 'Cinderfall', 'cinderfall', 6, 13);
  sanctum('sanctum-cinderfall', 'Ember Sanctum', 'cinderfall', 21, 21, 'M');

  makeMap('cinder-house', {
    name: 'Cinderfall Home', w: 13, h: 10, base: '-', indoor: true,
    paint: [['border', '|', 1], ['rect', 8, 2, 3, 2, 'T'], ['rect', 2, 3, 3, 2, 'c']],
    warps: [[6, 9, 'cinderfall', 22, 13, 's'], [7, 9, 'cinderfall', 22, 13, 's']]
  });

  makeMap('cinder-shop', {
    name: 'Cinderfall Supply', w: 13, h: 10, base: '-', indoor: true,
    paint: [['border', '|', 1], ['rect', 3, 2, 7, 2, 'T']],
    warps: [[6, 9, 'cinderfall', 7, 21, 's'], [7, 9, 'cinderfall', 7, 21, 's']]
  });

  /* =====================================================================
     Route 3 — Cinderfall south to Brackwater, with Mirewood off to the side
     ===================================================================== */
  makeMap('route3', {
    name: 'Route 3', w: 26, h: 32,
    paint: [
      ['border', '#', 2],
      ['scatter', 3, 3, 20, 26, '#', 61, 0.05],
      ['rect', 4, 5, 7, 6, ','],
      ['rect', 15, 13, 8, 6, ','],
      ['rect', 4, 22, 7, 6, ','],
      ['vline', 12, 1, 30, '='],
      ['hline', 4, 18, 8, '='],
      ['rect', 2, 16, 3, 3, 'B'],
      ['hline', 8, 26, 8, '^'],
      ['put', 14, 6, 'S'],
      ['put', 10, 20, 'S'],
      ['rect', 18, 3, 5, 5, '~']
    ],
    warps: [
      [12, 0, 'cinderfall', 14, 23, 'n'],
      [12, 1, 'cinderfall', 14, 23, 'n'],
      [12, 31, 'brackwater', 14, 3, 's'],
      [12, 30, 'brackwater', 14, 3, 's'],
      [2, 18, 'mirewood', 20, 18, 'w'],
      [3, 18, 'mirewood', 20, 18, 'w']
    ],
    enc: table(20, [e(17, 12, 15, 20), e(16, 13, 16, 15), e(34, 13, 16, 20), e(28, 12, 15, 20), e(32, 12, 15, 25)])
  });

  makeMap('mirewood', {
    name: 'Mirewood', w: 24, h: 26, base: ',',
    paint: [
      ['border', '#', 2],
      ['scatter', 2, 2, 20, 22, '#', 71, 0.1],
      ['rect', 4, 6, 6, 5, '~'],
      ['rect', 13, 14, 7, 6, '~'],
      ['rect', 5, 18, 5, 4, '~'],
      ['hline', 3, 18, 18, '='],
      ['vline', 20, 4, 16, '='],
      ['put', 18, 8, 'S'],
      ['rect', 15, 4, 4, 4, 'B']
    ],
    warps: [[21, 18, 'route3', 4, 18, 'e'], [22, 18, 'route3', 4, 18, 'e']],
    enc: table(24, [e(28, 14, 17, 30), e(16, 15, 18, 25), e(32, 14, 17, 25), e(26, 15, 18, 20)]),
    water: table(18, [e(28, 15, 20, 60), e(7, 15, 18, 40)])
  });

  /* =====================================================================
     4. Brackwater — Tide Sanctum, coastal
     ===================================================================== */
  makeMap('brackwater', {
    name: 'Brackwater', w: 32, h: 26, base: ':',
    paint: [
      ['border', '#', 2],
      ['rect', 2, 18, 28, 6, '~'],
      ['hline', 2, 17, 28, ':'],
      ['vline', 14, 2, 15, '='],
      ['hline', 3, 10, 26, '='],
      ['building', 4, 5, 6, 4, 6],
      ['building', 20, 5, 6, 4, 22],
      ['building', 4, 12, 6, 4, 6],
      ['building', 19, 12, 8, 5, 22],
      ['rect', 12, 18, 4, 5, 'b'],
      ['put', 12, 10, 'S'],
      ['put', 17, 17, 'S']
    ],
    warps: [
      [6, 8, 'hearth-brackwater', 7, 9, 's'],
      [22, 8, 'brack-shop', 6, 8, 's'],
      [6, 15, 'brack-house', 6, 8, 's'],
      [22, 16, 'sanctum-brackwater', 7, 15, 's'],
      [14, 2, 'route3', 12, 30, 'n'],
      [14, 1, 'route3', 12, 30, 'n'],
      [31, 10, 'route4', 1, 10, 'e'],
      [30, 10, 'route4', 1, 10, 'e']
    ],
    water: table(16, [e(43, 16, 20, 40), e(28, 16, 19, 30), e(7, 15, 18, 30)])
  });

  hearth('hearth-brackwater', 'Brackwater', 'brackwater', 6, 9);
  sanctum('sanctum-brackwater', 'Tide Sanctum', 'brackwater', 22, 17, '~');

  makeMap('brack-house', {
    name: 'Brackwater Home', w: 13, h: 10, base: '-', indoor: true,
    paint: [['border', '|', 1], ['rect', 2, 2, 3, 2, 'T'], ['rect', 8, 3, 3, 2, 'c']],
    warps: [[6, 9, 'brackwater', 6, 16, 's'], [7, 9, 'brackwater', 6, 16, 's']]
  });

  makeMap('brack-shop', {
    name: 'Brackwater Supply', w: 13, h: 10, base: '-', indoor: true,
    paint: [['border', '|', 1], ['rect', 3, 2, 7, 2, 'T']],
    warps: [[6, 9, 'brackwater', 22, 9, 's'], [7, 9, 'brackwater', 22, 9, 's']]
  });

  /* =====================================================================
     Route 4 — Brackwater to Stormreach (east)
     ===================================================================== */
  makeMap('route4', {
    name: 'Route 4', w: 42, h: 22,
    paint: [
      ['border', '#', 2],
      ['scatter', 3, 3, 36, 16, '#', 81, 0.05],
      ['rect', 5, 3, 9, 6, ','],
      ['rect', 20, 13, 10, 6, ','],
      ['rect', 32, 3, 7, 6, ','],
      ['hline', 1, 10, 40, '='],
      ['rect', 16, 3, 6, 5, '~'],
      ['rect', 16, 8, 2, 3, 'b'],
      ['put', 12, 11, 'S'],
      ['put', 28, 9, 'S'],
      ['rect', 24, 3, 4, 4, 'C'],
      ['hline', 30, 14, 6, '^']
    ],
    warps: [
      [0, 10, 'brackwater', 30, 10, 'w'],
      [1, 10, 'brackwater', 30, 10, 'w'],
      [41, 10, 'stormreach', 14, 25, 'e'],
      [40, 10, 'stormreach', 14, 25, 'e']
    ],
    enc: table(20, [e(23, 16, 19, 25), e(12, 16, 19, 20), e(13, 18, 20, 10), e(43, 17, 20, 20), e(34, 17, 20, 25)])
  });

  /* =====================================================================
     5. Stormreach — Storm Sanctum, high plains
     ===================================================================== */
  makeMap('stormreach', {
    name: 'Stormreach', w: 30, h: 28,
    paint: [
      ['border', 'M', 2],
      ['scatter', 3, 3, 24, 22, 'F', 91, 0.05],
      ['vline', 14, 3, 23, '='],
      ['hline', 3, 9, 24, '='],
      ['hline', 3, 18, 24, '='],
      ['building', 4, 5, 6, 4, 6],
      ['building', 20, 5, 6, 4, 22],
      ['building', 4, 14, 6, 4, 6],
      ['building', 18, 13, 8, 5, 21],
      ['put', 12, 9, 'S'],
      ['put', 17, 18, 'S'],
      ['rect', 3, 21, 6, 4, ','],
      ['rect', 22, 21, 5, 4, ',']
    ],
    warps: [
      [6, 8, 'hearth-stormreach', 7, 9, 's'],
      [22, 8, 'storm-shop', 6, 8, 's'],
      [6, 17, 'storm-house', 6, 8, 's'],
      [21, 17, 'sanctum-stormreach', 7, 15, 's'],
      [14, 26, 'route4', 40, 10, 's'],
      [14, 27, 'route4', 40, 10, 's'],
      [14, 2, 'route5', 12, 32, 'n'],
      [14, 1, 'route5', 12, 32, 'n']
    ],
    enc: table(10, [e(23, 18, 21, 60), e(10, 18, 20, 40)])
  });

  hearth('hearth-stormreach', 'Stormreach', 'stormreach', 6, 9);
  sanctum('sanctum-stormreach', 'Storm Sanctum', 'stormreach', 21, 18, 'P');

  makeMap('storm-house', {
    name: 'Stormreach Home', w: 13, h: 10, base: '-', indoor: true,
    paint: [['border', '|', 1], ['rect', 2, 2, 3, 2, 'T'], ['rect', 8, 3, 3, 2, 'c']],
    warps: [[6, 9, 'stormreach', 6, 18, 's'], [7, 9, 'stormreach', 6, 18, 's']]
  });

  makeMap('storm-shop', {
    name: 'Stormreach Supply', w: 13, h: 10, base: '-', indoor: true,
    paint: [['border', '|', 1], ['rect', 3, 2, 7, 2, 'T']],
    warps: [[6, 9, 'stormreach', 22, 9, 's'], [7, 9, 'stormreach', 22, 9, 's']]
  });

  /* =====================================================================
     Route 5 — Stormreach north to Gravemoor
     ===================================================================== */
  makeMap('route5', {
    name: 'Route 5', w: 26, h: 34,
    paint: [
      ['border', '#', 2],
      ['scatter', 3, 3, 20, 28, '#', 101, 0.06],
      ['rect', 4, 5, 7, 7, ','],
      ['rect', 15, 14, 8, 7, ','],
      ['rect', 4, 24, 7, 6, ','],
      ['vline', 12, 1, 32, '='],
      ['hline', 5, 21, 8, '='],
      ['rect', 16, 5, 5, 5, 'C'],
      ['rect', 4, 16, 4, 3, 'R'],
      ['put', 10, 28, 'S'],
      ['put', 14, 12, 'S'],
      ['hline', 14, 25, 7, '^']
    ],
    warps: [
      [12, 33, 'stormreach', 14, 3, 's'],
      [12, 32, 'stormreach', 14, 3, 's'],
      [12, 0, 'gravemoor', 14, 25, 'n'],
      [12, 1, 'gravemoor', 14, 25, 'n']
    ],
    enc: table(20, [e(23, 20, 23, 20), e(39, 20, 23, 25), e(26, 20, 23, 20), e(20, 21, 24, 20), e(21, 22, 24, 15)])
  });

  /* =====================================================================
     6. Gravemoor — Umbra Sanctum
     ===================================================================== */
  makeMap('gravemoor', {
    name: 'Gravemoor', w: 30, h: 28,
    paint: [
      ['border', '#', 2],
      ['scatter', 3, 3, 24, 22, ',', 111, 0.08],
      ['vline', 14, 3, 23, '='],
      ['hline', 3, 12, 24, '='],
      ['hline', 3, 20, 24, '='],
      ['building', 4, 8, 6, 4, 6],
      ['building', 20, 8, 6, 4, 22],
      ['building', 4, 16, 6, 4, 6],
      ['building', 18, 15, 8, 5, 21],
      ['put', 12, 12, 'S'],
      ['put', 17, 20, 'S'],
      ['rect', 10, 4, 3, 3, 'R'],
      ['rect', 20, 23, 5, 3, ',']
    ],
    warps: [
      [6, 11, 'hearth-gravemoor', 7, 9, 's'],
      [22, 11, 'grave-shop', 6, 8, 's'],
      [6, 19, 'grave-house', 6, 8, 's'],
      [21, 19, 'sanctum-gravemoor', 7, 15, 's'],
      [14, 26, 'route5', 12, 1, 's'],
      [14, 27, 'route5', 12, 1, 's'],
      [29, 12, 'route6', 1, 10, 'e'],
      [28, 12, 'route6', 1, 10, 'e']
    ],
    enc: table(12, [e(39, 22, 25, 50), e(26, 22, 25, 50)])
  });

  hearth('hearth-gravemoor', 'Gravemoor', 'gravemoor', 6, 12);
  sanctum('sanctum-gravemoor', 'Umbra Sanctum', 'gravemoor', 21, 20, 'X');

  makeMap('grave-house', {
    name: 'Gravemoor Home', w: 13, h: 10, base: '-', indoor: true,
    paint: [['border', '|', 1], ['rect', 2, 2, 3, 2, 'T'], ['rect', 8, 3, 3, 2, 'c']],
    warps: [[6, 9, 'gravemoor', 6, 20, 's'], [7, 9, 'gravemoor', 6, 20, 's']]
  });

  makeMap('grave-shop', {
    name: 'Gravemoor Supply', w: 13, h: 10, base: '-', indoor: true,
    paint: [['border', '|', 1], ['rect', 3, 2, 7, 2, 'T']],
    warps: [[6, 9, 'gravemoor', 22, 12, 's'], [7, 9, 'gravemoor', 22, 12, 's']]
  });

  /* =====================================================================
     Route 6 — Gravemoor to Ironhold (east), Hollow Spire branch
     ===================================================================== */
  makeMap('route6', {
    name: 'Route 6', w: 44, h: 22,
    paint: [
      ['border', '#', 2],
      ['scatter', 3, 3, 38, 16, '#', 121, 0.05],
      ['rect', 6, 3, 9, 6, ','],
      ['rect', 20, 13, 10, 6, ','],
      ['rect', 33, 3, 7, 6, ','],
      ['hline', 1, 10, 42, '='],
      ['vline', 30, 3, 8, '='],
      ['rect', 29, 2, 3, 2, 'M'],
      ['put', 30, 3, 'D'],
      ['rect', 14, 13, 4, 4, 'R'],
      ['put', 12, 11, 'S'],
      ['put', 26, 9, 'S']
    ],
    warps: [
      [0, 10, 'gravemoor', 28, 12, 'w'],
      [1, 10, 'gravemoor', 28, 12, 'w'],
      [43, 10, 'ironhold', 2, 14, 'e'],
      [42, 10, 'ironhold', 2, 14, 'e'],
      [30, 3, 'hollowspire', 11, 28, 'n']
    ],
    enc: table(20, [e(39, 24, 27, 20), e(37, 24, 27, 20), e(21, 24, 27, 20), e(33, 25, 28, 20), e(34, 25, 27, 20)])
  });

  makeMap('hollowspire', {
    name: 'Hollow Spire', w: 24, h: 30, base: 'x', dark: true,
    paint: [
      ['border', 'X', 2],
      ['scatter', 2, 2, 20, 26, 'X', 131, 0.14],
      ['vline', 11, 2, 27, 'x'],
      ['hline', 4, 7, 16, 'x'],
      ['hline', 4, 14, 16, 'x'],
      ['hline', 4, 21, 16, 'x'],
      ['rect', 4, 3, 5, 4, 'x'],
      ['rect', 15, 3, 5, 4, 'x'],
      ['rect', 16, 23, 5, 4, 'x'],
      ['rect', 4, 17, 4, 3, 'R'],
      ['put', 11, 28, 'x'], ['put', 11, 29, 'x']
    ],
    warps: [[11, 29, 'route6', 30, 4, 's'], [11, 28, 'route6', 30, 4, 's']],
    enc: table(11, [e(53, 26, 29, 15), e(50, 27, 30, 25), e(27, 27, 30, 30), e(52, 28, 32, 30)])
  });

  /* =====================================================================
     7. Ironhold — Iron Sanctum
     ===================================================================== */
  makeMap('ironhold', {
    name: 'Ironhold', w: 30, h: 26, base: '=',
    paint: [
      ['border', 'M', 2],
      ['vline', 14, 3, 21, '='],
      ['hline', 3, 14, 24, '='],
      ['hline', 3, 7, 24, '='],
      ['building', 4, 9, 6, 4, 6],
      ['building', 20, 9, 6, 4, 22],
      ['building', 5, 17, 5, 4, 7],
      ['building', 18, 16, 8, 5, 21],
      ['put', 12, 14, 'S'],
      ['put', 17, 7, 'S'],
      ['rect', 3, 3, 5, 3, 'R'],
      ['rect', 24, 22, 4, 2, 'R']
    ],
    warps: [
      [6, 12, 'hearth-ironhold', 7, 9, 's'],
      [22, 12, 'iron-shop', 6, 8, 's'],
      [7, 20, 'iron-house', 6, 8, 's'],
      [21, 20, 'sanctum-ironhold', 7, 15, 's'],
      [1, 14, 'route6', 42, 10, 'w'],
      [2, 14, 'route6', 42, 10, 'w'],
      [14, 25, 'route7', 12, 1, 's'],
      [14, 24, 'route7', 12, 1, 's']
    ]
  });

  hearth('hearth-ironhold', 'Ironhold', 'ironhold', 6, 13);
  sanctum('sanctum-ironhold', 'Iron Sanctum', 'ironhold', 21, 21, 'T');

  makeMap('iron-house', {
    name: 'Ironhold Home', w: 13, h: 10, base: '-', indoor: true,
    paint: [['border', '|', 1], ['rect', 2, 2, 3, 2, 'T'], ['rect', 8, 3, 3, 2, 'c']],
    warps: [[6, 9, 'ironhold', 7, 21, 's'], [7, 9, 'ironhold', 7, 21, 's']]
  });

  makeMap('iron-shop', {
    name: 'Ironhold Supply', w: 13, h: 10, base: '-', indoor: true,
    paint: [['border', '|', 1], ['rect', 3, 2, 7, 2, 'T']],
    warps: [[6, 9, 'ironhold', 22, 13, 's'], [7, 9, 'ironhold', 22, 13, 's']]
  });

  /* =====================================================================
     Route 7 — Ironhold north to Frostvale, Ashen Hand hideout branch
     ===================================================================== */
  makeMap('route7', {
    name: 'Route 7', w: 26, h: 34,
    paint: [
      ['border', '#', 2],
      ['scatter', 3, 3, 20, 28, '#', 141, 0.06],
      ['rect', 4, 6, 7, 6, ','],
      ['rect', 15, 15, 8, 6, ','],
      ['rect', 4, 25, 7, 5, ','],
      ['vline', 12, 1, 32, '='],
      ['hline', 5, 12, 8, '='],
      ['rect', 3, 10, 3, 3, 'M'],
      ['put', 4, 12, 'D'],
      ['rect', 16, 25, 5, 4, 'C'],
      ['put', 14, 18, 'S'],
      ['put', 10, 30, 'S'],
      ['hline', 14, 8, 7, '^']
    ],
    warps: [
      [12, 33, 'ironhold', 14, 23, 's'],
      [12, 32, 'ironhold', 14, 23, 's'],
      [12, 0, 'frostvale', 14, 25, 'n'],
      [12, 1, 'frostvale', 14, 25, 'n'],
      [4, 12, 'ashen-hideout', 10, 20, 'w']
    ],
    enc: table(20, [e(37, 28, 31, 20), e(21, 28, 31, 20), e(40, 29, 32, 15), e(49, 29, 32, 15), e(39, 28, 30, 30)])
  });

  makeMap('ashen-hideout', {
    name: 'Ashen Hand Hideout', w: 22, h: 22, base: '-', indoor: true,
    paint: [
      ['border', '|', 1],
      ['rect', 3, 3, 4, 3, 'T'],
      ['rect', 15, 3, 4, 3, 'T'],
      ['rect', 3, 10, 3, 4, 'T'],
      ['rect', 16, 10, 3, 4, 'T'],
      ['rect', 8, 3, 6, 3, 'c'],
      ['rect', 9, 16, 4, 2, 'c'],
      ['vline', 10, 6, 10, '-']
    ],
    warps: [[10, 21, 'route7', 5, 12, 's'], [11, 21, 'route7', 5, 12, 's'],
            [10, 20, 'route7', 5, 12, 's']]
  });

  /* =====================================================================
     8. Frostvale — Frost Sanctum
     ===================================================================== */
  makeMap('frostvale', {
    name: 'Frostvale', w: 30, h: 28, base: 'w',
    paint: [
      ['border', 'M', 2],
      ['scatter', 3, 3, 24, 22, 'W', 151, 0.07],
      ['vline', 14, 3, 23, '='],
      ['hline', 3, 12, 24, '='],
      ['hline', 3, 20, 24, '='],
      ['building', 4, 8, 6, 4, 6],
      ['building', 20, 8, 6, 4, 22],
      ['building', 4, 16, 6, 4, 6],
      ['building', 18, 15, 8, 5, 21],
      ['put', 12, 12, 'S'],
      ['put', 17, 20, 'S'],
      ['rect', 3, 23, 6, 3, 'W'],
      ['rect', 22, 23, 5, 3, 'W']
    ],
    warps: [
      [6, 11, 'hearth-frostvale', 7, 9, 's'],
      [22, 11, 'frost-shop', 6, 8, 's'],
      [6, 19, 'frost-house', 6, 8, 's'],
      [21, 19, 'sanctum-frostvale', 7, 15, 's'],
      [14, 26, 'route7', 12, 1, 's'],
      [14, 27, 'route7', 12, 1, 's'],
      [29, 12, 'route8', 1, 10, 'e'],
      [28, 12, 'route8', 1, 10, 'e']
    ],
    enc: table(12, [e(41, 30, 33, 50), e(45, 30, 33, 50)])
  });

  hearth('hearth-frostvale', 'Frostvale', 'frostvale', 6, 12);
  sanctum('sanctum-frostvale', 'Frost Sanctum', 'frostvale', 21, 20, 'W');

  makeMap('frost-house', {
    name: 'Frostvale Home', w: 13, h: 10, base: '-', indoor: true,
    paint: [['border', '|', 1], ['rect', 2, 2, 3, 2, 'T'], ['rect', 8, 3, 3, 2, 'c']],
    warps: [[6, 9, 'frostvale', 6, 20, 's'], [7, 9, 'frostvale', 6, 20, 's']]
  });

  makeMap('frost-shop', {
    name: 'Frostvale Supply', w: 13, h: 10, base: '-', indoor: true,
    paint: [['border', '|', 1], ['rect', 3, 2, 7, 2, 'T']],
    warps: [[6, 9, 'frostvale', 22, 12, 's'], [7, 9, 'frostvale', 22, 12, 's']]
  });

  /* =====================================================================
     Route 8 — Frostvale to Skyhaven (east), climbing
     ===================================================================== */
  makeMap('route8', {
    name: 'Route 8', w: 42, h: 22, base: 'w',
    paint: [
      ['border', 'M', 2],
      ['scatter', 3, 3, 36, 16, 'M', 161, 0.05],
      ['rect', 5, 3, 9, 6, 'W'],
      ['rect', 20, 13, 10, 6, 'W'],
      ['rect', 32, 3, 7, 6, 'W'],
      ['hline', 1, 10, 40, '='],
      ['rect', 17, 3, 4, 4, 'C'],
      ['rect', 26, 13, 4, 4, 'R'],
      ['put', 12, 11, 'S'],
      ['put', 30, 9, 'S']
    ],
    warps: [
      [0, 10, 'frostvale', 28, 12, 'w'],
      [1, 10, 'frostvale', 28, 12, 'w'],
      [41, 10, 'skyhaven', 14, 25, 'e'],
      [40, 10, 'skyhaven', 14, 25, 'e']
    ],
    enc: table(20, [e(41, 32, 35, 15), e(42, 33, 36, 20), e(45, 32, 35, 20), e(48, 33, 36, 20), e(46, 34, 36, 10), e(49, 33, 36, 15)])
  });

  /* =====================================================================
     9. Skyhaven — Gale Sanctum, on the heights
     ===================================================================== */
  makeMap('skyhaven', {
    name: 'Skyhaven', w: 30, h: 28,
    paint: [
      ['border', 'M', 2],
      ['scatter', 3, 3, 24, 22, 'F', 171, 0.06],
      ['vline', 14, 3, 23, '='],
      ['hline', 3, 10, 24, '='],
      ['hline', 3, 19, 24, '='],
      ['building', 4, 6, 6, 4, 6],
      ['building', 20, 6, 6, 4, 22],
      ['building', 4, 15, 6, 4, 6],
      ['building', 18, 14, 8, 5, 21],
      ['put', 12, 10, 'S'],
      ['put', 17, 19, 'S'],
      ['rect', 3, 22, 6, 3, ','],
      ['rect', 22, 22, 5, 3, ',']
    ],
    warps: [
      [6, 9, 'hearth-skyhaven', 7, 9, 's'],
      [22, 9, 'sky-shop', 6, 8, 's'],
      [6, 18, 'sky-house', 6, 8, 's'],
      [21, 18, 'sanctum-skyhaven', 7, 15, 's'],
      [14, 26, 'route8', 40, 10, 's'],
      [14, 27, 'route8', 40, 10, 's'],
      [14, 2, 'route9', 12, 32, 'n'],
      [14, 1, 'route9', 12, 32, 'n']
    ],
    enc: table(10, [e(13, 34, 37, 50), e(49, 34, 37, 50)])
  });

  hearth('hearth-skyhaven', 'Skyhaven', 'skyhaven', 6, 10);
  sanctum('sanctum-skyhaven', 'Gale Sanctum', 'skyhaven', 21, 19, 'C');

  makeMap('sky-house', {
    name: 'Skyhaven Home', w: 13, h: 10, base: '-', indoor: true,
    paint: [['border', '|', 1], ['rect', 2, 2, 3, 2, 'T'], ['rect', 8, 3, 3, 2, 'c']],
    warps: [[6, 9, 'skyhaven', 6, 19, 's'], [7, 9, 'skyhaven', 6, 19, 's']]
  });

  makeMap('sky-shop', {
    name: 'Skyhaven Supply', w: 13, h: 10, base: '-', indoor: true,
    paint: [['border', '|', 1], ['rect', 3, 2, 7, 2, 'T']],
    warps: [[6, 9, 'skyhaven', 22, 10, 's'], [7, 9, 'skyhaven', 22, 10, 's']]
  });

  /* =====================================================================
     Route 9 — the Conclave approach
     ===================================================================== */
  makeMap('route9', {
    name: 'Route 9', w: 26, h: 34,
    paint: [
      ['border', 'M', 2],
      ['scatter', 3, 3, 20, 28, 'M', 181, 0.05],
      ['rect', 4, 5, 7, 7, ','],
      ['rect', 15, 14, 8, 7, ','],
      ['rect', 4, 24, 7, 6, ','],
      ['vline', 12, 1, 32, '='],
      ['rect', 16, 5, 5, 5, 'C'],
      ['rect', 5, 17, 4, 3, 'R'],
      ['put', 14, 28, 'S'],
      ['put', 10, 12, 'S'],
      ['hline', 14, 22, 7, '^']
    ],
    warps: [
      [12, 33, 'skyhaven', 14, 3, 's'],
      [12, 32, 'skyhaven', 14, 3, 's'],
      [12, 0, 'aurel', 14, 25, 'n'],
      [12, 1, 'aurel', 14, 25, 'n']
    ],
    enc: table(20, [e(51, 36, 39, 20), e(47, 36, 39, 20), e(49, 36, 39, 15), e(54, 37, 40, 10), e(42, 37, 40, 20), e(52, 38, 41, 15)])
  });

  /* =====================================================================
     10. Aurel Citadel — the Conclave
     ===================================================================== */
  makeMap('aurel', {
    name: 'Aurel Citadel', w: 30, h: 28, base: '=',
    paint: [
      ['border', 'M', 2],
      ['rect', 3, 3, 24, 22, ':'],
      ['vline', 14, 3, 23, '='],
      ['hline', 3, 12, 24, '='],
      ['hline', 3, 20, 24, '='],
      ['building', 4, 8, 6, 4, 6],
      ['building', 20, 8, 6, 4, 22],
      ['building', 9, 3, 12, 7, 14],
      ['put', 12, 12, 'S'],
      ['put', 17, 20, 'S'],
      ['rect', 4, 22, 5, 2, 'F'],
      ['rect', 21, 22, 5, 2, 'F']
    ],
    warps: [
      [6, 11, 'hearth-aurel', 7, 9, 's'],
      [22, 11, 'aurel-shop', 6, 8, 's'],
      [14, 9, 'conclave', 9, 33, 'n'],
      [14, 26, 'route9', 12, 1, 's'],
      [14, 27, 'route9', 12, 1, 's']
    ]
  });

  hearth('hearth-aurel', 'Aurel', 'aurel', 6, 12);

  makeMap('aurel-shop', {
    name: 'Aurel Supply', w: 13, h: 10, base: '-', indoor: true,
    paint: [['border', '|', 1], ['rect', 3, 2, 7, 2, 'T']],
    warps: [[6, 9, 'aurel', 22, 12, 's'], [7, 9, 'aurel', 22, 12, 's']]
  });

  /* The Conclave: five chambers stacked vertically, one Master each, Champion last. */
  makeMap('conclave', {
    name: 'The Conclave', w: 19, h: 35, base: '-', indoor: true,
    paint: [
      ['border', '|', 1],
      ['hline', 1, 29, 17, '|'], ['put', 9, 29, 'c'],
      ['hline', 1, 23, 17, '|'], ['put', 9, 23, 'c'],
      ['hline', 1, 17, 17, '|'], ['put', 9, 17, 'c'],
      ['hline', 1, 11, 17, '|'], ['put', 9, 11, 'c'],
      ['hline', 1, 5, 17, '|'], ['put', 9, 5, 'c'],
      ['rect', 7, 1, 5, 3, 'c'],
      ['rect', 3, 31, 3, 2, 'T'], ['rect', 13, 31, 3, 2, 'T']
    ],
    warps: [[9, 34, 'aurel', 14, 10, 's'], [8, 34, 'aurel', 14, 10, 's']]
  });

  /* Towns the Recall field skill can return you to, in story order. */
  AE.TOWNS = [
    { map: 'willowmere', name: 'Willowmere', x: 14, y: 12 },
    { map: 'thornhollow', name: 'Thornhollow', x: 14, y: 12 },
    { map: 'cinderfall', name: 'Cinderfall', x: 14, y: 14 },
    { map: 'brackwater', name: 'Brackwater', x: 14, y: 10 },
    { map: 'stormreach', name: 'Stormreach', x: 14, y: 9 },
    { map: 'gravemoor', name: 'Gravemoor', x: 14, y: 12 },
    { map: 'ironhold', name: 'Ironhold', x: 14, y: 14 },
    { map: 'frostvale', name: 'Frostvale', x: 14, y: 12 },
    { map: 'skyhaven', name: 'Skyhaven', x: 14, y: 10 },
    { map: 'aurel', name: 'Aurel Citadel', x: 14, y: 12 }
  ];

  /* Where you reappear after a blackout, per map. */
  AE.RESPAWN = {
    willowmere: 'willowmere', route1: 'willowmere', home_willowmere: 'willowmere',
    thornhollow: 'thornhollow', route2: 'thornhollow', emberdeep: 'thornhollow',
    cinderfall: 'cinderfall', route3: 'cinderfall', mirewood: 'cinderfall',
    brackwater: 'brackwater', route4: 'brackwater',
    stormreach: 'stormreach', route5: 'stormreach',
    gravemoor: 'gravemoor', route6: 'gravemoor', hollowspire: 'gravemoor',
    ironhold: 'ironhold', route7: 'ironhold', 'ashen-hideout': 'ironhold',
    frostvale: 'frostvale', route8: 'frostvale',
    skyhaven: 'skyhaven', route9: 'skyhaven',
    aurel: 'aurel', conclave: 'aurel'
  };

  /* Warps carve their own tile when their map is built, but the tile they land
     on belongs to a map that may not exist yet. Sweep the destinations once
     everything is defined. */
  (function finalizeWarpDestinations() {
    Object.keys(MAPS).forEach(function (id) {
      var m = MAPS[id];
      Object.keys(m.warps).forEach(function (k) {
        var w = m.warps[k];
        var dest = MAPS[w.to];
        if (dest) carveWarp(dest, w.tx, w.ty);
      });
    });
  })();

  AE.townEntry = function (mapId) {
    var t = AE.TOWNS.find(function (x) { return x.map === mapId; });
    return t || AE.TOWNS[0];
  };

})(window.AE = window.AE || {});
