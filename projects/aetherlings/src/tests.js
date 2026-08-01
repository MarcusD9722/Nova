/* Aetherlings — in-page self tests. Load index.html?test=1 to run them.

   These exist because the content is too large to check by playing: 56 species,
   84 moves, 43 maps and a few hundred scripted references. The map reachability
   test in particular walks the entire warp graph, which no amount of manual
   play would cover in reasonable time. */
(function (AE) {
  'use strict';

  var results = [];
  var group = '';

  function describe(name, fn) { group = name; fn(); }

  function ok(name, cond, detail) {
    results.push({ group: group, name: name, pass: !!cond, detail: cond ? '' : (detail || '') });
  }

  function eq(name, actual, expected) {
    ok(name, actual === expected, 'expected ' + expected + ', got ' + actual);
  }

  function near(name, actual, lo, hi) {
    ok(name, actual >= lo && actual <= hi, 'expected ' + lo + '..' + hi + ', got ' + actual);
  }

  /* ================= type chart ================= */
  function testTypes() {
    describe('types', function () {
      ok('12 types defined', AE.TYPES.length === 12, 'got ' + AE.TYPES.length);

      var missing = [];
      AE.TYPES.forEach(function (a) {
        AE.TYPES.forEach(function (d) {
          if (typeof AE.EFF[a][d] !== 'number') missing.push(a + '->' + d);
        });
      });
      ok('effectiveness matrix is complete', missing.length === 0, missing.join(', '));

      var noOffence = AE.TYPES.filter(function (a) {
        return !AE.TYPES.some(function (d) { return AE.EFF[a][d] > 1; });
      });
      ok('every type is super-effective against something', noOffence.length === 0, noOffence.join(', '));

      var noWeakness = AE.TYPES.filter(function (d) {
        return !AE.TYPES.some(function (a) { return AE.EFF[a][d] > 1; });
      });
      ok('every type is weak to something', noWeakness.length === 0, noWeakness.join(', '));

      eq('dual-type stacking multiplies', AE.effectiveness('Ember', ['Verdant', 'Frost']), 4);
      eq('resistance cancels weakness', AE.effectiveness('Ember', ['Verdant', 'Tide']), 1);
      eq('immunity beats everything', AE.effectiveness('Beast', ['Spirit', 'Frost']), 0);
      eq('Storm cannot hit Stone', AE.EFF.Storm.Stone, 0);
    });
  }

  /* ================= data integrity ================= */
  function testData() {
    describe('data', function () {
      var badEvo = [], badMove = [], badStone = [], noMoves = [], badType = [];

      AE.DEX_LIST.forEach(function (sp) {
        if (sp.evo) {
          if (!AE.DEX[sp.evo.to]) badEvo.push(sp.name + '->' + sp.evo.to);
          if (sp.evo.stone && !AE.ITEMS[sp.evo.stone]) badStone.push(sp.name + ':' + sp.evo.stone);
          if (!sp.evo.lvl && !sp.evo.stone) badEvo.push(sp.name + ' has no trigger');
        }
        sp.types.forEach(function (t) { if (AE.TYPES.indexOf(t) < 0) badType.push(sp.name + ':' + t); });
        sp.learn.forEach(function (entry) {
          if (!AE.MOVES[entry[1]]) badMove.push(sp.name + ':' + entry[1]);
        });
        if (!sp.learn.some(function (e) { return e[0] <= 1; })) noMoves.push(sp.name);
      });

      ok('every evolution target exists', badEvo.length === 0, badEvo.join(', '));
      ok('every evolution stone is a real item', badStone.length === 0, badStone.join(', '));
      ok('every learnset move exists', badMove.length === 0, badMove.join(', '));
      ok('every species knows a move at level 1', noMoves.length === 0, noMoves.join(', '));
      ok('every species type is valid', badType.length === 0, badType.join(', '));

      var badMoveType = AE.MOVE_LIST.filter(function (m) { return AE.TYPES.indexOf(m.type) < 0; });
      ok('every move has a valid type', badMoveType.length === 0,
        badMoveType.map(function (m) { return m.id; }).join(', '));

      var badPower = AE.MOVE_LIST.filter(function (m) {
        return (m.cat === 'status') !== (m.pow === 0);
      });
      ok('status moves have no power, damaging moves do', badPower.length === 0,
        badPower.map(function (m) { return m.id; }).join(', '));

      /* Struggle is the out-of-PP fallback. It must exist, must be usable, and
         must never appear in a learnset (it is substituted, not taught). */
      ok('Struggle exists as a fallback move', !!AE.MOVES.struggle);
      ok('Struggle never misses', AE.MOVES.struggle && AE.MOVES.struggle.acc === 0);
      ok('Struggle costs the user HP', !!(AE.MOVES.struggle && AE.MOVES.struggle.eff.recoil));
      var taughtStruggle = AE.DEX_LIST.filter(function (sp) {
        return sp.learn.some(function (e) { return e[1] === 'struggle'; });
      });
      ok('Struggle is in no learnset', taughtStruggle.length === 0,
        taughtStruggle.map(function (s) { return s.name; }).join(', '));

      var badShop = [];
      Object.keys(AE.SHOPS).forEach(function (id) {
        AE.SHOPS[id].forEach(function (item) { if (!AE.ITEMS[item]) badShop.push(id + ':' + item); });
      });
      ok('every shop stocks real items', badShop.length === 0, badShop.join(', '));

      /* Both evolution stones sold somewhere must actually evolve something. */
      var unusedStone = AE.ITEM_LIST.filter(function (it) {
        return it.use.evoStone && !AE.DEX_LIST.some(function (sp) {
          return sp.evo && sp.evo.stone === it.id;
        });
      });
      ok('every evolution stone evolves something', unusedStone.length === 0,
        unusedStone.map(function (i) { return i.id; }).join(', '));

      ok('starters all exist', AE.STARTERS.every(function (id) { return !!AE.DEX[id]; }));
    });
  }

  /* ================= stats and damage ================= */
  function testCombat() {
    describe('combat', function () {
      var mon = AE.makeMon(1, 50, { perfect: true, nature: 'Hardy' });
      /* Gen-3 HP: floor((2*45 + 31 + 0) * 50/100) + 50 + 10 = 60 + 60 = 120 */
      eq('HP formula matches Gen 3', AE.calcStat(mon, 'hp'), 120);
      /* Attack: floor((2*49 + 31)*50/100) + 5 = 64 + 5 = 69 */
      eq('Attack formula matches Gen 3', AE.calcStat(mon, 'atk'), 69);

      var up = AE.makeMon(1, 50, { perfect: true, nature: 'Adamant' });
      eq('nature raises the right stat', AE.calcStat(up, 'atk'), Math.floor(69 * 1.1));
      eq('nature lowers the right stat', AE.calcStat(up, 'spa'),
        Math.floor((Math.floor((2 * 60 + 31) * 50 / 100) + 5) * 0.9));
      eq('nature never touches HP', AE.calcStat(up, 'hp'), 120);

      function side(m) {
        return { mon: m, stages: { atk: 0, def: 0, spa: 0, spd: 0, spe: 0, acc: 0, eva: 0 } };
      }

      var atk = side(AE.makeMon(4, 50, { perfect: true, nature: 'Hardy' }));
      var defVerdant = side(AE.makeMon(1, 50, { perfect: true, nature: 'Hardy' }));
      var defTide = side(AE.makeMon(7, 50, { perfect: true, nature: 'Hardy' }));

      var superSum = 0, resistSum = 0;
      for (var i = 0; i < 200; i++) {
        superSum += AE.calcDamage(atk, defVerdant, AE.move('flamewave'), false).dmg;
        resistSum += AE.calcDamage(atk, defTide, AE.move('flamewave'), false).dmg;
      }
      ok('super effective beats resisted', superSum > resistSum * 3,
        'super=' + superSum + ' resisted=' + resistSum);

      var critSum = 0, normalSum = 0;
      for (var j = 0; j < 200; j++) {
        critSum += AE.calcDamage(atk, defVerdant, AE.move('flamewave'), true).dmg;
        normalSum += AE.calcDamage(atk, defVerdant, AE.move('flamewave'), false).dmg;
      }
      near('critical hits roughly double damage', critSum / normalSum, 1.85, 2.15);

      eq('immune matchup deals zero', AE.calcDamage(
        side(AE.makeMon(10, 50, { perfect: true })),
        side(AE.makeMon(26, 50, { perfect: true })),
        AE.move('ram'), false).dmg, 0);

      var boosted = side(AE.makeMon(4, 50, { perfect: true, nature: 'Hardy' }));
      boosted.stages.spa = 2;
      var plainSum = 0, boostSum = 0;
      for (var k = 0; k < 200; k++) {
        plainSum += AE.calcDamage(atk, defVerdant, AE.move('flamewave'), false).dmg;
        boostSum += AE.calcDamage(boosted, defVerdant, AE.move('flamewave'), false).dmg;
      }
      near('+2 Sp. Atk is about double', boostSum / plainSum, 1.75, 2.25);

      ok('a move that cannot miss never misses',
        AE.accuracyCheck(atk, defVerdant, AE.move('howl')) === true);
    });
  }

  /* ================= catching and escaping ================= */
  function testCatching() {
    describe('catching', function () {
      var full = AE.makeMon(10, 10);
      var hurt = AE.makeMon(10, 10);
      hurt.hp = 1;

      var fullCaught = 0, hurtCaught = 0;
      for (var i = 0; i < 400; i++) {
        if (AE.catchAttempt(full, 1).caught) fullCaught++;
        if (AE.catchAttempt(hurt, 1).caught) hurtCaught++;
      }
      ok('a weakened target is easier to catch', hurtCaught > fullCaught,
        'full=' + fullCaught + ' weakened=' + hurtCaught);

      var asleep = AE.makeMon(10, 10);
      asleep.hp = 1; asleep.status = 'sleep';
      var sleepCaught = 0;
      for (var j = 0; j < 400; j++) if (AE.catchAttempt(asleep, 1).caught) sleepCaught++;
      ok('sleep helps the catch rate', sleepCaught >= hurtCaught,
        'awake=' + hurtCaught + ' asleep=' + sleepCaught);

      var legend = AE.makeMon(56, 50);
      ok('Truestone always catches', AE.catchAttempt(legend, 255).caught === true);

      var legendCaught = 0;
      for (var k = 0; k < 200; k++) if (AE.catchAttempt(legend, 1).caught) legendCaught++;
      ok('the titan resists an ordinary Bindstone', legendCaught < 20, 'caught ' + legendCaught + '/200');

      function side(m) { return { mon: m, stages: { spe: 0 } }; }
      var fast = side(AE.makeMon(13, 50, { perfect: true }));
      var slow = side(AE.makeMon(20, 5));
      ok('a much faster team escapes', AE.escapeChance(fast, slow, 1) === true);
    });
  }

  /* ================= levelling and evolution ================= */
  function testProgression() {
    describe('progression', function () {
      var mon = AE.makeMon(1, 5);
      var needed = AE.expForLevel('medium', 6) - mon.exp;
      var events = AE.giveExp(mon, needed);
      ok('crossing the threshold levels up', mon.lvl === 6, 'level ' + mon.lvl);
      ok('a level event is reported', events.some(function (e) { return e.t === 'level'; }));

      var hpBefore = mon.hp;
      AE.giveExp(mon, AE.expForLevel('medium', 12) - mon.exp);
      ok('level-ups raise current HP too', mon.hp > hpBefore, hpBefore + ' -> ' + mon.hp);

      var pre = AE.makeMon(1, 15);
      var evoEvents = AE.giveExp(pre, AE.expForLevel('medium', 16) - pre.exp);
      ok('evolution triggers at the right level',
        evoEvents.some(function (e) { return e.t === 'evolve' && e.to === 2; }));

      var evolving = AE.makeMon(1, 16);
      var maxBefore = AE.maxHP(evolving);
      var curBefore = evolving.hp;
      AE.evolveMon(evolving, 2);
      eq('evolution changes the species', evolving.sp, 2);
      ok('evolution keeps the HP difference', evolving.hp === curBefore + (AE.maxHP(evolving) - maxBefore));

      var high = AE.makeMon(1, 100);
      var noEvents = AE.giveExp(high, 100000);
      ok('level 100 stops gaining', high.lvl === 100 && noEvents.length === 0);

      var m4 = AE.makeMon(3, 60);
      ok('a party member never exceeds four moves', m4.moves.length <= 4, 'got ' + m4.moves.length);
      ok('makeMon always gives at least one move', m4.moves.length >= 1);

      var healed = AE.makeMon(1, 20);
      healed.hp = 1; healed.status = 'burn'; healed.moves[0].pp = 0;
      AE.healMon(healed);
      ok('healing restores HP, status and PP',
        healed.hp === AE.maxHP(healed) && healed.status === 'none' && healed.moves[0].pp === healed.moves[0].maxpp);
    });
  }

  /* ================= save / load ================= */
  function testSave() {
    describe('save', function () {
      var g = AE.newGame('Testy');
      g.party.push(AE.makeMon(1, 12));
      g.party.push(AE.makeMon(23, 15));
      AE.addItem(g, 'bindstone', 7);
      g.badges.push('sigil1');
      g.flags.gotStarter = true;
      g.skills.push('cleave');
      g.money = 4242;
      g.map = 'thornhollow'; g.x = 9; g.y = 4;

      var wrote = AE.save(g, 2);
      ok('save reports success', wrote === true);

      var back = AE.load(2);
      ok('a save can be loaded back', !!back);
      if (back) {
        eq('name survives', back.name, 'Testy');
        eq('money survives', back.money, 4242);
        eq('position survives', back.map + ':' + back.x + ',' + back.y, 'thornhollow:9,4');
        eq('party size survives', back.party.length, 2);
        eq('bag survives', back.bag.bindstone, 7);
        eq('badges survive', back.badges.length, 1);
        eq('skills survive', back.skills[0], 'cleave');
        ok('round trip is byte-identical', JSON.stringify(back) === JSON.stringify(g));
        ok('loaded party is still usable', AE.maxHP(back.party[0]) === AE.maxHP(g.party[0]));
      }

      var summary = AE.saveSummary(2);
      ok('save summary reads the slot', summary && summary.name === 'Testy');

      AE.deleteSave(2);
      ok('deleting a slot empties it', AE.load(2) === null);
    });
  }

  /* ================= maps ================= */

  /* Walkability for the reachability crawl: assumes every field skill and every
     Sigil, because those gate progress in time rather than in space. */
  function crawlWalkable(m, x, y, fromDir) {
    if (x < 0 || y < 0 || x >= m.w || y >= m.h) return false;
    var ch = AE.tileAt(m, x, y);
    if (ch === '^') return fromDir === 's';     /* ledges are one-way */
    if (ch === '~' || ch === 'C') return true;  /* Surge / Ascend */
    return !AE.isSolid(ch);
  }

  function crawl(npcsSolid) {
    var start = { map: 'willowmere', x: 14, y: 12 };
    var seen = {}, reachedMaps = {}, queue = [start];
    var key = function (s) { return s.map + '|' + s.x + ',' + s.y; };

    /* NPCs block movement. Ones that exist to bar the road until a story flag
       flips are excluded, since they stand aside once you deal with them. */
    var blocked = {};
    if (npcsSolid) {
      Object.keys(AE.NPCS).forEach(function (mapId) {
        AE.NPCS[mapId].forEach(function (n) {
          if (n.sign || n.blocksUntil) return;
          blocked[mapId + '|' + n.x + ',' + n.y] = true;
        });
      });
    }
    seen[key(start)] = true;
    reachedMaps[start.map] = true;
    var badWarps = [];

    var DIRS = [['n', 0, -1], ['s', 0, 1], ['e', 1, 0], ['w', -1, 0]];

    while (queue.length) {
      var cur = queue.shift();
      var m = AE.MAPS[cur.map];
      if (!m) continue;

      var warp = AE.warpAt(m, cur.x, cur.y);
      if (warp) {
        var dest = AE.MAPS[warp.to];
        if (!dest) {
          badWarps.push(cur.map + '(' + cur.x + ',' + cur.y + ') -> missing map ' + warp.to);
        } else if (warp.tx < 0 || warp.ty < 0 || warp.tx >= dest.w || warp.ty >= dest.h) {
          badWarps.push(cur.map + ' -> ' + warp.to + ' lands out of bounds');
        } else if (AE.isSolid(AE.tileAt(dest, warp.tx, warp.ty)) &&
                   AE.tileAt(dest, warp.tx, warp.ty) !== 'D') {
          badWarps.push(cur.map + ' -> ' + warp.to + ' lands inside "' +
            AE.tileAt(dest, warp.tx, warp.ty) + '"');
        } else {
          var landed = { map: warp.to, x: warp.tx, y: warp.ty };
          if (!seen[key(landed)]) {
            seen[key(landed)] = true;
            reachedMaps[warp.to] = true;
            queue.push(landed);
          }
        }
      }

      for (var i = 0; i < DIRS.length; i++) {
        var d = DIRS[i];
        var nx = cur.x + d[1], ny = cur.y + d[2];
        if (!crawlWalkable(m, nx, ny, d[0])) continue;
        if (AE.tileAt(m, nx, ny) === '^') ny += 1;   /* the hop lands one further */
        if (!crawlWalkable(m, nx, ny, 's') && AE.tileAt(m, nx, ny) !== '^') continue;
        var next = { map: cur.map, x: nx, y: ny };
        if (blocked[key(next)]) continue;
        if (seen[key(next)]) continue;
        seen[key(next)] = true;
        reachedMaps[cur.map] = true;
        queue.push(next);
      }
    }
    return { reached: reachedMaps, badWarps: badWarps, tiles: seen };
  }

  var crawlResult = null;

  function testMaps() {
    describe('maps', function () {
      var ids = Object.keys(AE.MAPS);
      ok('maps are defined', ids.length > 30, 'got ' + ids.length);

      var ragged = ids.filter(function (id) {
        var m = AE.MAPS[id];
        return m.grid.length !== m.h || m.grid.some(function (r) { return r.length !== m.w; });
      });
      ok('every grid matches its declared size', ragged.length === 0, ragged.join(', '));

      var unknownTiles = [];
      ids.forEach(function (id) {
        var m = AE.MAPS[id];
        m.grid.forEach(function (row, y) {
          row.forEach(function (ch, x) {
            if (!AE.TILE[ch] && unknownTiles.length < 10) unknownTiles.push(id + ' "' + ch + '" at ' + x + ',' + y);
          });
        });
      });
      ok('every tile character is known', unknownTiles.length === 0, unknownTiles.join(', '));

      crawlResult = crawl();
      ok('every warp lands somewhere real', crawlResult.badWarps.length === 0,
        crawlResult.badWarps.slice(0, 6).join(' | '));

      var unreachable = ids.filter(function (id) { return !crawlResult.reached[id]; });
      ok('every map is reachable from the start', unreachable.length === 0,
        unreachable.join(', '));

      var towns = AE.TOWNS.map(function (t) { return t.map; });
      var missedTowns = towns.filter(function (id) { return !crawlResult.reached[id]; });
      ok('all ten towns are reachable', missedTowns.length === 0, missedTowns.join(', '));

      var badTownEntry = AE.TOWNS.filter(function (t) {
        var m = AE.MAPS[t.map];
        return !m || AE.isSolid(AE.tileAt(m, t.x, t.y));
      });
      ok('every town entry point is standable', badTownEntry.length === 0,
        badTownEntry.map(function (t) { return t.map; }).join(', '));

      var badRespawn = Object.keys(AE.RESPAWN).filter(function (k) {
        return !AE.MAPS[AE.RESPAWN[k]];
      });
      ok('every blackout destination exists', badRespawn.length === 0, badRespawn.join(', '));

      /* Standing NPCs are solid. One parked on a chokepoint — a doorway
         approach, a bridge — would wall off part of the world. */
      var solidCrawl = crawl(true);
      var walledOff = ids.filter(function (id) { return !solidCrawl.reached[id]; });
      ok('no NPC walls off part of the world', walledOff.length === 0, walledOff.join(', '));

      var encBad = [];
      ids.forEach(function (id) {
        var m = AE.MAPS[id];
        [m.enc, m.water].forEach(function (t) {
          if (!t) return;
          t.table.forEach(function (e) {
            if (!AE.DEX[e.sp]) encBad.push(id + ':' + e.sp);
            if (e.min > e.max) encBad.push(id + ' level range reversed');
          });
        });
      });
      ok('encounter tables reference real species', encBad.length === 0, encBad.join(', '));

      /* A map with an encounter table needs tiles that can actually trigger it. */
      var encNoTiles = ids.filter(function (id) {
        var m = AE.MAPS[id];
        if (!m.enc) return false;
        return !m.grid.some(function (row) {
          return row.some(function (ch) { return AE.isEncounterTile(ch); });
        });
      });
      ok('encounter maps have grass to trigger in', encNoTiles.length === 0, encNoTiles.join(', '));
    });
  }

  /* ================= story ================= */
  function testStory() {
    describe('story', function () {
      var badMaps = Object.keys(AE.NPCS).filter(function (id) { return !AE.MAPS[id]; });
      ok('every NPC list belongs to a real map', badMaps.length === 0, badMaps.join(', '));

      var oob = [], onSolid = [], unreachableNPC = [];
      Object.keys(AE.NPCS).forEach(function (id) {
        var m = AE.MAPS[id];
        if (!m) return;
        AE.NPCS[id].forEach(function (n) {
          if (n.x < 0 || n.y < 0 || n.x >= m.w || n.y >= m.h) { oob.push(id + ':' + n.name); return; }
          var ch = AE.tileAt(m, n.x, n.y);
          if (n.sign) {
            if (ch !== 'S') onSolid.push(id + ' sign at ' + n.x + ',' + n.y + ' is on "' + ch + '"');
          } else if (AE.isSolid(ch)) {
            onSolid.push(id + ':' + n.name + ' stands on "' + ch + '"');
          }
          /* You must be able to stand next to them to talk. */
          var neighbours = [[0, -1], [0, 1], [1, 0], [-1, 0]].filter(function (d) {
            var nx = n.x + d[0], ny = n.y + d[1];
            if (nx < 0 || ny < 0 || nx >= m.w || ny >= m.h) return false;
            var t = AE.tileAt(m, nx, ny);
            return !AE.isSolid(t) || t === '~' || t === 'C';
          });
          if (!neighbours.length) unreachableNPC.push(id + ':' + n.name);
        });
      });
      ok('no NPC is placed out of bounds', oob.length === 0, oob.join(', '));
      ok('NPCs stand on walkable ground, signs on sign tiles', onSolid.length === 0,
        onSolid.slice(0, 6).join(' | '));
      ok('every NPC can be stood next to', unreachableNPC.length === 0, unreachableNPC.join(', '));

      if (crawlResult) {
        var stranded = [];
        Object.keys(AE.NPCS).forEach(function (id) {
          var m = AE.MAPS[id];
          if (!m) return;
          AE.NPCS[id].forEach(function (n) {
            var adjacentReached = [[0, -1], [0, 1], [1, 0], [-1, 0]].some(function (d) {
              return crawlResult.tiles[id + '|' + (n.x + d[0]) + ',' + (n.y + d[1])];
            });
            if (!adjacentReached) stranded.push(id + ':' + n.name);
          });
        });
        ok('every NPC is actually walkable-to from the start', stranded.length === 0,
          stranded.slice(0, 8).join(' | '));
      }

      /* Walk every script and validate its references. */
      var badRefs = [], badParty = [], flagsSet = {}, flagsRequired = {};

      function checkTrainer(t, where) {
        var list = t.dynamic ? t.dynamic(AE.newGame('Test')) : t.party;
        if (!list || !list.length) { badParty.push(where + ': empty party'); return; }
        list.forEach(function (e) {
          if (!AE.DEX[e.sp]) badParty.push(where + ': unknown species ' + e.sp);
          if (!(e.lvl > 0 && e.lvl <= 100)) badParty.push(where + ': bad level ' + e.lvl);
        });
      }

      function walk(lines, where) {
        (lines || []).forEach(function (step) {
          if (typeof step === 'string') return;
          if (step.give && !AE.ITEMS[step.give]) badRefs.push(where + ': item ' + step.give);
          if (step.skill && !AE.FIELD_SKILLS.some(function (s) { return s.id === step.skill; })) {
            badRefs.push(where + ': skill ' + step.skill);
          }
          if (step.wild && !AE.DEX[step.wild.sp]) badRefs.push(where + ': wild species ' + step.wild.sp);
          if (step.shop && !AE.SHOPS[step.shop]) badRefs.push(where + ': shop ' + step.shop);
          if (step.warp && !AE.MAPS[step.warp.map]) badRefs.push(where + ': warp ' + step.warp.map);
          if (step.flag) flagsSet[step.flag] = true;
          if (step.badge) flagsSet[step.badge] = true;   /* Sigils set a flag too */
          if (step.require) flagsRequired[step.require] = where;
          if (step.battle) checkTrainer(step.battle, where);
        });
      }

      Object.keys(AE.NPCS).forEach(function (id) {
        AE.NPCS[id].forEach(function (n) {
          var where = id + ':' + (n.name || 'sign');
          walk(n.lines, where);
          if (n.trainer) checkTrainer(n.trainer, where);
          if (n.once) flagsRequired[n.once] = where;
          if (n.requireFlag) flagsRequired[n.requireFlag] = where;
          if (n.shop && !AE.SHOPS[n.shop]) badRefs.push(where + ': shop ' + n.shop);
        });
      });

      ok('every script reference resolves', badRefs.length === 0, badRefs.slice(0, 8).join(' | '));
      ok('every trainer party is valid', badParty.length === 0, badParty.slice(0, 8).join(' | '));

      /* Flags that gate content but are never set would soft-lock the story. */
      var neverSet = Object.keys(flagsRequired).filter(function (f) {
        if (flagsSet[f]) return false;
        if (f === 'starter' || f === 'gameComplete') return false;
        return true;
      });
      ok('every gating flag is set somewhere', neverSet.length === 0,
        neverSet.map(function (f) { return f + ' (' + flagsRequired[f] + ')'; }).join(', '));

      /* Eight Wardens, each awarding a distinct Sigil. */
      var wardenIds = Object.keys(AE.WARDENS);
      eq('there are eight Wardens', wardenIds.length, 8);
      var sigils = wardenIds.map(function (k) { return AE.WARDENS[k].sigil; });
      eq('each Warden awards a distinct Sigil', new Set(sigils).size, 8);

      var badWardenMap = wardenIds.filter(function (k) { return !AE.MAPS['sanctum-' + k]; });
      ok('every Warden has a Sanctum map', badWardenMap.length === 0, badWardenMap.join(', '));

      /* Field skills must be obtainable. */
      var grantedSkills = {};
      Object.keys(AE.NPCS).forEach(function (id) {
        AE.NPCS[id].forEach(function (n) {
          (n.lines || []).forEach(function (s) { if (s && s.skill) grantedSkills[s.skill] = true; });
        });
      });
      var ungrantable = AE.FIELD_SKILLS.filter(function (s) { return !grantedSkills[s.id]; });
      ok('every field skill is granted somewhere', ungrantable.length === 0,
        ungrantable.map(function (s) { return s.id; }).join(', '));

      /* Every obstacle type must have a matching skill. */
      var badObstacle = Object.keys(AE.OBSTACLES).filter(function (ch) {
        return !AE.FIELD_SKILLS.some(function (s) { return s.id === AE.OBSTACLES[ch].skill; });
      });
      ok('every obstacle maps to a real skill', badObstacle.length === 0, badObstacle.join(', '));
    });
  }

  /* ================= runner ================= */
  AE.runTests = function () {
    results = [];
    testTypes();
    testData();
    testCombat();
    testCatching();
    testProgression();
    testSave();
    testMaps();
    testStory();

    var failed = results.filter(function (r) { return !r.pass; });
    var byGroup = {};
    results.forEach(function (r) {
      byGroup[r.group] = byGroup[r.group] || { pass: 0, fail: 0 };
      byGroup[r.group][r.pass ? 'pass' : 'fail']++;
    });

    console.log('%cAetherlings self-tests', 'font-weight:bold;font-size:14px');
    Object.keys(byGroup).forEach(function (gname) {
      var b = byGroup[gname];
      console.log((b.fail ? 'FAIL' : ' ok ') + '  ' + gname + '  ' + b.pass + ' passed' +
        (b.fail ? ', ' + b.fail + ' FAILED' : ''));
    });
    failed.forEach(function (r) {
      console.error('FAILED [' + r.group + '] ' + r.name + (r.detail ? ' — ' + r.detail : ''));
    });
    console.log(failed.length
      ? 'RESULT: ' + failed.length + ' of ' + results.length + ' assertions FAILED'
      : 'RESULT: all ' + results.length + ' assertions passed');

    return { total: results.length, failed: failed.length, results: results };
  };

})(window.AE = window.AE || {});
